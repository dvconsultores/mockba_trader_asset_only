# Arbitrage System — Refactored Architecture

## Why the Transfer Model Was Abandoned

The previous arbitrage system used a buy → on-chain withdraw/transfer → sell model. This was abandoned because:

1. **Per-trade fixed costs** (withdrawal fees, transfer-risk haircut, slippage safety) forced a minimum executable spread of ~1.6–1.8%, which does not occur on liquid pairs.
2. **Unhedged window**: During the 3–5 minute on-chain transfer, the position was unhedged; the spread captured at entry no longer existed at exit.
3. **Stale pricing**: Spreads were computed from last-trade prices, which are not executable. Apparent opportunities were frequently stale ticks on the less liquid venue.
4. **Wrong selection criterion**: The analyzer selected assets by instantaneous best spread, which is the wrong criterion once capital must be pre-positioned — the instantaneous spread is gone before capital arrives.

## Execution Model (Target)

- Capital is **pre-positioned** on both exchanges (Binance and Bitget): USDT plus the working asset on each side.
- When an executable spread is detected, a **buy order and sell order fire concurrently** on their respective exchanges. No on-chain transfer exists in the trade path.
- One working asset is held at a time with the full allocated arbitrage capital.
- The asset is selected and rotated by a **statistical opportunity score** over a rolling observation window.
- Rotation is executed as exchange orders (sell old on both exchanges, buy new on both), never as an on-chain transfer.
- On-chain transfers exist only as occasional manual bulk rebalancing, outside automation.

## Module Responsibilities

### `trade/spread_llm_analyzer.py` — Asset Scanner & Scorer
- Fetches all common USDT pairs from Binance and Bitget.
- Uses **top-of-book bid/ask exclusively** for spread computation (no last-trade prices).
- Computes executable spreads in both directions.
- Runs periodic observation cycles, persisting per-sample data.
- Scores each candidate over a rolling observation window by:
  - Count of samples where executable spread exceeded the break-even threshold.
  - Average magnitude of exceedances.
  - Conservative top-of-book notional relative to trade size.
- Excludes candidates with deposits/withdrawals closed.
- Provides the **single shared `calculate_break_even_threshold()`** function used identically by the orchestrator.

### `trade/trading_executor.py` — Order Execution
- Provides `execute_simultaneous_legs()`: dispatches buy and sell orders **concurrently** on the two exchanges.
- Returns a **structured result** with both legs' details independently.
- On **partial failure**: immediately attempts to unwind the filled leg with a market order on the same exchange, records the error to `execution_errors`, and returns `None`.
- Retains all existing exchange API functions (balances, order books, market buy/sell, withdrawal helpers) for other modules.

### `trade/arbitrage_compounding.py` — Orchestrator
- Runs the continuous arbitrage loop.
- **Inventory gate (FR-03)**: Before every trade, checks that the buy exchange has sufficient USDT and the sell exchange has sufficient working asset, both bounded by the **capital allocation**.
- **Unified break-even threshold (FR-06)**: Uses `calculate_break_even_threshold()` from the analyzer.
- **Rotation policy (FR-08)**: Evaluates rotation on a configurable cadence (and on dry-spell). Rotation executes only when the candidate's score exceeds the current asset's score by a configurable margin.
- **Direction preference (FR-09)**: After a completed trade, prefers the direction that restores inventory balance. Flags rebalance-needed when inventory imbalance exceeds a configurable ratio.
- **Startup reconciliation (FR-10)**: On startup, compares persisted inventory against live exchange balances and logs discrepancies.
- **Simulation mode parity (FR-11)**: Simulation uses live top-of-book bid/ask with fees applied; simulated results are flagged in persistence.
- **Run-state check (FR-12)**: Checks persisted `arbitrage_run_state` before each cycle; stop means no new trade but in-flight trades complete.
- **Capital allocation compounding (FR-13)**: After every trade, adjusts the allocation by the realized net gain/loss.

### `db/db_ops.py` — Database Layer
- Central `initialize_database_tables()` creates all tables using the additive migration pattern.
- **New tables**: `arbitrage_inventory`, `arbitrage_capital_allocation`, `arbitrage_observations`, `arbitrage_rotation_decisions`.
- **New columns** on `arbitrage_compounding`: both-leg fields, spread at detect/fill, simulation flag, inventory snapshot.
- **Settings**: `arbitrage_run_state`, `arbitrage_session_asset`, `arbitrage_initial_capital_*`.

### `telegram.py` — Telegram Bot (existing, minimally modified)
- **Notifications** (non-blocking): completed trades, failed trades, rotations, rebalance-needed flags.
- **Remote control**: `/arb_start` and `/arb_stop` commands, plus inline buttons in the `/list` menu.
- State is persisted via `arbitrage_run_state` setting, surviving process restarts.

## Inventory Ledger & Reconciliation

The `arbitrage_inventory` table records per-exchange, per-asset free balance snapshots with:
- **`source`**: `"api"` for exchange-reported balances, `"derived"` for post-fill local estimates.
- **Timestamps** for audit trail.

The orchestrator refreshes inventory from exchange APIs:
- At startup (reconciliation).
- After every completed or failed trade.
- Before every rotation.

All balance reads for trade gating come from this ledger. Exchange-reported balances represent the **whole shared account**; the portion available to arbitrage is governed by the capital allocation.

## Capital Allocation Model

The `arbitrage_capital_allocation` table records USDT-denominated capital per exchange with a running history.

