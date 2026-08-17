# Plan: Dashboard Settings Read-Only

**Feature**: 007-dashboard-settings-readonly | **Date**: 2026-08-12 | **Spec**: `.specify/specs/007-dashboard-settings-readonly/spec.md`
**Status**: Draft — awaiting implementation authorization
**Branch**: `main` (per spec Q6 — implementation directly on `main`, the repo tracks only main, no feature branches)

**Input**: Feature spec (Draft, Q1–Q2 resolved, 11 acceptance criteria). This plan
does not re-litigate resolved decisions; it verifies every decision against the
actual code, pins the exact 403 message and header/footer copy the spec
delegated to planning, and fixes the design (including which `MiniSettings.tsx`
code becomes inert vs. removed, the deterministic auth strategy for the
`__ping__` test, and a **grounded correction**: CapitalManager writes its four
capital keys through `POST /api/miniapp`, so the 403 applies to settings keys
only and the capital-key write path is retained — see `research.md` §2.3).

## Summary

The dashboard Settings page (Mini App) becomes strictly read-only: the full
settings display (powered by the unchanged `GET /api/miniapp`) stays, but the
edit path is locked. **Part 1**: `dashboard-ui/src/MiniSettings.tsx` forces
`editable` permanently `false` — the module-level `isTelegram` /
`browserSessionChecked` / `browserSessionValid` globals and the `__ping__`
probe effect are removed, the `editable` state becomes a `const false`, the
existing `disabled={!editable}` read-only rendering is preserved, and the
header/footer copy is updated to state settings are managed via DB. **Part 2**:
`dashboard/main.py` `POST /api/miniapp` keeps the `__ping__` no-write auth
probe byte-for-byte (CapitalManager + StatusBar browser-session detection
depend on it), and any real setting key now returns
**403 `"settings are read-only — manage via local DB push"`** with **no DB
write**. **Grounded correction to the spec's "any real key" wording**: the four
capital keys written by the Capital page (`capital_cex_usdt`,
`capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct`) go through this same
endpoint (there is no `POST /api/capital`), so the write path (auth →
`INSERT … ON CONFLICT … DO UPDATE` → post-save validation → Telegram alert) is
**retained for exactly those keys** — this is what keeps AC7 ("CapitalManager
still edits declared capital") true.
`GET /api/miniapp` unchanged. **Part 3**: a new dashboard test
(`tests/test_dashboard_settings_readonly.py`) following the
`tests/test_closed_trades_page.py` conventions (FastAPI `TestClient`,
throwaway SQLite via `monkeypatch.setattr(api, "DB_PATH", …)`) asserting a
settings key → 403 + stored value unchanged, `__ping__` → unchanged behavior
(403 "Invalid auth" unauthenticated / `{"ok": True}` with the admin-session
validator monkeypatched for determinism), and the capital keys → still writable
(AC7 regression guard). **Part 4**: docs
(`docs/CHANGELOG.md` `ux:` entry + short `docs/CURRENT_STATE.md` note).
`dashboard-ui/src/CapitalManager.tsx`, `telegram.py`, `bot.py`, the `settings`
table, and the DB write path used by `bot.py`/`telegram.py` are all untouched.

## Technical Context

**Language/Version**: Python 3.11 (repo venv `./venv/bin/python`); frontend
TypeScript 5.5 / React 18 (`dashboard-ui/`).

**Primary Dependencies**: backend `fastapi`, stdlib `sqlite3` — **no new
dependencies**; frontend existing `react`, `lucide-react` — no new packages.

**Storage**: SQLite `data/trading.db` — **no schema change / no migration**;
the `settings` table is untouched (its write path used by `bot.py` /
`telegram.py` / `push-db.sh` is unchanged).

**Testing**: pytest — `./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q`.
The dashboard UI has a build step (`npm run build` = `tsc && vite build`) but
**no UI test command** (`dashboard-ui/package.json` scripts: `dev`, `build`,
`preview`) — the pytest backend test is the required automated verification;
`tsc` type-checking is the UI verification.

**Target Platform**: Linux server (dashboard served as a FastAPI app; the UI is
a static Vite build behind nginx).

**Project Type**: web dashboard (FastAPI backend + React/TS frontend) inside a
single-repo trading bot; operator/UX tooling, not the trading hot path.

**Performance Goals**: none — this change removes work from the POST path (no
DB write, no validation, no Telegram alert) and adds no network traffic.

**Constraints**: minimum modification (minimal-change policy,
`.github/instructions/minimal-change.instructions.md`); display stays
functional (read-only means no editing, not hidden); `__ping__` probe
unchanged; CapitalManager untouched; no change to how `bot.py` reads settings
("Settings are read fresh each cycle"); implementation on `main` directly.

**Scale/Scope**: 1 UI component (`MiniSettings.tsx`, ~11 lines removed), 1
endpoint (`dashboard/main.py` `POST /api/miniapp`: +1 module constant + a
7-line 403 guard before the retained capital write path), 1 new test file,
2 docs; 11 acceptance criteria.

## Constitution Check

*GATE: evaluated before Phase 0 research and re-checked after Phase 1 design
(below).*

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy (mean reversion) | ✅ | Unaffected — operator/UX tooling on the dashboard Settings page; no strategy, entry, exit, or signal-path code touched. |
| **II** Reward Exceeds Risk (NON-NEGOTIABLE) | ✅ | Unaffected — no settings validation change; startup gates (`tp_pct > sl_pct`, net-edge, breakeven) and `settings_rules.py` untouched. The dashboard merely stops being a write path; the bot still reads settings fresh each cycle. |
| **III** No Position Without a Stop (NON-NEGOTIABLE) | ✅ | Unaffected — no trading-path change (spot/futures scalpers, executor untouched). |
| **IV** Unknown State = No Trading (NON-NEGOTIABLE) | ✅ | Unaffected — no state-query or fail-closed logic touched. |
| **V** Real Fills Only | ✅ | Unaffected — PnL/fill code untouched. |
| **VI** Restart Safety | ✅ | Unaffected — no new persistent state; dashboard-only change. |; `dashboard/main.py` adds a small constant + 403 guard (the retained capital write path means no large deletion, but no growth beyond ~10 lines and no new module)
| **VII** Simplicity Is a Constraint | ✅ | No hot-path line growth — this change **reduces** code: `MiniSettings.tsx` −~11 lines, `dashboard/main.py` −~45 lines (write path replaced by one 403 raise). No new module. |
| **VIII** The Bot Trades | ✅ | Unaffected — no trade-frequency impact; the dashboard display and `GET /api/miniapp` remain fully functional (read-only means no editing, not hidden). |

**Post-design re-check**: the design below satisfies II (bot read path and
startup gates untouched), VII (net line reduction), and the operational rule
"Settings are read fresh each cycle" (only the dashboard's editing path is
locked; `bot.py`/`telegram.py` write paths and the DB are untouched). No gate
violation.

## Project Structure

### Documentation (this feature)

```text
.specify/specs/007-dashboard-settings-readonly/
├── spec.md              # authoritative feature spec (Draft, Q1–Q2 resolved)
├── plan.md              # this file
├── research.md          # verified signatures / line anchors / consumer map
├── data-model.md        # settings table (unchanged), UI state model, API contract
├── contracts/
│   └── miniapp-api.md   # /api/miniapp HTTP contract (POST real key now 403)
├── quickstart.md        # validation scenarios
└── tasks.md             # Phase 2 — NOT created by this plan
```

### Source Code (repository root)

```text
dashboard-ui/src/MiniSettings.tsx   # −~11 lines — editable const false, globals +
                                    #   probe effect removed, header/footer copy
dashboard/main.py                   # POST /api/miniapp: +CAPITAL_SETTING_KEYS guard —
                                    #   non-capital keys → 403 (no write); capital
                                    #   keys keep the existing write path; __ping__ kept
tests/test_dashboard_settings_readonly.py  # NEW — 5 tests (AC4/AC5/AC7/AC9)
docs/CURRENT_STATE.md               # +feature 007 section + Updated header line
docs/CHANGELOG.md                   # +ux: entry (2026-08-12)
```

**Structure Decision**: single-repo project. The backend change is confined to
one endpoint (a guard + a small module constant); the frontend change to one
component. No new module, no new endpoint, no schema change.
`dashboard-ui/src/CapitalManager.tsx` and `telegram.py` are untouched
(explicit out-of-scope in the spec) — and because CapitalManager writes its
capital keys through this same endpoint, the backend retains that write path
(§2.3 of research).

## Research Summary

See `research.md` for the full code-verified inventory. Key findings:

- **`editable` in `MiniSettings.tsx` is initialized from a module global**
  (line 350 `useState(isTelegram)`; `isTelegram` at line 7) and can be set true
  by a browser probe effect (lines 367–373) that POSTs `{ key: '__ping__' }`
  to `/api/miniapp`. Both are removed so `editable` is permanently `false`.
  All preset/free/text inputs already render `disabled={!editable}` (lines 473,
  496, 507) — the read-only look already exists and is preserved unchanged.
- **`TG` stays**: `MiniSettings.tsx` line 6 defines `TG` (used at line 362 for
  `TG.ready()/TG.expand()` and line 386 for the `X-Telegram-InitData` header).
  Only the `isTelegram`/`browserSessionChecked`/`browserSessionValid` globals
  (lines 7–9) are removed; `MiniSettings` defines its own `isTelegram` — it
  does **not** import from `TelegramProvider` (unlike `CapitalManager.tsx`
  line 3), so no cross-file impact.
- **The `saveSetting`/`debouncedSave`/`selectAndSave` plumbing and the
  `saving`/`errors`/`timers` state become unreachable** once `editable ===
  false` (disabled inputs never fire `onChange`) but remain referenced — they
  are kept **inert** (no removal) per the minimal-change policy; `tsconfig`
  has `noUnusedLocals: false` so no build errors result.
- **`__ping__` is consumed by three UI files** — `MiniSettings.tsx` (probe
  being removed), `CapitalManager.tsx` (lines 167–173) and `StatusBar.tsx`
  (line 79). The backend `__ping__` branch must stay byte-for-byte;
  `CapitalManager` and `StatusBar` are untouched and keep working.
- **CRITICAL — `CapitalManager.tsx` also POSTs real keys through
  `/api/miniapp`**: `DECLARED_KEY`/`SLOT_PCT_KEY` (lines 29–30) are the
  settings-table keys `capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`,
  `dex_slot_pct`; `saveSetting` (line 179, called from `renderEditable`'s
  `commit` at line 249) writes them via this endpoint. There is **no**
  `POST /api/capital` (only `GET`, line 627). ⇒ A blanket "any real key →
  403" would break AC7; the write path is therefore **retained for exactly
  these four capital keys** and the 403 applies to all other real keys
  (documented deviation from the spec's literal "any real setting key"
  wording — see `research.md` §2.3/§5).
- **`MiniSettings.saveSetting` (line 380) is the only non-capital real-key
  consumer** and becomes unreachable once `editable === false`; the
  Settings-page keys (PRESETS/FREE_INPUTS/TEXT_INPUTS, `research.md` §1.6) do
  not overlap with the capital keys.
- **`POST /api/miniapp` (`api_miniapp_update`, `dashboard/main.py` lines
  552–620)**: missing-key 400 (lines 558–560) and the `__ping__` auth probe
  (lines 561–572, no DB write) stay; a guard is inserted after the probe —
  `key not in CAPITAL_SETTING_KEYS` (new module constant
  `{"capital_cex_usdt", "capital_dex_usdc", "cex_slot_pct", "dex_slot_pct"}`)
  → **403 `"settings are read-only — manage via local DB push"`** before any
  auth; the existing capital-key write path (lines 574–620: auth, `INSERT …
  ON CONFLICT … DO UPDATE` at 594, `validate_all` + `_send_validation_alert`
  at 618) is kept unchanged for capital keys.
- **`_get_db_rw` stays** (line 529) — still used by `_upsert_setting` (line
  56), the asset-blacklist endpoint (line 726), `/api/bot/control` (line 797)
  **and the retained capital write path**. No dead-code cleanup is needed.
- **`GET /api/miniapp` (`api_miniapp_get`, lines 536–549)** is unchanged — it
  continues to power the full read-only settings display (read-only means no
  editing, not hidden).
- **Test conventions** (`tests/test_closed_trades_page.py`): module docstring
  of ACs, `ROOT` sys.path insert, `import dashboard.main as api`,
  `from fastapi.testclient import TestClient`, and a `client` fixture with a
  `tmp_path` throwaway SQLite and `monkeypatch.setattr(api, "DB_PATH", …)`
  inside `with TestClient(api.app) as c:`. The `settings` DDL for the fixture
  mirrors `db/schema_v2.sql` lines 5–10 (`id`, `key` UNIQUE, `value`,
  `updated_at`). Run with `--basetemp=.pytest_tmp` (sandbox tmpdir ownership
  quirk — see repo memory).
- **UI verification**: `dashboard-ui/package.json` `build` = `tsc && vite
  build`; tsconfig is `strict: true` with `noUnusedLocals: false` /
  `noUnusedParameters: false`. There is **no UI test command** — the pytest
  backend test is the required automated verification for this feature.

## Data Model / Contracts

- **`settings` table** (`data-model.md` §1): **unchanged** — no schema change,
  no migration, no default change. Its write path used by `bot.py`,
  `telegram.py` and `push-db.sh` is untouched; the operator continues to manage
  settings locally via the AI assistant + `push-db.sh` (DB push to the VPS).
- **UI state model** (`data-model.md` §2): `MiniSettings` `editable` becomes a
  `const false`; the `isTelegram`/`__ping__` edit-enablement (module globals +
  probe effect) is removed. Read-only rendering (`disabled={!editable}`) is
  preserved.
- **API contract change** (`contracts/miniapp-api.md`): `POST /api/miniapp`
  with a **real non-capital setting key** now returns **403
  `"settings are read-only — manage via local DB push"`** and performs **no DB
  write** (previously: auth → upsert → post-save validation → Telegram alert →
  200). **Capital keys** (`capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`,
  `dex_slot_pct`) **keep the existing write contract unchanged** (AC7 — the
  Capital page stays editable). The `__ping__` probe contract and
  `GET /api/miniapp` are unchanged.

## Detailed Design

### Part 1 — `dashboard-ui/src/MiniSettings.tsx`: settings always read-only

**Where**: `MiniSettingsComponent` and the module header. Four edits plus two
copy changes; nothing else.

1. **Remove the module-level edit-enablement globals (lines 7–9)** —
   `const isTelegram = !!TG?.initData`, `let browserSessionChecked = false`,
   `let browserSessionValid = false`. Line 6 (`const TG = …`) stays — it is
   still used at line 362 (`TG.ready()/TG.expand()`) and line 386
   (`TG?.initData` header in `saveSetting`).
2. **`editable` becomes a constant `false` (line 350)** —
   `const [editable, setEditable] = useState(isTelegram)` →
   `const editable = false` (no `useState`, per spec). `setEditable` disappears
   with the state; `useState` stays imported (settings/saving/errors/loaded
   states remain).
3. **Remove the `__ping__` probe effect (lines 367–373)** — the entire
   `useEffect` that POSTs `{ key: '__ping__', value: '' }` to `/api/miniapp`
   and calls `setEditable(r.ok)`. `useEffect` stays imported (the settings-load
   effect at line 356 remains).
4. **Keep the read-only rendering unchanged** — every input already renders
   `disabled={!editable}`: preset `ComboList` (line 473), `NumberField` (line
   496), `TextField` (line 507). `disabled={!editable}` stays exactly as-is
   (now permanently `disabled`).
5. **Header copy (line 453)** — the subtitle
   `{editable ? 'Tap a value to change it' : 'Read-only outside Telegram'}`
   becomes the static string **`Settings are read-only — managed via DB`**
   inside the existing `<p className="text-xs text-[#7a7090] mt-0.5">`
   wrapper (styling preserved). The read-only badge (lines 455–457) already
   renders `'read-only'` with the read-only styling in the false branch — it is
   kept unchanged.
6. **Footer copy (line 515)** —
   `{editable ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}`
   becomes the static string **`Settings are read-only — manage via local DB push`**
   (styling preserved).

**Inert (kept, not removed)**: `saveSetting` (lines 377–392),
`debouncedSave` (lines 394–400), `selectAndSave` (lines 402–406), the
`saving`/`errors` states and the `timers` ref. Once `editable === false` the
inputs are disabled and never fire `onChange`, so these paths are unreachable
but remain referenced — removing them would be extra churn against the
minimal-change policy, and `tsconfig` (`noUnusedLocals: false`) means no build
errors. This is the **pinned design decision** (rationale in `research.md`
§1.6).

### Part 2 — `dashboard/main.py`: harden `POST /api/miniapp` (write path locked)

**Where**: `api_miniapp_update` (lines 552–620). `GET /api/miniapp`
(`api_miniapp_get`, lines 536–549) is **unchanged**.

1. **Docstring (line 554)** — update from `"""Update a single setting. Requires
   Telegram initData or a valid admin session."""` to reflect the split
   semantics: settings keys are read-only (403); the four capital keys keep the
   write path (Capital page stays editable); `__ping__` remains a no-write auth
   probe.
2. **Keep the missing-key guard (lines 558–560)** — `if not key:
   raise HTTPException(status_code=400, detail="Missing key")` unchanged.
3. **Keep the `__ping__` auth probe byte-for-byte (lines 561–572)** — no DB
   write; 403 `"Invalid auth"` when unauthenticated, `{"ok": True}` when
   authenticated. `CapitalManager.tsx` (lines 167–173) and `StatusBar.tsx`
   (line 79) depend on it (AC5/AC7).
4. **Add a module-level capital allow-list constant** (near the endpoint or
   beside `_get_db_rw`, line 529):

   ```python
   # Capital settings are still edited in the Capital Manager UI (AC7);
   # every other settings key is read-only — managed via local DB push.
   CAPITAL_SETTING_KEYS = {"capital_cex_usdt", "capital_dex_usdc",
                           "cex_slot_pct", "dex_slot_pct"}
   ```

5. **Insert the 403 guard immediately after the `__ping__` branch (after line
   572), before any auth** — for any key **not** in `CAPITAL_SETTING_KEYS`:

   ```python
   if key not in CAPITAL_SETTING_KEYS:
       # Settings are managed locally via DB push; the dashboard is read-only.
       raise HTTPException(status_code=403,
                           detail="settings are read-only — manage via local DB push")
   ```

   - **Pinned message**: `settings are read-only — manage via local DB push`
     (spec-delegated wording; user-actionable and distinct from `"Invalid
     auth"`).
   - **No DB write**: for non-capital keys the function returns before any
     `_get_db_rw()` call, INSERT/UPDATE, or commit — the stored setting value
     is provably unchanged (AC4).
   - **No post-save validation / no Telegram alert** — non-capital keys never
     reach `validate_all` or `_send_validation_alert`.
   - **No auth needed for the 403** — non-capital keys write nothing, so the
     response is identical for any caller; this makes the 403 test fully
     deterministic without auth machinery.
6. **Keep the existing capital-key write path (lines 574–620) unchanged** —
   Telegram-initData + admin-session auth, `INSERT … ON CONFLICT … DO UPDATE`
   (line 594), post-save `validate_all` loop, `_send_validation_alert` (line
   618), `return {"ok": True, "key":…, "value":…, "alerts":…}` — all retained
   **verbatim** for the four capital keys so `CapitalManager.tsx` keeps editing
   Declared Capital and Slot % (AC7). **This is the required, documented
   deviation** from the spec's literal "any real setting key returns 403"
   wording (Q2/feature description) — grounded in §2.3 of `research.md`.
7. **No other backend change** — `_get_db_rw` (line 529), `_validate_telegram_init_data`
   (line 450), `_validate_admin_session` (line 473), `_send_validation_alert`
   (line 394) all stay (used by other endpoints and the capital write path).
   `GET /api/miniapp`, the Telegram bot-control endpoint (lines 782–810), the
   blacklist endpoint and `bot.py`/`telegram.py` are untouched.

### Part 3 — new dashboard test `tests/test_dashboard_settings_readonly.py`

Mirrors `tests/test_closed_trades_page.py` conventions: module docstring of the
ACs covered, `ROOT` sys.path insert, `import dashboard.main as api`,
`from fastapi.testclient import TestClient`, and a `client` fixture that points
`api.DB_PATH` at a `tmp_path` throwaway SQLite (settings DDL mirroring
`db/schema_v2.sql` lines 5–10) inside `with TestClient(api.app) as c:`. See
`quickstart.md` Scenario 1 and the Testing Strategy table for the exact tests.

### Part 4 — docs

- `docs/CHANGELOG.md` — `ux:` entry under the existing `## 2026-08-12` heading
  (convention: `type: short description`, per `how-to-work-with-specs.md`).
- `docs/CURRENT_STATE.md` — bump the `> Updated:` header line to
  `2026-08-12 | Feature 007 — Dashboard Settings Read-Only` and add a short
  `## 0. Dashboard Settings Read-Only (feature 007, 2026-08-12)` section at the
  top (mirroring feature 006's placement).

## Edge Cases

| Edge case | Handling |
|---|---|
| Browser (no Telegram initData) opens Settings | `editable` is `const false` — no probe, inputs stay disabled, full display via `GET /api/miniapp` (AC1/AC2). |
| Telegram Mini App opens Settings | Same — no `isTelegram` edit-enablement; inputs disabled (AC1/AC2). |
| `POST /api/miniapp` with a real **settings** key from an old/dangling client | 403 `"settings are read-only — manage via local DB push"`; no write, no validation, no alert (AC4). |
| `POST /api/miniapp` with a **capital** key (`capital_cex_usdt` / `capital_dex_usdc` / `cex_slot_pct` / `dex_slot_pct`) | Existing write path retained — auth → upsert → post-save validation → alert → 200; CapitalManager stays editable (AC7). |
| `POST /api/miniapp` with `__ping__` | Unchanged probe: no write; 403 `"Invalid auth"` unauthenticated, `{"ok": True}` authenticated — CapitalManager and StatusBar keep working (AC5/AC7). |
| `POST /api/miniapp` with empty/missing key | 400 `"Missing key"` (unchanged ordering — the missing-key guard runs before the `__ping__` branch) |
| `GET /api/miniapp` fails (DB error) | Unchanged 500 path; display shows the error as today (AC6). |
| Operator edits a setting through the UI | Impossible — every input renders `disabled={!editable}`; `saveSetting` is unreachable. |
| `saveSetting`/`debouncedSave`/`selectAndSave` left inert | Harmless: disabled inputs never fire `onChange`; `tsconfig` `noUnusedLocals: false` → no build errors (AC2, minimal-change). |
| `__ping__` probe removed from MiniSettings | No behavioral impact — CapitalManager + StatusBar still probe; MiniSettings no longer derives editability (AC2). |
| Settings changed via Telegram/`push-db.sh` while dashboard open | `GET /api/miniapp` reads fresh each load / refresh — display reflects current DB values; only the dashboard's editing path is locked. |

## Out of Scope

- `dashboard-ui/src/CapitalManager.tsx` — untouched; capital management stays
  editable in the UI and its `__ping__` probe keeps working.
- `telegram.py` — untouched; Telegram setting commands remain the operator's
  auth-gated remote control (including toggling `auto_trade` on/off when
  funding).
- `dashboard-ui/src/StatusBar.tsx` — untouched (its `__ping__` probe keeps
  working against the retained backend probe path).
- DB schema change / migration; change to how `bot.py` reads settings (settings
  still read fresh each cycle); change to the DB write path used by
  `bot.py`/`telegram.py`/`push-db.sh`.
- New auth scheme; removal of `_get_db_rw` or the auth validators (still used
  by other endpoints).
- Removal of the now-inert `saveSetting`/`debouncedSave`/`selectAndSave`
  plumbing in `MiniSettings.tsx` (deferred cleanup; minimal-change policy).
- Any trading hot-path module or trading behavior.

## Testing Strategy (11 acceptance criteria)

`tests/test_dashboard_settings_readonly.py` mirrors
`tests/test_closed_trades_page.py` (FastAPI `TestClient` over `api.app`,
throwaway SQLite via `monkeypatch.setattr(api, "DB_PATH", …)`, settings DDL
from `db/schema_v2.sql`). The settings-key 403 test needs **no auth machinery**
— the new code returns 403 before any auth check, so it is deterministic. The
authenticated `__ping__` case and the capital-key write case monkeypatch
`api._validate_admin_session` with an `async def` returning `True`
(deterministic; no `BOT_TOKEN`, no cookie crafting).

| # | Test | Verifies AC |
|---|---|---|
| 1 | `test_settings_key_returns_403_and_does_not_write` — seed `settings` with `tp_min_pct='0.8'`; POST `/api/miniapp` `{key: 'tp_min_pct', value: '5.0'}` with no auth → **403**, `detail` contains `"read-only"`; assert the stored value is still `'0.8'` and the row count is unchanged | AC4, AC9 |
| 2 | `test_ping_unauthenticated_returns_403_invalid_auth` — POST `{key: '__ping__', value: ''}` with no auth → **403** `"Invalid auth"`; `settings` table still empty (no write) | AC5, AC9 |
| 3 | `test_ping_authenticated_returns_ok` — monkeypatch `api._validate_admin_session` with an `async def` returning `True`; POST `__ping__` → **200** `{"ok": True}`; `settings` table still empty (no write) | AC5, AC9 |
| 4 | `test_missing_key_returns_400` — POST `{}` → **400** `"Missing key"` (guard ordering unchanged) | AC4 (regression) |
| 5 | `test_capital_keys_still_writable` — monkeypatch `api._validate_admin_session` returning `True`; seed `capital_cex_usdt='1000'`; POST `{key: 'capital_cex_usdt', value: '1500'}` → **200**; assert the stored value is `'1500'` (AC7 regression guard — catches a blanket-403 mistake) | AC7, AC9 |

Run:

```bash
./venv/bin/python -m pytest tests/test_dashboard_settings_readonly.py --basetemp=.pytest_tmp -q
# plus the full suite for regressions
./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q
```

UI verification (no automated UI test command exists): `npm run build` in
`dashboard-ui/` (runs `tsc && vite build`) must pass — this type-checks the
`editable = false` change and the removed globals/probe. Manual browser check
in `quickstart.md` Scenario 3 covers the rendered read-only state and
`CapitalManager` editability.

## Docs Update

- **`docs/CHANGELOG.md`** — `ux:` entry (dated 2026-08-12, appended under the
  existing `## 2026-08-12` heading): "Dashboard Settings page is now
  read-only — settings managed via local DB push (007)" summarizing: the
  MiniSettings `editable` lock + copy change; `POST /api/miniapp` returning 403
  `"settings are read-only — manage via local DB push"` for settings keys with
  no DB write, while the four **capital keys keep their write path** (Capital
  page stays editable); the retained `__ping__` probe; `GET /api/miniapp`
  unchanged; CapitalManager/Telegram untouched; the new test file.
- **`docs/CURRENT_STATE.md`** — bump the `> Updated:` header line and add a
  short top-level section (feature 007, dated 2026-08-12): dashboard settings
  are read-only and managed via local DB push; `POST /api/miniapp` settings
  keys → 403 no-write (capital keys keep the write path); `__ping__` probe and
  `GET /api/miniapp` unchanged; Capital Manager stays editable; Telegram
  setting commands remain the remote control.

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/007-dashboard-settings-readonly/` | ✅ spec, plan, research, data-model, contracts, quickstart (this phase) |
| `dashboard-ui/src/MiniSettings.tsx` | remove globals (lines 7–9) + probe effect (lines 367–373); `editable` → `const editable = false` (line 350); header copy (line 453) + footer copy (line 515) updated to "managed via DB"; disabled rendering (lines 473/496/507) untouched; save plumbing left inert |
| `dashboard/main.py` | `api_miniapp_update`: docstring (line 554) updated; `+CAPITAL_SETTING_KEYS` constant; 403 guard for non-capital keys inserted after the `__ping__` branch (line 573); missing-key guard (558–560) and `__ping__` branch (561–572) and the capital-key write path (574–620) unchanged; `GET /api/miniapp` unchanged |
| `tests/test_dashboard_settings_readonly.py` | New — 5 tests (AC4, AC5, AC7, AC9) |
| `docs/CURRENT_STATE.md` | Updated (`> Updated:` header + feature 007 section) |
| `docs/CHANGELOG.md` | Updated (`ux:` entry, 2026-08-12) |
| `dashboard-ui/src/CapitalManager.tsx`, `StatusBar.tsx`, `telegram.py`, `bot.py`, `db/*`, `trading_bot/*`, `trade/*` | Unchanged |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Blanket-403 mistake breaks CapitalManager's Declared Capital / Slot % writes (AC7) | Medium | High | **Grounded allow-list** of the four capital keys (§2.3 research); the write path for those keys is retained verbatim; dedicated regression test 5 (`test_capital_keys_still_writable`) fails if a blanket-403 ever slips in. |
| Removing the `__ping__` backend branch breaks CapitalManager/StatusBar editability | Low | High | Branch is kept **byte-for-byte** (lines 561–572); `CapitalManager.tsx`/`StatusBar.tsx` untouched; dedicated `__ping__` tests (AC5). |
| Spec/plan wording drift — "any real key" vs the capital allow-list | Medium | Low | Deviation is documented in the plan, research (§2.3/§5), contracts, CHANGELOG and CURRENT_STATE; flagged for user confirmation before implementation. |
| Operator loses the ability to change settings (locked out) | Low | Medium | Intentional (spec decision) — Telegram setting commands (`telegram.py`) and local `push-db.sh` remain the management paths; the display stays fully readable so drift is still observable. |
| Dead `saveSetting`/`debouncedSave`/`selectAndSave` confuses future maintenance | Medium | Low | Pinned decision: keep inert (minimal-change policy; `noUnusedLocals: false` → no build errors). Flagged as deferred cleanup in Out of Scope; copy no longer references "auto-save". |
| A stale client still POSTs settings keys | Low | Low | Endpoint returns 403 read-only message with no write — fail-safe regardless of client state (AC4). |
| Test auth flakiness (Telegram initData / cookie) | Low | Medium | Settings-key test needs no auth (403 before auth); `__ping__` + capital-key tests monkeypatch `_validate_admin_session` deterministically (no `BOT_TOKEN`/cookie crafting). |
| UI build regression from removed code | Low | Medium | `npm run build` (`tsc && vite build`) as the UI verification gate; tsconfig `strict` with `noUnusedLocals: false`; change is purely additive-removal of already-unused globals. |
| Accidental change to `GET /api/miniapp` or the bot/telegram write path | Low | High | Explicitly pinned unchanged; File Manifest lists untouched files; full `tests/` suite re-run for regressions. |
