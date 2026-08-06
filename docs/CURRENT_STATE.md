# MockbaV4 — Current State Analysis

> Generated: 2026-07-26 | Phase 1, Section 1.1

---

## 1. Module Inventory

### Core Trading Path (order placement capability)

| File | Lines | Purpose |
|---|---|---|
| `trade/main.py` | 2,171 | **Orchestrator.** `ReversalScalper` class (pattern detection, ML gate, LLM gate, regime detection, manipulation detection, CEX smart-entry gates). `process_signal()` for manual triggers. `autotrade()` loop: DEX→grid scalper, CEX→grid scalper (RANGE) or reversal scalper (non-RANGE). **Can place orders.** |
| `trading_bot/spot_grid_scalper.py` | 330 | **Spot mean-reversion grid.** Dip detection + OBI → limit buy → limit sell TP. Tracks positions in module-level `_open_positions: list[dict]`. NO position persistence (restart = lost). **Can place orders** via `_limit_buy_with_fallback` + `_place_tp_sell`. |
| `trading_bot/futures_grid_scalper.py` | 326 | **DEX futures mean-reversion grid.** Dip/pump detection + OBI → bracket order (entry + TP + SL). **Has P0 bugs:** `_grid_setting` returns float but used for string `"long"` → `ValueError` at import; `qty` computed but never sent in payload; `place_futures_order()` return value discarded; `get_user_statistics()` return type mismatch (returns dict, compared to int). Positions tracked via `_open_position_count: int` (just count, no side/size). **Can place orders.** |
| `trading_bot/spot_executor_binance.py` | 566 | **Binance spot order execution.** `place_spot_order()` — market buy + GTC limit sell. `_limit_buy_with_fallback()` — tries LIMIT, falls back to MARKET after timeout. Fee-adjusted sell quantity. Exchange info fetching. |
| `trading_bot/futures_executor_apolo.py` | 611 | **Orderly DEX futures execution.** `place_futures_order()` — bracket: MARKET entry + POSITIONAL_TP_SL. Rate limiter (8 req/s). WebSocket live price. `get_user_statistics()` returns **dict** with `"positions"` list, not an int. `get_available_balance()` returns float. |
| `trading_bot/send_bot_message.py` | 66 | Telegram message sender. MarkdownV2 escaping, chunking, retry. |

### Data and DB

| File | Lines | Purpose |
|---|---|---|
| `db/db_ops.py` | 453 | **SQLite operations.** CRUD for `settings`, `signal_history`, `trades_daily`, `dex_asset_wallets`. Migrations via `_ensure_*_schema()` functions. `save_signal_to_history()`, `get_signal_history()`. **No grid position persistence functions exist.** `save_grid_position`/`load_grid_positions` referenced in ARCHITECTURE.md do NOT exist. |
| `db/migrations/` | 8 files, ~417 total | Incremental schema migrations (001–008). Add columns to existing tables. |
| `logs/log_config.py` | 52 | Custom `DateRotatingFileHandler`. Rotates at 5MB, 5 backups. Dual output: file (DEBUG) + stdout (INFO). |

### Exchange Data & Utilities

| File | Lines | Purpose |
|---|---|---|
| `trade/binance_data.py` | 162 | Binance orderbook + price fetching. `get_orderbook_binance()`, `get_binance_price()`, symbol mapping. |
| `trade/historical_data.py` | 233 | Orderly DEX OHLCV + orderbook + market trades. Rate-limited (10 req/s). |
| `trade/get_binance_trades.py` | 237 | Fetches Binance trade history via API. |
| `trade/get_trades.py` | 93 | Fetches Orderly DEX trade history. Exports to `data/all_trades.json`. |
| `trade/trading_executor.py` | 1,012 | **Standalone. Not imported by any module.** Chain/wallet management for cross-chain deposits/withdrawals on Binance + Bitget. **Does NOT place trading orders.** Dead code for the bot's purposes. |
| `trade/add_wallet_chain_des.py` | 38 | Wallet/chain mapping utility (standalone). |
| `trade/seed_chains.py` | 87 | Chain cache seeding (standalone). |
| `trade/test_data.py` | 179 | Test data utilities (standalone). |

### ML & Analysis

