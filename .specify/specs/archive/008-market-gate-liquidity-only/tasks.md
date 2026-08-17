# Tasks: Market Gate: Liquidity-Only Suspension (008)

**Input**: Design documents from `/specs/008-market-gate-liquidity-only/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/warn-escalation.md ✅, quickstart.md ✅, checklists/requirements.md ✅, constitution.md ✅

**Tests**: Update `tests/test_market_check.py` (REQUIRED — plan.md Testing Strategy: update the `test_debounce_transitions` regime block, update `test_settings_validation` to the 9-key list, add the NEW `test_warn_notifications_strong_only` covering AC11; notification assertions use the established `mock.patch("trading_bot.send_bot_message.send_message")` pattern from `test_transition_notifications_once`/`test_warn_lifecycle_notifications`).

**Organization**: Tasks grouped in dependency order: settings (schema only — **no `settings_rules.py` change**) → `trade/market_check.py` escalation → `bot.py` gate application → tests → docs → full-suite regression. Schema registration precedes the code that reads it (T001 before T002/T003); `bot.py` (T003) imports `_warn_is_strong` from `trade.market_check.py`, so it follows T002; docs and the full-suite regression come last. (spec.md has no user stories — it is organized by parts, so no story labels, matching the `005`/`006` repo format.)

**Branch**: work directly on `main` (repo convention — no feature branches, per spec Q6; the repo tracks only `main`).

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths + function names + line anchors in descriptions
- Each task: id, title, file(s), what to do, definition of done / verification command

## Path Conventions

- Escalation classifier: `trade/market_check.py` (`_warn_is_strong` def line 311, regime branch 326–327, docstring 312–316; `update_gate_state` def line 331, WARN branch 361–373, docstring WARN line 338–341)
- Gate application: `bot.py` (`_gate_apply` def line 676, settings dict 683–687, `update_gate_state` call 689–690, WARN lifecycle 701–712, `trade.market_check` import line 34; `get_setting_bool` already imported line 23)
- Setting registry: `trade/settings_schema.py` (gate group 197–216; insert after `market_gate_warn_liquidity_share` line 213, before `market_filter_enabled` line 215; bool template `market_gate_enabled` line 199)
- Tests: `tests/test_market_check.py` (`test_debounce_transitions` line 403, regime block 449–454; `test_transition_notifications_once` line 510; `test_warn_lifecycle_notifications` line 546; `test_settings_validation` line 659, key list 663–668, `validate_all` dict ≈681–687)
- Docs: `docs/CURRENT_STATE.md` (feature-005 gate section 81–189, "Debounce state machine" bullet 139–143, settings table 180–186, `## 0.` convention per 006/007), `docs/CHANGELOG.md` (`## 2026-08-12` at top)
- Run market-check tests: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`
- Full regression: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`

---

## Phase 1: Settings — `trade/settings_schema.py` (schema only)

**Purpose**: register the new bool setting so the Amendment 002 deterministic validator accepts it and the consumers (T002/T003) find it; default lives in the `get_setting_bool(..., False)` fallback — **no DB migration**, **no `settings_rules.py` change** (AC8). Must complete before any consumer.

- [X] T001 Register `market_gate_regime_escalates` in `trade/settings_schema.py` `ALL` list — insert **after** the `market_gate_warn_liquidity_share` row (line 213), **before** `market_filter_enabled` (line 215), exactly:
  ```python
      SettingSpec("market_gate_regime_escalates", bool, "gate", None, None, None, None, None,
                  "Regime-trending/unknown WARNs escalate to suspension when true (default false — liquidity-only suspension; the broad-market filter owns macro trends)"),
  ```
  Bool with no ranges / no `depends_on` — the exact `market_gate_enabled` shape (line 199). `BY_KEY`/`GROUPS` auto-derive; `market_gate_*` count becomes 8 → **9**. Do NOT touch `trade/settings_rules.py` (grep confirms no `market_gate` cross-check exists — a bool with no ranges passes the generic validator like `market_gate_enabled`), `db/*`, or `db/db_ops.py` (no migration).
  - *Verify*: quickstart Scenario 1 — `./venv/bin/python -c "
  from trade.settings_schema import BY_KEY
  from trade.settings_rules import validate
  from db.db_ops import get_setting_bool
  keys = sorted(k for k in BY_KEY if k.startswith('market_gate_'))
  print(len(keys), 'market_gate_* keys:', keys)
  s = BY_KEY['market_gate_regime_escalates']
  print(s.type.__name__, s.group, 'default', get_setting_bool('market_gate_regime_escalates', False))
  print(validate('market_gate_regime_escalates', True).level)   # ok
  print(validate('market_gate_regime_escalates', False).level)  # ok
  "` → `9 market_gate_* keys` (incl. `market_gate_regime_escalates`), `bool gate default False`, `ok` / `ok`.

