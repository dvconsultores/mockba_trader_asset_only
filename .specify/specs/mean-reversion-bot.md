# Feature Specification: MockbaV4 Mean-Reversion Trading Bot

**Feature Branch**: `mockbav4-rebuild`

**Created**: 2026-07-26

**Status**: Draft (Phase 1 — GATE 1)

**Input**: Rebuild of crypto mean-reversion bot from ~10,600 lines to ~1,500 lines. Single strategy: buy dips, sell rips. Multi-asset, multi-venue (Binance spot + Orderly DEX futures). Starting capital $50, same math for $15,000.

## User Scenarios & Testing

### User Story 1 — Bot enters a mean-reversion trade (Priority: P1)

The bot monitors price and order book for a configured asset. When price dips below a rolling peak by `dip_pct` AND the order book is bullish (OBI < `obi_buy_threshold`), the bot opens a long/buy position with a take-profit and (for futures) a stop-loss bracket. When price pumps above a rolling trough AND OBI is bearish, it opens a short/sell position with the same bracket structure.

**Why this priority**: This is the core value proposition. Without entries, nothing else matters.

**Independent Test**: Run the bot with `dry_run=true` against live market data. Verify that dip+OBI conditions produce a simulated entry; verify that dip-without-OBI produces none; verify that cooldown and spacing are enforced.

**Acceptance Scenarios**:

1. **Given** RANGE regime, price dipped 0.5% below rolling peak, OBI=0.92, cooldown expired, **When** the cycle runs, **Then** a long entry is opened with TP at fill × (1 + tp_pct) and SL at fill × (1 − sl_pct).
2. **Given** RANGE regime, price dipped 0.5%, OBI=1.05 (neutral), **When** the cycle runs, **Then** NO entry is opened (AND condition not met).
3. **Given** TREND_UP regime, price dipped 0.5%, OBI=0.92, **When** the cycle runs, **Then** a long entry IS opened (trend-aligned dip buying permitted).
4. **Given** TREND_DOWN regime, price dipped 0.5%, OBI=0.92, **When** the cycle runs, **Then** NO long entry is opened (counter-trend blocked).
5. **Given** an open position at $10.00 with entry spacing 0.6%, **When** price is at $10.03 and dip conditions are met, **Then** NO entry is opened (within spacing of existing position).

---

### User Story 2 — Bot manages and exits open positions (Priority: P1)

The bot monitors open positions on every cycle and closes them when any exit condition is met: take-profit fill, stop-loss fill, time stop expiry, or adverse regime change.

**Why this priority**: Without exit management, the bot accumulates positions indefinitely. On DEX futures, an unmonitored position is a liquidation risk.

**Independent Test**: Create a simulated open position and run cycles. Verify TP fill detection, time stop triggers after `max_hold_minutes`, and regime-change exit moves TP to breakeven.

**Acceptance Scenarios**:

1. **Given** an open long with TP limit order at $10.08, **When** the exchange reports the TP order FILLED, **Then** the position is recorded as closed with exit_reason="tp" and a `closed_trades` row is written with real fill prices.
2. **Given** an open DEX long with SL at $9.95, **When** the exchange reports the position closed at $9.95, **Then** exit_reason="sl" is recorded.
3. **Given** a position open for longer than `max_hold_minutes`, **When** the cycle runs, **Then** the position is market-closed with exit_reason="time_stop".
4. **Given** an open long and regime changes from RANGE to TREND_DOWN, **When** the cycle runs, **Then** the TP is moved to breakeven-plus-fees; on the next cycle with confirmed TREND_DOWN, the position is market-closed.

---

### User Story 3 — PnL is tracked and kill switches protect capital (Priority: P1)

Every closed trade records its net PnL (fill prices, real fees). Daily PnL is summed. If `daily_loss_limit` or `max_consecutive_losses` is breached, new entries are disabled but existing positions exit normally. Both switches require manual re-enable.

**Why this priority**: Without PnL tracking, the bot is blind. Without kill switches, a bad regime can drain the account in one session.

**Independent Test**: Feed a sequence of trades through `pnl.py`. Verify daily PnL aggregation, kill switch trigger at exactly the limit, and that the switch blocks new entries but not exits.

**Acceptance Scenarios**:

1. **Given** `daily_loss_limit = 10` and cumulative daily PnL = −$9.50, **When** a new trade closes with −$0.60 loss, **Then** new entries are disabled and a Telegram notification is sent.
2. **Given** kill switch active, **When** `manage_open_positions()` runs, **Then** existing position exits still execute normally.
3. **Given** `max_consecutive_losses = 4` and 4 consecutive losses, **When** the 4th loss is recorded, **Then** new entries are disabled.

---

### User Story 4 — Bot detects market regime from OHLCV data (Priority: P2)

Using 1h and 4h OHLCV candles, the bot classifies the market as RANGE, TREND_UP, or TREND_DOWN. The regime gates which entry directions are permitted. Regime is cached per asset for `regime_cache_sec` to avoid excessive API calls.

