# Implementation Plan: MockbaV4 Mean-Reversion Bot

**Plan Version**: 1.1.0 | **Created**: 2026-07-26 | **Status**: ✅ GATE 1 approved — Phase 2

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