| File | Lines | Purpose |
|---|---|---|
| `trade/signal_agent/__init__.py` | 27 | Package init. |
| `trade/signal_agent/features.py` | 121 | Feature extraction (11 features) from signal_history for XGBoost training + live inference. |
| `trade/signal_agent/model.py` | 149 | XGBoost wrapper: train, load, save, predict, decide. Threshold-based binary classification. |
| `trade/signal_agent/labeler.py` | 667 | Background process: matches trades to signals by timestamp proximity, labels `win`/`loss`/`breakeven`. |
| `trade/signal_agent/train.py` | 242 | CLI trainer: loads labeled signals, trains XGBoost, saves model to `data/signal_model.json`. |
| `trade/signal_analyzer.py` | 421 | Post-hoc signal history analysis. Approval rates, rejection reasons, pattern distribution. CLI tool. |
| `trade/performance-llm.py` | 983 | **LLM-based trade analysis.** Regex-extracts parameters from `main.py` source code, builds prompt for DeepSeek/OpenAI, generates JSON recommendations. Offline advisory only. Has bugs: average-win calculation divides sum of all PnLs by win count (should filter positives); hardcoded timezone offset of 4; hardcoded `-$10` large-loss threshold. |

### Orchestration & UI

| File | Lines | Purpose |
|---|---|---|
| `forever.py` | 64 | **Process supervisor.** Launches `telegram.py` as subprocess. Restarts on exit. Monitors ONE process — not designed for two. |
| `telegram.py` | 584 | **Telegram bot + entry point.** Starts `autotrade()` in daemon thread. Commands: /start, /list, /trades, settings via inline keyboards. Imports `process_signal` and `autotrade` from `trade.main`. |
| `dashboard/main.py` | 716 | **FastAPI backend.** SSE log stream, signal history API, ML stats API, Mini App settings CRUD. Has its own inline DB helpers (duplicating `db_ops.py`). |
| `dashboard-ui/src/` | ~6 TSX files | React frontend. Tabs: Live, Signals, ML Monitor, Status, Settings, Assets. |

### Tests

| File | Lines | Purpose |
|---|---|---|
| `tests/test_spot_grid_scalper.py` | 1 (placeholder) | Not implemented. |
| `trade/tests/__init__.py` | 0 | Empty. |

---

## 2. The Trading Path

### Entry point
```
forever.py → telegram.py → autotrade() [daemon thread]
                          → bot.polling() [main thread, Telegram listener]
```

### DEX (Orderly) trading path
```
autotrade() in trade/main.py
  → ReversalScalper.quick_scan()          # get regime, OBI, live_price
  → if regime == "RANGE":
      → futures_grid_scalp_cycle()        # in trading_bot/futures_grid_scalper.py
        → _is_price_dip() OR _is_price_pump()  # rolling 40-sample deque
        → place_futures_order(payload)    # in trading_bot/futures_executor_apolo.py
          → Orderly POST /v1/order        # bracket: MARKET + TP + SL
        → return value DISCARDED          # BUG: no fill verification
```

### CEX (Binance) trading path
```
autotrade() in trade/main.py
  → ReversalScalper.quick_scan()          # get regime, OBI, live_price
  → if regime == "RANGE":
      → grid_scalp_cycle()                # in trading_bot/spot_grid_scalper.py
        → _is_price_dip()                 # rolling peak from 40-sample deque
        → _limit_buy_with_fallback()      # in trading_bot/spot_executor_binance.py
          → POST /api/v3/order (LIMIT, then MARKET fallback)
        → _place_tp_sell()                # GTC limit sell at entry * (1 + tp_pct)
  → if regime != "RANGE":
      → scalper.analyze_signal()          # full ReversalScalper analysis
        → ML gate (XGBoost score >= 0.80)
        → LLM gate (DeepSeek second opinion, 8s timeout)
        → CEX smart-entry gates ×6
        → place_spot_order(payload)
```

### Where position state lives
| Venue | State location | Persisted? | Restart-safe? |
|---|---|---|---|
| DEX futures | `_open_position_count: int` (module global in futures_grid_scalper.py) | No | No — counts as 0 on restart |
| CEX spot | `_open_positions: list[dict]` (module global in spot_grid_scalper.py) | No | No — empty list on restart |
| DEX actual | Orderly API `GET /v1/positions` | Exchange | Yes — but bot doesn't query it on startup |
| CEX actual | Binance open orders | Exchange | Yes — but bot doesn't reconcile on startup |

---

## 3. Database Reality

### Tables (4 actual)

