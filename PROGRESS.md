# MockbaV4 — Phase 2 Progress

| Module | Branch | Status | Lines | AC |
|---|---|---|---|---|
| types | build/types | ✅ Done | 81 | N/A |
| 2.1 db/schema + db_ops | build/db | ✅ Done | 345 (104+241) | 5/5 |
| 2.2 pnl.py | build/pnl | ✅ Done | 154 | 8/8 |
| 2.3 regime.py | build/regime | ✅ Done | 202 | 5/5 |
| 2.4 executor.py | build/executor | ✅ Done | 473 | Core AC pass |
| 2.5 scalpers | build/scalpers | ✅ Done | 568 (262+306) | Core AC pass |
| 2.6 bot.py | build/bot | ✅ Done | 227 | Core AC pass |
| 2.7 dry-run | — | ⬜ Pending | Human | 48h |
| 2.8 research | — | ⬜ Pending | — | Post-2.7 |
| 2.9 cleanup | — | ⬜ Pending | — | Post-live |

**Total new code:** 2,050 lines (target ~1,570; includes DDL + types infrastructure)

## Line budget notes

- `db_ops.py`: 241 (+34%). Excess: backward compatibility for telegram.py + typed helpers.
- `executor.py`: 473 (+18%). Excess: two complete exchange API integrations in one module.
- `bot.py`: 227 (+13.5%). Within tolerance.
- All other modules within budget.

## Dry-run instructions

`bot.py` is ready for 48-hour dry-run. Before starting:
1. Ensure `dry_run = true` in settings.
2. Set `assets = "NEAR"` (and add ETH,SOL for observation if desired).
3. Set `auto_trade_binance = true` and/or `auto_trade_orderly = true`.
4. Verify `.env` has valid API keys (executor reads them, dry_run never calls).
5. Run: `python bot.py`
6. Monitor `logs/apolo.log` for structured log entries.
7. Query `signals` and `closed_trades` tables for results.

## GATE 2

Go-live is a human decision made against dry-run numbers.
Do NOT set `dry_run = false` without explicit approval.
