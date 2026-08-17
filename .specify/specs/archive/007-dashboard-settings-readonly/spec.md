# Feature Specification: 007 — Dashboard Settings Read-Only

**Feature Branch**: `007-dashboard-settings-readonly` *(implementation on `main` per repo convention — no feature branches, the repo tracks only main)*

**Created**: 2026-08-12

**Status**: Draft — awaiting implementation authorization

**Flow**: constitution → specify → clarify → plan → checklist → tasks → analyze → **implement (AUTHORIZATION REQUIRED)** → converge

---

## What

The dashboard's Settings page (Mini App) becomes strictly read-only. The operator can still view every setting's current value — the display is fully powered by the existing read-only `GET /api/miniapp` — but the edit path is locked: `POST /api/miniapp` no longer writes settings, and any attempt to save a real setting returns a clear "read-only" 403. The `__ping__` auth probe stays untouched (Capital Manager and browser session detection depend on it). Capital management (Capital Manager page) remains fully editable — the operator still manages declared capital in the UI. The bot keeps reading settings fresh each cycle; only the dashboard's editing path is locked. The operator manages settings locally via the AI assistant + `push-db.sh` (DB push to the VPS), and the Telegram bot's setting commands remain the remote control (`telegram.py` untouched).

### Part 1 — Settings page forced read-only (`dashboard-ui/src/MiniSettings.tsx`)

- The Settings page stays fully **visible** — read-only means no editing, not hidden: the tab remains and every current value stays viewable, just never editable.
- The settings editor is ALWAYS read-only: `editable` is forced permanently `false`. The `isTelegram`/`__ping__` edit-enablement is removed — the module-level `isTelegram`, `browserSessionChecked`, `browserSessionValid` globals and the probe effect that POSTs `{ key: '__ping__' }` to `/api/miniapp` are gone.
- The existing disabled-input read-only rendering is kept as-is: every input (preset `ComboList`, `NumberField`, `TextField`) already renders with `disabled={!editable}` — unchanged, so the read-only look already exists and only needs to be forced permanently.
- Header/copy is updated to clearly state read-only + managed via DB (e.g., subtitle "Settings are read-only — managed via DB"), replacing the "Read-only outside Telegram" / "Open via Telegram bot to edit" copy; styling preserved.
- **`dashboard-ui/src/CapitalManager.tsx` is NOT touched** — capital management stays editable (the operator still manages capital in the UI), and its `__ping__` probe keeps working because the backend probe path is retained.

### Part 2 — Backend write path locked (`dashboard/main.py` `POST /api/miniapp`)

- The `__ping__` auth probe path is kept exactly as-is: no DB write, auth-checked (403 "Invalid auth" when unauthenticated, `{"ok": True}` when authenticated) — Capital Manager's browser-session detection and the Mini App's session detection depend on it.
- For any **real** setting key (anything other than `__ping__`), the endpoint returns **403** with a clear, user-actionable message (e.g., "settings are read-only — manage via local DB push") and performs **no DB write** — the auth-gated `INSERT ... ON CONFLICT ... DO UPDATE` write path (plus post-save validation and Telegram alert) is removed from the endpoint.
- `GET /api/miniapp` is unchanged — it continues to power the full read-only settings display (read-only means no editing, not hidden).
- No change to the DB write path used by `bot.py` / `telegram.py`.

### Part 3 — Test

- A small dashboard test following the existing dashboard test conventions (`tests/test_closed_trades_page.py`: FastAPI `TestClient`, throwaway SQLite DB via `monkeypatch.setattr(api, "DB_PATH", ...)`):
  - `POST /api/miniapp` with a real key → **403** with the read-only message, and the stored setting value is **unchanged** (seed the `settings` table first, assert the value still equals the seed after the call).
  - `__ping__` still behaves as before — no write and the same status/behavior as the retained probe path.

### Part 4 — Docs

- `docs/CHANGELOG.md` — `ux:` entry (the change is a UX/behavior change to the dashboard; `feat:` is also acceptable per convention).
- `docs/CURRENT_STATE.md` — a short note documenting that dashboard settings are read-only and managed via local DB push.

---

## Clarifications

### Session 2026-08-12

- Q: Settings tab — hide it or keep it visible but read-only? → A: Option A — keep the Settings tab visible but fully read-only (all inputs disabled + "managed via DB" copy).
- Q: Lock only the dashboard settings editor, or also lock the Telegram setting commands? → A: Option A — lock only the dashboard settings editor; `telegram.py` is untouched and Telegram setting commands remain the operator's auth-gated remote control (including toggling `auto_trade` on/off when funding), matching the out-of-scope decision.