**Checkpoint**: setting registered + validated — quickstart Scenario 1 green (9 keys).

---

## Phase 2: Part 1 — Escalation classification in `trade/market_check.py`

**Purpose**: `_warn_is_strong` stops treating regime WARNs as strong by default; regime escalation becomes setting-gated (AC1, AC2, AC7). Only the classifier + comments change — `_evaluate` verdict rules and `_gate_share_settings` are **untouched**. Depends on T001 (the settings dict key it reads). Sequential before T003 (bot.py imports `_warn_is_strong`).

- [X] T002 Change the regime branches in `_warn_is_strong` (`trade/market_check.py` def line 311, body 311–328) — (a) add `regime_escalates = settings.get("market_gate_regime_escalates", False)` next to `share_thr` (line 319); (b) change the unconditional regime branch at **lines 326–327** from `if r.startswith("regime_trending=") or r.startswith("regime_unknown="): return True` to `if (r.startswith("regime_trending=") or r.startswith("regime_unknown=")) and regime_escalates: return True` — regime WARNs are strong **only** when the setting is truthy; (c) rewrite the docstring (312–316): strong = `liquidity_partial=` ≥ `market_gate_warn_liquidity_share`, or regime WARNs only when `market_gate_regime_escalates` is true (default false — liquidity-only suspension; the broad-market filter owns macro trends); a lone bad asset (small `liquidity_partial`) is mild. The liquidity branch (321–325) is **untouched** (byte-for-byte). Also update `update_gate_state` (def 331): the WARN-branch comment (362–364) and the docstring WARN line (338–341) to describe liquidity-only-by-default + the `market_gate_regime_escalates` re-enable path — **no logic change** in `update_gate_state` (strong → `bad_streak`+1/suspend, mild → reset both streaks stays). `_evaluate` (line 27) and `_gate_share_settings` (line 240) must remain byte-for-byte unchanged (AC2).
  - *Verify*: quickstart Scenario 2/3 logic — `./venv/bin/python -c "
  from trade.market_check import _warn_is_strong as w
  print(w(['regime_trending=1.00'], {}),                          # False (mild by default)
        w(['regime_unknown=1.00'], {'market_gate_regime_escalates': True}),  # True (re-enable)
        w(['liquidity_partial=0.33'], {}),                        # True (strong, unchanged)
        w(['liquidity_partial=0.11'], {}))                        # False (mild, unchanged)
  "` → `False True True False`.

**Checkpoint**: classifier is liquidity-only by default; re-enable path works; liquidity strong/mild unchanged.

---

## Phase 3: Part 2 — `bot.py` `_gate_apply` (strong-only notifications)

**Purpose**: read the new setting fresh each cycle and gate the ⚠️/✅ WARN lifecycle on the same `_warn_is_strong` classification (clarify Q1 — pinned mechanism, AC11). Depends on T001 (setting) and T002 (imported `_warn_is_strong`).

- [X] T003 Update `bot.py` `_gate_apply` (def line 676): (a) **import** — extend the `trade.market_check` import at **line 34** to `from trade.market_check import check_venue_observed, update_gate_state, _warn_is_strong`; (b) **settings dict** (683–687) — add `"market_gate_regime_escalates": get_setting_bool("market_gate_regime_escalates", False),` (read fresh each evaluation alongside `market_gate_warn_liquidity_share`; `get_setting_bool` already imported at line 23) — this same dict already feeds `update_gate_state` at 689–690; (c) **WARN lifecycle** (701–712) — compute `warn_strong = _warn_is_strong(report.get("reasons"), settings)` before the block and gate the ⚠️/`warn_start` branch: line 703 becomes `if report["verdict"] == "WARN" and warn_strong and not state.get("warn_active"):` so ⚠️ (line 705) + `warn_active=True` fire **only** for strong/escalating WARNs; the clearing branch (707–711) is **unchanged** (a previously-notified strong WARN still gets ✅ "warning cleared" on PASS; FAIL silently clears the flag); mild regime WARNs never set `warn_active`, never send ⚠️/✅ — only the existing `action=hold` log line (701–712). Update the `_gate_apply` docstring (677–682) and the lifecycle comment (701–702) to state ⚠️/✅ fires only for strong/escalating WARNs (mild regime WARNs are log-only). **Nothing else in bot.py changes** — cadence, streaks, debounce mechanics, cold-start warmup, `_record_gate_skip`, `_broad_market_downtrend`, entry blocking all untouched. No new report field (pinned mechanism).
  - *Verify*: covered by T006 (AC11) — run `./venv/bin/python -m pytest tests/test_market_check.py::test_warn_notifications_strong_only --basetemp=.pytest_tmp -q` after T006; plus quickstart Scenario 5 table: `liquidity_partial=0.33` → fires, `0.11` → none, regime WARN default → none, regime WARN + setting true → fires.

