# Tasks: Dashboard Settings Read-Only (007)

**Input**: Design documents from `/specs/007-dashboard-settings-readonly/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/miniapp-api.md ✅, quickstart.md ✅, checklists/requirements.md ✅, constitution.md ✅

**Tests**: New `tests/test_dashboard_settings_readonly.py` (REQUIRED — plan.md Testing Strategy lists 5 tests covering AC4/AC5/AC7/AC9; mirrors `tests/test_closed_trades_page.py` fixture conventions — FastAPI `TestClient`, throwaway SQLite via `monkeypatch.setattr(api, "DB_PATH", …)`).

**Organization**: Tasks grouped in dependency order: backend (main.py guard) → frontend (MiniSettings.tsx) → tests → docs → full-suite regression. Backend and frontend touch **different files** and are **independent → parallelizable**; tests depend on the backend (T002) only. Docs come last; the full-suite regression is the final task. (spec.md has no user stories — it is organized by parts, so no story labels, matching the `006` repo format.)

**Branch**: work directly on `main` (repo convention — spec Q6 / plan: the repo tracks only `main`, no feature branches; verified: current branch is `main`).

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths + function names in descriptions
- Each task: id, title, file(s), what to do, definition of done / verification command

## Path Conventions

- Backend: `dashboard/main.py` (`api_miniapp_update`, `_get_db_rw`); `GET /api/miniapp` unchanged
- Frontend: `dashboard-ui/src/MiniSettings.tsx` (module globals lines 7–9, `editable` line 350, `__ping__` probe effect lines 367–373, header subtitle line 453, footer line 515)
- Tests: `tests/test_dashboard_settings_readonly.py` (new)
- Docs: `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`
- Run new tests: `./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q`
- Full regression: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`
- UI verification (no UI test command exists — `dashboard-ui/package.json` scripts are `dev`/`build`/`preview` only): `cd dashboard-ui && npx tsc --noEmit`

---

## Phase 1: Backend — `dashboard/main.py` `POST /api/miniapp` write path locked (AC4, AC5)

**Purpose**: keep the `__ping__` no-write auth probe byte-for-byte (CapitalManager + StatusBar browser-session detection depend on it); any real **non-capital** setting key returns 403 `"settings are read-only — manage via local DB push"` with **no DB write**; the four **capital keys** (`capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct`) keep the existing authenticated write path (auth → `INSERT … ON CONFLICT … DO UPDATE` → post-save `validate_all` → Telegram alert) so the Capital page stays editable (AC7 — grounded in `research.md` §2.3: CapitalManager writes these keys through this same endpoint; there is no `POST /api/capital`). `GET /api/miniapp` (`api_miniapp_get`, lines 536–549) is **unchanged**. Same-file sequential: T001 → T002.

- [X] T001 Add the module-level allow-list `CAPITAL_SETTING_KEYS = {"capital_cex_usdt", "capital_dex_usdc", "cex_slot_pct", "dex_slot_pct"}` in `dashboard/main.py` beside `_get_db_rw` (line 529), with the comment "Capital settings are still edited in the Capital Manager UI (AC7); every other settings key is read-only — managed via local DB push." Do NOT touch the endpoint yet. Do NOT remove `_get_db_rw` (still used by `_upsert_setting`, the blacklist endpoint, `/api/bot/control` and the retained capital write path — no dead-code cleanup).
  - *Verify*: `./venv/bin/python -c "from dashboard.main import CAPITAL_SETTING_KEYS; print(sorted(CAPITAL_SETTING_KEYS))"` → exactly the 4 capital keys.

- [X] T002 Insert the 403 read-only guard in `dashboard/main.py` `api_miniapp_update` (POST /api/miniapp, lines 552–620) and update the docstring (line 554). Keep the missing-key 400 guard (lines 558–560) and the `__ping__` branch (lines 561–572) **byte-for-byte**. Immediately **after** the `__ping__` branch and **before any auth or DB write**, add:
  ```python
  if key not in CAPITAL_SETTING_KEYS:
      # Settings are managed locally via DB push; the dashboard is read-only.
      raise HTTPException(status_code=403,
                          detail="settings are read-only — manage via local DB push")
  ```
  Pinned message: `settings are read-only — manage via local DB push` (distinct from `"Invalid auth"`; no auth needed — non-capital keys write nothing, so the 403 is deterministic for any caller). Keep the capital-key write path (lines 574–620: Telegram-initData + admin-session auth, `INSERT … ON CONFLICT … DO UPDATE`, post-save `validate_all`, `_send_validation_alert`, `return {"ok": True, "key":…, "value":…, "alerts":…}`) **verbatim**. Update the docstring to describe the split semantics (settings keys read-only 403; capital keys keep the write path; `__ping__` is a no-write auth probe).
  - *Verify*: `git diff dashboard/main.py` shows only the constant (T001), the guard + docstring; the `__ping__` branch and capital write path are unchanged; `GET /api/miniapp` untouched. Spot-check via quickstart Scenario 4 (curl against a scratch DB copy).

