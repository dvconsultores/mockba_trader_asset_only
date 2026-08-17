# Tasks: Spot Exit Hardening — gap/crash protection (006)

**Input**: Design documents from `/specs/006-spot-exit-hardening/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/exit-reasons.md ✅, quickstart.md ✅, checklists/requirements.md ✅, constitution.md ✅

**Tests**: New `tests/test_spot_exit_hardening.py` (REQUIRED — plan.md Testing Strategy lists 14 tests covering AC1–AC12; mirrors `tests/test_amendment003.py` + `tests/test_market_check.py` fixture conventions).

**Organization**: Tasks grouped in dependency order: settings (schema → rules) → Part 1 universe cap → Part 2 crash guard → dashboard label → tests → docs → full-suite regression. Settings registration precedes the code that reads them (T001 before T003/T004); the crash guard is implemented before its tests (T004/T005 before T008); docs and the full-suite regression come last. (spec.md has no user stories — it is organized by parts, so no story labels, matching the `005` repo format.)

**Branch**: work directly on `develop` (repo convention — no feature branches, per spec Q7). Note: the repo currently tracks only `main` — verify/switch to `develop` per repo convention before implementing.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths + function names in descriptions
- Each task: id, title, file(s), what to do, definition of done / verification command

## Path Conventions

- Settings: `trade/settings_schema.py`; `trade/settings_rules.py`
- Universe cap: `trade/universe.py` (`scan_venue`)
- Crash guard: `trading_bot/spot_scalper.py` (`manage_open_positions`, `_close`)
- Dashboard: `dashboard/main.py` (`REASON_LABELS`, line 823)
- Tests: `tests/test_spot_exit_hardening.py` (new)
- Docs: `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`
- Run new tests: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q`
- Full regression: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`

---

## Phase 1: Settings — `trade/settings_schema.py` + `trade/settings_rules.py`

**Purpose**: register both settings so the Amendment 002 deterministic validator accepts them and the code that reads them (T003/T004) finds the keys in `BY_KEY`; defaults live in `get_setting_float` fallbacks — **no DB migration** (AC3, AC10). Must complete before any consumer.

- [X] T001 Register the two new `SettingSpec`s in `trade/settings_schema.py` `ALL` list — `universe_max_atr_pct` (float, group `"universe"`, unit `"%"`, hard 0.1–20, soft 0.5–5, short: "Max replay median ATR% for a spot universe candidate — crash-prone names above the cap never enter the universe") and `max_loss_per_position_pct` (float, group `"exit"`, unit `"%"`, hard 0.1–20, soft 1–5, short: "Crash-guard floor — market-sell a spot position when live price falls below entry × (1 − pct/100)", `depends_on=("sl_min_pct_spot",)`). Append to `ALL` only — `BY_KEY`/`GROUPS` auto-derive (schema lines 228–230). Do NOT touch `db/*` or `db/db_ops.py` (no migration).
  - *Verify*: quickstart Scenario 1 — `./venv/bin/python -c "from trade.settings_schema import BY_KEY; from db.db_ops import get_setting_float; [print(k, BY_KEY[k].type.__name__, get_setting_float(k, 1.5 if 'atr' in k else 3.0), BY_KEY[k].hard_min, BY_KEY[k].hard_max, BY_KEY[k].soft_min, BY_KEY[k].soft_max) for k in ('universe_max_atr_pct','max_loss_per_position_pct')]"` → both listed as `float`, hard 0.1–20, soft 0.5–5 / 1–5, defaults 1.5 / 3.0.

- [X] T002 Add the two cross-check blocks in `trade/settings_rules.py` `validate(key, value, ctx=None)` — (a) **hard error** when `key == "max_loss_per_position_pct"` and `value < get_setting_float("sl_min_pct_spot", get_setting_float("sl_min_pct", 0.5))`, mirroring the `tp_min_pct <= sl_min_pct` hard-error pattern (lines 85–90): message states the guard would pre-empt the spot stop; **equality (`==`) returns ok** — guard stays a pure gap-catcher (Constitution III, AC10). (b) **optional empty-universe warn** for `key == "universe_max_atr_pct"`: query the stored binance universe `MIN(atr_pct_median)` and warn "universe will be empty" when the cap is below it, mirroring the depth-multiple empty-universe warn (lines 228–257) — Constitution VIII guardrail (AC10).
  - *Verify*: quickstart Scenario 1 cross-check block (live DB has `sl_min_pct_spot = 0.6`): `validate('max_loss_per_position_pct', 0.5).level` → `error`, `0.6` → ok (equality), `3.0` → ok, `validate('universe_max_atr_pct', 1.5)` → ok; `validate_all()` passes with defaults.

**Checkpoint**: settings registered + validated — quickstart Scenario 1 green.

---

## Phase 2: Part 1 — Universe cap in `trade/universe.py` `scan_venue`

**Purpose**: spot-only, strictly-additive max-ATR hard filter (AC1–AC3). Reads the setting registered in T001. Independent of Phases 3/4 (different files) — parallelizable [P].

- [X] T003 [P] Add the spot-only max-ATR cap in `trade/universe.py` `scan_venue(venue, equity=None, depth_budget=None)` — immediately after the Stage-4 replay loop (metrics built at lines 656–671) and **before** Stage 5 `select_ranked` (call at line 677) / `replace_universe` (line 679), under an `if venue == "binance":` branch (the `hold_key` venue-branch precedent, line 651): read `max_atr = get_setting_float("universe_max_atr_pct", 1.5)` fresh; filter `checked` keeping candidates whose `metrics.get(c["asset"])` is `None` (replay failed) OR whose `m.get("atr_pct_median")` is `None` OR `m["atr_pct_median"] <= max_atr` — i.e. only genuinely high-ATR names are dropped, so `summary["dropped_by_max_atr"] = _before - len(checked)` counts real drops only (never inflated by replay failures). Missing/`None`-ATR candidates stay for the existing `select_ranked` `m is None` exclusion — the cap never loosens the Stage-2 volume/spread/rank/fundability filters (AC2). Surface the count in `_scan_summary_message` (line 782) so the Telegram scan notification reports `dropped_by_max_atr=N` (L1). Orderly/futures scans skip the block and never set the key (AC1, spec Q3).
  - *Verify*: quickstart Scenario 2 + plan tests 2/3/4 (T007) — with cap 1.5, only BICO-class names (atr_pct_median 1.86) are dropped while MMT (0.87), PUMP (0.60), GIGGLE, RE, CRV, ZAMA remain (research.md §1.4 calibration); `summary["dropped_by_max_atr"]` is set for binance only.

**Checkpoint**: a binance scan with cap 1.5 drops only BICO-class names; the summary reports the dropped count; the orderly universe is untouched.

---

## Phase 3: Part 2 — Crash guard in `trading_bot/spot_scalper.py`

**Purpose**: catastrophic-move guard (AC4–AC9). Same-file sequential: T004 then T005.

- [X] T004 [P] Implement the crash guard in `trading_bot/spot_scalper.py` `manage_open_positions(asset, exchange)` — **Change A**: replace the conditional price fetch (line 80, `live = exchange.get_price(asset) if any(pd.get("sl_price") for pd in positions) else None`) with `live = exchange.get_price(asset)` (unconditional — the floor applies to all positions, including `sl_price=None` ones saved by `_save_open`, AC4); read `mlp = get_setting_float("max_loss_per_position_pct", 3.0)` once before the `for pd in positions:` loop. **Change B**: add the guard-first, fill-aware block as the FIRST per-position check inside the loop (before the existing exchange-fill checks at lines 91–95): when `live is not None and live < ep * (1 - mlp / 100)` — (1) if `tpid` and `exchange.get_order_status(sym, tpid) == "FILLED"` → `_real_fill` + `_close(..., "tp", ...)`, `continue`; (2) if `slid` and `exchange.get_order_status(sym, slid) == "FILLED"` → `_close(..., "sl", ...)` at the stored `sl_price`, `continue`; (3) else cancel TP (verify `tp_cancel_ok`, on failure error-log `[EXIT] {asset} crash_guard: TP cancel failed — keeping position to retry` and `continue`) + cancel SL, `market_sell(asset, q)`; on `None` → reuse the existing no-balance/orphan recovery (real TP fill via `get_order_fills` → `"tp"`, else `"orphan"` at `live or ep`; balance ≥ qty → error-log keep-to-retry); on sell → `xp = sell.fill_price if sell.fill_price > 0 else ep` (dry-run `Fill(fill_price=0.0)` → `ep`, same convention as `sl`/`time_stop`) and `_close(asset,"binance","long",ep,xp,sp,q,pid,si,"crash_guard",fee_ep,sell.fee_amount)`, `continue`. `live is None` → skip the block entirely (Constitution IV, AC6). Above-floor positions fall through byte-for-byte unchanged (AC7). Structured single-line logs `[EXIT] asset=... reason=crash_guard ...`, no emoji.
  - *Verify*: plan tests 5–11 (T008) pass; `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q` after T008; quickstart Scenario 3 (dry-run) — same-cycle exit, reason `crash_guard`, real fill, cooldown stamped.

- [X] T005 Extend the `_close` cooldown stamping in `trading_bot/spot_scalper.py` `_close(a,v,s,ep,xp,sp,q,pid,si,rsn,fee_ep=0.0,fee_xp=0.0)` (line 164) — change line 166 from `if rsn == "sl":` to `if rsn in ("sl", "crash_guard"):` so a crash-guard exit stamps `_last_sl[f"{v}:{a}:{s}"] = time.time()` and blocks re-entry for `cooldown_sec × SL_COOLDOWN_MULT` (~10 min), identical to an `sl` exit (AC4, clarified Q2). One-line change only — fee fallbacks and `record_closed_trade`/`delete_position` untouched.
  - *Verify*: plan test 9 (T008) — after a `crash_guard` close, `spot_scalper._last_sl["binance:AAA:long"]` is set and `_cooldown_ok("AAA", "long", cs)` is False within `cs × SL_COOLDOWN_MULT`.

**Checkpoint**: guard-first, fill-aware crash exit live; cooldown stamped; normal TP/SL/time-stop exits byte-for-byte unchanged.

---

## Phase 4: Dashboard label — `dashboard/main.py` (independent, parallel)

- [X] T006 [P] Add the `crash_guard` label to `REASON_LABELS` in `dashboard/main.py` (line 823) — `REASON_LABELS = {"tp": "TP", "sl": "SL", "time_stop": "Time stop", "orphan": "Orphan", "crash_guard": "Crash guard"}` so the Closed Trades page (render at line 935) shows "Crash guard" instead of the uppercased raw fallback `CRASH_GUARD`. One-line change (AC11, `contracts/exit-reasons.md` Rule 4).
  - *Verify*: quickstart Scenario 6 — a `crash_guard` closed trade renders "Crash guard"; structural assertion `REASON_LABELS["crash_guard"] == "Crash guard"` in T009.

---

## Phase 5: Tests — `tests/test_spot_exit_hardening.py` (14 tests, AC1–AC12)

**Purpose**: mirror `tests/test_amendment003.py` (tmp-DB fixture via `db.db_ops.DB_PATH` monkeypatch + `initialize_database_tables`; network isolation via `mock.patch.object` on `trade.universe`) and `tests/test_market_check.py` (autouse fixture clearing module state). New pattern: fake `BinanceSpot`-shaped exchange (`get_price`, `get_order_status`, `cancel_order`, `market_sell`, `get_asset_balance`, `get_order_fills`) + positions seeded via `save_position`. Same file — sequential. Subjects (T003/T004/T005/T006) must be done first.

- [X] T007 Create `tests/test_spot_exit_hardening.py` — scaffold + settings + universe-cap tests: `db(tmp_path)` fixture (monkeypatch `db.db_ops.DB_PATH` to `tmp_path` and call `initialize_database_tables()`, restore after — mirror `test_amendment003.py` line 28); autouse fixture clearing `spot_scalper._last_sl`/`_last_entry` between tests; `test_settings_registered_with_defaults` (plan test 1 — both keys in `BY_KEY` with type/group/ranges; `get_setting_float(k, default)` returns 1.5/3.0 when unset, AC3/AC10); `test_universe_cap_rejects_high_atr_spot_only` (plan test 2 — `scan_venue("binance")` with patched `_fetch_candidates`/`_fetch_depth`/`replay_symbol`/`replace_universe`; candidates atr 2.1 / 0.6, cap 1.5 → only low-ATR stored, `summary["dropped_by_max_atr"] == 1`, AC1/AC3); `test_universe_cap_venue_branch_orderly_untouched` (plan test 3 — same candidates via `scan_venue("orderly")` → both stored, no drop key, AC1); `test_universe_cap_additive_only` (plan test 4 — low-ATR candidate failing `min_volume` still rejected, pre-existing filter intact, AC2).
  - *Verify*: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q` — these 4 pass.

- [X] T008 Extend `tests/test_spot_exit_hardening.py` — crash-guard tests with the fake exchange + `save_position` seeding: `test_crash_guard_fires_below_floor` (plan test 5 — entry 100, `max_loss_per_position_pct=3`, live 96 < floor 97, orders open → TP+SL cancelled, `market_sell` called, `closed_trades` row `exit_reason='crash_guard'` + real fill price, AC4/AC9); `test_crash_guard_fill_aware_sl_filled` (plan test 6 — below floor + SL status `FILLED` → reason `sl`, `market_sell` **not** called, AC5/AC8); `test_crash_guard_fill_aware_tp_filled` (plan test 7 — below floor + TP `FILLED` → reason `tp` with real fill via `get_order_fills`, no market sell, AC5/AC8/AC9); `test_crash_guard_none_price_no_action` (plan test 8 — `get_price → None` → no cancel, no sell, position kept, AC6); `test_crash_guard_stamps_cooldown` (plan test 9 — after a crash-guard close, `_last_sl["binance:AAA:long"]` set and `_cooldown_ok` False within `cs × SL_COOLDOWN_MULT`, AC4); `test_crash_guard_applies_to_no_sl_price_positions` (plan test 10 — `sl_price=None`, live below floor → guard fires, Change A, AC4).
  - *Verify*: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q` — 10 tests pass (quickstart Scenarios 3/4/5).

- [X] T009 Extend `tests/test_spot_exit_hardening.py` — invariance/validation/dry-run/dashboard tests: `test_above_floor_normal_exits_unchanged` (plan test 11 — above floor + live ≤ `sl_price` → existing `sl` path reason `sl`; TP FILLED → reason `tp`, AC7); `test_validation_hard_error_vs_equality` (plan test 12 — `sl_min_pct_spot=0.6`: `validate("max_loss_per_position_pct", 0.5)` → error, `0.6` → ok, `3.0` → ok; hard-range `0.05`/`25` → error; `validate_all` passes with defaults, AC10); `test_universe_cap_empty_universe_warn` (plan test 13 — seeded binance universe `MIN(atr_pct_median)=0.5`; `validate("universe_max_atr_pct", 0.3)` → warn, AC10); `test_dry_run_unchanged` (plan test 14 — dry-run `market_sell` returns `Fill(fill_price=0.0)` → crash-guard close falls back to `xp = ep`, same convention as `sl`/`time_stop`, no new order path, AC12); plus a structural `REASON_LABELS["crash_guard"] == "Crash guard"` assertion (`contracts/exit-reasons.md` Rule 4).
  - *Verify*: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q` — all 14 tests green.

**Checkpoint**: AC1–AC12 verified by unit tests; quickstart Scenarios 1–6 reproducible.

---

## Phase 6: Docs (AC11)

- [X] T010 [P] Update `docs/CURRENT_STATE.md` — new feature-006 section (dated 2026-08-12) describing: the spot-only `universe_max_atr_pct` cap (pinned to Stage-4 replay `atr_pct_median`, venue-branch in `scan_venue`, `dropped_by_max_atr` summary key, calibration impact — BICO removed / PUMP-MMT kept); the `max_loss_per_position_pct` crash guard (floor formula `entry × (1 − pct/100)`, guard-first/fill-aware ordering, `None`-price no-action, `crash_guard` exit reason + `_last_sl` cooldown stamping, no-balance/orphan recovery); the two new settings table (defaults 1.5 / 3.0, hard 0.1–20, soft 0.5–5 / 1–5, no migration); the `REASON_LABELS["crash_guard"]` entry.
  - *Verify*: a reader can reproduce both protections and both settings from the doc alone.

- [X] T011 [P] Add a `feat:` entry to `docs/CHANGELOG.md` — dated 2026-08-12, "Spot exit hardening — universe max-ATR cap + crash-guard floor (006)": the three parts, the two settings with defaults, the validator cross-checks (hard error vs equality), the `crash_guard` exit reason + dashboard label, and `tests/test_spot_exit_hardening.py`; per the `how-to-work-with-specs.md` convention (AC11).
  - *Verify*: entry at the top of the changelog, `feat:` prefix, references feature 006.

**Checkpoint**: docs updated — feature complete.

---

## Phase 7: Full-Suite Regression

- [X] T012 Final regression — run the full test suite from the project root: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`. All suites (`test_amendment003.py`, `test_market_check.py`, `test_closed_trades_page.py`, `test_spot_exit_hardening.py`) must pass with 0 errors — the 005 converge run left the suite fully green; do not introduce collection errors or regressions (AC12 — `dry_run` and normal exits unchanged).
  - *Verify*: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q` → 0 failed, 0 errors; quickstart Scenario 5 both commands green.

---

## ⛔ Out of Scope — MUST NOT Be Implemented

Explicitly excluded by spec.md (Scope / Out of Scope) and plan.md (Out of Scope). Do NOT create tasks or code for these:

- **Entry logic, TP/SL sizing, adaptive thresholds, and the OCO placement** — `trading_bot/executor.py` untouched.
- **Futures/DEX path** — `trading_bot/futures_scalper.py` and Orderly brackets untouched; no futures-side ATR cap (spec Q3; DEX uses exchange-side bracket stops).
- **DB schema change / migration** — `db/schema_v2.sql`, `db/migrations/`, `db/db_ops.py` untouched; new settings defaults live in `get_setting_*` fallbacks.
- **PUMP closed-trade #65 fee anomaly** (43% fee, −44% PnL) — separate execution/fill investigation (feature 005 Q8) — out of scope.
- **Dashboard/UI exposure of the two new settings** — the operator manages settings locally; only the `REASON_LABELS` line changes.
- **No imports from `trade/main.py` / `trade/signal_agent/`** (modules do not exist in this repo — Constitution VII); `dry_run` behavior unchanged.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Settings)**: T001 → T002 (rules reference the schema keys via `BY_KEY`; `validate_all` iterates `ALL`)
- **Phase 2 (Universe cap)**: depends on T001 (reads `universe_max_atr_pct`); parallel with Phases 3/4 (different files)
- **Phase 3 (Crash guard)**: depends on T001 (reads `max_loss_per_position_pct`); same-file sequential T004 → T005; parallel with Phases 2/4
- **Phase 4 (Dashboard)**: no dependencies — [P] anywhere
- **Phase 5 (Tests)**: depends on T003/T004/T005/T006 (subjects first) and T002 (validation tests); T007 → T008 → T009 sequential (same file)
- **Phase 6 (Docs)**: depends on implementation (T001–T006)
- **Phase 7 (Regression)**: after everything (T001–T011)

### Within Each Phase

- Settings before consumers: T001 (schema) → T002 (rules) → T003/T004 (readers)
- Universe: T003 alone (single-function change in `scan_venue`)
- Scalper: T004 (guard block in `manage_open_positions`) → T005 (`_close` cooldown) — same file
- Tests: scaffold + settings + universe (T007) → crash guard (T008) → invariance/validation/dry-run/dashboard (T009)
- Docs: `CURRENT_STATE.md` (T010) ∥ `CHANGELOG.md` (T011)
- Regression: T012 last

### Parallel Opportunities

- T003 (`trade/universe.py`) ∥ T004 (`trading_bot/spot_scalper.py`) ∥ T006 (`dashboard/main.py`) — different files, all after T001
- T010 ∥ T011 (docs) — different files
- All behavior testable without a live exchange (patch the network layer)

---

## Parallel Examples

```bash
# After T001: Part 1 + Part 2 + dashboard in parallel (different files)
Task: "T003 — trade/universe.py scan_venue max-ATR cap"
Task: "T004 — trading_bot/spot_scalper.py crash guard (then T005 _close)"
Task: "T006 — dashboard/main.py REASON_LABELS"

# Docs in parallel (different files)
Task: "T010 — docs/CURRENT_STATE.md"
Task: "T011 — docs/CHANGELOG.md"
```

---

## Implementation Strategy

### MVP First (Settings + Universe Cap + Crash Guard)

1. Phase 1 settings (T001–T002) — quickstart Scenario 1 green
2. Phases 2 + 3 + 4 in parallel (T003, T004→T005, T006)
3. **STOP and VALIDATE**: quickstart Scenario 2 (universe cap) + Scenario 3 (crash guard in dry-run) end-to-end
4. Phase 5 tests (T007–T009) → Phase 6 docs (T010–T011) → Phase 7 regression (T012)

### Incremental Delivery

1. Settings + universe cap (Part 1) → verify BICO-class names drop on the next binance scan
2. Crash guard (Part 2) → dry-run Scenario 3 → tests → docs
3. Full-suite regression; production enablement (adjusting the defaults) is an OPERATOR action, not a code task

### Parallel Team Strategy

1. Person A: T001 → T002 (settings, fast)
2. After T001: Person B → T003 (universe); Person C → T004→T005 (scalper); Person D → T006 (dashboard)
3. Together: T007→T009 (tests) → T010/T011 (docs) → T012 (regression)

---

## Notes

- [P] tasks = different files, no dependencies
- No user stories in spec.md — organized by the three parts + settings/tests/docs (matches the `005` repo format)
- Work directly on `develop` — no feature branches (spec Q7); the repo currently tracks only `main`, so confirm the branch convention before implementing
- Verify tests fail before implementing (fixed 14-test list in plan.md Testing Strategy)
- Commit after each task or logical group
- Do NOT touch executor / futures scalper / DB schema / PUMP #65 (see Out of Scope)
- Settings defaults live in `get_setting_*` fallbacks — no migration
- After implementation: run quickstart.md Scenarios 1–6