| Table | Rows | Read by | Write by | Assessment |
|---|---|---|---|---|
| `settings` | 20 | All modules via `get_setting()` | `upsert_setting()`, dashboard, Telegram | **Keep.** Core config. |
| `signal_history` | 39,047 | Dashboard (API), signal_agent (train/label), signal_analyzer, trade/main.py (CEX smart-entry gates) | `save_signal_to_history()` in trade/main.py | **Remove from hot path.** 39K rows, mostly rejected signals (33,854 rejected vs 5,193 approved). Dashboard uses it for UI. Keep for `research/` analysis only. |
| `trades_daily` | 9 | `get_trades_today()` in db_ops.py, `increment_trades_today()` in spot_executor | `increment_trades_today()` | **Remove.** Only counts "positive trades" per day — no PnL, no detail. Replaced by `closed_trades`. |
| `dex_asset_wallets` | 0 | `get_dex_asset_wallet()`, `get_latest_dex_asset_wallet()` | `upsert_dex_asset_wallet()` | **Remove.** Empty table. Used by standalone `trading_executor.py` (dead code). |

### Tables ARCHITECTURE.md claimed exist but don't

ARCHITECTURE.md listed these as existing tables to drop: `arbitrage_compounding`, `arbitrage_cycle_steps`, `arbitrage_inventory`, `arbitrage_capital_allocation`, `arbitrage_observations`, `arbitrage_rotation_decisions`, `ai_recommendations`, `performance_metrics`, `strategy_parameters`, `execution_errors`, `market_regimes`.

**None of these tables exist in the current database.** They appear in `DROP TABLE IF EXISTS` statements in `db/db_ops.py:initialize_database_tables()` but have already been dropped. ARCHITECTURE.md was wrong — the DB is already clean of these. Only 4 tables actually exist.

### Settings inventory (20 keys)

```
asset                    = PERP_NEAR_USDC
auto_trade_cex           = False
auto_trade_dex           = False
capital_usage            = 50
cex_capital              = 45
current_asset            = PERP_NEAR_USDC
exchange                 = dex
grid_cooldown_sec        = 300
grid_max_positions       = 1
grid_obi_buy             = 0.96
grid_obi_sell            = 1.18
grid_position_capital    = 15
grid_price_dip_pct       = 0.2
grid_tp_pct              = 0.3
interval                 = 5m
leverage                 = 3
ml_threshold             = 0.80
risk_level               = 2.5
stop_loss                = 1
take_profit              = 0.5
```

Missing from this list vs what the rebuilder will need:
- `grid_sl_pct` — exists as code constant but NOT as DB setting (futures_grid_scalper.py reads `_grid_setting("grid_sl_pct", "0.8")` but setting was never inserted)
- `grid_direction` — exists as code constant but NOT as DB setting (same issue)
- `grid_pump_pct` — doesn't exist anywhere
- No `dex_slot_pct` / `cex_slot_pct` (new design uses equity-based sizing)
- No `dry_run` setting
- No `daily_loss_limit` setting
- No `max_consecutive_losses` setting
- No `max_hold_minutes` setting
- No `max_leverage` setting
- No `round_trip_fee_pct` setting
- No `assumed_slippage_pct` setting
- No `min_net_edge_pct` setting

---

## 4. What Trading Data Actually Exists

### Orderly DEX (`data/all_trades.json`)
- **25 trades** from 2026-06-18 to 2026-06-20 (3 days)
- 12 BUY, 13 SELL
- **Has real fill prices, quantities, and fees** — suitable for slippage calibration
- Fields: `executed_price`, `executed_quantity`, `fee`, `fee_asset`, `realized_pnl`, `side`, `symbol`

### Binance (`data/binance_trades.json`)
- **989 entries** — raw Binance trade history (likely includes ALL account trades, not just bot)
- Structure needs inspection to determine bot vs manual trades

### Accumulated (`data/accumulated_trades.json`)
- **1,450 entries** — combined trade history
- Structure needs inspection

### signal_history (SQLite)
- **39,047 rows** from 2026-04-12 to 2026-07-21 (~100 days)
- **1,370 rows have trade outcomes** (1,132 win, 234 loss, 4 breakeven)
- **2,900 rows have ML scores**
- These are SIGNALS, not trades. Outcomes were labeled post-hoc by `labeler.py` matching signals to trades by timestamp proximity.
- For calibration: the outcome-labeled rows can provide win rate per regime, but fill prices are mostly NULL.

