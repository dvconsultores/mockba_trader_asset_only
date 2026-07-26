# GATE 1 Report — MockbaV4 Rebuild

**Date**: 2026-07-26 | **Status**: ✅ APPROVED — proceed to Phase 2

---

## Phase 1 Deliverables Produced

| Deliverable | Location | Status |
|---|---|---|
| Current state analysis | `docs/CURRENT_STATE.md` | ✅ Complete |
| Calibration study | `docs/CALIBRATION.md` | ✅ Complete (partial — see below) |
| Constitution | `.specify/memory/constitution.md` | ✅ Complete |
| Feature specification | `.specify/specs/mean-reversion-bot.md` | ✅ Complete |
| Implementation plan | `.specify/plan.md` | ✅ Complete |
| Architecture reference | `ARCHITECTURE.md` | Exists (pre-Phase-1) |

## Spec Kit Installation

**CLI version**: `specify` CLI is installed. Available commands: `init`, `check`, `version`, `self`, `extension`, `integration`, `preset`, `bundle`, `workflow`.

**Slash commands available** (via `/speckit.*` namespace):
- `/speckit.constitution` — Establish project principles
- `/speckit.specify` — Create baseline specification
- `/speckit.plan` — Create implementation plan
- `/speckit.tasks` — Generate actionable tasks
- `/speckit.implement` — Execute implementation
- `/speckit.converge` — Assess codebase and append remaining work
- `/speckit.clarify` — Structured questions (optional)
- `/speckit.analyze` — Cross-artifact consistency (optional)
- `/speckit.checklist` — Quality checklists (optional)

The prompt expected `/constitution`, `/specify`, etc. The actual names are `/speckit.constitution`, `/speckit.specify`, etc. — namespaced under `speckit.`. All expected commands exist under this namespace.

---

## ARCHITECTURE.md — Errors Found

| # | Claim | Reality | Severity |
|---|---|---|---|
| 1 | "15+ tables" in DB to drop | Only 4 tables exist. The 11 "legacy" tables were already dropped by `initialize_database_tables()`. | **Low** — overstated cleanup scope. Migration is simpler than documented. |
| 2 | `spot_grid_scalper.py` uses `save_grid_position`/`load_grid_positions` from `db_ops.py` | These functions **do not exist**. Positions are tracked in a module-level list `_open_positions: list[dict]`. | **High** — DB position persistence must be built from scratch. |
| 3 | `pnl.py` exists with `close_position()` — "nothing calls it" | `pnl.py` **does not exist yet**. It's a proposed new module. The document described it as if it exists but is unused. | **Medium** — framing error. The exit management gap is real; the fix is new code. |
| 4 | Omitted `trading_executor.py` (1,012 lines) | Standalone chain/wallet management. Not imported by any trading module. Dead code for bot purposes. | **Low** — not part of trading path. |
| 5 | Omitted 5+ other Python files | `trade/test_data.py` (179), `trade/get_binance_trades.py` (237), `trade/get_trades.py` (93), `trade/add_wallet_chain_des.py` (38), `trade/seed_chains.py` (87). All standalone utilities, not in trading path. | **Low**. |
| 6 | Omitted `trade/performance-llm.py` (983 lines) | LLM trade analysis tool with known bugs (average-win miscalculation, hardcoded timezone). | **Medium** — Phase 2.8 addresses this explicitly. |

### Things ARCHITECTURE.md got right
- `trade/main.py` is 2,171 lines and over-engineered ✅
- The grid scalper concept is sound ✅
- `futures_grid_scalper.py` has import-time bug (`float("long")`) ✅
- Positions are not persisted (restart = lost) ✅
- No real exit management in the hot path ✅
- `get_user_statistics()` return type mismatch exists ✅
- The bot needs PnL tracking ✅

---

## Calibration Summary

### What we measured

