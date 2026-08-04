# Implementation Plan: MockbaV4 Mean-Reversion Bot

**Plan Version**: 1.3.0 | **Created**: 2026-07-26 | **Status**: ✅ GATE 1 approved — Phase 2 (Amendment 002 complete)

---

## Module Boundaries & Line Budgets

| Module | Target Lines | Responsibility |
|---|---|---|
| `db/db_ops.py` | ~180 | CRUD for 4 tables: settings, closed_trades, open_positions, signals. Migration from legacy schema. **Position persistence built from scratch — `save_grid_position`/`load_grid_positions` don't exist in current code.** |
| `trade/pnl.py` | ~120 | Trade recording, daily PnL, kill switch evaluation, equity-based slot sizing. |
| `trade/regime.py` | ~150 | Slope-based regime detection on 1h+4h, volume strength grade, per-asset caching. |
| `trading_bot/executor.py` | ~400 | Unified exchange abstraction. Order placement, fill verification, symbol caching. |
| `trading_bot/spot_scalper.py` | ~250 | Spot entry logic, `manage_open_positions()` for Binance. |
| `trading_bot/futures_scalper.py` | ~300 | DEX entry logic, `manage_open_positions()` for Orderly, SL verification. |
| `bot.py` | ~200 | Autotrade loop, startup validation gate, kill switch check, structured logging. |
| **Total target** | **~1,570** | Slightly above 1,500 due to executor complexity (two exchange APIs). |

## Exchange Abstraction

`executor.py` presents `class Exchange` with a single interface over Binance spot and Orderly perps:

```python
class Exchange(ABC):
    name: str                           # "binance" | "orderly"
    quote_asset: str                    # "USDT" | "USDC"

    def get_equity(self) -> float: ...
    def get_open_positions(self, asset: str) -> list[Position]: ...
    def get_open_orders(self, asset: str) -> list[Order]: ...
    def place_entry(self, asset: str, side: str, qty: float,
                    tp_pct: float, sl_pct: float | None,
                    leverage: int | None) -> FillResult: ...
    def place_stop(self, asset: str, position: Position) -> bool: ...
    def market_close(self, asset: str, position: Position) -> FillResult: ...
    def get_fill(self, order_id: str) -> FillResult | None: ...
    def get_order_status(self, order_id: str) -> str: ...
    def get_symbol_info(self, asset: str) -> SymbolInfo: ...
```

Venue-specific implementations (`BinanceSpot`, `OrderlyPerps`) handle API differences internally. The scalpers never import `requests` or construct HTTP payloads — they call `exchange.place_entry(...)` and receive a `FillResult`.

## Symbol & Filter Caching

- `SymbolInfo` is a frozen dataclass: `base_tick`, `quote_tick`, `min_qty`, `min_notional`, `lot_step`, `price_precision`.
- Fetched via `Exchange.get_symbol_info(asset)` on first access per asset per session.
- Cached in `exchange._symbol_cache: dict[str, SymbolInfo]`.
- Cache lives for the process lifetime (valid for the session; filters rarely change).

## Order Idempotency

- Client order IDs derived from position IDs: `f"mockba-{venue}-{asset}-{position_id}-{order_type}"`.
- `place_entry()` generates a `position_id` (UUID) before placing the order, includes it as `client_order_id`.
- If the API call times out but the exchange accepted the order, a retry with the same `client_order_id` is idempotent (Orderly and Binance both support this).

## Error Taxonomy

