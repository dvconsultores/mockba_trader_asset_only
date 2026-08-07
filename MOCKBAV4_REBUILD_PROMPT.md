# MockbaV4 Rebuild — Phase 1 (Specification) and Phase 2 (Implementation)

Paste **Phase 1 only** to start. Phase 2 begins after the specs are reviewed and approved at GATE 1.

---

# PHASE 1 — Specification

You are planning a rebuild of this crypto trading bot. `ARCHITECTURE.md` at the repo root describes the target. Your job in this phase is to turn it into reviewable specifications using GitHub Spec Kit. **Write no implementation code in Phase 1.**

## 1.0 — Install Spec Kit

Fetch the current README at https://github.com/github/spec-kit and follow its installation instructions — the CLI syntax changes, so use what the repo says rather than what you remember. As of early 2026 the flow is roughly:

```bash
uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
specify init --here --ai copilot
```

This installs slash commands (`/constitution`, `/specify`, `/clarify`, `/plan`, `/tasks`, `/analyze`, `/implement`). Verify which exist in the version you installed and report them before continuing.

## 1.1 — Analyze what exists (do this before writing any spec)

Produce `docs/CURRENT_STATE.md` containing:

1. **Module inventory** — every Python file, line count, and one line on what it does.
2. **The trading path** — which modules can place an order, what calls them, and where position state currently lives.
3. **Database reality** — every table, row count, and whether anything reads it. `ARCHITECTURE.md` lists tables to drop; verify each is genuinely unused before it goes on the list. Report any disagreement with the document.
4. **What data you actually have** — how many closed trades exist, over what date range, on which venues. This determines whether the calibration studies in 1.5 are possible.
5. **Anything in `ARCHITECTURE.md` that is wrong about the current code.** The document was written from memory; check it.

## 1.2 — Constitution (`/constitution`)

Encode these as project principles. They override any later spec, plan, or task that conflicts.

1. **One strategy.** Mean reversion: buy dips, sell rips. No pattern detection, no ML in the execution path, no LLM in the execution path. If a proposed feature does not directly serve "is price at an extreme and likely to revert," it does not go in the bot.
2. **Reward must exceed risk.** `tp_pct > sl_pct` always. The bot refuses to start otherwise.
3. **No leveraged position without a confirmed stop.** A filled futures entry whose stop cannot be verified is closed immediately, in the same cycle.
4. **Unknown state means no trading.** If position count, equity, or fill status cannot be determined, open nothing. Fail closed, never open.
5. **Real fills only.** Every PnL number derives from the exchange's actual fill price and actual fee. Never from the signal price, never from an assumed fee rate.
6. **Restart safety.** Killing the process and restarting must not duplicate positions, orphan orders, or lose PnL history.
7. **Simplicity is a constraint, not a preference.** Target ≤1,500 lines total. Any module exceeding its budget in the plan requires justification.
8. **The bot trades.** Filters exist to avoid bad trades, not to avoid trading. A configuration producing near-zero trade frequency is a bug to investigate, not a safe default.

## 1.3 — Business rules to specify (`/specify`)

These are decisions, not suggestions. Encode them precisely; ask about anything ambiguous rather than choosing for me.

### Entry

- Entry requires **both** a price extreme **and** OBI confirmation. Never OR. Long: `dip >= dip_pct AND obi < obi_buy_threshold`. Short: `pump >= pump_pct AND obi > obi_sell_threshold`.
- Price extremes measure against a rolling window peak (dips) and trough (pumps). The window must warm up before signals are valid.
- A cooldown applies per asset per venue between entries.
- A minimum price spacing applies between concurrent positions in the same asset, so multiple slots are a grid rather than one concentrated position.

### Regime gates direction, not trading

**This supersedes `ARCHITECTURE.md`,** which blocks all trading outside RANGE. That design idles the bot for days at a time during trends, which conflicts with constitution principle 8.

| Regime | Spot (Binance) | Futures (Orderly) |
|---|---|---|
| RANGE | buy dips | long dips, short pumps |
| TREND_UP | buy dips | long dips only |
| TREND_DOWN | no new entries | short pumps only |

Buying a dip inside an uptrend is the same mean-reversion trade with the trend as tailwind. Counter-trend entries are never permitted in any regime.

Regime detection is pure price and volume — linear regression slope on 1h and 4h candles, plus volume as a strength grade. Two corrections to the pseudocode in `ARCHITECTURE.md`:

- A strong slope with weak volume must **not** classify as RANGE. Volume grades trend strength; it does not decide whether a trend exists. Mean-reverting into a low-volume trend is a primary loss mode.
- Regime is cached per asset for `regime_cache_sec` (default 300) rather than recomputed every cycle. Fetching 1h and 4h candles per asset per 30-second loop will exhaust rate limits.

### Capital and compounding