- Initialized from `ARB_INITIAL_CAPITAL_BINANCE` / `ARB_INITIAL_CAPITAL_BITGET` on first run.
- After every completed trade, the realized net gain is **added** (compounding).
- After any loss or unwind cost, it is **subtracted**.
- All trade sizing and the inventory gate derive the available amount from this allocation, **bounded above** by the actual free balance from the inventory ledger.
- This protects the shared exchange account from the arbitrage consuming capital needed by the spot trading bot.

## Rotation Policy

Rotation is evaluated:
- On a configurable **cadence** (`ARB_ROTATION_CADENCE_SEC`, default 300s).
- On a configurable **dry-spell** (`ARB_DRY_SPELL_DURATION_SEC`, default 1800s) if no executable opportunity has occurred.

Rotation executes only when the candidate's score exceeds the current asset's score by the **configurable margin** (`ARB_ROTATION_SCORE_MARGIN`, default 1.5).

The estimated rotation cost accounts for trading fees and spread crossing on both exchanges.

Every rotation decision (executed or declined) is persisted with scores and estimated cost.

## Telegram Notification & Start/Stop Behavior

**Notifications** (non-blocking, dispatched in daemon threads):
| Event | Content |
|-------|---------|
| Completed trade | Asset, direction, both legs (exchange, side, price, qty, fee), net gain, updated capital |
| Failed trade | Failure reason code |
| Rotation | Old asset, new asset, estimated cost |
| Rebalance-needed | Imbalance details |

**Remote control**:
- `/arb_start` or "▶ Start Arbitrage" button: Sets state to `running`. Loop resumes.
- `/arb_stop` or "⏸ Stop Arbitrage" button: Sets state to `stopped`. No new trade or rotation begins. If a trade is in flight, it completes (including unwind) before pausing.
- State is persisted in the `arbitrage_run_state` setting (survives restarts).

## Configuration Variables

All tunables are read from `.env` with sensible defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADE_AMOUNT` | 100 | USDT per trade (base sizing) |
| `SPREAD_MIN_PCT` | 0.5 | Legacy min spread (overridden by break-even calc) |
| `TRADING_FEE_PCT` | 0.1 | Trading fee % per leg |
| `MIN_PROFIT_USD` | 0.16 | Minimum profit target per trade |
| `SLIPPAGE_SAFETY_MARGIN_PCT` | 0.05 | Safety margin against slippage |
| `MIN_LIQUIDITY_24H_USDT` | 1,000,000 | Minimum 24h volume filter |
| `MIN_TOP_BOOK_NOTIONAL_USDT` | 2,000 | Minimum top-of-book notional filter |
| `ARB_INITIAL_CAPITAL_BINANCE` | 100 | Initial capital allocation on Binance |
| `ARB_INITIAL_CAPITAL_BITGET` | 100 | Initial capital allocation on Bitget |
| `ARB_SAMPLING_INTERVAL_SEC` | 30 | Observation sampling interval |
| `ARB_OBSERVATION_WINDOW_SEC` | 3600 | Rolling observation window for scoring |
| `ARB_ROTATION_CADENCE_SEC` | 300 | How often to evaluate rotation |
| `ARB_DRY_SPELL_DURATION_SEC` | 1800 | Max time without opportunity before rotation check |
| `ARB_ROTATION_SCORE_MARGIN` | 1.5 | Score margin required to rotate |
| `ARB_INVENTORY_IMBALANCE_RATIO` | 2.0 | Max USDT ratio between exchanges before flagging |
| `ARB_INVENTORY_IMBALANCE_PERIOD_SEC` | 3600 | Period for imbalance auto-correction |
| `ARB_CYCLE_SLEEP_SEC` | 5 | Sleep between cycles |

## Operational Runbook

### Startup
1. Ensure `.env` is configured with API keys and desired capital allocation.
2. Run `python trade/arbitrage_compounding.py`.
3. The orchestrator loads state from DB, reconciles inventory, and begins cycles.
4. The loop runs until stopped or interrupted.

### Rebalance-Needed Handling
- When inventory imbalance exceeds `ARB_INVENTORY_IMBALANCE_RATIO`, a warning is logged and a Telegram notification is sent.
- The orchestrator attempts to trade in the opposite direction to restore balance.
- If imbalance persists, trading in the depleted direction is paused.
- Automated on-chain rebalancing is **out of scope**; manual intervention may be required.

### Remote Stop Semantics
- `/arb_stop` prevents any **new** trade or rotation from starting.
- If a trade is in flight, it **completes** (including any unwind handling) before the loop pauses.
- The state is persisted — if the process is killed and restarted, it remains stopped.

### Failure Reason Codes
| Code | Meaning |
|------|---------|
| `book_fetch_failed` | Could not retrieve order book data |
| `spread_below_be` | Executable spread below break-even threshold |
| `inventory_gate` | Pre-trade inventory gate failed |
| `insufficient_usdt_<exchange>` | Not enough USDT on buy exchange |
| `insufficient_asset_<exchange>` | Not enough working asset on sell exchange |
| `zero_allocation` | Capital allocation is zero for an exchange |
| `price_fetch_failed` | Could not get reference price |
| `execution_failed` | Trade execution returned no result |
| `partial_fill_unwind` | One leg filled, other failed; unwind attempted |

---

## Change History

| Date | Affected Modules | Description |
|------|-----------------|-------------|
| 2026-07-11 | `arbitrage_compounding.py`, `spread_llm_analyzer.py` (new, renamed from `spread-llm-analizer.py`), `trading_executor.py`, `db/db_ops.py`, `telegram.py` | Major refactor: transfer-based model replaced with inventory-based simultaneous execution with statistical asset rotation. See top of this document for full details. |