### Calibration feasibility
- **Slippage:** `all_trades.json` has real fill prices. 25 trades over 3 days is small but better than nothing. Can measure entry slippage vs signal price by cross-referencing with signal_history timestamps.
- **Fees:** `all_trades.json` has real fees. Measurable directly: `fee / (executed_price * executed_quantity)`.
- **Win rate:** 1,370 labeled signals give statistical significance. Win rate = 1,132/1,370 = 82.6% — but this is likely biased (only signals that became trades got labeled).
- **Regime distribution:** Need historical OHLCV data (not in DB). Must fetch from exchange APIs.

---

## 5. ARCHITECTURE.md Errors and Corrections

| # | ARCHITECTURE.md claim | Reality | Impact |
|---|---|---|---|
| 1 | "15+ tables" in DB | 4 tables exist. The 11 "legacy" tables were already dropped. | Overstated cleanup scope. Migration is simpler. |
| 2 | `spot_grid_scalper.py` uses `save_grid_position`/`load_grid_positions` from `db_ops.py` | These functions **do not exist**. Positions are tracked in module-level `_open_positions: list[dict]`. | DB ops for positions must be built from scratch. |
| 3 | `_grid_setting` returns `float` — P0.1 in hardening doc | Confirmed. `GRID_DIRECTION = _grid_setting("grid_direction", "long")` → float("long") raises. | ARCHITECTURE.md is correct here. This is a real bug. |
| 4 | "No real PnL tracking" | Partially wrong. `all_trades.json` has `realized_pnl` per trade from Orderly. `signal_history` has `realized_pnl` column (populated by labeler). But the bot itself doesn't compute or use PnL in decision-making. | The data exists; the bot just doesn't use it. |
| 5 | Omitted `trading_executor.py` (1,012 lines) | Standalone module for chain/wallet management. Not imported by any trading module. Dead code. | Not part of trading path. Can be safely removed or moved to `research/`. |
| 6 | Omitted `trade/test_data.py` (179 lines) | Standalone test utility. | Low impact. |
| 7 | Omitted `trade/get_binance_trades.py` (237 lines) and `trade/get_trades.py` (93 lines) | Trade history fetchers. Not in hot path. | Data collection tools. Can stay or move to `research/`. |
| 8 | Omitted `trade/performance-llm.py` (983 lines) | LLM analysis tool. Has known bugs (average-win calculation, hardcoded timezone). | Phase 2.8 addresses this. |
| 9 | "2,171-line monster" for `trade/main.py` | Line count is correct (2,171). Assessment is accurate. | No correction needed. |
| 10 | Regime blocks all trading outside RANGE | Correct about current design. The rebuild prompt supersedes this — regime gates direction, not trading. | This is an intentional design change, not an error. |
| 11 | `pnl.close_position()` "exists but nothing calls it" | `pnl.py` **doesn't exist yet**. It's proposed in the new architecture. ARCHITECTURE.md described it as if it exists but is unused — incorrect framing. | The exit management gap is real; the fix is new code, not wiring up existing code. |
| 12 | Missing `manage_open_positions()` | This is the largest gap. Neither scalper has exit management beyond the TP order placement. Spot has `_check_open_positions()` which only detects TP fills. Futures has NO exit detection at all — fire and forget. | Confirmed. Must be built from scratch. |

---

## 6. Summary Statistics

| Metric | Value |
|---|---|
| Total Python lines (all files) | ~10,600 |
| Python lines in trading path | ~4,000 (main.py + scalpers + executors) |
| Lines that can place orders | ~5,200 (main.py + scalpers + executors + telegram.py) |
| DB tables | 4 |
| DB rows (signal_history) | 39,047 |
| Labeled trade outcomes | 1,370 |
| Actual DEX trades (all_trades.json) | 25 over 3 days |
| Trading days recorded (trades_daily) | 9 days over ~1 month |
| Settings in DB | 20 |
| Settings needed for rebuild | ~19 (see spec) |
| Modules with import-time bugs | 1 (`futures_grid_scalper.py` — `float("long")`) |
| Modules with no position persistence | 2 (both scalpers) |
| Modules with no exit management | 2 (both scalpers — spot has partial, futures has none) |

---

# Amendment 003 — Current State (Dynamic Universe & Capital View)

> Added: 2026-08-04. This section reflects the CURRENT codebase; sections above
> describe the historical Phase-1 code and are preserved for reference.

## New modules & tables

