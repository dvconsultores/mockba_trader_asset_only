# Tasks: Spot Exit Parity (011)

**Prerequisites**: spec.md ✅ (clarified inline — fallback rule per 010's `_real_exit`), plan.md ✅, constitution v1.1.0 ✅
*(Written retroactively at operator request; all tasks were executed 2026-08-16.)*

- [X] T001 Crash-guard SL pre-check (`spot_scalper.py` ~line 100): record via `_real_fill(exchange, sym, slid, stored_sl_price)` instead of the trigger price with `fee_exit=0`.
- [X] T002 Main SL fill branch (~line 137): same `_real_fill` treatment.
- [X] T003 `_close`: trailing `opened_at=0.0` parameter; `record_closed_trade(opened_at=opened_at, ...)`; fee fallback `cex_round_trip_fee_pct/100/2` replaces hardcoded `0.001`.
- [X] T004 All 14 `_close` call sites in `manage_open_positions` append `op` (scripted replace over the six distinct closing-argument shapes; verified 14/14 carry it — one apparent miss was a multi-line call whose continuation line has it).
- [X] T005 `tests/test_spot_exit_parity.py` — 5 tests (AC1×3, AC2, AC3+AC4 equivalence guard).
- [X] T006 Full regression: **107 passed**; `test_spot_exit_hardening.py` unmodified (AC4).
- [X] T007 Docs: CURRENT_STATE §0 (011) + CHANGELOG 2026-08-16 `fix:`.

⛔ **Not tasks**: exit-logic changes of any kind; historical-row backfill; migrations (none needed).
