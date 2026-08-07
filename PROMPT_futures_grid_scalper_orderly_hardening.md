# Implementation Prompt — Harden `futures_grid_scalper.py` (Orderly DEX) for live capital

## Role and scope

You are working on a **leveraged perpetual futures** mean-reversion scalper running on **Orderly Network** (USDC-margined perps, e.g. `PERP_NEAR_USDC`), inside an existing Python trading bot. Target file: `trading_bot/futures_grid_scalper.py` (adjust path if it differs in the repo).

This module trades **real money with leverage**. On spot, a bug means holding an unwanted coin. Here, a bug means liquidation. Every unhandled path is a financial loss, not a stack trace.

**Do not rewrite the module from scratch.** Apply the changes surgically. Keep the public entry point `futures_grid_scalp_cycle(asset, regime, obi, live_price)` and its return contract (`"buy"`, `"sell"`, `None`). Keep the existing imports from `db.db_ops`, `logs.log_config`, `trading_bot.futures_executor_apolo`, `trading_bot.send_bot_message`. No new third-party dependencies.

Before writing code, **read `trading_bot/futures_executor_apolo.py` in full** and confirm the actual signatures and return types of `place_futures_order`, `get_user_statistics`, `get_available_balance`, and `get_close_price`. Several tasks below depend on what these really return, and the current module makes assumptions that are probably wrong. If a required capability does not exist in the executor, add it there rather than inlining raw HTTP in this module.

---

## Non-negotiable invariants

If any task below conflicts with one of these, the invariant wins.

1. **No naked leveraged position, ever.** A filled entry without a confirmed stop-loss is an emergency: attach a stop immediately, and if that fails, market-close the position. Do not wait for the next cycle.
2. **Unknown state means no trading.** If the bot cannot determine its current position count, side, or size, it opens nothing. State queries fail *closed*, never open.
3. **No order is ever placed without its size explicitly specified**, rounded to the symbol's filters, and validated against min-notional.
4. **Reward must exceed risk.** The module refuses to run with `grid_tp_pct <= grid_sl_pct`.
5. **The stop must sit far inside the liquidation price.** Validated numerically at startup, not assumed.
6. **RANGE regime only for entries.** Exits and risk management run in every regime.
7. **Restart-safe.** Restarting the process must not duplicate positions or lose track of live ones.
8. **Every order result is inspected.** No fire-and-forget calls.

---

## P0 — Blockers. The module does not currently work at all.

### P0.1 — The module cannot be imported

```python
GRID_DIRECTION = _grid_setting("grid_direction", "long")
```

`_grid_setting` returns `float`. `float("long")` raises inside the `try`, execution falls through to `return float(os.getenv("GRID_DIRECTION", "long"))`, which raises an uncaught `ValueError` **at import time**. And `_grid_direction_ok` then calls `.strip().lower()` on what would be a float. This file has never executed.

Add a separate string reader and use it for `grid_direction`:

```python
def _grid_setting_str(key: str, default: str) -> str:
    try:
        val = get_setting(key)
        if val is not None and str(val).strip():
            return str(val).strip().lower()
    except Exception:
        pass
    return (os.getenv(key.upper(), default) or default).strip().lower()
```

Validate the value is one of `{"long", "short", "both"}`; on anything else, log an ERROR and fall back to `"long"`.

### P0.2 — The order payload has no quantity

```python
qty = notional / live_price      # computed
order_payload = {                # never includes it
    "symbol": asset, "side": "BUY", "entry": live_price,
    "take_profit": tp_price, "stop_loss": sl_price, "leverage": leverage,
}
```

`qty` is used only in the Telegram message. The size the exchange actually opens bears no necessary relation to the size the collateral check was based on or the size reported to the user.

Fetch the symbol's trading filters from Orderly (base tick / min size / price tick / min notional — add a cached `get_symbol_info(symbol)` to the executor if absent), floor `qty` to the base tick, reject the entry if it falls below min size or min notional, and include the size in the payload under whatever key the executor actually expects. Verify that key by reading the executor.

### P0.3 — Order results are discarded; brackets are unverified

