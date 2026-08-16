# Plan: Kill-Switch Integrity

**Feature**: 015-kill-switch-integrity | **Date**: 2026-08-16 | **Spec**: `specs/015-kill-switch-integrity/spec.md`
**Status**: Draft — proceeding under the operator's full-cycle authorization
**Branch**: `main`

## Summary

`get_equity` becomes `float | None` with whole-account valuation on Binance;
bot.py's equity block handles `None` by counting a **cross-cycle consecutive**
per-venue failure streak that disables + notifies at 5 (Constitution IV,
including its previously-missing Telegram half) and resets on success; the
scalpers fail closed on unknown equity live and fall back to the declared pool
in dry-run. The per-cycle `_venue_failures` dict, its per-asset increments, and
the end-of-cycle escalation loop are removed.

## Constitution Check

| Principle | Compliance |
|---|---|
| **IV** (NON-NEGOTIABLE) | ✅ **restored** — unknown equity is `None`, never 0; escalation is genuinely consecutive AND notifies; entries fail closed on unknown state. |
| **V** | ✅ Position valuation uses DB quantities/prices that came from real fills; no fabricated number enters `closed_trades`. |
| II (v1.1.0), I, III | ✅ Entry gate, strategy, brackets untouched. |
| VI | ✅ Streak is in-memory and resets on restart — a restart starts clean, which is safe (worst case: 5 more cycles ≈ 2.5 min before a genuine outage re-trips). |
| VII | ⚠️ pre-existing overrun (spec 014); net ≈ +35 lines (helper ~15, equity method ~15, scalper guards ~8, removals −10). |
| VIII | ✅ AC3/AC4 *increase* tradeability: correct equity keeps slot sizing at the intended $20 instead of collapsing to the floor; the false-kill path (one blip disabling the venue) is removed. |

## Verified call-site inventory (all six, at plan time)

| Caller | Handling of `None` |
|---|---|
| `bot.py:388` main loop | The change site: streak + `continue`; cache write only on success |
| `bot.py:233` scanner `_equity_for` | Already `None`-tolerant (`scan_venue` falls back to `venue_state`) — no change |
| `spot_scalper.py:227` | Q1 guard (skip live / pool in dry-run) |
| `futures_scalper.py:191` | Same |
| `market_check` | Reads the `venue_state` **cache**, never calls `get_equity` — protected by AC2 |
| dashboard | Grep-verified: no executor import — out of scope |

## Pinned mechanisms

- **M1 — Binance equity**: one `/api/v3/account` call (unchanged weight) →
  USDT free+locked + `Σ qty × entry_price` over `load_all_positions(venue="binance")`.
  The DB read is local. A DB error degrades to USDT-only (logged direction-safe
  fallback), never to `None` — only the *exchange* being unreachable is unknown.
- **M2 — Streak helper** `bot._equity_failure(venue)`: module-level
  `_venue_fail_streak`, increment → log with the count → at **5** (constitutional
  constant, Q4) send Telegram + `upsert_setting(auto_trade_{venue}, "false")` +
  reset. Success path in the loop sets the streak to 0. Testable directly.
- **M3 — Scalper guard** sits before `is_entry_blocked`, so an unknown-equity
  skip is recorded with its own reason (`equity_unavailable`), not misattributed.

## Testing

New `tests/test_kill_switch_integrity.py` (7 tests): equity `None` on transport
failure (both venues), whole-account valuation (USDT 10 + position 40 ⇒ 50),
streak discipline (5 consecutive disables + notifies exactly once; 4+reset+4
never does), live fail-closed skip with `equity_unavailable` recorded, dry-run
pool fallback still enters. Regression: full suite (107) — fakes already return
floats, unaffected.

## Out of scope

Everything in the spec's Out list; also the day-cache interaction in
`compute_slot_size` (correct equity now feeds it; the caching itself is 013+
territory).
