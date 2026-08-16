# Tasks: Futures Exit Integrity (010)

**Input**: Design documents from `/specs/010-futures-exit-integrity/`

**Prerequisites**: spec.md ✅ (Clarified, Q1–Q6), plan.md ✅, constitution.md ✅ (v1.1.0)

**Tests**: NEW `tests/test_futures_exit_integrity.py` (REQUIRED — 14 tests, AC1–AC14). Fake `OrderlyFutures` per the `test_spot_exit_hardening.py` pattern; no network.

**Organization**: executor primitives first (the scalper calls them), then the exit paths, then tests, docs, regression.

**Branch**: `main` (repo convention).

## Path Conventions

- Executor: `trading_bot/executor.py` — `OrderlyFutures` class from line 399; `_post` 440, `place_entry` 482 (market body 519-524, sl_body 562-567, fill parse 531-533), `get_open_positions` 590, `get_order_status` 601, `cancel_order` 608 (junk POST at 610)
- Scalper: `trading_bot/futures_scalper.py` — `manage_open_positions` def 76, TP 86-88, SL 90-92, time stop 94-98, regime exit 100-106, `_close` 108-113
- Tests: `tests/test_futures_exit_integrity.py` (NEW)
- Feature tests: `./venv/bin/python -m pytest tests/test_futures_exit_integrity.py --basetemp=.pytest_tmp -q`
- Full regression: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`

---

## Phase 1: Executor primitives (AC13, AC14)

- [X] T001 `OrderlyFutures.market_close(asset, side, qty) -> Fill | None` in `trading_bot/executor.py`. `dry_run` short-circuit first (synthetic `Fill`, no HTTP — AC14), mirroring `place_entry` lines 487-496. Then `get_symbol_info`; POST `/v1/order` with `symbol`, `side` (`"SELL"` closing a long, `"BUY"` closing a short), `order_type: "MARKET"`, `order_quantity: _fmt(qty, info.base_tick)`, **`reduce_only: True`** (verified Q1 — `ccxt/woofipro.py:1464`), `client_order_id`. Non-200 ⇒ log a warning and return `None`. Parse `average_executed_price` / `executed_quantity` / `total_fee` exactly as `place_entry` does at 531-533.
- [X] T002 `OrderlyFutures.get_order_fills(order_id) -> tuple[float, float] | None`. `GET /v1/order/{order_id}`; return `(average_executed_price, total_fee)` when the price is > 0, else `None`. Mirrors `BinanceSpot.get_order_fills` (line 353). Never raises.
- [X] T003 Remove the junk `POST /v1/order` with `side: "CANCEL"` from `cancel_order` (line 610) plus its stale "Actually need to check…" comment. Keep the `DELETE /v1/order` exactly as-is. **One** request per cancel (AC13).
  - *Verify T001–T003*: `./venv/bin/python -c "import trading_bot.executor as e; o=e.OrderlyFutures(); print(hasattr(o,'market_close'), hasattr(o,'get_order_fills'))"` → `True True`

**Checkpoint**: the scalper has the primitives it needs.

---

## Phase 2: Exit paths (AC1–AC12)

- [X] T004 Add `_real_exit(exchange, order_id, fallback_price, qty)` helper to `trading_bot/futures_scalper.py` per plan M2 — returns `(price, fee)` from `get_order_fills`, else the fallback price and a fee derived from `dex_round_trip_fee_pct / 2`, logging a WARNING that the numbers are estimates (Constitution V audit trail).
- [X] T005 Change `_close` (def 108) to take explicit `fee_ep`, `fee_xp` and `opened_at` instead of the `fr` rate — matching `spot_scalper._close`. Pass the position's real `opened_at` (AC11) and real fees (AC12). Keep the `_last_sl` cooldown write for `sl`, and add `time_stop`-triggered closes to it only if the exit reason is `sl` (unchanged semantics).
- [X] T006 TP branch (86-88) and SL branch (90-92): replace `float(pd["tp_price"])` / `float(pd["sl_price"])` with `_real_exit(...)` (AC9, AC10).
- [X] T007 Time stop (94-98) → the plan M1 ladder: cancel TP, cancel SL, `market_close`, **verify** via `get_open_positions(asset)` empty, then record + delete (AC1, AC5, AC6). On a `None` fill or failed verification: **keep the DB row**, no `closed_trades` write, re-place the SL from the stored `sl_price`, log ERROR (AC2, AC3). If the SL re-placement also fails: `send_message` alert + `upsert_setting("auto_trade_orderly", "false")` (AC4, Q3).
- [X] T008 Regime exit (100-106) → plan M3: cancel the live TP, place the replacement at the breakeven price, and call `update_position` **only** if a new order id came back (AC7). On failure: DB unchanged, ERROR logged, SL untouched (AC8).
  - *Verify T004–T008*: covered by Phase 3; smoke: `./venv/bin/python -c "import trading_bot.futures_scalper"`

**Checkpoint**: no path ends a cycle with an unprotected position or deletes an unverified row.

---

## Phase 3: Tests (AC1–AC14)

- [X] T009 Create `tests/test_futures_exit_integrity.py` — `db` fixture (tmp DB), autouse state reset, and a scriptable fake `OrderlyFutures` recording `market_close` / `cancel_order` / `get_open_positions` / `place_entry` / `get_order_fills` calls, each independently forceable to fail.
- [X] T010 Time-stop happy path: `test_time_stop_closes_position` (AC1), `test_time_stop_records_real_fill` (AC6), `test_opened_at_is_real` (AC11).
- [X] T011 Time-stop failure paths: `test_time_stop_failed_close_keeps_position` (AC2), `test_time_stop_failed_close_replaces_sl` (AC3), `test_time_stop_unverified_close_keeps_position` (AC5), `test_double_failure_escalates` (AC4).
- [X] T012 Regime exit: `test_regime_exit_amends_exchange` (AC7), `test_regime_exit_failure_leaves_db` (AC8).
- [X] T013 Fills and fees: `test_tp_exit_uses_real_fill` (AC9), `test_sl_exit_uses_real_fill` (AC10), `test_fee_fallback_uses_setting` (AC12).
- [X] T014 Executor: `test_cancel_order_single_request` (AC13), `test_dry_run_places_nothing` (AC14).
  - *Verify*: `./venv/bin/python -m pytest tests/test_futures_exit_integrity.py --basetemp=.pytest_tmp -q` → all pass

---

## Phase 4: Docs (AC16)

- [X] T015 [P] `docs/CURRENT_STATE.md` — `## 0. Futures Exit Integrity (feature 010, 2026-08-15)`: the three Constitution III/V violations, the exit ladder, the verified Orderly contract (Q1 + its ccxt evidence), the Q3 escalation, and the manual `dry_run` checklist.
- [X] T016 [P] `docs/CHANGELOG.md` — `fix:` entry under `## 2026-08-15` recording the Constitution III/V repair.

---

## Phase 5: Regression (AC15)

- [X] T017 `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q` → 0 failed (88 + 14 new).

---

## ⛔ Out of Scope

- **Spot** exit paths — `spot_scalper` untouched (its `opened_at=0` and theoretical-SL defects stay open).
- `BinanceSpot`, entry logic, thresholds, sizing, leverage, regime detection, toxicity, kill switches, market gate, universe scanner, feature 009.
- `place_entry`'s dangling-TP-on-emergency-close defect; `_save_open` not storing `fee_entry` (converge items).
- DB schema/migration; new dependency (**ccxt is documentation only — never import it**); arming DEX.

---

## Dependencies

T001-T003 → T004-T008 → T009-T014 → T015-T016 → T017. T015 ∥ T016.