`place_futures_order(order_payload)` is called inside a bare `try` and its return value is thrown away. You never confirm the entry filled, never confirm the SL/TP attached, and never keep an order or position id.

Implement `_open_position_verified(payload) -> dict | None`:

1. Place the order; capture the response and log it in full at DEBUG.
2. Poll for the resulting position/order state (bounded retry, e.g. 5 attempts over ~10s) until the entry is confirmed filled, partially filled, or rejected.
3. On fill, **query the live algo/conditional orders for the symbol and confirm a stop-loss exists at the expected price.**
4. If the entry filled but no stop is present: place a standalone stop immediately. If that also fails, **market-close the position now** and send a Telegram alert at WARNING. Never leave the cycle with an unprotected leveraged position.
5. Return a position record; return `None` on rejection.

### P0.4 — Position state fails open

```python
def _refresh_open_positions() -> int:
    try:
        _open_position_count = get_user_statistics()
    except Exception:
        pass  # Keep last known count
```

Two problems. First, `get_user_statistics()` is named like it returns an account statistics object, not an integer count — verify, and if it returns a dict, extract the actual open-position count *for this symbol*. As written, `open_count >= GRID_MAX_POSITIONS` may be comparing a dict to an int and throwing.

Second and worse: swallowing the exception and keeping a stale count means an Orderly API outage leaves the bot believing it has zero positions, so it opens more on top of what it already holds. With leverage, that compounds fast.

Change the signature to `-> Optional[int]`, return `None` on any failure, and in the cycle: if the count is `None`, log a WARNING and `return None` without trading. Track consecutive failures; after 5, set `grid_enabled = 0`, notify Telegram, and stop.

The refresh must also return the **side and size** of open positions, not just a count — P1.5 and P1.6 depend on it.

---

## P1 — Required for the strategy to have positive expectancy

### P1.1 — The reward/risk ratio is inverted (highest-impact single change)

Current defaults: `grid_tp_pct = 0.5`, `grid_sl_pct = 0.8`. Risking 0.8 to make 0.5 requires a **66% win rate** just to break even after fees. A 0.8% stop on an alt perp is inside the normal noise band — it gets hit by random movement on entries that would have reverted.

Change the defaults to `grid_tp_pct = 0.8`, `grid_sl_pct = 0.5`, and enforce a minimum ratio at startup:

```python
GRID_MIN_RR = _grid_setting("grid_min_rr", "1.2")   # TP/SL floor

def _validate_risk_config() -> tuple[bool, str]:
    if GRID_SL_PCT <= 0:
        return False, "stop-loss must be > 0 on leveraged positions"
    rr = GRID_TP_PCT / GRID_SL_PCT
    if rr < GRID_MIN_RR:
        return False, f"reward/risk {rr:.2f} below minimum {GRID_MIN_RR}"
    ...
```

Also compute and log the implied breakeven win rate on every startup so it is visible:

```python
breakeven_wr = (GRID_SL_PCT + fee_round_trip_pct) / (GRID_TP_PCT + GRID_SL_PCT)
```

### P1.2 — Net-edge and leverage validation

Perp fees are charged on **notional**, so leverage multiplies fee cost exactly as it multiplies P&L. Leverage is not an edge; it does not change breakeven win rate. Treat it purely as a risk multiplier and cap it.

Add settings and a startup gate that refuses to trade when they are not satisfied:

- `grid_fee_pct_round_trip` (default `0.06`) — round-trip taker fee as a percentage of notional. Verify against the account's actual Orderly fee tier and log the assumption.
- `grid_assumed_slippage_pct` (default `0.03`).
- `grid_min_net_edge_pct` (default `0.30`) — `grid_tp_pct` minus fees minus slippage must clear this.
- `grid_max_leverage` (default `3`). Clamp `_dex_leverage()` to this ceiling and log loudly when clamping.

### P1.3 — Liquidation distance guard

Compute the estimated liquidation distance from leverage and the symbol's maintenance-margin rate (fetch it; do not hardcode). Require:

```
stop_distance_pct <= 0.25 * liquidation_distance_pct
```

