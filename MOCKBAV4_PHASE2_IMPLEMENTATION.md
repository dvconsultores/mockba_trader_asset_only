# MockbaV4 — Phase 2: Implementation

Paste this after GATE 1 is approved. Do not start Phase 2 until the specs, `docs/CURRENT_STATE.md`, and `docs/CALIBRATION.md` from Phase 1 exist and I have signed off on them.

`dry_run` stays `true` for all of Phase 2 up to GATE 2.

---

## Working method

One module per branch, in the order below. Do not start a module until the previous one's acceptance criteria all pass.

For each module:

1. `git checkout -b build/<module>` from main.
2. Re-read the Phase 1 spec and plan for that module. Where this document and the Phase 1 spec disagree, **stop and ask** — do not silently pick one.
3. Implement. Stay inside the line budget; if you exceed it by more than 25%, stop and explain what the spec underestimated.
4. Write the tests listed under that module. Run them.
5. Report acceptance criteria pass/fail **by number**.
6. Commit, update `PROGRESS.md`, continue.

Every module is written against mocked exchange responses in tests. No test touches the network or a real account.

---

## Interface contracts (define these first, in `trading_bot/types.py`)

Every module below depends on these. Get them agreed before writing anything else.

```python
@dataclass(frozen=True)
class SymbolFilters:
    symbol: str            # venue-native, e.g. "NEARUSDT" or "PERP_NEAR_USDC"
    base_tick: float       # quantity step
    quote_tick: float      # price tick
    min_qty: float
    min_notional: float

@dataclass(frozen=True)
class Fill:
    filled_qty: float      # actual, from the exchange
    fill_price: float      # actual average, never the signal price
    fee_amount: float      # actual
    fee_asset: str
    sellable_qty: float    # filled_qty minus base-asset fee, floored to base_tick
    order_id: str
    client_order_id: str
    raw: dict              # full exchange response, for logging

@dataclass(frozen=True)
class Position:
    id: str
    asset: str
    venue: str             # "binance" | "orderly"
    side: str              # "long" | "short"
    qty: float
    entry_price: float     # actual fill
    signal_price: float    # what triggered it — slippage = entry - signal
    tp_price: float
    sl_price: float | None # None for spot
    tp_order_id: str | None
    sl_order_id: str | None
    opened_at: float
```

`Fill.fill_price` and `Fill.fee_amount` come from the exchange response. Any code path that substitutes the signal price or an assumed fee rate is a constitution violation.

---

## 2.1 — `db/schema_v2.sql` + `db/db_ops.py` (~150 lines)

Four tables: `settings`, `open_positions`, `closed_trades`, `signals`.

`closed_trades` records: id, asset, venue, side, entry_price, exit_price, signal_price, qty, fee_entry, fee_exit, pnl_net, pnl_pct, opened_at, closed_at, exit_reason.

`signals` records: timestamp, asset, venue, regime, obi, extreme_pct, action (`entered` | `skipped`), reason.

Write a migration script that creates the new schema alongside the old one and preserves any existing closed-trade history worth keeping. Do not drop old tables in this module — that happens in 2.9, after live trading is stable.

**Acceptance criteria**

1. Schema creates cleanly on an empty database and is idempotent on re-run.
2. Migration runs against a copy of the production database without data loss, and is reversible.
3. Settings round-trip with correct types (float, int, bool, string) — a stored `"false"` reads back as `False`, not a truthy string.
4. `open_positions` enforces one row per (asset, venue, position id) and supports concurrent read/write from the bot loop and the dashboard.
5. Every write is parameterized. No string-interpolated SQL anywhere.

---

## 2.2 — `trade/pnl.py` (~120 lines)

Trade recording, daily PnL, kill switches, and equity-based position sizing.

**Sizing** (per venue, independent):

```
equity        = read from exchange this cycle, never from the DB
raw_slot      = equity × (slot_pct / 100)
slot_size     = max(raw_slot, min_notional × 1.5)
```

