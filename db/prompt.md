# Prompt: Refactor Cross-Exchange Arbitrage Bot to Inventory-Based Simultaneous Execution with Statistical Asset Rotation

You are working inside an existing Python project. Read this entire prompt before writing any code. Implement exactly what is specified — nothing more, nothing less. Follow the minimum-modification principle: preserve existing structure, naming, persistence patterns, and configuration style wherever compatible with the requirements. Do not rewrite modules from scratch. Do not include time estimates anywhere.

---

## 1. Project Context

The project is a cross-exchange spot arbitrage system between Binance and Bitget (USDT pairs), composed of three modules plus a shared database layer:

- `arbitrage_compounding.py` — orchestrator. Runs continuous cycles: selects an asset, checks spread, executes a cycle, alternates direction, persists results to SQLite.
- `spread-llm-analizer.py` — asset scanner. Fetches all common USDT pairs from both exchanges, computes spreads from last-trade prices, applies 24h-volume and top-of-book liquidity filters, and returns the single best asset above a threshold via `get_best_spread_asset`.
- `trading_executor.py` — order execution. Exposes `buy_binance_sell_bitget` and `buy_bitget_sell_binance`, which implement a buy → on-chain withdraw/transfer → sell sequence.
- `db/db_ops.py` — SQLite layer. Central `initialize_database_tables` function creates all tables, including `arbitrage_compounding` (cycle results) and `arbitrage_cycle_steps` (step logging). Schema evolution follows an additive pattern using column checks and ALTER statements.

The project also contains, outside the scope of this refactor: a spot trading bot on Binance, a futures trading bot on Orderly (a DEX), and an existing Telegram bot used for operational control and notifications. During the analysis pass, locate the existing Telegram bot module in the codebase; do not create a new bot or a new notification framework.

The exchange accounts used by the arbitrage are shared with the spot trading bot: the free balance reported by the exchange APIs is NOT entirely available to the arbitrage.

Configuration is read from a `.env` file at project root via `python-dotenv`, with defaults in code. Logging goes to `logs/arbitrage.log`.

## 2. Why This Refactor

The current transfer-based model cannot be profitable:

1. Per-trade fixed costs (withdrawal fee buffer, transfer-risk haircut, slippage safety) force a minimum executable spread of roughly 1.6–1.8 percent, which does not occur on liquid pairs under normal conditions.
2. During the 3–5 minute on-chain transfer the position is unhedged; the spread captured at entry no longer exists at exit.
3. Spreads are computed from last-trade prices, which are not executable prices; apparent opportunities are frequently stale ticks on the less liquid venue.
4. The analyzer selects assets by instantaneous best spread, which is the wrong criterion once capital must be pre-positioned: the instantaneous spread is gone before capital arrives.

## 3. Target Model

- Capital is pre-positioned on both exchanges: USDT plus the working asset on each side.
- When an executable spread is detected, a buy order fires on one exchange and a sell order fires on the other exchange concurrently. No on-chain transfer exists in the trade path.
- One working asset is held at a time with the full allocated arbitrage capital. The asset is selected and rotated by a statistical opportunity score over a rolling observation window.
- Rotation, when it happens, is executed as exchange orders on each venue (sell the current asset on both exchanges, buy the new asset on both), never as an on-chain transfer.
- On-chain transfers exist only as occasional manual bulk rebalancing, outside this system's automation.
- The operator can start and stop the arbitrage loop remotely through the existing Telegram bot, and receives a Telegram notification for every trade.

## 4. Scope

Modify only: `arbitrage_compounding.py`, `spread-llm-analizer.py`, `trading_executor.py`, `db/db_ops.py` (additive changes through the existing initialization function only), and the existing Telegram bot module (minimal additive changes strictly limited to registering the arbitrage notifications and the start/stop commands specified in FR-12). Create: unit tests and one documentation file as specified in sections 8 and 9. Do not touch any other module, table, or setting.

---

## 5. Functional Requirements

### FR-01 — Executable price feed

Replace last-trade price sources with top-of-book bid/ask sources on both exchanges everywhere in spread detection, threshold gating, and gain estimation. The executable spread for a direction is defined exclusively as the sell-side best bid versus the buy-side best ask, expressed as a percentage of the buy-side ask. The analyzer already fetches book-ticker data for its liquidity filter; that same data source becomes the primary pricing source. Last-trade prices must not be used for any economic decision after this refactor.