**Checkpoint**: ⚠️/✅ fire only for strong/escalating WARNs; `warn_active` lifecycle consistent with the state-machine classification by construction.

---

## Phase 4: Tests — `tests/test_market_check.py` (AC1, AC3–AC8, AC11)

**Purpose**: assert the new default (regime WARNs mild), the re-enable path, the 9-key list, and the strong-only notification scope; the unchanged liquidity tests remain the regression guards. Same file → sequential (T004 → T005 → T006). Depends on T001–T003. **Do NOT change** `test_transition_notifications_once` (line 510) or `test_warn_lifecycle_notifications` (line 546) — their liquidity WARNs (`liquidity_partial=0.25`/`0.33`) stay **strong** and double as regression guards (AC3–AC5, AC11).

- [X] T004 Update `test_debounce_transitions` in `tests/test_market_check.py` (def line 403; regime block **449–454**) — replace the "regime WARNs (trending/unknown) are always strong" block with: (a) **mild by default** — with the existing local `settings = {"market_gate_bad_streak": 2, "market_gate_good_streak": 2}` (no `regime_escalates` key → `settings.get(..., False)` default applies), 5× repeated `regime_trending=1.00` / `regime_unknown=1.00` WARNs reset both streaks (`bad_streak == 0 and good_streak == 0`) and never suspend; (b) **re-enable regression (AC7)** — `settings_regime = dict(settings, market_gate_regime_escalates=True)`: `regime_trending=1.00` → `bad_streak` 1, then `regime_unknown=1.00` → `{"type": "suspend"}` (005 behavior restored). The strong (`liquidity_partial=0.33`) and mild (`liquidity_partial=0.11`) liquidity blocks above stay byte-for-byte (no regime reasons involved).
  - *Verify*: `./venv/bin/python -m pytest tests/test_market_check.py::test_debounce_transitions --basetemp=.pytest_tmp -q` → pass.

- [X] T005 Update `test_settings_validation` in `tests/test_market_check.py` (def line 659) — (a) key list (663–668): add `"market_gate_regime_escalates"` → **9** `market_gate_*` keys, all group `gate`; (b) valid-value asserts: add `assert validate("market_gate_regime_escalates", True).level == "ok"` and `assert validate("market_gate_regime_escalates", False).level == "ok"` (bool, no ranges → no hard-range violation cases, same shape as `market_gate_enabled`); (c) `validate_all` dict (≈681–687): add `"market_gate_regime_escalates": "false"`. AC8.
  - *Verify*: `./venv/bin/python -m pytest tests/test_market_check.py::test_settings_validation --basetemp=.pytest_tmp -q` → pass.

- [X] T006 Add `test_warn_notifications_strong_only` to `tests/test_market_check.py` (new test, AC11) — follow the established pattern from `test_transition_notifications_once`/`test_warn_lifecycle_notifications` (line 510/546): clear `botmod._gate_state`/`_last_gate_eval`, patch `trading_bot.send_bot_message.send_message` via `mock.patch`. Assert: (a) **mild regime WARN default** — `botmod._gate_apply("binance", {"verdict": "WARN", "reasons": ["regime_trending=1.00"]})` with `market_gate_regime_escalates` unset → `send_message` **never** called, `warn_active` stays unset; a subsequent `PASS` fires **no** ✅ "warning cleared"; (b) **re-enable** — `db.upsert_setting("market_gate_regime_escalates", "true")` → the same regime WARN fires exactly one ⚠️ (`"WARNING"` in the message, reason present) and sets `warn_active`; a second identical WARN fires nothing (debounced); a following `PASS` fires exactly one ✅ "warning cleared" and clears `warn_active`; (c) **liquidity regression guard** — a strong liquidity WARN (`liquidity_partial=0.25`) still notifies (unchanged path). Optionally reset the setting after the test (`db.upsert_setting("market_gate_regime_escalates", "false")`) to avoid cross-test leakage.
  - *Verify*: `./venv/bin/python -m pytest tests/test_market_check.py::test_warn_notifications_strong_only --basetemp=.pytest_tmp -q` → pass.

