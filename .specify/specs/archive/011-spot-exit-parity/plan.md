# Plan: Spot Exit Parity

**Feature**: 011-spot-exit-parity | **Date**: 2026-08-16 | **Spec**: `specs/011-spot-exit-parity/spec.md`
**Status**: Implemented 2026-08-16 — 107/107 tests green *(plan/tasks written retroactively at operator request to complete the speckit flow; content documents what was built)*
**Branch**: `main`

## Summary

Constitution V repair on the live venue, using the mechanisms feature 010
proved on futures: exchange-SL exits record **real fills** via the existing
`_real_fill` (both the main branch and the crash-guard pre-check), every
`_close` carries the position's **real `opened_at`**, and the fee fallback
reads **`cex_round_trip_fee_pct` / 2** instead of a hardcoded `0.001`. Exit
*decisions* are byte-identical — only recorded values change.

## Constitution Check

| Principle | Compliance |
|---|---|
| **V** Real Fills Only | ✅ **restored** — the last spot path recording theoretical prices is gone; kill switches now see true (slipped) losses. |
| III / IV / VI | ✅ No order flow, verification, or reconciliation change. |
| II (v1.1.0), I | ✅ Entry side untouched. |
| VII | ✅ ~+10 lines net; pre-existing overrun (spec 014). |
| VIII | ✅ Exits only; frequency unaffected (operator HF directive holds). |

## Pinned mechanisms

- **M1 — SL branches reuse `_real_fill`** (already in the file for TP): fallback
  = stored `sl_price`, which was the *entire* old behaviour — so the fix is
  strictly additive in information.
- **M2 — `opened_at` threading**: `_close` gains a trailing `opened_at=0.0`
  parameter; all **14 call sites** in `manage_open_positions` append `op`
  (already loaded per position). Trailing-default keeps any stray caller valid.
- **M3 — Fee fallback**: `cex_round_trip_fee_pct/100/2` per leg. At the current
  0.20% setting this equals the old `0.001` exactly — **zero numeric behaviour
  change today**, asserted by test.

## Testing

`tests/test_spot_exit_parity.py` (5 tests): real SL fill (main + crash-guard
branches), fallback with a distinctive 0.30% setting, real `opened_at`, and the
old-rate equivalence guard. `test_spot_exit_hardening.py` passes **unmodified**
— the proof that exit decisions did not move.

## Out of scope

Backfill of historical `opened_at=0` rows; `BinanceSpot`; futures; entries;
everything listed in the spec.