Recompute the slot size **once per UTC day**, cached, not per trade. If `slot_size > equity / max_slots`, reduce `max_slots` for the day and log it. If equity cannot fund a single slot at the floor, that venue is skipped entirely with a logged reason.

Compounding uses realized PnL only. `dex_compound_pct` (default 100) scales how much realized profit feeds back into equity-based sizing.

**Kill switches:** `daily_loss_limit` and `max_consecutive_losses`. Both block new entries, leave open positions to their normal exits, and require manual re-enable. Daily counters reset at UTC midnight.

**Acceptance criteria**

1. PnL arithmetic matches hand-computed fixtures for long and short, on both venues, with fees on both sides.
2. `pnl_pct` is relative to capital deployed, and is identical for the same percentage move at $15 and $15,000 position size.
3. The floor applies: at $50 equity and 10% slot percentage, slot size is the min-notional floor, not $5.
4. Insufficient equity for one slot skips the venue and logs; it does not send an order.
5. Slot size is stable within a UTC day even after several closed trades, and updates at the day boundary.
6. `daily_loss_limit` fires at exactly the limit, blocks entries, and does not close existing positions.
7. `max_consecutive_losses` counts consecutively and resets on any win.
8. Daily counters reset at UTC midnight, verified across a boundary.

---

## 2.3 — `trade/regime.py` (~150 lines)

Returns `RANGE` | `TREND_UP` | `TREND_DOWN` from 1h and 4h candles. Thresholds come from `docs/CALIBRATION.md`, not from `ARCHITECTURE.md`'s guessed `0.0012`.

Volume grades trend strength; it does not decide whether a trend exists. A strong slope with weak volume is still a trend.

Cache per asset for `regime_cache_sec` (default 300).

**Acceptance criteria**

1. Synthetic OHLCV fixtures classify correctly for each of the three regimes.
2. A strong-slope, low-volume series classifies as a trend, **not** RANGE.
3. 1h and 4h disagreeing resolves per the Phase 1 spec, and the resolution is logged.
4. Cache returns the same value within the TTL and recomputes after it, verified by counting fetch calls.
5. Insufficient candle history returns a distinct "unknown" state that blocks entries rather than defaulting to RANGE.

---

## 2.4 — `trading_bot/executor.py` (~400 lines)

The integration point. One interface over two venues, returning `Fill` on every path.

**Binance spot:**
- Buy, then place the resting limit TP sell.
- Fee is taken in the base asset unless paying in BNB — compute `sellable_qty` as `filled_qty` minus base-asset commission, floored to `base_tick`. Selling `filled_qty` will be rejected.
- If `fills` is absent from the response, fall back to querying the free base-asset balance.
- If `sellable_qty` is below `min_qty` or its notional is below `min_notional`, the position cannot be exited by limit order: log ERROR, notify, mark the position `needs_manual_review`. Never silently drop it.

**Orderly futures:**
- Place the bracket (entry + TP + SL), then **verify the stop exists** by querying live conditional orders.
- Entry filled with no stop present → place a standalone stop. That failing → market-close immediately, same cycle, and alert.
- Compute TP and SL from the actual fill price, not the signal price. Round the stop *away* from entry and the TP *toward* entry, so rounding never widens risk.

**Both:**
- Cache `SymbolFilters` per asset per session.
- Format every quantity and price with the symbol's precision. Never interpolate a raw float.
- Client order IDs derived from position IDs, so retries are idempotent.
- `dry_run` short-circuits every order path: log the exact payload that would be sent, return a simulated `Fill` at the current price, send nothing.

**Acceptance criteria**