**Checkpoint**: AC1, AC3–AC8, AC11 verified by unit tests; unchanged liquidity tests (`test_transition_notifications_once`, `test_warn_lifecycle_notifications`) still pass as regression guards.

---

## Phase 5: Docs (AC9, AC10)

**Purpose**: reflect the liquidity-only suspension in the feature-005 gate section + a new feature-008 section + a `fix:` changelog entry. Depends on implementation (T001–T003). Parallelizable [P] (different files).

- [X] T007 [P] Update `docs/CURRENT_STATE.md` — (a) **feature-005 gate section** (lines 81–189): rewrite the "Debounce state machine" bullet (lines 139–143, currently the pre-follow-up "WARN = neutral hold") to state: strong WARNs (`liquidity_partial` ≥ `market_gate_warn_liquidity_share`) count toward `bad_streak` and suspend; **regime WARNs are informational by default (feature 008 — liquidity-only suspension)**; `market_gate_regime_escalates=true` re-enables the 005 regime escalation; ⚠️/✅ fire only for strong/escalating WARNs. Add the `market_gate_regime_escalates` row to the settings table (lines 180–186): `| market_gate_regime_escalates | bool | false | — | — |`. (b) **New `## 0. Market Gate: Liquidity-Only Suspension (feature 008, 2026-08-12)`** top-level section at the top of the file (following the 006/007 `## 0.` convention — lines 8–12/28–79): the escalation-rule change (`_warn_is_strong` regime branch conditioned on the new setting, `_evaluate` verdicts unchanged), the new setting (bool, default false, no migration), the strong-only ⚠️/✅ notification scope (clarify Q1), motivation (08-11, 3,709 skips), constitution notes (VIII restores throughput; III/IV/V unaffected).
  - *Verify*: a reader can reproduce the liquidity-only rule, the re-enable path, and the notification scope from the doc alone.

- [X] T008 [P] Add a `fix:` entry to `docs/CHANGELOG.md` under the existing `## 2026-08-12` section (top of file): "Market gate: liquidity-only suspension (008) — regime-trending/unknown WARNs are informational by default (mild: never count toward `bad_streak`, no ⚠️ notification); new `market_gate_regime_escalates` (default false) re-enables the 005 regime escalation; ⚠️/✅ notification pair fires only for strong/escalating WARNs. Corrects the 08-11 over-blocking (3,709 entry skips) — Constitution VIII." Per the `how-to-work-with-specs.md` convention (existing `fix:` entries under 2026-08-09 set the format).
  - *Verify*: entry at the top of the changelog under `## 2026-08-12`, `fix:` prefix, references feature 008.

**Checkpoint**: docs updated — feature complete.

---

## Phase 6: Full-Suite Regression