| Error Type | Action | Escalation |
|---|---|---|
| **Transient API error** (5xx, timeout, rate limit) | Retry up to 3 times with exponential backoff | Skip cycle after 3 failures |
| **Order rejected** (invalid qty/price, insufficient margin) | Log ERROR, skip this asset this cycle | None (fix config) |
| **Fill verification timeout** (entry placed but fill status unknown after 10s) | Query position state from exchange; if position exists, adopt it; if not, log CRITICAL | Disable trading for this asset until manual review |
| **Stop verification failed** (entry filled, no SL attached) | Place standalone stop; if that fails, market-close | Notify Telegram at WARNING |
| **Consecutive state-query failures** (can't get equity/positions 5x in a row) | Disable trading entirely | Notify Telegram, set `trading_enabled = 0` |
| **Kill switch breach** (daily loss limit or consecutive losses) | Disable new entries; existing positions run to normal exits | Notify Telegram; require manual `trading_enabled = 1` |

## Migration Strategy

### Schema migration (`db/migrate_to_v2.py`)

1. Create new tables: `closed_trades`, `open_positions`, `signals` (idempotent `CREATE TABLE IF NOT EXISTS`).
2. Copy `settings` rows to new schema (same table, same structure).
3. Insert missing default settings: `grid_sl_pct`, `grid_direction`, `grid_pump_pct`, `dex_slot_pct`, `cex_slot_pct`, `dry_run`, `daily_loss_limit`, `max_consecutive_losses`, `max_hold_minutes`, `max_leverage`, `round_trip_fee_pct`, `assumed_slippage_pct`, `min_net_edge_pct`, `dex_compound_pct`, `regime_cache_sec`, `min_entry_spacing_pct`.
4. **Do NOT drop `signal_history`** — it contains 1,370 labeled trade outcomes valuable for `research/` analysis. Move it aside or leave it. The new bot does not read it; the research analyzer does.
5. **Do NOT drop `trades_daily`** — keep for reference; new bot writes to `closed_trades`.
6. Run `migrate_to_v2.py` as a one-shot script before starting `bot.py`.

### Preserving existing trade history

- `all_trades.json` (25 DEX trades with real fills) — keep in `data/`. Can be imported into `closed_trades` for PnL continuity.
- `signal_history` — leave in DB. Research tools read it. New bot ignores it.
- Legacy code — tag with `git tag legacy-v3` before deletion (Phase 2.9).

## Test Strategy

### Unit tests (mocked exchange responses)

| Module | What's tested | Mock boundary |
|---|---|---|
| `pnl.py` | PnL arithmetic (long/short, both venues, fees), daily reset, kill switch triggers, slot sizing floor | `Exchange` methods return controlled values |
| `regime.py` | Classification from synthetic OHLCV (RANGE, TREND_UP, TREND_DOWN), cache expiry, low-volume trend classification | `fetch_ohlcv()` returns synthetic DataFrames |
| `spot_scalper.py` | AND condition enforcement, regime direction matrix, cooldown, spacing, `manage_open_positions()` time stop, TP fill detection | `Exchange` mocked |
| `futures_scalper.py` | Same as spot + SL verification, emergency close on SL failure | `Exchange` mocked |
| `bot.py` | Startup validation gate (tp_pct ≤ sl_pct → refused, net edge below min → refused), kill switch blocks entries not exits, `manage_open_positions` runs before entries | All exchanges mocked |
| `executor.py` | Quantity/price rounding to symbol ticks, base-asset fee deduction for Binance, client order ID format, dry_run returns simulated fill | HTTP layer mocked |

### Dry-run harness (Phase 2.7)

- Runs against **live market data** (real OHLCV, real order books, real prices).
- **No real orders** — `dry_run=true` throughout.
- 48-hour continuous run.
- **Live trading**: NEAR only (calibrated asset, worst-case slippage on Orderly's thin book).
- **Observation mode**: ETH and SOL run regime detection + signal evaluation in parallel. They write `signals` rows (action="skipped_observed") but place no orders. This collects regime distribution data for these assets at zero risk during the same 48 hours.
- Reports: trades/day/asset (NEAR live), win rate, avg win/loss, net PnL, measured slippage, max time in position, exit reason distribution, regime distribution (all 3 assets).
- **`max_hold_minutes` derivation**: measure time-to-TP distribution among winners. Set final value at the 90th percentile. Split per venue — futures time stop is a backstop behind SL; spot time stop is load-bearing (no SL). Record as provisional until derived.

### SQLite WAL mode

Enable WAL mode before Phase 2 starts:

```sql
PRAGMA journal_mode=WAL;
```

Two processes (bot.py + telegram.py) will concurrently access the DB. WAL mode allows concurrent reads with a single writer without locking. Test concurrent write behavior before dry-run.

## Process Supervision

`forever.py` currently supervises one process. The rebuild needs two:

1. `bot.py` — the trading loop (runs continuously)
2. `telegram.py` — the Telegram listener (runs continuously)

**Decision: Two supervised processes in `forever.py`.**

```python
scripts = ["telegram.py", "bot.py"]
```

`forever.py` already iterates a list and restarts on exit. Adding `bot.py` requires no architectural change — just add it to the list. Both processes are independent: `telegram.py` reads/writes settings via `db_ops`, `bot.py` reads settings each cycle. SQLite handles concurrent reads safely; writes are brief and WAL-mode prevents locking.

Alternative considered and rejected: one process with a thread (current approach — `telegram.py` starts `autotrade()` in a daemon thread). Rejected because daemon threads are killed without cleanup on main-thread exit, which violates restart safety (principle VI). Separate processes get their own lifecycle and cleanup.

## Build Order

1. `db/schema_v2.sql` + `db/db_ops.py` — foundation
2. `trade/pnl.py` — no dependencies beyond DB
3. `trade/regime.py` — needs OHLCV fetcher (new, not from legacy code)
4. `trading_bot/executor.py` — needs exchange APIs, symbol caching
5. `trading_bot/spot_scalper.py` — needs executor, pnl, regime
6. `trading_bot/futures_scalper.py` — needs executor, pnl, regime
7. `bot.py` — needs all of the above
8. Dry-run validation — 48 hours
9. `research/performance_llm.py` — rewrite (offline, no dependency on hot path)
10. Cleanup — tag legacy, delete, move

---

# Amendment 001 — Adaptive Thresholds & Toxicity Observability

**Plan Version**: 1.2.0 | **Date**: 2026-07-26 | **Spec**: `.specify/specs/amendment-001-adaptive-toxicity.md`

**Status**: Partial — spot scalper complete ✅, futures scalper pending 🔧, bot.py verification pending 🔧

---

## Summary

Amendment 001 addresses three problems discovered in live testing: (1) OBI thresholds are useless as a gate — NEAR OBI sits in a narrow 1.03–1.13 band, so a threshold either never triggers (1.0) or always passes (1.20); (2) fixed `dip_pct`/`pump_pct` ignore volatility — 0.4% is too strict in quiet markets and too loose in volatile ones; (3) dip magnitude alone can't distinguish mean reversion from a dump. The fix: demote OBI to a logged metric, add adaptive thresholds via ATR, and build a toxicity observability pipeline that collects data for future evidence-based gating.

The spot scalper (`trading_bot/spot_scalper.py`) is fully rewritten with all Amendment 001 logic. The futures scalper (`trading_bot/futures_scalper.py`) still uses the pre-amendment OBI-gated, fixed-threshold logic and must be ported.

---

## Already Implemented (Baseline)

| Artifact | Status | What it provides |
|---|---|---|
| `db/schema_v2.sql` | ✅ Deployed | 24-column `signals` table with all toxicity columns; `signal_id` FK on `closed_trades` and `open_positions`; superseded settings removed |
| `db/migrations/002_amendment_001.sql` | ✅ Applied to prod | Idempotent migration adding new columns and settings defaults |
| `trade/regime.py` | ✅ `get_atr_pct()` | Computes ATR as % of price from 5m candles, cached per asset per venue for `candle_cache_sec` (60s) |
| `trade/toxicity.py` | ✅ `evaluate()` + `record_observation()` | Four checks (velocity, spread, depth, OBI) with per-asset-per-venue rolling windows; all enforcement OFF by default; returns dict with all verdicts and z-scores |
| `trading_bot/spot_scalper.py` | ✅ Full rewrite | Adaptive thresholds, no OBI gate, toxicity observation, 24-column `_log()` INSERT, `signal_id` wired through `_save_open` and `_close` |

---

## Remaining Work

### Task 1: Rewrite `trading_bot/futures_scalper.py`

Port the futures scalper to match the spot scalper's Amendment 001 logic. The rewrite is a **structural port** — the spot scalper already implements every pattern; the futures scalper needs the same pipeline with venue-specific adaptations.

#### 1.1 Imports and Dependencies

**Add** (from spot scalper pattern):
```python
from trade.regime import get_atr_pct
from trade.toxicity import evaluate as tox_eval, record_observation
from db.db_ops import get_db_connection
```

**Remove** (OBI-gate imports no longer needed):
- `save_signal` (replaced by raw 24-column INSERT in `_log`)
- `can_trade_venue` (not used by spot scalper; if needed, keep)

**Keep** (futures-specific):
- `OrderlyFutures`, `Fill`, `record_closed_trade`, `is_entry_blocked`, `compute_slot_size`
- `save_position`, `load_all_positions`, `update_position`, `delete_position`

#### 1.2 Price Memory Functions — No Change

The `_ensure_memory`, `_update_price_memory`, `_peak`, `_trough`, `WINDOW_SIZE` are identical between spot and futures. No changes needed.

#### 1.3 Rename/Replace Detection Functions

| Current (futures) | Replace with (spot pattern) |
|---|---|
| `_is_price_dip(asset, price, dip_pct)` | `_is_dip(a, p, th)` — same logic, shorter name |
| `_is_price_pump(asset, price, pump_pct)` | `_is_pump(a, p, th)` — same logic, shorter name |

Add `_extreme_pct(a, p)` — computes signed extreme percentage (negative for dips, positive for pumps). Identical to spot scalper.

#### 1.4 Entry Logic (`scalp_cycle`) — Full Rewrite

The current futures `scalp_cycle` has this structure:
```
1. Direction gate by regime
2. Kill switch check
3. Slot limit check
4. Update price memory
5. Read fixed dip_pct/pump_pct/obi_buy/obi_sell from settings
6. Check is_dip + OBI < obi_buy → LONG
7. Check is_pump + OBI > obi_sell → SHORT
8. _log_signal (old 8-column schema) on skip
```

The new structure must match spot scalper:
```
1. Regime gate (TREND_UP→long only, TREND_DOWN→short only; RANGE→both)
2. Kill switch check
3. Slot limit check
4. Update price memory
5. Compute adaptive thresholds (same formula as spot)
6. tp_eff > sl_eff gate
7. Determine dip/pump with adaptive thresholds
8. Toxicity evaluation (tox_eval + record_observation)
9. Direction from dip/pump (no OBI gate)
10. Toxicity enforcement check (if any tox flag enabled AND verdict=1 → block)
11. Cooldown check
12. Spacing check
13. Quantity computation + place_entry
14. _log with 24-column INSERT, wire signal_id
```

**Key differences from spot scalper to preserve**:

| Aspect | Spot (Binance) | Futures (Orderly) |
|---|---|---|
| `place_entry` signature | `(asset, side, qty, price, tp_pct, position_id, sl_pct=0.0)` | `(asset, side, qty, price, tp_pct, sl_pct, leverage, position_id)` |
| Leverage | None | `min(get_setting_int("leverage",3), get_setting_int("max_leverage",3))` |
| `Fill.sellable_qty` | Yes (spot has base-fee deduction) | No — use `fill.filled_qty` |
| Short side | Not supported (spot is long-only) | Supported (RANGE+TREND_DOWN) |
| Venue key | `"binance"` | `"orderly"` |
| Symbol derivation | `f"{asset}USDT"` (in manage_open_positions) | `f"PERP_{asset}_USDC"` (not needed in scalp_cycle) |
| Fee rate for _close | `0.001` | `0.0003` |

**Direction matrix for futures** (constitution: regime gates direction, not blocks all):

| Regime | Long OK | Short OK |
|---|---|---|
| RANGE | ✅ | ✅ |
| TREND_UP | ✅ | ❌ |
| TREND_DOWN | ❌ | ✅ |
| UNKNOWN / other | ❌ | ❌ |

This differs from spot, which is long-only (TREND_DOWN blocks all entries).

#### 1.5 The `_log` Function

Replace `_log_signal(asset, venue, regime, obi, extreme_pct, action, reason)` with the 24-column `_log()` from spot scalper:

```python
def _log(a, v, r, d, p, ex, th, at, ob, vl, dr, tx, act, rsn):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""INSERT INTO signals (ts,asset,venue,regime,direction,price,
                extreme_pct,threshold_pct,atr_pct,velocity_pct,obi,obi_z,
                spread_pct,spread_z,depth_top10,depth_ratio,
                tox_velocity,tox_spread,tox_depth,tox_obi,tox_any,tox_enforced,
                action,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), a, v, r, d, p, ex, th, at, vl, ob,
                 tx.get("obi_z"), None, tx.get("spread_z"), None, dr,
                 tx.get("tox_velocity"), tx.get("tox_spread"), tx.get("tox_depth"),
                 tx.get("tox_obi"), tx.get("tox_any"), tx.get("tox_enforced", 0),
                 act, rsn))
            conn.commit()
            return cur.lastrowid
    except:
        return None
```

Notes:
- `spread_pct` and `depth_top10` are passed as `None` for futures (same as spot — the bot.py fetches OBI but not spread/depth; these are placeholders until a depth endpoint is integrated)
- Returns `cur.lastrowid` as `signal_id` for linking to position

#### 1.6 `_save_open` — Add `signal_id`

Current signature: `_save_open(asset, venue, side, fill, signal_price, tp_pct, sl_pct, pos_id)`

New signature: `_save_open(asset, venue, side, fill, signal_price, tp_pct, sl_pct, pos_id, signal_id)`

Add `"signal_id": signal_id` to the `save_position` dict. Use `fill.filled_qty` (not `fill.sellable_qty` — Orderly doesn't deduct base fees).

#### 1.7 `_close_position` — Add `signal_id` Parameter

Current signature: `_close_position(asset, venue, side, entry, exit, signal, qty, fee_rate, pos_id, reason)`

New signature: `_close_position(asset, venue, side, entry, exit, signal, qty, fee_rate, pos_id, signal_id, reason)`

Pass `signal_id` to `record_closed_trade` (the function already accepts it per the schema).

#### 1.8 `manage_open_positions` — Pass `signal_id` Through

Read `signal_id` from position dict (`pos_dict.get("signal_id")`) and pass it to `_close_position`. All other logic (TP/SL fill detection, time stop, regime exit) stays unchanged.

#### 1.9 Toxicity Settings Readings

All toxicity settings are read fresh per cycle via `get_setting_float` / `get_setting_bool` — identical to spot scalper. The `toxicity.py` module uses the same settings keys regardless of venue. Futures gets toxicity observability "for free" by calling `tox_eval` and `record_observation` with `venue="orderly"`.

---

### Task 2: Verify `bot.py` Compatibility

The `bot.py` main loop calls both scalpers with identical signatures:

```python
spot_cycle(asset, binance, regime, obi, price)
futures_cycle(asset, orderly, regime, obi, price)
```

**Verification checklist**:

| Check | Status |
|---|---|
| `scalp_cycle` signature unchanged (5 params) | ✅ Spot scalper already uses this signature; futures scalper currently uses it too |
| `manage_open_positions` signature unchanged | ✅ Spot: `(asset, exchange)` — unchanged. Futures: `(asset, exchange, current_regime)` — unchanged |
| OBI still computed in `bot.py` | ✅ `_get_obi_binance` / `_get_obi_orderly` still run; OBI is passed to scalpers for logging (not gating) |
| Settings refresh list in `bot.py` | ⚠️ The settings refresh list still references `dip_pct`, `pump_pct`, `obi_buy_threshold`, `obi_sell_threshold`. These are no-ops now (settings deleted from DB, `get_setting` returns empty string). No crash, but stale log noise. **Recommendation**: update the settings list in `bot.py` to include the new Amendment 001 settings keys for change-detection logging. |
| `validate_startup` still works | ✅ Uses `tp_pct` and `sl_pct` which are unchanged |

---

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**: `requests`, `python-dotenv` (already in `requirements.txt`)

**Storage**: SQLite with WAL mode — `schema_v2.sql` already deployed with 24-column `signals` table

**Testing**: Dry-run validation (48 hours with `dry_run=true`). No unit test framework changes needed — the futures scalper test mock boundaries stay the same.

**Target Platform**: Linux server (same as current deployment)

**Performance Goals**: Sub-100ms per-cycle decision. ATR cached for 60s. Toxicity rolling windows are O(1) per observation (deque append). No new API calls introduced by the rewrite.

**Constraints**:
- Constitution VII line budget: ~1,570 total target. Futures rewrite is net-neutral (~300 lines, same as current).
- Minimal-change principle: reuse spot scalper patterns verbatim where possible; only diverge for venue-specific differences.
- Toxicity enforcement remains OFF by default — this rewrite does not enable gating, only observability.

---

## Constitution Check (Amendment 001)

*Re-evaluated against all 8 principles for the remaining work.*

| Principle | Assessment |
|---|---|
| **I. One Strategy** | ✅ No change. Mean reversion is still the only strategy. Adaptive thresholds make it work across volatility regimes rather than changing the strategy. |
| **II. Reward Must Exceed Risk** | ✅ `tp_eff > sl_eff` checked every cycle before entry. If adaptive computation violates this, the cycle is skipped. |
| **III. No Leveraged Position Without Confirmed Stop** | ✅ Futures scalper retains bracket-order SL verification. No change to stop-loss logic. |
| **IV. Unknown State Means No Trading** | ✅ No change to fail-closed state queries. Toxicity warmup (NULL verdicts) does NOT block entries. |
| **V. Real Fills Only** | ✅ No change. Fill prices, fees, and slippage still come from exchange responses. |
| **VI. Restart Safety** | ✅ Toxicity history is in-memory only — a restart resets warmup, but NULL verdicts don't block entries. No trade safety impact. |
| **VII. Simplicity** | ✅ Futures rewrite is net-neutral on line count (~300 lines). Toxicity integration adds ~30 lines; OBI-gate removal saves ~20 lines. No new modules. |
| **VIII. The Bot Trades** | ✅ Adaptive thresholds make the bot more likely to find entries in diverse volatility regimes. Observability without gating means filters collect data but don't block trades. |

**Gate**: ✅ PASS — no violations, no unjustified deviations.

---

## Phase 0: Research (Resolved)

All technical unknowns are resolved by the completed spot scalper implementation. The futures rewrite is a structural port with no novel research questions.

| Unknown | Resolution |
|---|---|
| How does toxicity.py handle per-venue history? | `_key(asset, venue)` — `"orderly:{asset}"` for futures, `"binance:{asset}"` for spot. Already supported. |
| Does `get_atr_pct` work for Orderly? | Yes — `_fetch_orderly_ohlcv` exists in `regime.py`. ATR is computed from the same 5m candle endpoint. |
| Can the 24-column `_log` INSERT be shared verbatim? | Yes — venue-agnostic. The `spread_pct` and `depth_top10` are `None` for both venues (no depth endpoint integrated yet). |
| Does `bot.py` need changes? | Minimal — only the settings refresh list should be updated to track new Amendment 001 keys. The `scalp_cycle` signature is unchanged. |
| Futures `place_entry` parameter order | `(asset, side, qty, price, tp_pct, sl_pct, leverage, position_id)` — leverage and sl_pct are positional, unlike spot where sl_pct is keyword with default. Must match exactly. |

---

## Phase 1: Design Artifacts

### Data Model — No Changes

The `signals` table (24 columns), `open_positions` (with `signal_id`), and `closed_trades` (with `signal_id` FK) are already deployed. No schema changes in this phase.

### Contracts — No New Interfaces

Both scalpers expose the same two public functions:

```python
def manage_open_positions(asset: str, exchange: OrderlyFutures, current_regime: str) -> None
def scalp_cycle(asset: str, exchange: OrderlyFutures, regime: str, obi: float, live_price: float) -> Optional[str]
```

Signatures unchanged. Return values unchanged (`"buy"`, `"sell"`, or `None`).

### Quickstart Validation Guide

See `quickstart.md` (to be generated) for runnable validation scenarios.

---

## File Manifest (This Amendment)

| File | Action | Lines (est.) |
|---|---|---|
| `trading_bot/futures_scalper.py` | Rewrite entry logic, replace `_log_signal` with `_log`, wire `signal_id` | ~300 (net-neutral) |
| `bot.py` | Update settings refresh list (optional, low-risk) | ~5 changed lines |
| `.specify/plan.md` | This section | +150 lines (appended) |

---

## Build Order (Amendment 001 Remaining)

1. Rewrite `trading_bot/futures_scalper.py` — port all Amendment 001 patterns from spot scalper
2. Verify `bot.py` compatibility — confirm `scalp_cycle` signatures match, update settings refresh list
3. Dry-run validation — 48 hours with `dry_run=true`, both venues, verify signals table has 24-column rows from both scalpers
4. Cross-venue consistency check — compare adaptive threshold values and toxicity verdicts for same asset across spot and futures (should match per Success Criterion SC-005)

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Futures `place_entry` parameter order mismatch | Low | High — entry silently fails or uses wrong SL | Verify against executor.py signature before running; dry-run catches this |
| Orderly OBI unavailable (API restricted) | Known | Low — OBI is logged not gated | `bot.py` already uses Binance OBI as proxy for Orderly (`_get_obi_orderly` → `_get_obi_binance`) |
| Toxicity warmup blocks futures entries | None | N/A | NULL verdicts don't block; enforcement is OFF by default |
| Signal ID FK violation on `closed_trades` | Low | Medium — PnL not linked to signal | `_close_position` must pass `signal_id` to `record_closed_trade`; dry-run validation catches NULL FK |

---

# Amendment 002 — Settings Validator & LLM Helper

**Plan Version**: 1.3.0 | **Date**: 2026-07-26 | **Spec**: `.specify/specs/001-amendment-002-settings-validator/spec.md`

**Status**: ✅ Complete — all modules built, migration applied

---

## Summary

Amendment 002 adds three new modules and two database tables to provide: (1) a static settings schema (`trade/settings_schema.py`) as the single source of truth for all 51 setting metadata records, (2) a deterministic, offline validator (`trade/settings_rules.py`) that catches invalid configurations before the bot starts or after any setting change, (3) an LLM-powered explainer and proposer (`research/settings_llm.py`) that runs exclusively in the `research/` path (never in the trading execution path, per constitution principle I), (4) a `settings_baseline` table for provenance tracking of each setting's value, and (5) a `settings_proposals` table for an append-only audit trail of every LLM-generated suggestion.

The validator backs three surfaces from a single `validate()` function: bot startup gate, Telegram `/list` inline validation, and dashboard real-time validation. The LLM helper is surfaced via Telegram `/explain` and `/propose` commands with caching and rate limiting.

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**: stdlib (`dataclasses`, `hashlib`, `json`, `os`, `time`), `requests` (LLM helper only, in `research/`)

**Storage**: SQLite (existing `mockba.db`), WAL mode. Two new tables added via migration `003_amendment_002.sql`. File-based JSON cache in `data/llm_cache/` for LLM explanations.

**Testing**: pytest (unit tests for validator rules; import isolation test for constitution I)

**Target Platform**: Linux server (same as bot)

**Project Type**: Trading bot internal tooling (schema + validator in hot path; LLM helper in offline `research/` path)

**Performance Goals**: Validator: <1ms per setting (O(1) lookup + type check + range check). Batch `validate_all()`: <50ms for 51 settings. LLM explain cache hit: <10ms (file read). LLM API calls: gated by rate limiter and timeout.

**Constraints**: Validator: pure function, no I/O (except documented baseline DB read with graceful degradation), no network, no LLM. LLM helper: never imported by `trading_bot/`, `bot.py`, or any execution-path module.

**Scale/Scope**: 51 settings across 7 groups. 2 new DB tables. 3 new modules (~330 lines total).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance | Evidence |
|---|---|---|
| **I. One Strategy** | ✅ PASS | `settings_llm.py` is in `research/` — verified by AST import scan that no `trading_bot/` or `bot.py` module imports it. LLM is advisory only, never in execution path. |
| **II. Reward Must Exceed Risk** | ✅ PASS (reinforced) | Validator enforces `tp > sl` and `net_edge >= min_net_edge_pct` as error-level gates. These are the same startup gates from the base plan, now backed by the schema and validator. |
| **III. No Leveraged Position Without Confirmed Stop** | ✅ N/A | Amendment 002 does not modify position management or order placement. |
| **IV. Unknown State Means No Trading** | ✅ PASS (reinforced) | Validator adds a startup gate: if schema module is missing or corrupted, bot refuses to start. Schema is now part of "known state." Settings missing from DB are errors. |
| **V. Real Fills Only** | ✅ N/A | Amendment 002 does not modify PnL or fill recording. |
| **VI. Restart Safety** | ✅ PASS | Validator runs on every startup. Baseline table preserves provenance across restarts. Proposals table is append-only (no lost data on crash). LLM cache is file-based (survives restart). |
| **VII. Simplicity Is a Constraint** | ✅ PASS | Validator is a pure function — maximally simple, no side effects, 100% testable. Schema is a frozen dataclass list (~140 lines, version-controlled). LLM helper is isolated in `research/` (~130 lines). No new frameworks, no new dependencies beyond `requests` (already used by bot). |
| **VIII. The Bot Trades** | ✅ N/A | Validator blocks only invalid configs, not valid ones. Warnings do not block trading. |

**Verdict**: All applicable principles pass. No violations to justify. No complexity tracking needed.

### Post-Design Re-Check

After reviewing the implemented modules against the spec:

| Check | Result |
|---|---|
| FR-B4 (pure computation) | ✅ Pass — validator has one `try/except` DB read for toxicity baseline check; degrades gracefully to `ok` if DB unavailable. All other checks are pure. |
| FR-C1 (no trading-path imports) | ✅ Pass — verified by AST scan (see quickstart Scenario 6). |
| FR-D3 (idempotent migration) | ✅ Pass — `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`. |
| FR-C5 (proposals never write to settings) | ✅ Pass — `_save_proposals` writes only to `settings_proposals` table. |

**Known deviation**: FR-B4 specifies "no database access." The validator reads `settings_baseline` for the toxicity baseline unvalidated check. This is documented in [research.md](specs/001-amendment-002-settings-validator/research.md#R7) with rationale. The check is wrapped in `try/except` — if DB is unavailable, it silently passes. This affects exactly one check out of 15+.

## Module Breakdown

### 1. `trade/settings_schema.py` (~140 lines)

**What**: Static metadata registry for all 51 settings. Frozen `SettingSpec` dataclass with key, type, group, unit, hard/soft bounds, description, and dependency list.

**Design decisions**:
- `frozen=True` dataclass — immutability guaranteed at the language level. No runtime mutation possible.
- `BY_KEY: dict[str, SettingSpec]` — O(1) lookup. Built once at import time.
- `ALL: list[SettingSpec]` — ordered iteration for UI rendering (grouped by category).
- `GROUPS: list[str]` — sorted unique groups for dashboard filter tabs.
- Type stored as `type` (not string) — enables `isinstance(value, spec.type)` in validator.
- `depends_on: tuple[str, ...]` — reserved for future cross-check automation. Current cross-checks are hardcoded in validator for clarity.

**Invariant**: Every setting in the DB has exactly one `SettingSpec`. Every `SettingSpec` has exactly one row in the DB settings table. Verified by the quickstart scenario.

### 2. `trade/settings_rules.py` (~140 lines)

**What**: Deterministic validator. Pure function `validate(key, value, ctx) → Verdict` with `validate_all(ctx) → dict[str, Verdict]` batch wrapper.

**Design decisions**:
- **Single entry point**: `validate()` is the only public validation function. `validate_all()` calls it in a loop. Every surface (bot startup, Telegram, dashboard) calls the same function — no forked logic.
- **`SettingsContext`**: Simple dataclass for cross-check data (`venue`, `equity`, `min_notional`). Caller provides it. If `None`, only type/range checks run. This is how the validator stays pure while supporting venue-specific checks.
- **`_coerce()`**: Type coercion with graceful failure. String `"true"` → `True`, `"1.5"` → `1.5`, `"abc"` → `None` (triggers type error).
- **Cross-check order**: Type → hard range → soft range → cross-setting. Early returns at first violation. A single setting can only produce one verdict per call — the most severe violation wins.
- **`suggested_value`**: Computed where possible (e.g., suggest `round(sl * 1.5, 2)` when tp ≤ sl). Enables one-click fix proposals.
- **`Verdict` dataclass**: Three fields (`level`, `message`, `suggested_value`). No inheritance, no polymorphism. Serialization-friendly for dashboard JSON.

**Cross-check rules implemented** (mapped to FR-B3):

| Rule | Level | Trigger | Suggested Fix |
|---|---|---|---|
| `tp_min_pct > sl_min_pct` | error | `tp <= sl` | `round(sl * 1.5, 2)` |
| `sl_min_pct < tp_min_pct` | error | `sl >= tp` | `round(tp * 0.66, 2)` |
| `tp_k > sl_k` | warn | `tp_k <= sl_k` | `round(sl_k * 1.5, 2)` |
| Net edge ≥ `min_net_edge_pct` | error | `tp - fee - slip < min_edge` | `round(min_edge + fee + slip + 0.05, 2)` |
| Slot % × equity ≥ min notional × 1.5 | error | slot < floor | `round((floor / equity) * 100, 1)` |
| `max_slots × slot_pct ≤ 100` | error | >100% of equity | `int(100 / slot_pct)` |
| `leverage ≤ max_leverage` | error | `lev > max_lev` | `max_lev` |
| `dip_k` too low with adaptive on | warn | `adaptive && dip_k < 0.3` | None |
| Toxicity enforce + unvalidated baseline | warn | enforcement on, baseline unvalidated | None |

**FR-B3 gaps** (deferred):
- Liquidation distance check: requires live entry price and liquidation price — not computable from settings alone. Deferred to per-trade validation in scalper.
- Toxicity warmup (`tox_window < 5`): covered by soft range on `tox_window` (soft_min=60, hard_min=20 — but spec says warn at <5). The hard_min=20 is stricter; this is acceptable.
- Adaptive inactivity (all `*_k` at defaults): deferred to future validator enhancement.

### 3. `research/settings_llm.py` (~130 lines)

**What**: LLM explainer and proposer. Two public functions: `explain(key, language, capital_band) → str` and `propose(context_summary) → list[Proposal]`.

**Design decisions**:
- **File-based cache**: `data/llm_cache/{sha256}.json`. Keyed by `(key, language, capital_band)`. TTL from `llm_explain_cache_days`. Rationale: no DB dependency, survives restarts, disposable (miss → regenerate).
- **Capital-band segmentation**: Explanations differ by account size (`under_100`, `100_to_1k`, `1k_to_10k`, `above_10k`). A $500 account gets different risk framing than a $50k account.
- **Rate limiter**: In-memory list of call timestamps. Pruned on each check. `llm_max_calls_per_hour` (default 20). Shared across explain and propose.
- **Deterministic-first proposals**: `propose()` always runs the validator first. Even if LLM is disabled/unreachable/rate-limited, the operator receives validator-driven proposals with `heuristic` confidence. LLM upgrades confidence to `measured` if data supports it.
- **LLM prompt constraints**: Temperature 0.1, `response_format: json_object` for proposals. LLM is asked to grade confidence, not invent values. Prompt includes current settings + measured context + deterministic suggestions.
- **Graceful degradation**: Every LLM call is wrapped in try/except. On failure: explain falls back to `spec.short` + validator verdict; propose returns deterministic-only proposals.
- **API configuration**: Model from `llm_model` setting, API key from `DEEP_SEEK_API_KEY` or `DEEPSEEK_API_KEY` env var, endpoint hardcoded to DeepSeek API (the project's chosen LLM provider).

**Constitution I compliance**: Verified by AST import scan. No `trading_bot/*.py` or `bot.py` imports `settings_llm`. The module is imported only by `telegram.py` (Telegram handlers) and `dashboard/main.py` (dashboard API). Neither is in the trading execution path.

## Database Changes (Migration 003)

### `settings_baseline`

```sql
CREATE TABLE IF NOT EXISTS settings_baseline (
    key            TEXT PRIMARY KEY,
    baseline_value TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unvalidated',  -- 'measured'|'unvalidated'|'overridden'
    evidence       TEXT,
    updated_at     REAL NOT NULL
);
```

**Purpose**: Provenance tracking. Every setting has a baseline row that records whether its value was derived from data (`measured`), is a placeholder (`unvalidated`), or was manually changed after measurement (`overridden`).

**Seed**: Migration inserts all 51 settings with `status='unvalidated'` and `evidence='Amendment 001 placeholder — no measurement performed'`.

### `settings_proposals`

```sql
CREATE TABLE IF NOT EXISTS settings_proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     REAL NOT NULL,
    source         TEXT NOT NULL,            -- 'deterministic'|'telegram'
    key            TEXT NOT NULL,
    current_value  TEXT NOT NULL,
    proposed_value TEXT NOT NULL,
    reason         TEXT NOT NULL,
    evidence       TEXT,                     -- JSON array
    confidence     TEXT NOT NULL,            -- 'measured'|'heuristic'|'no_basis'
    status         TEXT NOT NULL DEFAULT 'pending',
    decided_at     REAL,
    model          TEXT
);
```

**Purpose**: Append-only audit log of every LLM-generated proposal. Rows are INSERT-only except `status` and `decided_at` on operator decision.

**Invariant**: `settings_proposals` NEVER writes to `settings`. Operator must manually apply accepted proposals via `/set`.

## Integration Points

### Bot Startup (`bot.py`)

```python
from trade.settings_rules import validate_all
results = validate_all()
errors = {k: v for k, v in results.items() if v.level == 'error'}
if errors:
    for k, v in errors.items():
        logger.error(f"setting={k} error=\"{v.message}\"")
    send_bot_message(f"❌ Startup blocked: {len(errors)} setting error(s)")
    sys.exit(1)
```

The validator is called before any exchange connection, before any position query, before any trading logic. This is the startup gate described in Story 2.

### Telegram (`telegram.py`)

- `/list` — each setting row includes validation verdict inline. Error rows are marked ❌, warnings ⚠️.
- `/explain <key>` — calls `research.settings_llm.explain(key, language, band)`. Returns cached or LLM-generated explanation.
- `/propose` — calls `research.settings_llm.propose(context_summary)`. Returns formatted list with accept/reject inline buttons.

### Dashboard (`dashboard-ui/`)

- Client-side validation runs the same rules as `settings_rules.py`, reimplemented in TypeScript for the dashboard UI. Rules are simple enough (type check, range check, cross-check) to replicate without a Python runtime.
- Debounced at 500ms (FR-F1).
- Error → red highlight, warning → amber highlight, ok → no highlight.
- The dashboard never calls the LLM — explanations are fetched from the Python backend via API.

## File Manifest (This Amendment)

| File | Action | Lines |
|---|---|---|
| `trade/settings_schema.py` | ✅ Created | ~140 |
| `trade/settings_rules.py` | ✅ Created | ~140 |
| `research/settings_llm.py` | ✅ Created | ~130 |
| `db/migrations/003_amendment_002.sql` | ✅ Created | ~40 |
| `db/schema_v2.sql` | ✅ Updated (LLM settings seeds) | +6 lines |
| `bot.py` | 🔧 Add startup validation gate | +8 lines |
| `telegram.py` | 🔧 Add `/explain`, `/propose` handlers | ~60 lines |
| `dashboard/main.py` | 🔧 Add validation API endpoint | ~20 lines |
| `dashboard-ui/src/` | 🔧 Client-side validation + inline errors | ~80 lines |
| **Total new** | **~410 lines** | Within Amendment 002 scope |

## Build Order (Amendment 002)

1. ✅ `trade/settings_schema.py` — static metadata (no dependencies)
2. ✅ `trade/settings_rules.py` — depends on schema + `db_ops` (for cross-check values)
3. ✅ `db/migrations/003_amendment_002.sql` — idempotent migration
4. ✅ `research/settings_llm.py` — depends on schema + validator + `db_ops`
5. 🔧 `bot.py` — startup validation gate (add ~8 lines)
6. 🔧 `telegram.py` — `/explain` and `/propose` handlers
7. 🔧 `dashboard/main.py` — validation API endpoint
8. 🔧 `dashboard-ui/` — client-side validation + inline error display

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Schema and DB diverge (setting added to one, not the other) | Medium | High — validator misses settings or flags ghosts | Quickstart Scenario "verify 1:1 match" runs in CI; `validate_all()` warns on unknown keys |
| LLM API key missing in production | Low | Low — explanations fall back to static descriptions; proposals use deterministic fallback | Graceful degradation in all code paths; `llm_helper_enabled` master switch |
| File-based cache grows unbounded | Low | Low — disk usage | TTL enforced by `llm_explain_cache_days`; cache files are tiny (~500 bytes each); max 51 × 3 languages × 4 bands = 612 files = ~300KB |
| Validator performance in hot path | None | N/A | `validate()` is O(1): dict lookup + isinstance + 2 comparisons. `validate_all()` is 51 × that. Benchmarked at <1ms per setting. |
| Migration 003 conflicts with future schema changes | Low | Medium | Idempotent (`IF NOT EXISTS`, `OR IGNORE`). Safe to re-run. |

---

# Amendment 003 — Dynamic Asset Universe & Capital View

**Plan Version**: 1.4.0 | **Date**: 2026-08-04 | **Spec**: `.specify/specs/003-dynamic-asset-universe-capital/spec.md`

**Status**: In progress

## Summary

Amendment 003 replaces the static per-asset configuration with a daily scanner (`trade/universe.py`) that selects the tradeable set per venue, and replaces the Assets view with a Capital view where capital is a per-venue pool. Rationale (static lists guess; depth alone selects the wrong assets; the ranking key must be the strategy's own replay hit rate; per-asset capital cannot survive a daily-changing universe) is recorded in the spec.

## Constitution Check

| Principle | Assessment |
|---|---|
| **I. One Strategy** | ✅ Replay measures the mean-reversion rule itself; no new strategy. Scanner runs offline from the trading cycle (background thread). |
| **II. Reward Must Exceed Risk** | ✅ Net-edge validation becomes per-venue via `dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`; `universe_min_recovery_rate='auto'` is the implied breakeven WR. Stale universe fails closed (no new entries). |
| **III. No Leveraged Position Without Confirmed Stop** | ✅ Unchanged — exit management is untouched by the universe. |
| **IV. Unknown State Means No Trading** | ✅ Stale universe → no new entries, exits still managed. Rate-limit exhaustion → keep previous universe (no partial write). |
| **V. Real Fills Only** | ✅ Slot sizing derives from live exchange equity (`slot_pct × equity`), never from declared `capital_*`. Replay recovery rate is explicitly labeled a relative ranking signal, not an expectancy estimate. |
| **VI. Restart Safety** | ✅ Startup scans if the stored scan is stale; blacklist survives rescans; churn never forces an exit. |
| **VII. Simplicity Is a Constraint** | ✅ `trade/universe.py` is the only new hot-path-adjacent module; shared threshold function is extracted, not duplicated. |
| **VIII. The Bot Trades** | ✅ The scanner targets ranks 15–90 — a band chosen to keep enough inefficiency while remaining executable. |
| **No hardcoded assets** | ✅ Mechanism change, intent preserved: the constitution's `assets`-DB-setting is superseded by the `asset_universe` table. The bot still trades whatever the scanner selects — never a hardcoded list. |

## Module: `trade/universe.py` (new)

Pipeline (per venue):

1. **Stage 1 (2 whole-exchange calls):** `ticker/bookTicker` (spread per symbol) + `ticker/24hr` (volume per symbol). Filter quote asset, exclude leveraged tokens / stablecoins / non-trading. DEX path falls back to Binance proxy (consistent with existing Orderly data handling) filtered to the venue's perp listing.
2. **Stage 2 (hard filters, no ranking):** volume ≥ `universe_min_volume_usd`; spread ≤ `tp_min_pct × universe_spread_ratio_max`; volume-rank within `[universe_rank_min, universe_rank_max]`; `min_notional × 1.5` fundable at current slot size.
3. **Stage 3 (depth, survivors only):** top-10 depth both sides ≥ `universe_depth_slot_multiple × slot_size`; token-bucket rate limit; abort on budget exhaustion keeping the previous universe.
4. **Stage 4 (replay):** fetch `universe_replay_days` of 5m candles; replay the live entry rule using the **shared threshold functions**; produce `signals_count`, `recovery_rate`, `median_minutes_to_tp`, `atr_pct_median`.
5. **Stage 5 (rank & store):** reject `recovery_rate < min_recovery_rate` (auto → breakeven `(sl+fee)/(tp+sl)`) and `signals_count < universe_min_signals`; rank by recovery_rate desc, tiebreak `atr_pct_median` desc; store top `universe_size`; carry blacklist forward.

**Shared threshold functions** (replay must equal live logic — enforced by a shared function and a test):
- `compute_thresholds(atr, dk, dm, pk, pm, tk, tm, sk, sm) -> (dn, pn, te, se)` — extracted from `spot_scalper.py`/`futures_scalper.py`; both scalpers now call it.
- The replay re-uses the same rolling peak/trough window (40 candles, 10-candle warmup) and dip rule as the live bot.

## Per-cycle guards (`bot.py`)

- **Live spread check:** the OBI depth snapshot (already fetched per cycle) also yields live spread; if it exceeds the scan-time spread by `universe_spread_degradation_multiple`, skip entries for that asset this cycle and log. No extra API call.
- **Churn never forces an exit:** iteration is split so `manage_open_positions()` runs for every open position regardless of universe membership; only entries consult the universe.
- **Stale universe:** `now - scanned_at > universe_max_age_hours` → no new entries on that venue, log warning, exits continue.

## Capital model (`trade/pnl.py` + settings)

- `compute_slot_size(venue, equity, min_notional)` uses `{venue}_slot_pct × equity` (percentage of venue equity, min-notional floor, daily recompute). The per-asset `capital` branch is removed — sizing never reads `capital_*`.
- Declared pools `capital_cex_usdt` / `capital_dex_usdc` are for UI/validation only; the exchange's live equity wins on disagreement.
- `max_slots_cex` / `max_slots_dex` replace the global per-asset `max_slots` gate in the scalpers.

## Settings added (Amendment 002 schema + validator)

`universe_scan_interval_hours`, `universe_max_age_hours`, `universe_size`, `universe_min_volume_usd`, `universe_spread_ratio_max`, `universe_rank_min`, `universe_rank_max`, `universe_depth_slot_multiple`, `universe_replay_days`, `universe_min_signals`, `universe_min_recovery_rate` ('auto' → computed breakeven; literal overrides), `universe_spread_degradation_multiple`, `capital_cex_usdt`, `capital_dex_usdc`, `max_slots_cex`, `max_slots_dex`.

Cross-checks (all in `settings_rules.py`): `rank_min >= rank_max` → error; `spread_ratio_max > 0.25` → warn; `max_age_hours < scan_interval_hours` → error; per-venue fee making net edge fail at `tp_min_pct` → error; `max_slots × slot_pct > 100` → error.

## Interfaces

- **Telegram:** `/capital`, `/universe [cex|dex]`, `/blacklist add|remove <asset>`. Per-asset add/toggle/remove handlers removed.
- **Dashboard API:** `GET /api/capital` (declared vs live equity, slot size, deployed, fee, net edge), `GET /api/universe/{venue}`, `PUT /api/universe/{venue}/{asset}/blacklist`, plus `venue_state` equity cache written by the bot each cycle.
- **Mini App:** Assets tab → Capital view (two venue panels + read-only Universe panels with blacklist toggles).

## Data model

- `asset_universe` table (one row per venue per asset, replaced wholesale per scan; `blacklisted` carried forward).
- `venue_state` table (`venue`, `equity`, `updated_at`) — live equity cache the dashboard reads.
- `asset_configs` remains in the DB (legacy) but is no longer read by the bot loop, Telegram, or the Capital view.

## Migration — `db/migrations/006_amendment_003.sql`

Creates `asset_universe` + index, seeds universe/capital settings, deletes any legacy `fee_round_trip_pct` key (no-op here — per-venue keys already exist), seeds `settings_baseline` for the new keys as `unvalidated`.

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/003-dynamic-asset-universe-capital/spec.md` | ✅ Created |
| `trade/universe.py` | ✅ New scanner + shared thresholds |
| `db/schema_v2.sql` | ✅ Add `asset_universe` DDL |
| `db/migrations/006_amendment_003.sql` | ✅ New migration |
| `db/db_ops.py` | ✅ Universe CRUD + `venue_state` |
| `trade/settings_schema.py` | ✅ New SettingSpecs |
| `trade/settings_rules.py` | ✅ Cross-checks |
| `trade/pnl.py` | ✅ Per-venue slot sizing |
| `trading_bot/spot_scalper.py` | 🔧 Shared thresholds + `max_slots_cex` |
| `trading_bot/futures_scalper.py` | 🔧 Shared thresholds + `max_slots_dex` |
| `bot.py` | 🔧 Scanner thread + universe loop + guards |
| `telegram.py` | 🔧 `/capital`, `/universe`, `/blacklist` |
| `dashboard/main.py` | 🔧 Capital + universe endpoints |
| `dashboard-ui/src/` | 🔧 Capital view + Universe panels |
| `tests/` | ✅ New unit tests |
| `docs/CALIBRATION.md`, `docs/CURRENT_STATE.md` | 🔧 Update |

## Dry-run reporting additions (module 2.7)

- Universe churn (entered/left daily; trade clustering in stable members vs newcomers).
- Recovery rate predicted vs realized per asset — record the gap in `docs/CALIBRATION.md`.
- Rank-band evidence (realized expectancy by volume-rank decile).
- Per-venue net expectancy side by side.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Replay diverges from live logic | Low | High — wrong ranking | Shared `compute_thresholds` + patch test observing both call sites |
| DEX public data restricted | Known | Medium | Binance proxy for Orderly data (existing pattern); empty scan preserves previous universe |
| Scanner blocks trading loop | Low | High — no trades | Dedicated background thread; never inside the cycle |
| Blacklist silently erased | Low | Medium | Carried forward by (venue, asset) on every rescan |
| Stale universe trades blindly | Medium | High | `universe_max_age_hours` blocks entries; exits continue |