| Metric | Value | Method |
|---|---|---|
| DEX per-trade fee rate | **0.0300%** | Measured from 25 real trades in `all_trades.json` |
| DEX round-trip fee | **0.0600%** | 2 × per-trade |
| CEX round-trip fee | **0.20%** (assumed) | Binance standard taker × 2. No bot trade data available. |
| Win rate (labeled signals) | **82.6%** | 1,132/1,370 labeled signals. ⚠️ Likely inflated — only signals that became trades were labeled. |
| Implied breakeven win rate | **43.1%** | `(0.5 + 0.06) / (0.8 + 0.5)` at proposed defaults |

### What we could NOT measure

| Metric | Why | Mitigation |
|---|---|---|
| **Slippage** | No signal-price-to-fill-price pairing exists. `all_trades.json` has fills; `signal_history` has signal prices. No order-ID link between them. | Dry-run harness (Phase 2.7) will capture `fill_price - signal_price` per entry. Use `assumed_slippage_pct = 0.03` (DEX) / `0.05` (CEX) until measured. |
| **Regime distribution** | No historical OHLCV data stored. `signal_history` records regime only when a pattern fired, not regime-over-time. | Dry-run will log regime on every cycle. `SLOPE_THRESHOLD` defaults to 0.0012 until validated. |

### Threshold implications

- At `tp_pct = 0.8`, `sl_pct = 0.5`, DEX fees = 0.06%: **net edge = 0.71%**, well above `min_net_edge_pct = 0.30%` ✅
- At CEX fees = 0.20%: **net edge = 0.55%**, also clears ✅
- Breakeven win rate of 43.1% is well below the measured 82.6% — **but** that 82.6% is almost certainly inflated. The real test is the dry-run.
- If measured slippage exceeds 0.20% (DEX) or 0.35% (CEX), the net edge validation will fail at startup and trading will be refused — which is the correct behavior.

---

## Open Questions Requiring Your Input

### Q1: Cross-margin vs. isolated margin

`all_trades.json` shows `"margin_mode": "CROSS"` on Orderly. Under cross margin, the liquidation price depends on total account equity and all open positions. The constitution requires a liquidation-distance guard (principle III), but computing it accurately under cross margin requires full account state. **Should the bot:**
- A) Use simplified liquidation distance (isolated-margin formula, conservative),
- B) Fetch full account state and compute cross-margin liquidation distance, or
- C) Switch the account to isolated margin mode?

### Q2: CEX spot during TREND_DOWN

The regime matrix blocks ALL CEX entries during TREND_DOWN (no short selling on spot). This means the bot idles on CEX during bear trends. **Is this acceptable**, or would you prefer:
- A) Accept idling (consistent with constitution principle VIII caveat — "near-zero trade frequency is a bug" but TREND_DOWN is a valid reason),
- B) Allow buying dips in TREND_DOWN at reduced size (counter-trend with tight TP)?

### Q3: `forever.py` — two processes or one?

The plan proposes two supervised processes (`bot.py` + `telegram.py`). The current design uses one (`telegram.py` launches `autotrade()` in a daemon thread). **Preference:**
- A) Two processes (cleaner lifecycle, restart-safe),
- B) One process with thread (simpler deployment, what you have today)?

### Q4: Legacy data preservation

`signal_history` has 39,047 rows with 1,370 labeled outcomes and 2,900 ML scores. This is valuable for offline analysis. **Should we:**
- A) Keep the table in the live DB (the new bot ignores it, research tools read it),
- B) Export to JSON/CSV and drop the table (cleaner DB, data preserved offline)?

### Q5: Trade history continuity

`all_trades.json` has 25 real DEX trades with fills and fees. **Should the migration import these into the new `closed_trades` table** so PnL history is continuous, or start fresh?

### Q6: Minimum viable asset list

The specification supports multi-asset, but calibration data exists only for NEAR. **For the initial dry-run, should the bot trade only NEAR** (validated), or include ETH/SOL immediately (no calibration data)?

### Q7: Default `max_hold_minutes`

The time stop has no historical data to calibrate against. The prompt didn't specify a default. **Proposed default: 240 minutes (4 hours).** A mean-reversion trade that hasn't reverted in 4 hours was not a mean-reversion trade. Acceptable?