**Checkpoint**: backend locked — a settings key POST returns 403 with no write; `__ping__` and capital keys behave exactly as before; `GET /api/miniapp` unchanged.

---

## Phase 2: Frontend — `dashboard-ui/src/MiniSettings.tsx` settings always read-only (AC1–AC3)

**Purpose**: `editable` is forced permanently `false`; the `isTelegram`/`__ping__` edit-enablement (module globals + probe effect) is removed; the existing `disabled={!editable}` read-only rendering (preset `ComboList` line 473, `NumberField` line 496, `TextField` line 507) is kept **unchanged**; header/footer copy updated to "managed via DB". `dashboard-ui/src/CapitalManager.tsx`, `StatusBar.tsx`, `telegram.py`, `bot.py` are **untouched**. Independent of Phase 1 — different file → parallelizable [P]. Same-file sequential: T003 → T004.

- [X] T003 [P] Remove the edit-enablement machinery in `dashboard-ui/src/MiniSettings.tsx`: (1) delete the module globals `const isTelegram = !!TG?.initData`, `let browserSessionChecked = false`, `let browserSessionValid = false` (lines 7–9) — **keep** line 6 `const TG = …` (still used at line 362 `TG.ready()/TG.expand()` and line 386 `TG?.initData` in `saveSetting`); (2) change line 350 `const [editable, setEditable] = useState(isTelegram)` → `const editable = false` (no `useState` — `useState`/`useEffect`/`useCallback`/`useRef` imports stay: settings/saving/errors/loaded states, the settings-load effect at line 356, and the inert save plumbing remain); (3) remove the entire `__ping__` probe `useEffect` (lines 367–373) that POSTs `{ key: '__ping__', value: '' }` and calls `setEditable(r.ok)`. Keep the read-only rendering `disabled={!editable}` (lines 473/496/507) **exactly as-is**; keep `saveSetting`/`debouncedSave`/`selectAndSave`, the `saving`/`errors` states and the `timers` ref **inert** (unreachable once inputs are disabled — minimal-change policy, `tsconfig` `noUnusedLocals: false` → no build errors; do NOT delete).
  - *Verify*: no remaining `isTelegram`/`browserSessionChecked`/`browserSessionValid`/`setEditable` references and no `__ping__` POST in `MiniSettings.tsx`; `cd dashboard-ui && npx tsc --noEmit` → 0 errors.

- [X] T004 [P] Update the copy in `dashboard-ui/src/MiniSettings.tsx` to state read-only + managed via DB, preserving styling: (1) header subtitle (line 453) `{editable ? 'Tap a value to change it' : 'Read-only outside Telegram'}` → static `Settings are read-only — managed via DB` inside the existing `<p className="text-xs text-[#7a7090] mt-0.5">`; (2) footer (line 515) `{editable ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}` → static `Settings are read-only — manage via local DB push`. The read-only badge (lines 455–457) already renders `'read-only'` in the false branch — keep **unchanged**. Re-check the `lucide-react` import (line 2) after T003: `Settings2` (line 440 loading spinner), `Save` (404), `Check` (333/406), `AlertCircle` (405), `Star` (330), `ChevronDown` (294), `X` (315) are **all still used** → keep the import line unchanged (verified; no import removal).
  - *Verify*: `cd dashboard-ui && npx tsc --noEmit` → 0 errors (and optionally `npm run build` passes); grep shows the two static strings; styling classes untouched.

**Checkpoint**: Settings tab visible and fully read-only in both browser and Telegram; no `POST /api/miniapp` fired by MiniSettings; `tsc` clean.

---

## Phase 3: Tests — `tests/test_dashboard_settings_readonly.py` (5 tests, AC4/AC5/AC7/AC9)

**Purpose**: mirror `tests/test_closed_trades_page.py` conventions — module docstring of the ACs covered, `ROOT` sys.path insert, `import dashboard.main as api`, `from fastapi.testclient import TestClient`, and a `client` fixture with a `tmp_path` throwaway SQLite: create the `settings` table (DDL mirroring `db/schema_v2.sql` lines 5–10: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `key TEXT UNIQUE NOT NULL`, `value TEXT NOT NULL`, `updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`), `monkeypatch.setattr(api, "DB_PATH", db_path)`, `with TestClient(api.app) as c: yield c, db_path`, plus a seed helper writing rows via `sqlite3.connect(db_path)`. Depends on T002 (backend guard) — the frontend is not required for these tests. Same-file sequential: T005 → T006.