- [X] T009 Final regression — run the full test suite from the project root: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`. All suites (`test_amendment003.py`, `test_market_check.py`, `test_closed_trades_page.py`, `test_dashboard_settings_readonly.py`, `test_spot_exit_hardening.py`) must pass with 0 errors — no collection errors or regressions (AC12 — `dry_run`, exits, verdicts unchanged). Run the market-check suite first per the user's verification order: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`, then the full suite.
  - *Verify*: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q` → 0 failed; `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q` → 0 failed, 0 errors; quickstart Scenario 6 both commands green.

---

## ⛔ Out of Scope — MUST NOT Be Implemented

Explicitly excluded by spec.md (Scope / Out of Scope) and plan.md (Out of Scope). Do NOT create tasks or code for these:

- **`_evaluate` verdict rules** (`trade/market_check.py` lines 36–63) and `_gate_share_settings` (line 240) — `regime_trending=`/`regime_unknown=` still downgrade PASS→WARN (informational); no change.
- **Gate cadence, streak/debounce thresholds, cold-start warmup, observation recording, entry-block mechanics, `/market` report, `format_report`** — untouched.
- **Broad-market filter** (`_broad_market_downtrend` in `bot.py`), per-asset regime gating, stale-universe guard, spread-degradation guard, kill switches — untouched.
- **`market_gate_fail_share` / `trend_share` / `unknown_share` / `warn_liquidity_share` defaults and semantics** — untouched.
- **`trade/settings_rules.py`** — NO change (confirmed: no `market_gate` cross-check; bool with no ranges passes the generic Amendment 002 validator).
- **DB schema change / migration** — `db/schema_v2.sql`, `db/migrations/`, `db/db_ops.py` untouched; the new default lives in the `get_setting_bool(..., False)` fallback.
- **Notification debounce mechanics** (one message per transition/lifecycle) — unchanged; only the WARN **trigger scope** narrows to strong/escalating WARNs (clarify Q1). No new report field (`contracts/market-report.md` unchanged).
- **No imports from `trade/main.py` / `trade/signal_agent/`** (modules do not exist in this repo — Constitution VII); `dry_run` behavior unchanged.
- **Do NOT modify** `test_transition_notifications_once` (line 510) / `test_warn_lifecycle_notifications` (line 546) — their liquidity WARNs stay strong (regression guards).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Settings)**: T001 alone (one schema row — no rules change)
- **Phase 2 (Escalation)**: depends on T001 (reads `market_gate_regime_escalates`); sequential before T003 (bot.py imports `_warn_is_strong`)
- **Phase 3 (bot.py)**: depends on T001 (setting) + T002 (imported classifier); sequential after T002 — same logical change, verify together
- **Phase 4 (Tests)**: depends on T001–T003 (subjects first); T004 → T005 → T006 sequential (same file)
- **Phase 5 (Docs)**: depends on implementation (T001–T003)
- **Phase 6 (Regression)**: after everything (T001–T008)

### Within Each Phase

- Schema before consumers: T001 (schema) → T002/T003 (readers)
- Classifier before importer: T002 (`market_check.py`) → T003 (`bot.py`)
- Tests: regime block (T004) → settings list (T005) → new notification test (T006) — same file
- Docs: `CURRENT_STATE.md` (T007) ∥ `CHANGELOG.md` (T008)
- Regression: T009 last

### Parallel Opportunities

- T007 (`docs/CURRENT_STATE.md`) ∥ T008 (`docs/CHANGELOG.md`) — different files
- T004 / T005 / T006 are same-file → sequential, but logically independent edits (can be one commit)
- All behavior testable without a live exchange (tmp-DB fixture + `send_message` mock — established pattern)

---

## Parallel Examples

```bash
# After T001–T003: docs in parallel (different files)
Task: "T007 — docs/CURRENT_STATE.md (feature-005 gate section + feature-008 section)"
Task: "T008 — docs/CHANGELOG.md (fix: entry under 2026-08-12)"

# After T001–T003: tests in one working session (same file, sequential)
Task: "T004 — tests/test_market_check.py::test_debounce_transitions regime block"
Task: "T005 — tests/test_market_check.py::test_settings_validation 9-key list"
Task: "T006 — tests/test_market_check.py::test_warn_notifications_strong_only (new)"
```

---

## Implementation Strategy

### MVP First (Settings + Escalation + Gate Application)

1. Phase 1 settings (T001) — quickstart Scenario 1 green (9 keys, ok/ok)
2. Phase 2 escalation (T002) — classifier verify: `False True True False`
3. Phase 3 gate application (T003) — ⚠️/✅ strong-only
4. **STOP and VALIDATE**: quickstart Scenarios 2/3/4/5 logic end-to-end
5. Phase 4 tests (T004–T006) → Phase 5 docs (T007–T008) → Phase 6 regression (T009)

### Incremental Delivery

1. Setting + classifier (T001–T002) → verify `_warn_is_strong` mild-by-default/re-enable
2. Gate application (T003) → verify strong-only notifications
3. Tests (T004–T006) → docs (T007–T008) → full-suite regression (T009)
4. Production enablement (operator sets `market_gate_regime_escalates` in the DB) is an OPERATOR action, not a code task

### Parallel Team Strategy

1. Person A: T001 (fast, one schema row)
2. Person A: T002 → T003 (classifier + gate application — same logical change, sequential)
3. Person B: T004–T006 (tests, after T001–T003)
4. Person C: T007 ∥ T008 (docs, after T001–T003)
5. Together: T009 (full regression)

---

## Notes

- [P] tasks = different files, no dependencies
- No user stories in spec.md — organized by parts + settings/tests/docs (matches the `005`/`006` repo format)
- Work directly on `main` — no feature branches (spec Q6); the repo tracks only `main`
- Verify tests fail before implementing (fixed expectations in plan.md Testing Strategy)
- Commit after each task or logical group (T002+T003 together is fine — same logical change)
- Do NOT touch `_evaluate` / `_gate_share_settings` / `settings_rules.py` / DB schema / report contract / the two unchanged notification tests (see Out of Scope)
- Setting default lives in the `get_setting_bool(..., False)` fallback — no migration
- After implementation: run quickstart.md Scenarios 1–6