1. Every quantity and price string sent matches the symbol's tick and step precision — assert on exact request parameters.
2. A buy response showing base-asset commission produces `sellable_qty` strictly less than `filled_qty`, floored to step.
3. A buy response with no `fills` key falls back to the balance query.
4. `sellable_qty` below the minimum marks the position `needs_manual_review` rather than dropping it.
5. A bracket order whose stop verification fails triggers a standalone stop, and if that fails, an emergency market close within the same call.
6. TP and SL derive from `Fill.fill_price`, not from the signal price passed in.
7. Stop rounding never reduces the distance from entry; TP rounding never reduces net edge below the minimum.
8. `dry_run = true` issues zero network calls and returns a well-formed `Fill`.
9. A rejected order returns a distinguishable failure, not an exception that aborts the cycle.
10. The same client order ID submitted twice does not create two positions.

---

## 2.5 — `spot_scalper.py` (~250) and `futures_scalper.py` (~300)

Each exposes two functions. **`manage_open_positions()` runs first, every cycle, before any entry evaluation.**

**`manage_open_positions(asset)`** handles:
- TP fill detection → close, record `closed_trades`, delete `open_positions` row.
- SL fill detection (futures).
- Time stop: open longer than `max_hold_minutes` → cancel exits (confirm the cancel succeeded), market close, reason `time_stop`.
- Regime exit: regime turned against the position → move TP to breakeven-plus-fees; on a confirmed adverse trend, market close.
- A position whose TP order has vanished (canceled, expired) → re-place it.

**`scalp_cycle(asset, regime, obi, live_price)`** entry logic:
- Both conditions required: extreme **and** OBI. Never OR.
- Direction permitted by regime:

| Regime | Spot | Futures |
|---|---|---|
| RANGE | buy dips | long dips, short pumps |
| TREND_UP | buy dips | long dips only |
| TREND_DOWN | no entry | short pumps only |

- Cooldown per asset per venue; minimum price spacing from every open position in that asset; slot limit; kill switch check.
- Every evaluation writes a `signals` row — `entered` with the trigger, or `skipped` with the specific blocking reason.

Spot has no stop-loss by design; the time stop is its exit of last resort. Futures stops are mandatory.

**Acceptance criteria**

1. A dip without OBI confirmation produces no entry, and writes a `skipped` signal naming OBI.
2. Each regime permits only its allowed directions — a short in TREND_UP opens nothing.
3. `manage_open_positions` is called before entry evaluation on every cycle, verified by call ordering.
4. A position past `max_hold_minutes` is market-closed, with the exit-order cancel confirmed before the close is sent.
5. Cooldown and minimum spacing are both enforced independently.
6. A TP fill writes exactly one `closed_trades` row and removes the `open_positions` row atomically.
7. A vanished TP order results in a replacement, not a stuck position.
8. Every cycle writes exactly one `signals` row per asset per venue.
9. A tripped kill switch blocks entries but does not block `manage_open_positions`.
10. Restart with an open position and a live exchange order results in one tracked position and zero new orders.

---

## 2.6 — `bot.py` (~200 lines)

The loop. For each asset: refresh settings, detect regime, `manage_open_positions()`, then evaluate entries.

Startup validation gate, run before the first cycle and re-run whenever a setting changes:
- `tp_pct > sl_pct`, else refuse and log the implied breakeven win rate.
- `tp_pct - fee_round_trip - assumed_slippage >= min_net_edge_pct`, else refuse.
- Leverage within `max_leverage`; stop distance well inside liquidation distance.
- Every configured asset resolves to valid symbol filters on its venue — skip and log those that don't rather than aborting.

Settings read fresh each cycle so Telegram and UI changes apply without restart. Log a diff line when a value changes.

Structured single-line logs: entries, exits, realized PnL, errors, kill switch, startup. No emoji. No per-cycle "waiting for dip" noise.

Resolve the supervision question from Phase 1 — `bot.py` and the Telegram listener both need to run.

**Acceptance criteria**

