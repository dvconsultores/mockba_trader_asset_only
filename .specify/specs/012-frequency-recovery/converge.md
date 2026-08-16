# Converge: Frequency Recovery (012)

**Date**: 2026-08-16 | **Status**: Implemented — settings live in the local DB, 107/107 tests green

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 DB values + validator | ✅ | `max_concurrent_positions=4`, `cex_slot_pct=20`, `capital_cex_usdt=100`; `validate` → ok ×3 |
| 2 No source change, suite green | ✅ | `git status` shows no new source modifications from 012; 107 passed |
| 3 Docs | ✅ | CURRENT_STATE §0 (012), CHANGELOG `ops:`, audit #12 recorded |

## Deviations

- **Values revised same-day by clarify Q1.** The interim implementation (25%,
  $50 assumption) ran for a few hours before the operator's capital plan
  ($100, 4 × 20%, $20 buffer) superseded it. Both DB backups exist.

## Remaining work

- **Slot equal-sizing depends on audit #12.** Until `get_equity` sees coin
  holdings, later slots in a burst are sized from shrinking free USDT and will
  be smaller than $20 (floored at ~$7.5). Conservative, but the operator's
  4×$20 plan is only fully realized once 015 fixes the equity definition.
- **Measure the effect.** After a week live: `max_slots`/`max_concurrent` skip
  count (expect ~0), trades/day, and burst-day capture vs the 08-06 baseline.
- Queue unchanged: **013** loop latency · **014** VII re-baseline · **015**
  kill-switch integrity (audit items 3, 4, **12** — now three `get_equity`/
  escalation defects, the recommended next spec).
