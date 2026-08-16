# Converge: Spot Exit Parity (011)

**Date**: 2026-08-16 | **Status**: Implemented — **107/107 tests green** (102 + 5 new)

| AC | Status | Evidence |
|---|---|---|
| 1 SL real fills (both branches) | ✅ | `test_sl_exit_uses_real_fill`, `test_crash_guard_sl_branch_uses_real_fill`, fallback in `test_sl_fill_query_failure_falls_back` |
| 2 Real `opened_at` | ✅ | `test_opened_at_recorded` — all 14 `_close` call sites pass `op` |
| 3 Fee fallback from settings | ✅ | `test_sl_fill_query_failure_falls_back` (0.30% setting honoured) |
| 4 No behaviour change | ✅ | `test_default_setting_matches_old_rate` (identical numerics at 0.20%); `test_spot_exit_hardening.py` passes **unmodified** |
| 5 Tests | ✅ | 5 new; full suite 107 passed |
| 6 Docs | ✅ | CURRENT_STATE §0 (011), CHANGELOG 2026-08-16 `fix:` |

**No migration** — columns already exist; nothing for `push-db.sh` beyond the
009 column already applied locally.

**Deviation**: originally shipped compact (spec + converge only). At operator
request (2026-08-16) the full speckit set was completed retroactively —
`plan.md` and `tasks.md` now document what was built, and the spec's inline
clarification (fallback rule per 010's `_real_exit`) stands as the clarify
record. Flow: specify → clarify → plan → tasks → implement → converge ✅.

**Remaining (unchanged queue)**: 012 frequency recovery · 013 loop latency /
bookTicker · 014 Constitution VII re-baseline · 015 kill-switch integrity
(audit items 3–4) · audit items 5, 8–11.

**Note for the operator**: historical rows are not backfilled — `opened_at=0`
rows from before 2026-08-16 stay as they are; hold-time analytics become
reliable for trades closed from now on.