- **Separate per venue.** `dex_*` and `cex_*` settings are independent and never pooled — different collateral currencies, different accounts.
- **Position size is a percentage of venue equity**, not a fixed dollar amount: `dex_slot_pct`, `cex_slot_pct`. This makes compounding automatic and makes scaling from $50 to $15,000 a zero-config change.
- **Equity is read from the exchange every cycle.** Never from a database counter, which drifts.
- **Floor:** `slot_size = max(slot_pct × equity, min_notional × 1.5)`. If equity cannot support a single slot at the floor, skip that asset and log it — do not send an order that will be rejected.
- **Realized PnL only.** Compound closed trades. Unrealized PnL never affects sizing.
- **Recompute slot size once daily**, not per trade. Per-trade compounding grows position size fastest immediately after a winning streak.
- DEX compounds 100% of realized PnL by default; make the fraction a setting (`dex_compound_pct`, default 100) so it can be dialed back without a code change.

### Risk

- `tp_pct` must exceed `sl_pct`. Validate at startup; refuse to trade and log the implied breakeven win rate if violated. Defaults: `tp_pct 0.8`, `sl_pct 0.5`.
- Net edge validation at startup: `tp_pct - round_trip_fee_pct - assumed_slippage_pct >= min_net_edge_pct`. Refuse to trade below it.
- Futures: stop-loss is mandatory and must be verified present after the entry fills. If verification fails, place a standalone stop; if that fails, market-close immediately.
- Spot: no stop-loss. The time stop is the exit of last resort. This is deliberate — on spot the worst case is holding the asset, and a tight stop on spot converts a survivable position into a realized loss.
- Leverage capped by `max_leverage` (default 3). Validate the stop distance sits well inside the liquidation distance; refuse to trade if not.
- Kill switches: `daily_loss_limit` and `max_consecutive_losses`. Both disable new entries and leave existing positions to their normal exits. Both require manual re-enable.

### Exits — `manage_open_positions()`

`ARCHITECTURE.md` has no exit path. Its pseudocode only opens positions; `pnl.close_position()` exists but nothing calls it and `max_hold_minutes` has nowhere to fire. **This is the largest gap in the proposal.**

Specify a `manage_open_positions()` that runs at the top of every cycle, for every venue, **before** any entry logic, and handles:

- TP fill detection (spot: query the resting limit sell; futures: query the bracket).
- SL fill detection (futures).
- Time stop: position open longer than `max_hold_minutes` is closed at market.
- Regime exit: when the regime turns against an open position, move the TP to breakeven-plus-fees; on a confirmed adverse trend, close at market.
- Every exit writes a `closed_trades` row with real fill prices and real fees before the position record is deleted.

### Data

Four tables, not three. `ARCHITECTURE.md` proposes `settings`, `closed_trades`, `open_positions`. Add:

- **`signals`** — timestamp, asset, venue, regime, obi, dip_pct/pump_pct, action (`entered` | `skipped`), reason. Six columns.

Without it, the LLM analyzer sees only trades taken and can never answer "were my filters too strict?" — which is the question that most needs answering. Keep it lightweight; it is not a replacement for `signal_history`.

`closed_trades` must record: asset, venue, side, entry fill price, exit fill price, qty, entry fee, exit fee, net PnL, PnL percent, opened_at, closed_at, exit_reason, and the signal price at entry (so slippage is measurable).

### Operational

- `dry_run` defaults to **true** and is honored on every order path.
- Settings are read fresh each cycle so Telegram and UI changes take effect without a restart. Re-run startup validations when a value changes; halt trading if the new configuration fails one.
- Structured single-line logs. No emoji, no per-cycle "waiting for dip" noise.
- `forever.py` currently supervises one process. The rebuild needs both `bot.py` and the Telegram listener running — specify how (two supervised processes, or one entry point with a thread). `ARCHITECTURE.md` does not address this.

## 1.4 — Technical decisions to record (`/plan`)

Module boundaries and line budgets per `ARCHITECTURE.md`. Additionally decide and document:

- Exchange abstraction: how `executor.py` presents one interface over Binance spot and Orderly perps without leaking venue specifics into the scalpers.
- Symbol and filter caching: fetched once per asset per session, with tick size, step size, min qty, min notional.
- Order idempotency: client order IDs derived from position IDs so retries are safe.
- Error taxonomy: which failures retry, which skip the cycle, which trip a kill switch.
- Migration: how to move from the current schema to the new one, and how to preserve any existing trade history worth keeping.
- Test strategy: what is unit-tested with mocked exchange responses, and what requires the dry-run harness.

## 1.5 — Calibration studies (run before any threshold is fixed)

Two numbers in `ARCHITECTURE.md` are guesses. Produce `docs/CALIBRATION.md` answering:

1. **Regime distribution.** Over 90 days of historical candles, per candidate asset: what percentage of time does each regime classify as, at the proposed `SLOPE_THRESHOLD` of 0.0012? If RANGE is a small minority, the bot idles regardless of entry quality. Report the threshold value that produces a reasonable RANGE share, per asset.
2. **Realized fee and slippage.** From existing closed trades, measure actual round-trip fee percentage and actual slippage (fill price vs signal price) per venue. These feed the net-edge validation. Do not use assumed values if measured ones are available.

If insufficient historical trade data exists to measure slippage, say so and specify how the dry-run harness will capture it.