**Why this priority**: Regime detection is necessary for the direction-gating matrix, but the bot can function in RANGE-only mode during early testing.

**Independent Test**: Feed synthetic OHLCV data with known slopes. Verify classification matches expectation. Verify a strong-slope low-volume series classifies as a trend, not RANGE.

**Acceptance Scenarios**:

1. **Given** 1h slope = +0.15%/candle, 4h slope = +0.10%/candle, volume = 130% of average, **When** regime is computed, **Then** result is TREND_UP.
2. **Given** 1h slope = +0.15%/candle, volume = 90% of average (weak), **When** regime is computed, **Then** result is TREND_UP (slope alone determines trend; volume grades strength).
3. **Given** both 1h and 4h slopes within ±0.12%, **When** regime is computed, **Then** result is RANGE.
4. **Given** cached regime less than `regime_cache_sec` old, **When** regime is requested, **Then** the cached value is returned without an API call.

---

### User Story 5 — Bot supports any asset on any venue (Priority: P2)

The bot reads an `assets` setting (comma-separated, e.g., "NEAR,ETH,SOL") and trades all of them. Symbol derivation is mechanical. Exchange info (tick size, lot size, min notional) is fetched once per asset per session and cached.

**Why this priority**: Multi-asset is a core requirement from the rebuild prompt, but single-asset operation must work first.

**Independent Test**: Configure `assets = "NEAR,ETH"`. Run against both exchanges. Verify NEAR and ETH symbols are derived correctly and exchange info is fetched and cached independently.

**Acceptance Scenarios**:

1. **Given** `assets = "NEAR"`, **When** the bot starts, **Then** it derives `PERP_NEAR_USDC` for Orderly and `NEARUSDT` for Binance.
2. **Given** an invalid asset "ZZZ", **When** exchange info is fetched, **Then** the asset is skipped with a logged warning and the bot continues with remaining assets.
3. **Given** an asset with min_notional = $5 and slot_size = $4, **When** entry is evaluated, **Then** the asset is skipped with reason "slot_size below min_notional floor".

---

### User Story 6 — Position sizing compounds automatically (Priority: P3)

Position size is a percentage of venue equity (`dex_slot_pct`, `cex_slot_pct`), not a fixed dollar amount. Equity is read from the exchange every cycle. Slot size is recomputed once daily. Realized PnL compounds; unrealized PnL does not affect sizing.

**Why this priority**: Compounding is the mechanism for scaling from $50 to $15,000, but fixed-size trading works for initial validation.

**Independent Test**: Simulate equity growth over multiple days. Verify slot size increases after profitable days and that the daily recompute prevents per-trade volatility in sizing.

**Acceptance Scenarios**:

1. **Given** DEX equity = $100 and `dex_slot_pct = 15%`, **When** slot size is computed, **Then** slot_size = $15.
2. **Given** equity drops to $8 after a loss, **When** slot size is computed, **Then** slot_size is floored at min_notional × 1.5 and the asset is skipped if equity is insufficient.
3. **Given** a winning day closes with +$5 realized PnL, **When** the next day's slot size is computed, **Then** equity has grown by $5.

---

### User Story 7 — Bot survives restart without state loss (Priority: P3)

On startup, the bot reconciles its local `open_positions` table with the exchange's actual positions. Orphaned DB records are cleaned up. Missing stops are re-attached. PnL history is preserved.

**Why this priority**: Restart safety is constitutionally required but is a robustness feature, not a core trading feature.

**Independent Test**: Simulate a kill -9 while a position is open. Restart. Verify the position is detected and adopted, and no duplicate is opened.

**Acceptance Scenarios**:

1. **Given** an open position on the exchange with no local DB record, **When** the bot starts, **Then** the position is adopted into `open_positions` with its exchange-side state.
2. **Given** a local DB record for a position that no longer exists on the exchange, **When** the bot starts, **Then** the record is deleted.
3. **Given** an open DEX position with a filled entry but no active stop order, **When** reconciliation runs, **Then** a stop is placed at the expected SL price.

---

### User Story 8 — Dry-run mode simulates without real orders (Priority: P2)

When `dry_run = true`, the bot executes all logic (regime detection, entry evaluation, exit management, PnL tracking) but sends no orders to any exchange. Simulated fills use the signal price as fill price with an assumed slippage applied.

**Why this priority**: Required for safe validation before live trading. Constitutionally, `dry_run` defaults to true.

**Independent Test**: Set `dry_run=true`. Run for 24 hours. Verify the signals table records entries, `closed_trades` records simulated exits, and no exchange API endpoints for order placement are called.

**Acceptance Scenarios**:

1. **Given** `dry_run = true` and entry conditions met, **When** the cycle runs, **Then** the signal is logged to the `signals` table with action="entered" (simulated) and no order is placed.
2. **Given** `dry_run = true` and a simulated position reaches TP, **When** manage_open_positions runs, **Then** a `closed_trades` row is written with the simulated fill price and exit_reason="tp".

---

## Functional Requirements

### FR-1: Entry Logic