If the stop sits closer than 4x the liquidation buffer, refuse to trade and log the numbers. Recompute on every leverage change. Note that under **cross margin** the liquidation price depends on total account equity and every other open position — if the account is cross-margined, compute the distance against account equity, not isolated position margin, and state clearly in the log which mode was assumed.

### P1.4 — Brackets must be derived from the fill price, not the signal price

```python
tp_price = live_price * (1 + GRID_TP_PCT / 100)
sl_price = live_price * (1 - GRID_SL_PCT / 100)
```

`live_price` is the price that triggered the signal, not the price you got. Every unit of slippage shifts your real stop distance. On a 0.5% stop, 0.05% of slippage is 10% of your risk budget.

Compute brackets from the actual average fill price returned in P0.3, then round to the symbol's price tick — **away from the entry for the stop, toward the entry for the take-profit** so that rounding never silently widens your risk. Re-verify the rounded TP still clears `grid_min_net_edge_pct`; if not, bump it one tick further out.

### P1.5 — Long and short can both fire; direction handling is unsafe

Two problems:

- `_is_price_dip` and `_is_price_pump` read `_peak_price` and `_trough_price` from the **same 40-sample deque**, so in a choppy range both can be true simultaneously. Because the long block is evaluated first and returns early, the module has a structural long bias. Replace with a single evaluation that scores both directions by signal magnitude (dip % vs pump %) and takes the stronger one, or requires exclusivity and skips the cycle when both fire.
- With `grid_direction = "both"`, nothing checks the side of existing positions. Opening a short while a long is open either nets the position out (one-way mode) or creates a hedge that pays funding on both legs. Neither is intended. Read the current position side from P0.4 and **never open an opposing position** unless a new `grid_hedge_mode_enabled` setting (default `0`) is explicitly on.

### P1.6 — Entry spacing, persistence, and reconciliation

- **Spacing:** add `grid_min_entry_spacing_pct` (default `0.6`). Reject an entry within that distance of any open position's entry price. Without it, "multiple positions" is one concentrated position paying fees several times.
- **Persistence:** position state currently lives in a module global (`_open_position_count`). On restart the bot believes it holds nothing while Orderly still holds leveraged positions with live brackets. Persist position records to the DB via `db/db_ops.py`, following the existing `get_setting`/`set_setting` style.
- **Reconciliation on startup, before any entry logic:** query Orderly for open positions and live algo orders; adopt any position with no local record; re-attach a stop to any position missing one; close out any local record with no matching exchange position. Log a one-line summary.

### P1.7 — Funding cost

Perp funding is a real cost this module ignores entirely. Fetch the current funding rate for the symbol and:

- Skip long entries when funding is strongly positive beyond a `grid_max_adverse_funding_bps` threshold (default `5` bps per interval) — you would be paying to hold a position whose entire target is 0.8%.
- Accrue paid/received funding into realized PnL so the reported numbers are true.

### P1.8 — Exit management beyond the bracket

The bracket covers TP and SL. Two exits are still missing, and they must run **before** entry logic on every cycle, in every regime:

- **Time stop** (`grid_max_hold_sec`, default `14400`): cancel brackets and market-close. A mean-reversion trade that has not reverted in four hours was not a mean-reversion trade.
- **Regime-change exit** (`grid_exit_on_regime_change`, default `1`): when `regime != "RANGE"` with a position open, move the take-profit to breakeven-plus-fees. On a confirmed trend against the position, market-close immediately. The edge only exists in a range; stop paying to find out.

### P1.9 — Kill switches

- `grid_enabled` (default `1`), checked at the top of every cycle.
- `grid_daily_loss_limit_usdc` (default `10`): track realized PnL per UTC day; on breach, set `grid_enabled = 0` and notify. Existing positions run to their normal exits.
- `grid_max_consecutive_losses` (default `4`): same treatment. A tight-stop strategy in the wrong regime loses in streaks, and the streak is the signal that the regime assumption is broken.

### P1.10 — Settings are frozen at import

Every `GRID_*` constant is evaluated once at module import. Changes made through the Telegram bot or Mini App do not take effect until a restart, which is almost certainly not what you or your users expect.