---

## Why

The operator manages settings locally via an AI assistant + `push-db.sh` (DB push to the VPS), so the dashboard settings editors are unnecessary and risk accidental config drift. This session already found `cex_slot_pct` had drifted to **90** (expected 40) and SL settings to losing values, likely via UI/Telegram edits. Because the bot reads settings fresh each cycle (Constitution operational rule "Settings are read fresh each cycle"), a drifted value takes effect immediately without restart — silently changing live risk (slot sizing, stop distances). Locking the dashboard's settings edit path removes one drift vector while keeping the display useful for monitoring. Capital management stays editable in the UI (the operator still manages capital there), and the Telegram bot's setting commands remain the operator's remote control — only the dashboard's settings editing path is locked.

---

## Resolved decisions (from feature description)

| # | Decision |
|---|---|
| Q1 | `MiniSettings.tsx` settings are **always read-only** — `editable` forced `false`; `isTelegram`/`__ping__` edit-enablement (module globals + probe effect) removed; the existing disabled-input read-only rendering is kept; header/copy updated to "read-only — managed via DB". |
| Q2 | `dashboard/main.py` `POST /api/miniapp`: keep the `__ping__` no-write auth probe path unchanged; any real setting key returns **403** with a clear "settings are read-only — manage via local DB push" message and performs **no DB write**. |
| Q3 | `GET /api/miniapp` unchanged — the read-only settings display stays fully functional (read-only means no editing, not hidden). |
| Q4 | `CapitalManager.tsx` untouched — stays editable (operator manages capital in the UI); its `__ping__` probe keeps working via the retained backend probe path. |
| Q5 | No change to `telegram.py` (Telegram setting commands remain the operator's remote control), no DB changes, no change to how `bot.py` reads settings. |
| Q6 | Implementation on `main` directly — no feature branches (repo convention). |

---

## Layout

### 1. `dashboard-ui/src/MiniSettings.tsx` — settings always read-only

- Force `editable` permanently `false`: remove the `isTelegram`/`__ping__` edit-enablement — the module-level `isTelegram`, `browserSessionChecked`, `browserSessionValid` globals and the `useEffect` that POSTs `{ key: '__ping__' }` to `/api/miniapp` are removed; `editable` becomes a constant `false` (no `useState`).
- Keep the existing disabled-input read-only rendering unchanged: all `ComboList` / `NumberField` / `TextField` inputs already render `disabled={!editable}`.
- Update the header/badge/footer copy to clearly state read-only + managed via DB (e.g., subtitle "Settings are read-only — managed via DB"); keep the existing styling classes and the read-only badge look.
- **Do NOT touch `dashboard-ui/src/CapitalManager.tsx`.**

### 2. `dashboard/main.py` — harden `POST /api/miniapp` (write path locked)

- Keep the `__ping__` auth probe path exactly as-is (no-write auth probe: 403 "Invalid auth" when unauthenticated, `{"ok": True}` when authenticated).
- For any real setting key: return **403** with a clear, user-actionable message (e.g., "settings are read-only — manage via local DB push") and perform **no DB write** — the auth/validation/`INSERT ... ON CONFLICT`/post-save-validation/Telegram-alert write path is removed from the endpoint.
- `GET /api/miniapp` unchanged (read-only display).
- No change to the DB write path used by `bot.py` / `telegram.py`.

### 3. Test — new dashboard test (e.g., `tests/test_dashboard_settings_readonly.py`)

- Follow the `tests/test_closed_trades_page.py` conventions: FastAPI `TestClient`, throwaway SQLite DB via `monkeypatch.setattr(api, "DB_PATH", ...)`, seeded `settings` table.
- AC4/AC5: `POST /api/miniapp` with a real key → **403** with the read-only message and the stored value unchanged; `__ping__` → no write and the same status/behavior as the retained probe path.

### 4. Docs

- `docs/CHANGELOG.md` — `ux:` entry (short, per the `type: short description` convention).
- `docs/CURRENT_STATE.md` — short note: dashboard settings are read-only, managed via local DB push.

---

## Scope

**In scope**

- `dashboard-ui/src/MiniSettings.tsx` — force settings read-only; remove `isTelegram`/`__ping__` edit-enablement; keep the disabled read-only rendering; update header/copy.
- `dashboard/main.py` — `POST /api/miniapp` returns 403 for real keys (no write); `__ping__` probe kept; `GET /api/miniapp` unchanged.
- New dashboard test asserting the 403/no-write behavior and the unchanged `__ping__` probe.
- `docs/CHANGELOG.md` + `docs/CURRENT_STATE.md`.

**Out of scope**

- No change to `dashboard-ui/src/CapitalManager.tsx` — capital management stays editable in the UI.
- No change to `telegram.py` — Telegram setting commands remain the operator's auth-gated remote control (including toggling `auto_trade` on/off when funding); only the dashboard's settings editing path is locked.
- No DB schema change / migration; no change to how `bot.py` reads settings (settings still read fresh each cycle).
- No change to the DB write path used by `bot.py` / `telegram.py`.
- No new auth scheme; the `__ping__` auth probe and Telegram-initData / admin-session validation stay as-is.
- No change to any trading hot-path module or trading behavior.

---

## Constraints

- **Minimum modification.** Preserve existing styles, behavior, and functionality not directly related to the request (minimal-change policy in `.github/instructions/minimal-change.instructions.md`).
- **Display stays functional.** Read-only means no editing, not hidden — `GET /api/miniapp` continues to power the full settings display.
- **Capital management stays editable.** `CapitalManager.tsx` and its `__ping__` probe are untouched; the backend probe path must keep working.
- **`__ping__` probe unchanged.** The no-write auth probe used by MiniSettings' browser-session detection and Capital Manager keeps its exact behavior.
- **Bot read path unchanged.** The bot still reads settings fresh each cycle (Constitution operational rule "Settings are read fresh each cycle"); only the dashboard's editing path is locked.
- **Constitution I–VIII unaffected.** This is operator/UX tooling, not the trading hot path; no trading behavior changes.
- **Implementation on `main` directly** — no feature branches (repo convention).

---

## Assumptions

- The operator manages settings locally via the AI assistant + `push-db.sh` (DB push to the VPS); Telegram setting commands (`telegram.py`) remain the operator's auth-gated remote control (including toggling `auto_trade` on/off when funding).
- The exact 403 read-only message wording is pinned during planning; it must be user-actionable and distinct from "Invalid auth".
- The dashboard test follows the existing `tests/test_closed_trades_page.py` conventions (FastAPI `TestClient`, throwaway SQLite, monkeypatched `DB_PATH`).
- `editable` becomes a constant `false` in `MiniSettings.tsx`; the existing disabled-input rendering already provides the read-only look, so no new UI styling is needed.
- The `settings` table write path used by `bot.py` / `telegram.py` is unaffected — only the dashboard endpoint's write path is locked.

---

## Acceptance criteria

1. **Settings page always read-only but visible** — opening the dashboard Settings page shows the full settings list but never editable controls: the tab stays visible (not hidden) and every input (preset dropdowns, number fields, text fields) renders disabled regardless of context (Telegram or browser).
2. **No edit-enablement probes** — `MiniSettings.tsx` no longer POSTs the `__ping__` probe and no longer derives editability from `isTelegram`; `editable` is permanently `false`.
3. **Clear read-only copy** — the Settings page header/badge/footer copy states the settings are read-only and managed via the database (e.g., "Settings are read-only — managed via DB"); styling preserved.
4. **Backend write path locked** — `POST /api/miniapp` with a real setting key returns **403** with a clear message (e.g., "settings are read-only — manage via local DB push") and performs **no DB write**; the stored value is unchanged.
5. **`__ping__` probe unchanged** — `POST /api/miniapp` with `key == "__ping__"` keeps its exact behavior: no DB write, auth-checked (403 "Invalid auth" when unauthenticated, `{"ok": True}` when authenticated) — Capital Manager's browser-session detection still works.
6. **Display fully functional** — `GET /api/miniapp` is unchanged; the Settings page still shows every setting's current value (read-only means no editing, not hidden).
7. **Capital management unaffected** — `CapitalManager.tsx` still edits declared capital, and its `__ping__` probe still enables editability in the browser/Telegram exactly as before.
8. **Bot & Telegram read/write paths untouched** — `bot.py` still reads settings fresh each cycle; `telegram.py` setting commands still write settings and remain the operator's auth-gated remote control (including toggling `auto_trade` on/off when funding); no DB schema change.
9. **Test coverage** — a dashboard test (FastAPI `TestClient`, following `tests/test_closed_trades_page.py` conventions) asserts: a real key → 403 + stored value unchanged; `__ping__` → same status/behavior as before.
10. **Docs** — `docs/CHANGELOG.md` gains a `ux:` (or `feat:`) entry; `docs/CURRENT_STATE.md` gains a short note.
11. **Minimal footprint** — only `dashboard-ui/src/MiniSettings.tsx`, `dashboard/main.py`, a new test file, and the two docs are touched; no other UI component, endpoint, or module changes.