### FR-02 — Inventory ledger

Add persistent inventory tracking in the database layer: free balance of USDT and of the working asset on each exchange, with a timestamp and a source indicator distinguishing values reported by the exchange API from values derived locally after an order fill. The orchestrator refreshes inventory from the exchange APIs at startup, after every completed or failed trade, and before every rotation. All balance reads used for trade gating come from this ledger, never from assumptions about prior trades. Exchange-reported balances represent the whole shared account; the portion available to the arbitrage is governed by the capital allocation defined in FR-13.

### FR-03 — Pre-trade inventory gate

Before firing any trade, verify that the buy-side exchange holds sufficient free USDT for the buy leg and the sell-side exchange holds sufficient free quantity of the working asset for the sell leg, including each exchange's minimum notional and lot-size (quantity step) constraints for the pair. The amount available to each leg is the lower of the arbitrage capital allocated to that exchange (FR-13) and the actual free balance from the inventory ledger. If either side is insufficient, skip the trade and log a distinct failure reason code. Repeated skips in the same direction must surface a rebalance-needed condition rather than retrying indefinitely.

### FR-04 — Simultaneous two-leg execution

Replace the buy → transfer → sell functions in `trading_executor.py` with a new execution flow that places the buy order on one exchange and the sell order on the other exchange concurrently — dispatched at the same time, not sequentially. The flow returns a structured result reporting, for each leg independently: order identifier, filled quantity, average fill price, fee paid, and final status.

### FR-05 — Leg failure handling and unwind

If one leg fills and the other fails (rejection, timeout, insufficient balance, API error), immediately attempt to unwind the filled leg with a market order on the same exchange, log at error severity, record the event and the realized unwind cost in the existing execution errors table, and return a failed result. The orchestrator must refresh inventory from the exchange APIs after any failure before allowing another trade. The system must never proceed to a new trade with an unreconciled partial position.

### FR-06 — Unified break-even threshold

Remove from all threshold and gain calculations: the transfer-risk haircut, the fixed withdrawal/network fee buffer as a per-trade cost, and the transfer timeout logic. The new break-even threshold consists of trading fees on both legs plus one configurable slippage safety margin plus the configurable minimum profit target. This calculation must exist in exactly one shared place and be used identically by the analyzer and the orchestrator, eliminating the current inconsistency where the analyzer proposes assets the orchestrator then rejects. Replace the hardcoded minimum profit value inside the cycle execution with the configuration variable already defined for that purpose.

### FR-07 — Statistical asset selection

Rework the analyzer so its primary output is a session asset chosen by opportunity statistics over a rolling observation window, sampling top-of-book data at a configurable interval for the candidate universe after the existing liquidity filters. Score each candidate on: the count of samples where the executable spread in either direction exceeded the break-even threshold, the average magnitude of those exceedances, the conservative top-of-book notional relative to the configured trade size, and confirmation that deposits and withdrawals are open for the asset on both exchanges. Exclude candidates failing the deposit/withdrawal check or the exchange minimum order constraints for the configured trade size, regardless of score. Persist per-sample observations so scores are reproducible and inspectable.

### FR-08 — Rotation policy with cost threshold

Rotation to a different asset is evaluated on a configurable cadence, and additionally whenever no executable opportunity has occurred for a configurable dry-spell duration. Rotation executes only when the candidate's score exceeds the current asset's score by a configurable margin sufficient to recover the estimated rotation cost, defined as the trading fees and spread crossing incurred by liquidating the current asset inventory on both exchanges and acquiring the new asset inventory on both exchanges. Rotation is executed as exchange orders on each venue. Persist every rotation decision — executed or declined — with the scores and estimated cost that produced it.

### FR-09 — Direction preference as passive rebalancing

Retain the direction-alternation concept but reinterpret it: after a completed trade, the preferred next direction is the one that restores inventory balance. Do not force alternation when the executable spread only exists in the opposite direction; direction preference is a tiebreaker and a rebalancing aid, not a gate. When inventory imbalance exceeds a configurable ratio and opposite-direction opportunities have not corrected it within a configurable period, flag a rebalance-needed condition and pause trading in the depleted direction until resolved. Automated on-chain rebalancing execution is explicitly out of scope; only the flag and pause behavior are in scope.

