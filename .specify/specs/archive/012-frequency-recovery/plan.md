# Plan: Frequency Recovery

**Feature**: 012-frequency-recovery | **Date**: 2026-08-16 | **Spec**: `specs/012-frequency-recovery/spec.md`
**Status**: Implemented 2026-08-16 — settings live in the local DB, 107/107 tests green
**Branch**: `main`

## Summary

Settings-only change (no source file touched): `max_concurrent_positions` 2→4,
`cex_slot_pct` 40→20, `capital_cex_usdt` 50→100, per the operator's capital
plan (clarify Q1: $100 funded, 4 × $20 slots, $20 standing loss buffer). The
implementation *is* three `upsert_setting` calls plus validation — the plan's
job is the safety analysis below.

## Technical Context

**Storage**: SQLite settings table only. No schema change, no migration, no
code. Applied to the local DB (operator pushes via `push-db.sh`).

**Verification**: `trade.settings_rules.validate` on each key (all `ok`); full
test suite unaffected (tests use tmp DBs) and re-run green.

## Constitution Check

| Principle | Compliance |
|---|---|
| I, III, IV, V, VI | ✅ Untouched — no code path changes. |
| **II** (v1.1.0) | ✅ Entry cost gate unaffected; slot size does not enter it. |
| **VII** | ✅ Zero lines. |
| **VIII** | ✅ **The point.** Capacity doubles where 183 skips/7d showed slots binding; no quality gate loosened (operator directive: HF capacity is never traded away). |

## Safety analysis (the actual design work)

- **Deployment ceiling**: 4 × 20% of live equity. At $100 fully free: $80
  deployed, $20 buffer. Worst concurrent loss at the 3% crash floor: ~$2.40 —
  the buffer absorbs >8 consecutive max-loss rounds.
- **Equity-definition caveat (audit #12)**: `get_equity` counts USDT only, so
  as slots fill, the *next* slot is sized from shrinking free cash — at 20% of
  free USDT the 3rd/4th slots would shrink toward the `min_notional × 1.5`
  floor (~$7.5). Direction-safe (never over-deploys the buffer) but it means
  the four slots will NOT be equal-sized until audit #12 is fixed. Accepted:
  conservative, and the fix belongs in 015 with the other `get_equity` defect.
- **Slot-size day-cache**: `compute_slot_size` caches per venue per UTC day
  (in-memory); the new percentage applies from the deploy restart.
- **Kill switches unchanged**: `daily_loss_limit_pct=2` now computes against
  the same (USDT-only) equity as before — behaviour identical in kind.

## Out of scope

Loop latency (013), Constitution VII re-baseline (014), `get_equity` repair
(015 — audit items 3, 4, 12), any quality-gate change.