- [X] T005 [P] Create `tests/test_dashboard_settings_readonly.py` — scaffold + fixture + settings-key and missing-key tests: `test_settings_key_returns_403_and_does_not_write` (AC4 — seed `settings` with `tp_min_pct='0.8'`; POST `/api/miniapp` `{key: 'tp_min_pct', value: '5.0'}` with **no auth** → 403, `detail` contains `"read-only"`; assert the stored value is still `'0.8'` and the row count is unchanged) and `test_missing_key_returns_400` (regression — POST `{}` → 400 `"Missing key"`, guard ordering unchanged). The settings-key test needs **no auth machinery** (the 403 is raised before any auth check — deterministic).
  - *Verify*: `./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q` — these 2 pass.

- [X] T006 [P] Extend `tests/test_dashboard_settings_readonly.py` — `__ping__` and capital-key tests with deterministic auth (monkeypatch `api._validate_admin_session` with an `async def _ok(request): return True` — avoids `BOT_TOKEN`/HMAC initData/cookie crafting; `_validate_telegram_init_data` is not reached since the request sends no `X-Telegram-InitData` header): `test_ping_unauthenticated_returns_403_invalid_auth` (AC5 — POST `{key: '__ping__', value: ''}` no auth → 403 `"Invalid auth"`; `settings` table still empty — no write), `test_ping_authenticated_returns_ok` (AC5 — monkeypatched validator; POST `__ping__` → 200 `{"ok": True}`; table still empty), `test_capital_keys_still_writable` (AC7 regression guard — monkeypatched validator; seed `capital_cex_usdt='1000'`; POST `{key: 'capital_cex_usdt', value: '1500'}` → 200; assert stored value `'1500'` — catches a blanket-403 mistake).
  - *Verify*: `./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q` → all 5 tests green (quickstart Scenario 1).

**Checkpoint**: AC4/AC5/AC7/AC9 verified by unit tests; quickstart Scenario 1 reproducible.

---

## Phase 4: Docs (AC10)

**Purpose**: record the UX/behavior change. Independent of each other — different files → parallelizable [P]. Both depend on the implementation (T001–T006).

- [X] T007 [P] Update `docs/CURRENT_STATE.md` — bump the `> Updated:` header line (line 4) to `2026-08-12 | Feature 007 — Dashboard Settings Read-Only` and insert a short `## 0. Dashboard Settings Read-Only (feature 007, 2026-08-12)` section at the top (right after the header, before the feature-006 section at line 8) — mirroring feature 006's placement: dashboard settings are read-only and managed via local DB push; `POST /api/miniapp` settings keys → 403 no-write (the four capital keys keep their write path — Capital page stays editable); the `__ping__` probe and `GET /api/miniapp` unchanged; Telegram setting commands remain the operator's remote control.
  - *Verify*: header bumped; the new section sits above feature 006; a reader can reproduce the settings-vs-capital split from the doc alone.

- [X] T008 [P] Add a `ux:` entry to `docs/CHANGELOG.md` under the existing `## 2026-08-12` heading (line 5), per the `how-to-work-with-specs.md` `type: short description` convention: "Dashboard Settings page is now read-only — settings managed via local DB push (007)" — summarizing: MiniSettings `editable` forced false (globals + `__ping__` probe removed) + copy change; `POST /api/miniapp` returns 403 `"settings are read-only — manage via local DB push"` for settings keys with no DB write while the four capital keys keep their write path; retained `__ping__` probe; `GET /api/miniapp` unchanged; CapitalManager/Telegram untouched; new `tests/test_dashboard_settings_readonly.py`.
  - *Verify*: `ux:` entry dated 2026-08-12 under the `## 2026-08-12` heading; references feature 007.

**Checkpoint**: docs updated — feature complete.

---

## Phase 5: Full-Suite Regression

- [X] T009 Final regression — run the full test suite from the project root: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`. All suites (`test_amendment003.py`, `test_market_check.py`, `test_closed_trades_page.py`, `test_spot_exit_hardening.py`, `test_dashboard_settings_readonly.py`) must pass with 0 failures / 0 errors — do not introduce collection errors or regressions (AC9/AC12 — `bot.py`/`telegram.py` write paths and `GET /api/miniapp` are untouched).
  - *Verify*: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q` → 0 failed, 0 errors.

---

## ⛔ Out of Scope — MUST NOT Be Implemented

Explicitly excluded by spec.md (Scope / Out of Scope), plan.md (Out of Scope) and research.md (§2.4). Do NOT create tasks or code for these:

