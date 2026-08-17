# Implementation Plan: Multi-Asset Trading with Per-Asset Capital and Independent CEX/DEX Activation

**Branch**: `002-multi-asset-capital` | **Date**: 2026-07-27 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-multi-asset-capital/spec.md`

## Summary

Replace the global capital model (`dex_slot_pct` / `cex_slot_pct` percentages of total equity) with per-asset absolute-USD capital allocations. Replace global venue activation booleans (`auto_trade_binance` / `auto_trade_orderly`) with per-asset independent flags (`active_dex` / `active_cex`). The bot loop iterates over all active (asset, venue) pairs each cycle, using each pair's own capital for position sizing. Both Telegram and Mini App UIs gain full asset CRUD parity. Legacy settings migrate automatically on first startup.

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**: FastAPI (dashboard), Telebot/pyTelegramBotAPI (telegram), requests (exchange APIs), SQLite (database)

**Storage**: SQLite via `data/trading.db` — key-value `settings` table + relational `open_positions`, `closed_trades`, `signals` tables. New `asset_configs` table for per-asset structured data.

**Testing**: pytest (existing) — unit tests for `settings_rules.py` validation, integration tests for DB operations

**Target Platform**: Linux server, Docker containers (bot + dashboard + dashboard-ui + nginx)

**Project Type**: Trading bot with web dashboard and Telegram bot interface

**Performance Goals**: Complete one full cycle (all active pairs) within ~20 seconds to leave margin before 30-second sleep. Each pair: 2 API calls (OBI + price) + 1 regime check (cached) + entry evaluation. Target: ~10 assets × 2 venues = 20 pairs, ~1s per pair.

**Constraints**:
- Target ≤1,500 lines across hot-path modules (Constitution VII) — current ~830 lines
- SQLite reads fresh each cycle; no in-memory state that survives restart
- Exchange credentials in `.env` only
- dry_run defaults to true; no code path bypasses it
- Structured single-line logging: `[LEVEL] key=value`

**Scale/Scope**: ~10 assets maximum, 6 active (asset, venue) pairs default (configurable), 9 concurrent open positions default (configurable)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| **I. One Strategy** | ✅ PASS | Mean reversion only. Per-asset capital is a data change, not a strategy change. No new entry/exit logic. |
| **II. Reward Exceeds Risk** | ✅ PASS | `tp_min_pct > sl_min_pct` still enforced globally. Per-asset override of TP/SL is NOT in scope — same thresholds for all assets. Net edge validation unchanged. |
| **III. No Leverage Without Stop** | ✅ PASS | Unchanged. Every DEX entry remains a bracket order. Stop verification unchanged. |
| **IV. Unknown State = No Trading** | ⚠️ EXTEND | Must fail closed independently per (asset, venue) pair. A balance query failure for one pair must not block others. Retry escalation per pair, not global. FR-020 addresses this. |
| **V. Real Fills Only** | ✅ PASS | Unchanged. `record_closed_trade` already accepts `asset` + `venue` params. |
| **VI. Restart Safety** | ⚠️ EXTEND | Must reconcile exchange state for ALL active pairs independently. Adopt orphaned positions per pair. FR-021 addresses this. |
| **VII. Simplicity Is a Constraint** | ⚠️ CHECK | Adding `asset_configs` table adds ~50 lines to `db_ops.py`. Removing legacy global settings removes ~20 lines from `settings_schema.py`. Net impact: ~+30 lines in hot path. Budget remains within 1,500-line target. |
| **VIII. The Bot Trades** | ✅ PASS | Multi-asset iteration increases trade frequency. Each pair evaluated independently. |

**Gate Result: PASS with EXTEND notes on IV, VI, VII** — all extensions are addressed by FR-020, FR-021, and line budget tracking in this plan.

## Post-Design Constitution Re-Check

*Re-evaluated after Phase 1 design (data-model.md, contracts/, quickstart.md).*

| Principle | Status | Post-Design Notes |
|-----------|--------|-------------------|
| **I. One Strategy** | ✅ PASS | `spot_scalper.py` and `futures_scalper.py` unchanged. Per-asset capital is a sizing parameter, not a strategy change. |
| **II. Reward Exceeds Risk** | ✅ PASS | `tp_min_pct > sl_min_pct` and net-edge validation remain startup gates. Per-asset TP/SL overrides NOT in scope. |
| **III. No Leverage Without Stop** | ✅ PASS | `futures_scalper.py` bracket-order logic unchanged. Each DEX entry still has mandatory SL. |
| **IV. Unknown State = No Trading** | ✅ PASS | Research §3: each pair's cycle wrapped in independent try/except. A balance query failure for ETH/CEX does not block BTC/DEX. Per-pair fail-closed scoping confirmed. |
| **V. Real Fills Only** | ✅ PASS | `record_closed_trade` already accepts `asset`+`venue`. No change to fill-price recording. |
| **VI. Restart Safety** | ✅ PASS | Reconciliation loop iterates `get_active_pairs()`. Each pair independently reconciles exchange positions with local DB. Same pattern as single-asset, applied across pairs. |
| **VII. Simplicity Is a Constraint** | ✅ PASS | Net line estimate: ~+100 lines across hot-path modules (~930 total, well within 1,500 budget). New `asset_configs` table is the only schema addition. No new modules. |
| **VIII. The Bot Trades** | ✅ PASS | Multi-asset iteration increases opportunity frequency. No new filters — all existing filters (regime, cooldown, spacing) already per-asset scoped. |

**Post-Design Gate: ALL PASS** — no unresolved violations. Ready for `/speckit.tasks`.

## Project Structure

### Documentation (this feature)

```text
specs/002-multi-asset-capital/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (API contracts)
│   └── api.md
└── tasks.md             # Phase 2 output (NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
bot.py                          # Main loop — iterate per-pair, remove global venue booleans
db/
├── db_ops.py                   # Asset CRUD (add/edit/list/remove), migration
├── schema_v2.sql               # Add asset_configs table
└── migrations/
    └── 004_multi_asset.sql     # Migration 004: create asset_configs, drop legacy keys

trade/
├── pnl.py                      # Per-pair kill switches, per-asset slot sizing
├── regime.py                   # Already per-asset — no change
├── settings_schema.py          # Remove dex_slot_pct, cex_slot_pct, auto_trade_*; add new keys
├── settings_rules.py           # Add per-asset capital validation rules
└── toxicity.py                 # No change (already observes per call)

trading_bot/
├── executor.py                 # No change (already per-call stateless)
├── spot_scalper.py             # Per-asset state dicts already keyed by asset — venue-scoped
└── futures_scalper.py          # Same — per-asset state already isolated

telegram.py                     # Asset management handlers: add/edit/list/remove with new fields
dashboard/
└── main.py                     # Asset API endpoints: CRUD with new fields, allocation summary
dashboard-ui/
└── src/
    └── AssetManager.tsx        # Full rewrite: per-asset capital + flags form, allocation summary
```