## GATE 1 — stop here

Produce: `docs/CURRENT_STATE.md`, `docs/CALIBRATION.md`, the constitution, the specs, and the plan. Report:

- Every place `ARCHITECTURE.md` was wrong about the existing code.
- Every open question you could not resolve without me.
- The calibration numbers and what they imply for thresholds.

**Do not begin implementation.** Wait for approval.

---

# PHASE 2 — Implementation

Begin only after GATE 1 is approved. Work in the order below. One module per branch, tests before moving on. `dry_run` stays `true` throughout Phase 2.

## Build order

**2.1 — `db/schema_v2.sql` + `db/db_ops.py`** (target ~150 lines)
Four tables. CRUD for settings, closed trades, open positions, signals. Migration script from the old schema. Nothing else.

**2.2 — `trade/pnl.py`** (~120 lines)
Trade recording, daily PnL with UTC midnight reset, kill switch evaluation, equity-based slot sizing with the daily recompute and the min-notional floor.
*Tests:* PnL arithmetic against hand-computed fixtures for long and short, both venues, fees included; kill switch fires at the limit exactly; slot sizing respects the floor and refuses when equity is insufficient.

**2.3 — `trade/regime.py`** (~150 lines)
Slope-based detection on 1h and 4h, volume as strength grade, per-asset caching, thresholds from `docs/CALIBRATION.md`.
*Tests:* synthetic OHLCV fixtures for each regime; a strong-slope low-volume series must classify as a trend, not RANGE; cache expiry.

**2.4 — `trading_bot/executor.py`** (~400 lines)
Unified order placement. Returns actual fill price, actual filled quantity, and actual fees on every path. Symbol filter caching. Tick and step rounding. Client order IDs. Binance: fee-adjusted sellable quantity. Orderly: bracket placement plus stop verification with emergency close.
*Tests:* every quantity and price string sent matches symbol filters; base-asset fee deduction produces a sell quantity strictly below the filled quantity; a bracket without a confirmed stop triggers emergency close; `dry_run` sends nothing and returns a simulated fill.

**2.5 — `trading_bot/spot_scalper.py`** (~250 lines) and `futures_scalper.py` (~300 lines)
Entry logic with the AND condition, the regime-direction matrix, cooldown, spacing, slot limits. `manage_open_positions()` for each venue.
*Tests:* dip without OBI confirmation produces no entry; each regime permits only its allowed directions; a position past `max_hold_minutes` is closed; cooldown and spacing both enforced; every exit writes a `closed_trades` row.

**2.6 — `bot.py`** (~200 lines)
The loop: for each asset, refresh settings, detect regime, manage open positions, then evaluate entries. Startup validation gate. Kill switch check. Structured logging.
*Tests:* startup refuses when `tp_pct <= sl_pct` or net edge is below minimum; `manage_open_positions` always runs before entries; a tripped kill switch blocks entries but not exits.

**2.7 — Dry-run validation**
Run 48 hours against live market data with `dry_run = true`. Report: trades per day per asset, win rate, average win and loss, net PnL after fees, measured slippage, maximum time in position, exit reason distribution, and regime distribution actually observed.

**GATE 2** — go-live is a human decision made against those numbers.

**2.8 — `research/performance_llm.py`** (rewrite, not port)
Offline and advisory only. Never writes settings, never places orders.

Changes from the current version:
- **Delete the entire parameter-extraction layer** — `_extract_parameters_from_main`, the regex resolution, the `eval()` call, and the full-source injection. All parameters now live in the `settings` table; read them.
- **Fix the average-win bug.** `round(sum(pnls) / positives, 4)` divides the sum of *all* PnLs by the win count. Filter to positive values, as `avg_loss_pnl` correctly does. This number feeds the fee-drag calculation.
- **Recommendations target settings keys**, not code identifiers — "set `tp_pct` to 0.8", not "change `ReversalScalper.SLOPE_THRESHOLD_1H`". The latter will not exist.
- **`is_large_loss` becomes relative to equity**, not a hardcoded `-$10`.
- **Include skipped signals** from the `signals` table so the analyzer can assess whether filters are too tight.
- Drop the `_is_preferred_window` time-of-day logic unless a `trading_hours` setting is specified — it is a leftover from the old strategy.
- Timezone offset comes from a setting, not a hardcoded 4.

**2.9 — Cleanup** (only after live trading is stable)
Tag the current state as `legacy-v3` before deleting anything. Then remove `trade/main.py`, move the ML and LLM code to `research/`, drop unused tables, update the UI tabs and Telegram commands, archive the old prompt files.

---

## Guardrails for both phases

Never, in any phase, without asking:
- Run any module that can place a real order.
- Set `dry_run` to `false`.
- Modify `.env` or read credential values into output.
- Change a live settings value in the database.
- `git push`, `git reset --hard`, or delete uncommitted work.

Stop and ask when: a business rule above is ambiguous, the calibration data contradicts a proposed threshold, `ARCHITECTURE.md` conflicts with what the code actually does, or a constitution principle cannot be satisfied as specified.
