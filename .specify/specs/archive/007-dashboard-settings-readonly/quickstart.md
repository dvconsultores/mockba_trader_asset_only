# Quickstart: Dashboard Settings Read-Only (feature 007)

**Feature**: 007-dashboard-settings-readonly | **Date**: 2026-08-12

Runnable validation scenarios proving the feature end-to-end. All commands run
from the project root with the venv Python (or `dashboard-ui/` for the build).
Contract details live in `contracts/miniapp-api.md` and `data-model.md` — this
guide does not repeat them. Implementation details belong to the tasks phase,
not here.

## Prerequisites

- Repo venv (`./venv/bin/python`); dashboard backend importable as
  `dashboard.main` (already used by the existing dashboard tests).
- `dashboard-ui/` node deps installed (`npm install` inside `dashboard-ui/`).
- A local copy of `data/trading.db` (or a throwaway DB for the API scenarios).

---

## Scenario 1: Automated Backend Test (REQUIRED verification)

**Goal**: the new dashboard test suite proves the settings-vs-capital split:
settings keys → 403 + stored value unchanged; `__ping__` unchanged; capital
keys still writable.

```bash
./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q
# plus the existing suites (no regressions)
./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q
```

**Expected**: all green — `test_settings_key_returns_403_and_does_not_write`,
`test_ping_unauthenticated_returns_403_invalid_auth`,
`test_ping_authenticated_returns_ok`, `test_missing_key_returns_400`,
`test_capital_keys_still_writable` (AC4/AC5/AC7/AC9).

## Scenario 2: UI Type-Check / Build

**Goal**: the `MiniSettings.tsx` change (removed globals + probe effect,
`editable = false`, new copy) compiles. There is no UI test command, so the
build is the automated UI verification.

```bash
cd dashboard-ui && npm run build   # tsc && vite build
```

**Expected**: `tsc` type-checks with no errors and Vite emits the bundle.

## Scenario 3: Manual Browser Verification

**Goal**: the Settings page is visible but fully read-only; Capital stays
editable.

1. Serve the dashboard (dev or the built bundle against the backend) and open
   the **Settings** tab in a browser (no Telegram).
2. **Expected**: the tab is visible; the header subtitle reads
   **"Settings are read-only — managed via DB"**; the badge reads
   **"read-only"**; the footer reads
   **"Settings are read-only — manage via local DB push"**; every preset
   dropdown / number field / text field renders **disabled**; all current
   values are displayed (from `GET /api/miniapp`).
3. Open the tab inside the Telegram Mini App (valid `initData`) — **Expected**:
   identical read-only state (no edit-enablement).
4. Open the **Capital** tab (browser after the `__ping__` probe succeeds, or
   Telegram) — **Expected**: Declared Capital and Slot % remain **editable**
   and save successfully (AC7); the `read-only` badge is gone when editable.
5. **Expected**: the Settings page no longer fires any `POST /api/miniapp`
   request (devtools network tab).

## Scenario 4: API Contract Spot-Check (curl)

**Goal**: verify the HTTP contract without the UI. Uses a throwaway DB copy so
the live DB is untouched.

```bash
# Point the dashboard at a scratch DB, e.g.:
#   cp data/trading.db /tmp/check.db   (or create an empty settings table)

# 1. Settings key → 403, no write (value stays '0.8')
curl -s -X POST http://localhost:8000/api/miniapp \
  -H 'Content-Type: application/json' \
  -d '{"key":"tp_min_pct","value":"5.0"}'
#   → {"detail":"settings are read-only — manage via local DB push"} (403)
#   then: SELECT value FROM settings WHERE key='tp_min_pct';  → '0.8'

# 2. __ping__ without auth → 403 Invalid auth
curl -s -X POST http://localhost:8000/api/miniapp \
  -H 'Content-Type: application/json' -d '{"key":"__ping__","value":""}'
#   → {"detail":"Invalid auth"} (403)

# 3. Missing key → 400
curl -s -X POST http://localhost:8000/api/miniapp \
  -H 'Content-Type: application/json' -d '{}'
#   → {"detail":"Missing key"} (400)

# 4. GET display unchanged
curl -s http://localhost:8000/api/miniapp
#   → {"ok":true,"settings":{...}} (200)
```

Capital-key write with a valid admin session cookie (obtained via
`POST /api/admin/login` with valid Telegram initData) still returns 200 and
writes — covered deterministically by the automated test instead.

**Expected**: every response above matches `contracts/miniapp-api.md`.

---

## Reference

- Contract: `contracts/miniapp-api.md`
- Data model: `data-model.md` (settings table unchanged; settings-vs-capital
  key split; UI state model)
- Backend anchors: `dashboard/main.py` `api_miniapp_update` (lines 552–620)
- Frontend anchors: `dashboard-ui/src/MiniSettings.tsx` (globals 7–9, `editable`
  line 350, probe 367–373, copy 453/515)