Move configuration into a `_load_config()` function returning a frozen dataclass, called once at the top of each cycle, with the resulting values threaded through as parameters instead of read from module globals. Log a diff line whenever a value changes between cycles. Re-run `_validate_risk_config()` on every change and halt trading if the new configuration fails it.

---

## P2 — Cleanup

- Remove unused imports and dead code: `logging`, `get_orderbook`, `get_close_price`, and `_compute_obi` (OBI arrives as a parameter; the local computation is never called). If `_compute_obi` is intentionally part of this module's public surface, add a test; otherwise delete it.
- `from trading_bot.futures_executor_apolo import get_available_balance` is imported mid-function while its siblings are imported at module top. Move it up.
- The balance pre-check `required = (open_count + 1) * margin_per_position` over-counts: margin for existing positions is already locked and not part of free collateral. It should be `margin_per_position`, plus a buffer (`* 1.05`) for fees and funding.
- The trailing "log why we're not entering" block recomputes every condition a second time. Refactor the entry decision into one function returning `(should_enter: bool, direction: str | None, reason: str)` and log the reason once.
- Pass a client-supplied order id derived from the position id on every order so retries are idempotent.
- Structured logging on every entry, exit, skip-reason, bracket verification, and reconciliation action, with `symbol`, `side`, `price`, `size`, `reason`.

---

## Acceptance criteria

Complete when all of the following hold. Write tests in `tests/test_futures_grid_scalper.py` against mocked Orderly responses — no live API calls in tests.

1. The module imports cleanly with `grid_direction` set to `long`, `short`, `both`, an invalid string, and unset.
2. Every order payload passed to `place_futures_order` contains an explicit size, floored to base tick, at or above min size and min notional.
3. An entry that fills with no stop-loss present results in a stop being placed; if stop placement fails, the position is market-closed within the same cycle.
4. `get_user_statistics` raising causes the cycle to return `None` and open nothing. Five consecutive failures disable the module.
5. `grid_tp_pct = 0.5` with `grid_sl_pct = 0.8` causes the module to refuse to trade, logging the reward/risk ratio and the implied breakeven win rate.
6. Leverage above `grid_max_leverage` is clamped, and a stop closer than 4x the liquidation buffer refuses to trade.
7. Brackets are computed from the average fill price, not the signal price, and rounding never widens the stop distance.
8. With dip and pump both true, exactly one direction is chosen deterministically by signal magnitude — verified by a test asserting no structural long bias across a symmetric fixture.
9. With `grid_direction = "both"`, `grid_hedge_mode_enabled = 0`, and a long open, a short signal opens nothing.
10. Restart with one live Orderly position and its brackets results in exactly one tracked position and zero new orders.
11. A position exceeding `grid_max_hold_sec` is market-closed with brackets cancelled first, and the cancel is confirmed before the close is sent.
12. Changing `grid_tp_pct` in the DB takes effect on the next cycle without a restart.
13. Realized PnL accounting includes fees and accrued funding.

---

## Validation before live capital

These fixes remove the ways the module *guarantees* a loss. They do not make it profitable — profitability is a property of the parameters, and parameters must be measured.

1. Add `grid_dry_run` (default `1`). In dry-run every order path logs the exact payload it would send and records a simulated fill against the same PnL accounting, including fees and funding, but sends nothing.
2. Run a minimum of 200 simulated round trips spanning at least one clear ranging period and one clear trending period. Report: win rate vs the computed breakeven win rate, average win, average loss, largest loss, net PnL after fees and funding, maximum consecutive losses, maximum time in position, and the split between TP exits, SL exits, time-stop exits, and regime exits.
3. Go live only if net PnL after all costs is positive across **both** regimes in that sample, actual win rate exceeds breakeven by a clear margin, and the largest single loss is smaller than the sum of the ten preceding wins.
4. First live run: `grid_max_positions = 1`, leverage `2`, `grid_daily_loss_limit_usdc` at the smallest workable value, under manual supervision.

Report the dry-run statistics before enabling live trading.
