# Plan: Dynamic Asset Universe & Capital View

**Feature**: 003-dynamic-asset-universe-capital | **Date**: 2026-08-04
**Status**: Implemented — remaining work is dry-run calibration

## Summary

Replace the static per-asset configuration (Amendment 004 `asset_configs`) with a
daily scanner (`trade/universe.py`) that selects the tradeable set per venue, and
replace the Assets view with a Capital view where capital is a per-venue pool.
The scanner runs in a dedicated background thread owned by `bot.py` — never
inside the trading cycle.

## Research Summary

- **Ranking by depth alone selects the wrong assets** (top = most arbitraged =
  least mean-reversion inefficiency); the target is a volume-rank band
  (defaults 15–90), "liquid enough that spread/slippage are a small fraction of
  TP, and no more liquid than that."
- **The ranking key must be the strategy's own replay hit rate**, not a
  statistical proxy: replay the live entry rule over `universe_replay_days` of 5m
  candles and rank by `recovery_rate`.
- **Fees are per-venue and first-class**: DEX 0.06% vs CEX 0.20% round-trip.
  Every net-edge calculation uses the venue's own fee.
- **DEX data**: Orderly public market data is restricted; use Binance as the
  data proxy for spread/volume/OHLCV (existing codebase pattern), with the DEX
  candidate set bounded by the Orderly perp listing. On failure the scan is
  abandoned and the previous universe preserved (fail closed).

## Design

### Module: `trade/universe.py`

| Function | Role |
|---|---|
| `compute_thresholds(atr, dk, dm, pk, pm, tk, tm, sk, sm)` | Shared adaptive-threshold function — used by BOTH live scalpers and the replay |
| `venue_fee_pct(venue)` / `breakeven_recovery_rate(venue)` / `min_recovery_rate(venue)` | Per-venue fee and breakeven (`auto` resolution) |
| `_fetch_candidates(venue)` | Stage 1 — 2 whole-exchange market-data calls (+ exchangeInfo config), DEX via Binance proxy |
| `_hard_filters_pass(...)` | Stage 2 — volume / spread / rank band / fundability |
| `_TokenBucket` + `_depth_check(...)` | Stage 3 — top-10 depth both sides, rate-limited; `ScanBudgetExhausted` aborts preserving previous universe |
| `replay_symbol(...)` | Stage 4 — rolling peak/trough (WINDOW_SIZE=40, warmup 10) + `compute_thresholds`; yields signals_count, recovery_rate, median_minutes_to_tp, atr_pct_median |
| `select_ranked(...)` | Stage 5 — filter min_signals/min_recovery, rank by recovery then ATR, truncate to size |
| `scan_venue(venue, equity, depth_budget)` | Orchestrates stages; only writes via `replace_universe` at the end |
| `run_scans_if_due(...)` / `is_universe_stale(venue)` | Due-ness and staleness for the bot thread |

### `bot.py` changes

- Background scanner thread: scans a venue when the stored scan is missing or
  older than `universe_scan_interval_hours`; checks every 10 minutes.
- Loop iterates `get_tradeable_universe(venue)` (non-blacklisted), unioned with
  assets having open positions — **exits always run first**, so churn never
  forces an exit.
- Guards: stale universe → no new entries (exits continue); live spread from the
  OBI snapshot exceeding scan spread × degradation multiple → skip entries
  (no extra API call).
- `_get_obi_and_spread(asset, venue)` replaces `_get_obi_binance/_orderly`
  (same single depth call, returns spread too).
- `set_venue_equity(venue, equity)` caches live equity into `venue_state` each
  cycle; startup validation now also runs `validate_capital_pools`.

### Capital model (`trade/pnl.py`)

`compute_slot_size(venue, equity, min_notional)` = `{venue}_slot_pct × live
equity`, floored at `min_notional × 1.5`, cached per UTC day. The per-asset
`capital` branch is removed — sizing never reads `capital_*` (exchange wins).
`max_slots_cex`/`max_slots_dex` replace the global `max_slots` gate in the
scalpers.

## Contracts

- `dashboard/main.py`: `GET /api/capital`, `GET /api/universe/{venue}`,
  `PUT /api/universe/{venue}/{asset}/blacklist`. `/api/assets*` removed.
- `telegram.py`: `/capital`, `/universe [cex|dex]`, `/blacklist add|remove`.
- `dashboard-ui`: Assets tab → `CapitalManager.tsx` (two venue panels + read-only
  Universe panels with blacklist toggles).

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/003-dynamic-asset-universe-capital/` | ✅ spec, data-model, plan, quickstart, research, tasks, checklists, contracts |
| `trade/universe.py` | ✅ New scanner + shared thresholds |
| `db/schema_v2.sql` | ✅ `asset_universe` + `venue_state` DDL |
| `db/migrations/006_amendment_003.sql` | ✅ New migration (006 — 004/005 taken) |
| `db/db_ops.py` | ✅ Universe CRUD, blacklist carry-forward, venue equity, capital pool |
| `trade/settings_schema.py` | ✅ 16 new SettingSpecs |
| `trade/settings_rules.py` | ✅ Cross-checks + `validate_capital_pools` |
| `trade/pnl.py` | ✅ Per-venue slot sizing |
| `trading_bot/spot_scalper.py`, `futures_scalper.py` | ✅ Shared thresholds + per-venue slot caps |
| `bot.py` | ✅ Scanner thread + universe loop + guards |
| `telegram.py` | ✅ `/capital`, `/universe`, `/blacklist` |
| `dashboard/main.py` | ✅ Capital + universe endpoints |
| `dashboard-ui/src/CapitalManager.tsx`, `App.tsx`, `validation.ts` | ✅ Capital view |
| `tests/test_amendment003.py` | ✅ 17 unit tests |
| `docs/CALIBRATION.md`, `docs/CURRENT_STATE.md` | ✅ Updated |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Replay diverges from live logic | Low | High | Shared `compute_thresholds` + patch test |
| DEX public data restricted | Known | Medium | Binance proxy; empty scan preserves previous universe |
| Scanner blocks trading loop | Low | High | Background thread, never in the cycle |
| Blacklist silently erased | Low | Medium | Carried forward by (venue, asset) |
| Stale universe trades blindly | Medium | High | `universe_max_age_hours` blocks entries; exits continue |

## Remaining (dry-run)

Universe churn, predicted-vs-realized recovery gap, rank-band decile evidence,
per-venue net expectancy — see `docs/CALIBRATION.md`.