### FR-10 — State persistence and crash recovery

Update cycle persistence to the new model: each executed trade records both legs' exchanges, prices, quantities, and fees, the executable spread at detection and at fill, and the resulting inventory snapshot. On startup, reconcile persisted state against live exchange balances and record any discrepancy before trading. Update the cycle step logging to the new step sequence (detect, gate, execute legs, reconcile) and remove the transfer and receive steps entirely — including in the real-execution path, where the current code logs fictitious transfer steps that never occur.

### FR-11 — Simulation mode parity

Simulation mode must model the same execution path as real mode: simultaneous legs priced at the live top-of-book bid and ask with fees applied, and simulated inventory movements recorded in the same ledger. Remove the previous simulation behavior of selling at a pre-transfer price with a haircut. Flag simulated results in persistence so they are never aggregated with real results in statistics.

### FR-12 — Telegram notifications and remote start/stop

Integrate with the existing Telegram bot located during the analysis pass; do not create a new bot, token, or notification framework.

Notifications: send a Telegram message for every completed trade (asset, direction, both legs with exchange, side, price, quantity and fee, net gain, and updated arbitrage capital), every failed trade with its failure reason code, every executed rotation (old asset, new asset, estimated cost), and every rebalance-needed flag. Notification dispatch must be non-blocking with respect to trade execution: a slow or failed Telegram delivery must never delay or fail a trade, and delivery errors are logged at warning level.

Remote control: add start and stop commands for the arbitrage loop to the existing Telegram bot. Stop means: do not begin any new trade or rotation; if a trade is in flight, complete it (including any unwind handling) and then pause. Stop must never interrupt a dispatched leg. Start resumes the loop. The current run state (running or stopped) must be persisted using the existing settings pattern so it survives process restarts, and the orchestrator must check it before every new cycle. Commands must acknowledge with a confirmation message including the resulting state.

### FR-13 — Arbitrage capital allocation and compounding

Because the exchange accounts are shared with the spot trading bot, the arbitrage must never size trades from the full exchange balance. Create a capital allocation table in the database layer that records, per exchange, the USDT-denominated capital allocated to the arbitrage, with timestamps and a running history of changes. Initialize the allocation from a configuration variable on first run. After every completed trade, add the realized net gain to the allocation (compounding); after any loss or unwind cost, subtract it. All trade sizing and the FR-03 gate derive the available amount from this allocation, bounded above by the actual free balance in the inventory ledger. Rotation sizing uses the same allocation. The table creation and any migration must run through the existing `initialize_database_tables` function following the existing additive migration pattern, so it is available when the project starts.

## 6. Non-Functional Requirements

- NFR-01: Rename the analyzer file to a valid Python module name using underscores, replace the importlib file-path loading in the orchestrator with a standard import, and update all references.
- NFR-02: Log error conditions (API failures, load failures, execution failures) at error level; fallbacks, skips, rebalance-needed flags, and notification delivery failures at warning level; cycle narration at info level. Correct existing misleveled calls in the modified modules.
- NFR-03: Read every new tunable (sampling interval, observation window, rotation cadence, dry-spell duration, rotation score margin, inventory imbalance ratio, slippage safety margin, initial capital allocation) from the environment file with sensible defaults, following the existing configuration pattern. No new hardcoded economic values anywhere.

## 7. Constraints

- Minimum-modification principle throughout; do not restructure what already works.
- No scope creep: no new strategies, exchanges, asset classes, dashboards, or on-chain transfer automation. The only notification and control surface is the existing Telegram bot as specified in FR-12; do not add any other channel. Anything not stated here is out of scope.
- Hard boundary: do not modify, refactor, or touch anything related to the Binance spot trading bot or the Orderly futures trading bot — their execution paths, signals, settings values, or tables. The arbitrage interacts with them only indirectly, through the shared exchange balances handled by FR-02 and FR-13. If a change appears to require touching those systems, stop and report it instead of proceeding.
- Database changes are additive only, through the existing initialization function and the existing additive migration pattern. Existing rows in the arbitrage tables must remain readable.
- Do not modify unrelated tables, settings, or helper functions in `db/db_ops.py`.
- Do not include time estimates in any output.