- **FR-1.1**: Entry requires BOTH a price extreme AND OBI confirmation (AND, never OR).
- **FR-1.2**: Long/buy: `dip_pct >= dip_threshold AND obi < obi_buy_threshold`.
- **FR-1.3**: Short/sell: `pump_pct >= pump_threshold AND obi > obi_sell_threshold`.
- **FR-1.4**: Price extremes measure against a rolling window (40 samples, 10-sample warm-up).
- **FR-1.5**: Cooldown applied per asset per venue between entries (`cooldown_sec`, default 300).
- **FR-1.6**: Minimum price spacing between concurrent positions in the same asset (`min_entry_spacing_pct`, default 0.6%).
- **FR-1.7**: Maximum concurrent positions capped by `max_slots`.

### FR-2: Regime Gating

- **FR-2.1**: RANGE: all directions permitted (long + short for DEX, buy for spot).
- **FR-2.2**: TREND_UP: long/buy only; short/sell blocked.
- **FR-2.3**: TREND_DOWN: short only for DEX; all entries blocked for spot.
- **FR-2.4**: Regime detection uses linear regression slope on 1h + 4h candles.
- **FR-2.5**: Slope alone determines trend; volume grades strength, never overrides classification.
- **FR-2.6**: Regime cached per asset for `regime_cache_sec` (default 300).

### FR-3: Exit Management

- **FR-3.1**: `manage_open_positions()` runs before entry logic on every cycle.
- **FR-3.2**: TP fill detection: query the resting order; on fill, record exit.
- **FR-3.3**: SL fill detection (futures only): detect position closure at SL.
- **FR-3.4**: Time stop: position open > `max_hold_minutes` → market close.
- **FR-3.5**: Regime exit: adverse regime change → TP to breakeven; confirmed adverse trend → market close.
- **FR-3.6**: Every exit writes `closed_trades` row before position record is deleted.

### FR-4: Position Sizing

- **FR-4.1**: `slot_size = slot_pct × equity` (per venue, independent).
- **FR-4.2**: Equity read from exchange every cycle.
- **FR-4.3**: Floor: `max(slot_size, min_notional × 1.5)`.
- **FR-4.4**: Slot size recomputed once daily.
- **FR-4.5**: Realized PnL only; unrealized never affects sizing.
- **FR-4.6**: DEX compounds `dex_compound_pct` of realized PnL (default 100%).

### FR-5: Risk Controls

- **FR-5.1**: `tp_pct > sl_pct` — startup gate, refuses to trade.
- **FR-5.2**: Net edge: `tp_pct - round_trip_fee_pct - assumed_slippage_pct >= min_net_edge_pct`.
- **FR-5.3**: Futures: SL verified present after entry fill; emergency close if not.
- **FR-5.4**: Spot: no stop-loss; time stop is the last-resort exit.
- **FR-5.5**: Leverage capped by `max_leverage` (default 3).
- **FR-5.6**: Kill switches: `daily_loss_limit`, `max_consecutive_losses`. Block entries, not exits. Require manual re-enable.

### FR-6: PnL Tracking

- **FR-6.1**: `closed_trades` table: asset, venue, side, entry_fill_price, exit_fill_price, qty, entry_fee, exit_fee, net_pnl, pnl_pct, opened_at, closed_at, exit_reason, signal_price.
- **FR-6.2**: `signals` table: timestamp, asset, venue, regime, obi, dip_pct/pump_pct, action (entered|skipped), reason.
- **FR-6.3**: PnL computed from actual fill prices and actual fees only.

### FR-7: Operational

- **FR-7.1**: `dry_run` defaults to true; honored on every order path.
- **FR-7.2**: Settings read fresh each cycle; changes take effect without restart.
- **FR-7.3**: Startup validations re-run on setting change; halt if new config fails.
- **FR-7.4**: Structured single-line logs: `[LEVEL] key=value ...`.

## Key Entities

- **Position**: An open trade. Fields: id, asset, venue, side, entry_fill_price, qty, sl_price, tp_price, opened_at, exchange_order_id, tp_order_id, sl_order_id.
- **ClosedTrade**: A completed trade. Fields: all Position fields plus exit_fill_price, entry_fee, exit_fee, net_pnl, pnl_pct, closed_at, exit_reason, signal_price_at_entry.
- **Signal**: A decision record. Fields: timestamp, asset, venue, regime, obi, extreme_pct, action, reason. Captures both entries and skips.
- **ExchangeInfo**: Cached per-asset exchange metadata. Fields: symbol, base_tick, quote_tick, min_qty, min_notional, lot_step.
- **Regime**: Market classification. Values: RANGE, TREND_UP, TREND_DOWN. Cached per asset with expiry.

## Success Criteria

1. Bot runs 48-hour dry-run with ≥1 trade per day per asset (constitution principle VIII).
2. Win rate after fees ≥ breakeven rate computed at startup.
3. Kill switch triggers within 1 cycle of limit breach.
4. Restart during open position does not duplicate or orphan.
5. All 19 settings changeable via UI/Telegram and take effect within one cycle.