---

## GATE 1 — Decisions

**Verdict: APPROVED to proceed to Phase 2.**

| Q | Decision |
|---|---|
| Q1 — Margin mode | **C: Switch to isolated margin.** Liquidation distance computable from position alone. At 10% slots × 3 max, deploying 30% of equity — not capital-constrained. If Orderly doesn't support isolated on this account, fall back to A with a conservative safety factor. |
| Q2 — CEX TREND_DOWN | **A: Accept idle.** Buying dips in a confirmed downtrend with no stop (spot) converts a bad entry into an indefinite bag. Futures still shorts in TREND_DOWN — the system keeps trading. |
| Q3 — Supervision | **A: Two processes.** Enable SQLite WAL mode. Trading loop and Telegram listener restart independently. |
| Q4 — Legacy data | **B: Export, then drop.** Export `signal_history` to JSON for offline research, then drop the table from the live DB. |
| Q5 — Trade history | **Don't import.** Start fresh with `closed_trades` in the new schema. Tag legacy code with `git tag legacy-v3`. |
| Q6 — Assets | **NEAR only for live trading.** But run ETH and SOL in **observation mode**: compute regime, evaluate signals, write `signals` rows — but place no orders. This collects regime distribution data during the dry-run at zero risk. |
| Q7 — max_hold_minutes | **240 to start, then derive.** Measure time-to-TP distribution among winners in the dry-run. Set final value near the 90th percentile. Split per venue: futures has SL as primary exit (time stop is backstop); spot has only TP + time stop, so it's load-bearing and may need to be tighter. |

### The 1,370-labels-vs-25-trades discrepancy — EXPLAINED

The 25 trades in `all_trades.json` are a **point-in-time snapshot** exported by `get_trades.py`. The labeler (`trade/signal_agent/labeler.py`) queries the **full Orderly trade history API** directly — it has access to all trades across the account's history.

The actual data:
- `accumulated_trades.json`: **1,450 trades** across **44 different Orderly perp symbols** (NEAR, ETH, SOL, BTC, SUI, DOGE, etc.) — this is the complete trade history the labeler matched against
- `binance_trades.json`: **989 entries**, all NEAR, all BUY side
- The labeler found trade matches for **1,370 out of 39,047 signals**: 727 DEX + 643 CEX
- The 82.6% win rate is across 44 symbols over ~100 days — **more meaningful, not less**

This also means the win rate includes many assets with different volatility profiles than NEAR. The dry-run will establish NEAR-specific numbers.

### Conditions for Phase 2

1. ✅ **Discrepancy explained** (above). The labeler had full API access; the JSON files were snapshots.
2. **Add ETH/SOL observation mode to Phase 2.7 dry-run spec.** (Updated in plan below.)
3. **Record `max_hold_minutes` as provisional** with the derivation rule: measure winner time-to-TP distribution, set at 90th percentile, split per venue.

### Plan adjustment: position persistence from scratch

`save_grid_position` and `load_grid_positions` do not exist in the current `db_ops.py`. Module 2.1 (`db/schema_v2.sql` + `db/db_ops.py`) must build position CRUD from scratch. The original estimate of ~150 lines for `db_ops.py` is tight; revised to ~180 lines to account for `open_positions` table with full CRUD (`save_position`, `load_positions`, `update_position`, `delete_position`, `load_all_positions`).

---

## Phase 2 — Approved

Build order:
1. `db/schema_v2.sql` + `db/db_ops.py` (~180 lines)
2. `trade/pnl.py` (~120 lines)
3. `trade/regime.py` (~150 lines)
4. `trading_bot/executor.py` (~400 lines)
5. `trading_bot/spot_scalper.py` (~250 lines)
6. `trading_bot/futures_scalper.py` (~300 lines)
7. `bot.py` (~200 lines)
8. Dry-run validation (48 hours, NEAR live + ETH/SOL observation)
9. `research/performance_llm.py` (rewrite)
10. Cleanup
