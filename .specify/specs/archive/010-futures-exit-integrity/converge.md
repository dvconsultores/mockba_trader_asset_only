# Converge: Futures Exit Integrity (010)

**Date**: 2026-08-15 | **Status**: Implemented — 17/17 tasks, **102/102 tests green**
**Constitution**: v1.1.0 — Principles III and V restored on the futures path

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 Time stop closes | ✅ | `test_time_stop_closes_position` — asserts `market_close` called *and* verification ran |
| 2 Failed close keeps position | ✅ | `test_time_stop_failed_close_keeps_position` — row survives, no `closed_trades` write |
| 3 Failed close re-protects | ✅ | `test_time_stop_failed_close_replaces_sl` — new stop id persisted |
| 4 Emergency escalation | ✅ | `test_double_failure_escalates` — alert sent, `auto_trade_orderly=false`, row retained |
| 5 Closure verified | ✅ | `test_time_stop_unverified_close_keeps_position` — fill returned but position still open ⇒ no delete |
| 6 Time-stop PnL is real | ✅ | `test_time_stop_records_real_fill` — exit 95.0, not entry 100.0; loss actually booked |
| 7 Regime exit reaches exchange | ✅ | `test_regime_exit_amends_exchange` — cancel + re-place, then DB |
| 8 Regime-exit failure safe | ✅ | `test_regime_exit_failure_leaves_db` — `tp_price` untouched |
| 9 TP real fills | ✅ | `test_tp_exit_uses_real_fill` |
| 10 SL real fills | ✅ | `test_sl_exit_uses_real_fill` — slipped fill (96.4 vs 98.0 trigger) captured |
| 11 Real `opened_at` | ✅ | `test_opened_at_is_real` |
| 12 Real fees | ✅ | `test_fee_fallback_uses_setting` — `dex_round_trip_fee_pct`, never `0.0003` |
| 13 One cancel request | ✅ | `test_cancel_order_single_request` — `post.call_count == 0` |
| 14 `dry_run` honoured | ✅ | `test_dry_run_places_nothing` — all three new order paths |
| 15 Tests | ✅ | 14 new + 88 existing = **102 passed** |
| 16 Docs | ✅ | CURRENT_STATE §0 feature-010, CHANGELOG `fix:` |

## Deviations from plan

1. **Two extra executor methods.** The plan named `market_close` and
   `get_order_fills`. Implementation required two more: **`place_tp`** (the
   regime exit cannot amend an order that has no placement method — the first
   draft reached for a non-existent `exchange.place_tp` behind a `hasattr` guard,
   which would have silently made AC7 unreachable) and **`place_stop`** (AC3's
   re-protection). Both are thin, both honour `dry_run`, both use the same
   verified `reduce_only` contract.

2. **Two missing imports in `futures_scalper.py`.** The module imported neither
   `logger` nor `upsert_setting`, both of which the new error and escalation
   paths need. Added.

3. **Entry-fee estimate is the normal path, not the exception.**
   `futures_scalper._save_open` does not store `fee_entry`, so `_close` falls
   back to the settings estimate for the *entry* leg on essentially every trade.
   The exit leg is real. Called out here rather than silently accepted — see
   remaining work.

## Remaining work

### Blocking, before DEX is ever armed

- **Run the manual `dry_run` checklist** (plan §Testing Strategy / CURRENT_STATE).
  CI cannot reach Orderly; the fakes prove the control flow, not the wire format.
  In particular `reduce_only` was verified from `ccxt/woofipro.py` (same host),
  **not** from a live response.
- **Fix `_save_open` to store `fee_entry`** (deviation 3) — otherwise futures
  entry fees remain estimates and Constitution V is only half-restored.

### Follow-up specs (renumbered in 009 converge)

- **012** Frequency recovery · **013** Loop latency / bookTicker · **014**
  Constitution VII re-baseline (hot path now **2,573** lines against a 1,500
  budget) · **015** Kill-switch integrity (audit items 3–4).

### Audit items still open

Items 5–11 from the 009 converge table, unchanged. Two are now *more* visible by
contrast, since the futures side was just repaired and the spot side was not:

| # | Defect | Note |
|---|---|---|
| 6 | `spot_scalper._close` still passes `opened_at=0` | Futures now records it; spot does not. One-line fix, deliberately out of 010's scope. |
| 7 | Spot exchange-SL exits record the theoretical `sl_price` with `fee_exit=0` | Futures now uses real fills for exactly this case. |

**Recommendation**: a small `011-spot-exit-parity` spec closing items 6 and 7
would bring spot to the standard 010 just set for futures — perhaps 30 lines,
and it operates on the venue that is **actually trading real money**, which
arguably outranks the DEX work that motivated 010.

### Also worth noting

`place_entry`'s emergency path (SL fails twice → market close → `return None`)
still leaves the take-profit order live. On futures a dangling reduce-only TP is
much less dangerous than it first appears — it cannot open a reverse position —
but it is an untracked resting order. Not fixed here; it belongs with the
entry-side work alongside `fee_entry`.
