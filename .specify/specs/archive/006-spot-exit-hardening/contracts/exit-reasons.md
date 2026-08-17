# Contract: `closed_trades.exit_reason` (spot exit reasons)

**Feature**: 006-spot-exit-hardening | **Date**: 2026-08-12

The `exit_reason` value written to `closed_trades` (via `trade/pnl.py`
`record_closed_trade`) and rendered by the dashboard Closed Trades page
(`dashboard/main.py` `REASON_LABELS`). Producers today: the spot scalper's
`_close` in `trading_bot/spot_scalper.py`. This contract is consumed by
`dashboard/main.py` (label map), tests, and any exit-path code.

## Values

| Value | Meaning | Written when | Exit price used | Fee used |
|---|---|---|---|---|
| `tp` | Take-profit filled | TP order status `FILLED` (guard or normal path) or no-balance recovery found a real TP fill | real fill price via `get_order_fills` (`_real_fill`) | real commission (or 0.001 × notional fallback) |
| `sl` | Stop-loss hit | SL order status `FILLED`, or live price ≤ stored `sl_price` (market sell), or crash-guard fill-aware SL-filled branch | stored `sl_price` (order-filled path) or market-sell fill | real commission (or fallback) |
| `time_stop` | Hold time exceeded | `now − opened_at > max_hold_minutes_spot` → cancel + market sell | market-sell fill; dry-run falls back to entry | real commission (or fallback) |
| `orphan` | No balance on exchange, no recoverable fill | market sell returned `None` and `get_asset_balance < qty` | live price, else entry | 0 (fallback applies) |
| `crash_guard` **(new)** | Emergency floor breached, no order pre-filled | live `< entry × (1 − max_loss_per_position_pct/100)` → verify fills → cancel TP/SL → market sell | market-sell fill; dry-run falls back to entry | real commission (or fallback) |

## Rules

1. **Real fills only (Constitution V).** Every value derives from the exchange's
   actual fill price/commission (or the documented dry-run fallback to the
   entry price for simulated fills with `fill_price == 0.0`). The `sl`
   order-filled path uses the stored `sl_price` (pre-existing behavior —
   unchanged by this feature).
2. **No phantom double-close (Constitution IV/V).** A position whose TP or SL
   order already shows `FILLED` is closed with its real reason (`tp`/`sl`)
   and is **never** market-sold. The crash guard performs this fill check
   before any cancel/sell, and the no-balance recovery path closes as
   `orphan` (or real `tp`) rather than selling a position the exchange no
   longer holds.
3. **Cooldown stamping.** `crash_guard` and `sl` both stamp the in-memory
   `_last_sl` re-entry cooldown (`cooldown_sec × SL_COOLDOWN_MULT`, ~10 min)
   for the same `(asset, side)`. `tp`, `time_stop`, `orphan` do not.
4. **Label rendering.** `dashboard/main.py` `REASON_LABELS` must contain a
   `"crash_guard": "Crash guard"` entry; unknown reasons render as the
   uppercased raw value (pre-existing fallback, `str(r["exit_reason"]).upper()`).

## Validation

- `tests/test_spot_exit_hardening.py` asserts `exit_reason` values written to
  `closed_trades` for the crash-guard path (`crash_guard`), the fill-aware
  paths (`tp`/`sl` with no market sell), and unchanged normal exits.
- `REASON_LABELS["crash_guard"] == "Crash guard"` is asserted structurally.
