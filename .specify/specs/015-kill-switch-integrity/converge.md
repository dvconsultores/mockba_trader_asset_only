# Converge: Kill-Switch Integrity (015)

**Date**: 2026-08-16 | **Status**: Implemented — 11/11 tasks, **115/115 tests green** (107 + 8 new)
**Cycle**: specify → clarify (Q1–Q4) → plan → checklist → tasks → analyze → implement → converge — all steps run, in order, this time.

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 Unknown is None (both venues) | ✅ | `test_binance_equity_none_on_failure`, `test_orderly_equity_none_on_failure` |
| 2 Cache never poisoned | ✅ by construction | `set_venue_equity` sits behind the success branch (bot.py equity block); reviewed in analyze.md |
| 3 Daily-loss limit stays armed | ✅ | `test_daily_loss_limit_stays_armed` — $50 equity, −$1.10 day, 2% limit fires |
| 4 Whole-account equity | ✅ | `test_equity_includes_open_positions` — USDT 10 + position 40 ⇒ 50 |
| 5 Streak is consecutive | ✅ | `test_five_consecutive_failures_disable_and_notify` + `test_success_resets_streak` (4/reset/4 never trips) |
| 6 Escalation notifies | ✅ | Same test — `send_message` exactly once, `auto_trade_binance=false` |
| 7 Per-asset errors don't escalate | ✅ by removal | The increment is gone from the per-asset `except`; comment records Q3 |
| 8 Live fails closed | ✅ | `test_live_unknown_equity_fails_closed` — no order, `equity_unavailable` recorded |
| 9 Dry-run pool fallback | ✅ | `test_dry_run_falls_back_to_declared_pool` |
| 10 Tests | ✅ | 8 new; 115 total green |
| 11 Docs | ✅ | CURRENT_STATE §0 (015), CHANGELOG `fix:` |

## Deviations from plan

None of substance. One judgement call worth recording: the Binance equity
method treats a **DB read failure** during position valuation as
degrade-to-USDT-only (logged), not `None` — only the *exchange* being
unreachable is unknown state. This is the direction-safe choice (equity can
only be understated, never fabricated) and was pinned in plan M1.

## Effects worth watching after deploy

- **First cycle after restart**: equity jumps from ~free-USDT to whole-account
  (~$100 when 4 slots deploy) — slot sizing and pct limits immediately compute
  against real capital. Intended (012 finally fully realized), but the log
  line `[STARTUP] validation …` and the dashboard capital view will show the
  step change.
- **During a genuine Binance outage**: entries stop venue-wide within one
  cycle (fail closed), the venue disables with a Telegram alert after ~2.5 min
  (5 × 30s), and `venue_state` keeps the pre-outage equity for the market
  gate. Recovery: the operator re-enables `auto_trade_binance` — same manual
  step as before, now with notice.

## Remaining queue

- **013 — Loop latency / bookTicker snapshot**: the next spec, as its own
  release after this batch deploys and baselines (per the two-step plan the
  operator approved).
- **014 — Constitution VII re-baseline**: hot path now ≈ 2,810 lines vs the
  1,500 budget; every plan since 009 carries a justification ritual.
- Audit items still open: 5 (dead toxicity inputs — folds naturally into 013,
  which touches the same per-cycle data), 8 (`NOTIONAL` filter key), 9
  (replay 1000-kline cap), 10 (WAL), 11 (dashboard CORS/auth).
- 010 leftovers: futures `fee_entry` at `_save_open`; `place_entry` dangling
  TP on emergency close.

## Deploy reminder (whole batch: 009, 010, 011, 012, 015)

Image first (commit → push `main` → Actions → Watchtower), confirm restart,
then `push-db.sh`. The DB carries: migration 009, wide-stop values, `tp_k=1.2`,
`max_loss_per_position_pct=3.0`, and the $100 capital plan (4 × 20%).