| Item | Purpose |
|---|---|
| `trade/universe.py` | Daily universe scanner (5 stages) + shared `compute_thresholds` used by both live scalpers and the replay. |
| `db/migrations/006_amendment_003.sql` | Creates `asset_universe` + `venue_state`, seeds universe/capital settings + baselines. |
| `asset_universe` table | One row per venue per asset; replaced wholesale per scan; `blacklisted` carried forward. |
| `venue_state` table | Live equity cache written by `bot.py` each cycle; read by the Capital view. |

## Where the trading universe comes from

`bot.py` iterates `get_tradeable_universe(venue)` (non-blacklisted `asset_universe`
rows) instead of the legacy `asset_configs` pairs. Exits run for universe members
AND dropped-out assets (churn never forces an exit). A stale scan
(`universe_max_age_hours`) blocks new entries but not exit management. A live
spread exceeding the scan-time spread by `universe_spread_degradation_multiple`
skips entries for that asset (no extra API call — spread comes from the OBI
snapshot).

The scanner runs in a dedicated background thread in `bot.py` — never inside the
trading cycle. It runs on startup if the stored scan is stale, then every
`universe_scan_interval_hours`. Rate-limit exhaustion aborts the scan and
preserves the previous universe (no partial write).

## Capital model

- Slot size = `{venue}_slot_pct` × **live exchange equity**, floored at
  `min_notional × 1.5`, recomputed daily (`trade/pnl.compute_slot_size`).
- Declared pools (`capital_cex_usdt` / `capital_dex_usdc`) are for display and
  validation only — sizing never reads them. Divergence beyond 25% surfaces a
  warning; the exchange wins.
- Per-venue slot limits: `max_slots_cex` / `max_slots_dex`.
- Fees are per-venue (`dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`) and
  drive all net-edge calculations, including the universe replay's minimum
  recovery rate (`universe_min_recovery_rate='auto'` → breakeven `(sl+fee)/(tp+sl)`).

## Interfaces

- **Telegram:** `/capital`, `/universe [cex|dex]`, `/blacklist add|remove <ASSET>`.
  Per-asset add/toggle/remove handlers removed; the manual signal asset picker
  now sources from the universe.
- **Dashboard API:** `GET /api/capital`, `GET /api/universe/{venue}`,
  `PUT /api/universe/{venue}/{asset}/blacklist`, and `GET /api/trades/closed`
  (read-only month view). The `/api/assets*` per-asset endpoints were replaced.
- **Mini App:** Assets tab → Capital view (`CapitalManager.tsx`) with per-venue
  panels and read-only Universe panels (blacklist toggles only). Plus the Closed
  Trades page (`ClosedTrades.tsx`) under More options.

### Closed Trades page (`GET /api/trades/closed?venue=all|dex|cex`)

Read-only month view of `closed_trades` (Amendment 004).

- **Window:** current calendar month **by close time** (`closed_at`), boundary in
  **Caracas UTC-4** (fixed −4h, matches `dashboard-ui/src/timezone.ts`), computed
  server-side in one place. The client never defines the window.
- **Totals:** per-venue `pnl_net` total + trade count for the full month, computed
  server-side in the same query pass as the rows; both cards are always shown
  (zero-filled when a venue has no trades) and are **unaffected by the venue filter**.
- **Trades:** most recent close first, capped at 200 (`truncated` flag; totals are
  uncapped). Venue filter (`all|dex|cex`) narrows the list only.
- **Reason mapping:** `tp → TP`, `sl → SL`, `time_stop → Time stop`; unknown values
  render uppercased.
- **`pnl_net` semantics (Q4):** `gross − fee_entry − fee_exit`, where fees are
  **estimated** at fixed per-side rates (spot `0.001`, futures `0.0003`); **funding
  is not included**. The page discloses this ("Net of estimated fees · funding not
  included"). Values are returned raw (no rounding); the UI formats with up to 4
  decimals.
- `asset_configs` remains in the DB as legacy data but is no longer read by the
  bot loop, Telegram, or the Capital view.

## Status

- Backend, scanner, guards, Telegram, dashboard API, Mini App Capital view, and
  unit tests implemented (`tests/test_amendment003.py`, 17 tests).
- **Not yet done:** dry-run validation under the new universe (48h), the
  predicted-vs-realized recovery-rate gap, rank-band decile evidence, and
  measured CEX fee rate — see `docs/CALIBRATION.md`.