## 8. Testing Requirements

Create a unit test suite that runs entirely offline with mocked HTTP responses, mocked exchange clients, and a mocked Telegram transport. No test may perform a network call. If the project has an existing test layout, follow it; otherwise create a dedicated tests directory adjacent to the modules under test. Minimum coverage:

1. Executable spread computation from mocked book-ticker payloads, both exchanges, both directions — including a case where a last-price spread exists but the executable bid/ask spread does not.
2. Break-even threshold: analyzer and orchestrator produce the identical value from the same configuration.
3. Pre-trade inventory gate: sufficient, insufficient USDT side, insufficient asset side, a lot-size violation, and a case where free balance exceeds the arbitrage allocation and the gate correctly limits sizing to the allocation.
4. Simultaneous execution success path: both mocked legs fill; structured result verified.
5. Leg failure path: one leg fills, the other fails, the unwind order is attempted, the error is recorded, a failed result is returned.
6. Rotation decision: a candidate above the margin rotates, a candidate below the margin declines, and both decisions are persisted.
7. Startup reconciliation: persisted inventory differing from mocked live balances produces a recorded discrepancy.
8. Telegram control: with state stopped, no new trade begins; a stop command received during an in-flight mocked trade lets the trade complete before pausing; start resumes; state persists across a simulated restart.
9. Telegram notifications: a completed trade dispatches a notification through the mocked transport; a failing transport logs a warning and the trade result is unaffected.
10. Capital allocation: a completed gain increases the allocation; an unwind cost decreases it; the allocation history records both changes.

All tests must pass before the work is considered complete.

## 9. Documentation Requirements

Create one Markdown document under a docs directory describing the refactored system: the execution model and why the transfer model was abandoned, each module's responsibility after the refactor, the inventory ledger and reconciliation behavior, the capital allocation model and its relationship to the shared spot-bot balances, the rotation policy and its cost threshold, the Telegram notification and start/stop behavior, every configuration variable with default and meaning, and an operational runbook covering startup, rebalance-needed handling, remote stop semantics, and interpretation of failure reason codes.

The document must end with a Change History section, initialized with a dated entry for this refactor, and structured so that every future modification to these modules appends a dated entry recording the date, the affected modules, and a concise description of what changed and why.

## 10. Deliverables and Order of Work

1. Analysis pass: read the existing modules, locate the Telegram bot module, and confirm understanding of the current flow; list any ambiguity as questions before changing code.
2. Database layer: inventory ledger, capital allocation table, and any additive schema changes.
3. Analyzer: rename, executable pricing, statistical scoring, observation persistence.
4. Executor: concurrent two-leg execution with unwind handling.
5. Orchestrator: inventory gate with allocation bounds, unified threshold, rotation policy, direction preference, crash recovery, simulation parity, run-state check.
6. Telegram integration: notifications and start/stop commands in the existing bot.
7. Unit test suite.
8. Documentation file with initialized Change History.

## 11. Acceptance Criteria

1. No code path performs or simulates an on-chain transfer as part of a trade.
2. Spread detection and gain estimation use top-of-book bid/ask exclusively.
3. A trade executes only when both legs' inventory requirements are satisfied within the arbitrage capital allocation, and both legs are dispatched concurrently.
4. A single-leg failure results in an unwind attempt, an error record, an inventory refresh, and no subsequent trade until reconciled.
5. The analyzer selects and rotates assets by persisted statistical scores, and rotation only occurs when the score margin covers the estimated rotation cost.
6. Every trade, failure, rotation, and rebalance flag produces a Telegram notification, and notification failures never affect trade execution.
7. The Telegram stop command pauses the loop without interrupting an in-flight trade, the state survives restarts, and start resumes.
8. The capital allocation compounds gains, absorbs losses, bounds all trade sizing, and its table is created through the existing initialization function at project start.
9. Nothing related to the Binance spot bot or the Orderly futures bot is modified.
10. The full unit test suite passes offline.
11. The documentation file exists, matches the implemented behavior, and contains the initialized Change History section.

Verify every acceptance criterion explicitly before declaring the task complete. If any criterion cannot be met, state which one and why instead of silently omitting it.