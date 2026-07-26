# MockbaV4 — Phase 2 Progress

| Module | Branch | Status | Lines | AC |
|---|---|---|---|---|
| types | build/types | ✅ Done | 81 | N/A (no logic) |
| 2.1 db/schema + db_ops | build/db | ✅ Done | 241 | 5/5 |
| 2.2 pnl.py | — | ⬜ Pending | — | — |
| 2.3 regime.py | — | ⬜ Pending | — | — |
| 2.4 executor.py | — | ⬜ Pending | — | — |
| 2.5 scalpers | — | ⬜ Pending | — | — |
| 2.6 bot.py | — | ⬜ Pending | — | — |
| 2.7 dry-run | — | ⬜ Pending | — | — |
| 2.8 research | — | ⬜ Pending | — | — |
| 2.9 cleanup | — | ⬜ Pending | — | — |

## 2.1 notes

Line budget: 241 (target ~180). Excess explained: backward compatibility for
telegram.py (~50 lines for settings CRUD + asset list helpers) + typed setting
helpers (~25 lines) consumed by every downstream module. Infrastructure cost,
not feature scope creep.