- **`dashboard-ui/src/CapitalManager.tsx`** — untouched; capital management stays editable in the UI and its `__ping__` probe keeps working (AC7).
- **`dashboard-ui/src/StatusBar.tsx`** — untouched (its `__ping__` probe keeps working against the retained backend probe path).
- **`telegram.py`** — untouched; Telegram setting commands remain the operator's auth-gated remote control (including toggling `auto_trade` on/off when funding).
- **`bot.py` / DB schema / migration** — no change to how the bot reads settings ("Settings are read fresh each cycle"); no change to the DB write path used by `bot.py`/`telegram.py`/`push-db.sh`; no schema change (`db/schema_v2.sql`, `db/migrations/`, `db/db_ops.py` untouched).
- **`GET /api/miniapp`** — unchanged; the read-only settings display stays fully functional.
- **Removal of now-inert `saveSetting`/`debouncedSave`/`selectAndSave`/`saving`/`errors`/`timers` in `MiniSettings.tsx`** — kept inert per the minimal-change policy (deferred cleanup).
- **Removal of `_get_db_rw` or the auth validators** (`_validate_telegram_init_data`, `_validate_admin_session`, `_send_validation_alert`) — still used by other endpoints and the retained capital write path.
- **Blanket-403 for capital keys** — the four capital keys MUST keep their write path (AC7); a blanket-403 would break the Capital page.
- Any trading hot-path module or trading behavior.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Backend)**: T001 → T002 (same file, sequential; guard consumes the constant)
- **Phase 2 (Frontend)**: T003 → T004 (same file, sequential; copy change after `editable = false`); **independent of Phase 1** (different file) — [P]
- **Phase 3 (Tests)**: depends on T002 (backend guard is the subject under test); **not** on the frontend; T005 → T006 (same file, sequential)
- **Phase 4 (Docs)**: depends on the implementation (T001–T006)
- **Phase 5 (Regression)**: after everything (T001–T008)

### Within Each Phase

- Backend: T001 (constant) → T002 (guard + docstring) — same file
- Frontend: T003 (edit-enablement removal) → T004 (copy + import re-check) — same file
- Tests: scaffold + fixture + settings-key 403 + missing-key (T005) → `__ping__` + capital-key (T006) — same file
- Docs: `CURRENT_STATE.md` (T007) ∥ `CHANGELOG.md` (T008)
- Regression: T009 last

### Parallel Opportunities

- T003/T004 (`dashboard-ui/src/MiniSettings.tsx`) ∥ T001/T002 (`dashboard/main.py`) — different files, no shared dependencies
- T005/T006 (`tests/test_dashboard_settings_readonly.py`) ∥ T003/T004 (frontend) — tests depend only on T002
- T007 ∥ T008 (docs) — different files

---

## Parallel Examples

```bash
# Backend + frontend in parallel (different files)
Task: "T001 → T002 — dashboard/main.py CAPITAL_SETTING_KEYS + 403 guard"
Task: "T003 → T004 — dashboard-ui/src/MiniSettings.tsx editable=false + copy"

# Tests (after backend) can overlap the frontend work
Task: "T005 → T006 — tests/test_dashboard_settings_readonly.py (5 tests)"

# Docs in parallel (different files)
Task: "T007 — docs/CURRENT_STATE.md"
Task: "T008 — docs/CHANGELOG.md"
```

---

## Implementation Strategy

### MVP First (Backend + Frontend lock)

1. Phase 1 backend (T001 → T002) — settings keys 403, `__ping__` + capital keys unchanged
2. Phase 2 frontend (T003 → T004) — can run in parallel with Phase 1; `npx tsc --noEmit` clean
3. **STOP and VALIDATE**: quickstart Scenario 4 (curl spot-check) + Scenario 3 (manual browser: Settings read-only, Capital editable)
4. Phase 3 tests (T005 → T006) → Phase 4 docs (T007/T008) → Phase 5 regression (T009)

### Incremental Delivery

1. Backend lock first (T001–T002) — the endpoint refuses settings writes before the UI is touched, so a stale client can never write
2. Frontend read-only + copy (T003–T004) → `tsc` clean
3. Tests (T005–T006) → docs (T007–T008) → full-suite regression (T009)

### Parallel Team Strategy

1. Person A: T001 → T002 (backend)
2. Person B: T003 → T004 (frontend) — in parallel with Person A
3. Person A (after T002): T005 → T006 (tests)
4. Together: T007/T008 (docs) → T009 (regression)

---

## Notes

- [P] tasks = different files, no dependencies
- No user stories in spec.md — organized by the two parts + tests/docs (matches the `006` repo format)
- Work directly on `main` — no feature branches (spec Q6); verified: repo currently tracks `main`
- Verify the 403 no-write behavior and the capital-key write path with the 5 new tests before moving on
- Commit after each task or logical group
- Do NOT touch CapitalManager / StatusBar / telegram.py / bot.py / DB schema / `GET /api/miniapp` / the inert save plumbing (see Out of Scope)
- UI verification = `cd dashboard-ui && npx tsc --noEmit` (no UI test command exists); `npm run build` is an optional full check
- After implementation: run quickstart.md Scenarios 1–4
