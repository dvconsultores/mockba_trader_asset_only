# Tasks: Kill-Switch Integrity (015)

**Prerequisites**: spec ✅ (Q1–Q4), plan ✅, checklist ✅, constitution v1.1.0 ✅

## Phase 1 — Executor (`trading_bot/executor.py`)

- [X] T001 `BinanceSpot.get_equity() -> float | None` (line 143): transport failure ⇒ `None`; success ⇒ USDT free+locked **+ Σ qty×entry_price over `load_all_positions(venue="binance")`** (M1; DB error degrades to USDT-only, logged). Extend the db_ops import with `load_all_positions`.
- [X] T002 `OrderlyFutures.get_equity() -> float | None` (line 473): failure ⇒ `None` (holding already reports total collateral — no valuation change).

## Phase 2 — bot.py

- [X] T003 Module state + helper: `_venue_fail_streak: dict[str,int]` and `_equity_failure(venue)` per plan M2 — increment, log `consecutive=N`, at 5 ⇒ Telegram alert + `upsert_setting("auto_trade_{venue}", "false")` + reset. The 5 is a constitutional constant (Q4).
- [X] T004 Equity block (line 386-393): `None` ⇒ `_equity_failure(venue); continue` (venue_state **not** written — AC2); success ⇒ streak reset + `set_venue_equity`.
- [X] T005 Remove the per-cycle `_venue_failures` dict (line 354), its per-asset increment (line 491 — keep the error log), and the end-of-cycle escalation loop (lines 532-537).

## Phase 3 — Scalpers (Q1)

- [X] T006 `spot_scalper.scalp_cycle` (line 227): `equity is None` ⇒ dry-run ⇒ `get_capital_pool(venue)`; live ⇒ `_log(..., "skipped", "equity_unavailable")` + return. Import `get_capital_pool`.
- [X] T007 `futures_scalper.scalp_cycle` (line 191): same.

## Phase 4 — Tests

- [X] T008 `tests/test_kill_switch_integrity.py`: AC1 ×2 (both venues None on failure), AC3+AC4 (whole-account valuation), AC5+AC6 (streak: 5 consecutive ⇒ disable + one alert; 4/reset/4 ⇒ never), AC8 (live fail-closed, reason recorded, no order), AC9 (dry-run pool fallback enters).
- [X] T009 Full regression — 107 existing + new, 0 failures.

## Phase 5 — Docs & converge

- [X] T010 CURRENT_STATE §0 (015) + CHANGELOG `fix:`; note that 012's 4×$20 plan is now fully realized (equal slot sizing).
- [X] T011 analyze.md consistency pass + converge.md.

⛔ **Not tasks**: touching `is_entry_blocked`'s formula, the market gate, `compute_slot_size` caching, dashboard, loop structure (013), `venue_fail_limit` as a setting (Q4: constitutional constant).