1. Startup refuses to trade when `tp_pct <= sl_pct`, logging the breakeven win rate.
2. Startup refuses when net edge is below minimum.
3. An invalid asset is skipped with a log line; valid assets still trade.
4. `manage_open_positions` runs before entries in every cycle, for every venue.
5. Changing `tp_pct` in the database takes effect on the next cycle without a restart, and re-runs validation.
6. A setting change that fails validation halts new entries and logs why.
7. An exception in one asset's cycle does not stop the loop for other assets.
8. Log output contains no per-cycle noise lines during a period with no signals.

---

## 2.7 — Dry-run validation

Run 48 hours against live market data with `dry_run = true`, all configured assets, both venues.

Report:
- Trades per day, per asset, per venue. **If this is near zero, stop** — the thresholds are too tight and no amount of correct code fixes that. Return to the calibration study.
- Win rate against the computed breakeven win rate.
- Average win, average loss, largest loss.
- Net PnL after fees, and after fees plus measured slippage.
- **Measured slippage** — `fill_price` vs `signal_price` across all entries. This replaces the assumed value.
- Maximum and median time in position.
- Exit reason distribution (tp / sl / time_stop / regime).
- Observed regime distribution vs what the calibration study predicted.

## GATE 2 — stop here

Go-live is my decision, made against those numbers. Do not set `dry_run = false`.

---

## 2.8 — `research/performance_llm.py` (rewrite)

Offline and advisory. Never writes settings, never places orders, never runs inside the trading loop.

Changes from the existing version:

- **Delete the parameter-extraction layer entirely** — `_extract_parameters_from_main`, `_resolve_parameter_value`, the regex matching, the `eval()` call, and `_read_full_main_source`. All parameters now live in the `settings` table. Read them. This removes roughly 200 lines and the injection risk with it.
- **Fix the average-win bug.** `round(sum(pnls) / positives, 4)` divides the sum of *all* PnLs by the win count. Filter to positive values, the way `avg_loss_pnl` already does. This number feeds the fee-drag calculation, so it is currently wrong in both places it appears.
- **Recommendations target settings keys** — "set `tp_pct` to 0.8", not "change `ReversalScalper.SLOPE_THRESHOLD_1H`". Output a list of `{setting, current_value, suggested_value, reason, expected_impact}`.
- **`is_large_loss` becomes relative to venue equity**, not a hardcoded `-$10`.
- **Read the `signals` table**, not just closed trades, so the analyzer can assess whether filters were too tight — report skipped-signal counts by reason alongside taken-trade outcomes.
- **Include measured slippage** from `signal_price` vs `entry_price` in `closed_trades`.
- Drop `_is_preferred_window` unless a `trading_hours` setting exists.
- Timezone offset from a setting, not a hardcoded 4.

**Acceptance criteria**

1. No file in `research/` is imported by anything under `trading_bot/` or `trade/`.
2. Running it makes no writes to `settings`, `open_positions`, or `closed_trades`.
3. `avg_win_pnl` matches a hand-computed fixture containing both wins and losses.
4. Every recommendation names an existing settings key.
5. Skipped-signal counts by reason appear in the report.
6. A malformed LLM response is handled without crashing, and the raw response is preserved.

---

## 2.9 — Cleanup (only after live trading is stable)

Tag the current state as `legacy-v3` before deleting anything.

Then: remove `trade/main.py`; move `signal_agent/`, `signal_analyzer.py`, and the old `performance-llm.py` to `research/`; drop unused tables per the verified list from `docs/CURRENT_STATE.md` (not the unverified list in `ARCHITECTURE.md`); update the dashboard to Positions and History tabs; update Telegram commands to the new settings; archive the old prompt files to `docs/archive/`.

---

## Guardrails

Never, without asking:
- Run any module that can place a real order.
- Set `dry_run` to `false`.
- Read credential values into output, or modify `.env`.
- Change a live settings value in the database.
- `git push`, `git reset --hard`, or delete uncommitted work.
- Drop a database table.

Stop and ask when: this document conflicts with the Phase 1 spec, a module exceeds its line budget by more than 25%, an acceptance criterion cannot be satisfied as written, or the dry-run shows near-zero trade frequency.
