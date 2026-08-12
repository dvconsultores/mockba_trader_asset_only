# Contract: `/api/miniapp` (Mini App settings + capital)

**Feature**: 007-dashboard-settings-readonly | **Date**: 2026-08-12

The Mini App API consumed by the React dashboard (`dashboard-ui/src/`).
Producers: `dashboard/main.py`. Consumers: `MiniSettings.tsx` (settings
display), `CapitalManager.tsx` (capital edit + editability probe), `StatusBar.tsx`
(editability probe). This contract is enforced by
`tests/test_dashboard_settings_readonly.py`.

## Endpoints

### `GET /api/miniapp` — UNCHANGED

- **Auth**: none.
- **Response 200**: `{"ok": true, "settings": {key: value, …}}` — every row in
  the `settings` table, keyed, ordered by `key`.
- **Response 500**: `{"detail": "<error>"}` on DB failure.
- Powers the full read-only settings display (read-only means no editing, not
  hidden).

### `POST /api/miniapp` — CHANGED (settings keys locked; capital keys retained)

**Request body**: `{ "key": string, "value": string }`.

| `key` | Behavior after 007 | Response |
|---|---|---|
| missing / empty | unchanged guard | **400** `{"detail": "Missing key"}` |
| `__ping__` | **unchanged** no-write auth probe (browser-session detection for CapitalManager + StatusBar) | unauthenticated → **403** `{"detail": "Invalid auth"}`; authenticated → **200** `{"ok": true}` |
| capital keys: `capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct` | **unchanged** write path: Telegram-initData/admin-session auth → `INSERT … ON CONFLICT(key) DO UPDATE` → post-save `validate_all` → Telegram alert on error/warn → success | unauthenticated → **403** `{"detail": "Invalid auth"}`; authenticated → **200** `{"ok": true, "key", "value", "alerts"}` |
| any other real key (settings-page keys + unknown keys) | **NEW** — read-only: no auth check, **no DB write**, no validation, no alert | **403** `{"detail": "settings are read-only — manage via local DB push"}` |

## Rules

1. **Settings keys are read-only (AC4).** Any real key outside
   `CAPITAL_SETTING_KEYS` returns 403 with the pinned message and performs no
   write — the stored value is provably unchanged.
2. **Capital stays editable (AC7).** The four capital keys keep their exact
   pre-007 write contract; the Capital page's Declared Capital and Slot % edits
   keep working. *(Documented deviation from the spec's literal "any real
   setting key returns 403" wording — grounded in `research.md` §2.3: there is
   no `POST /api/capital`; capital writes flow through this endpoint.)*
3. **`__ping__` is a pure auth probe (AC5).** Never writes; 403 `"Invalid
   auth"` when unauthenticated; `{"ok": true}` when authenticated.
4. **Display unchanged (AC6).** `GET /api/miniapp` is byte-for-byte the same.
5. **The `settings` write path used by `bot.py` / `telegram.py` / `push-db.sh`
   is untouched** — only the dashboard endpoint's real-key write surface is
   narrowed to capital keys.

## Validation

- `tests/test_dashboard_settings_readonly.py` asserts: settings key → 403 +
  stored value unchanged; `__ping__` unauthenticated → 403 "Invalid auth";
  `__ping__` authenticated (monkeypatched `_validate_admin_session`) → 200
  `{"ok": true}`; missing key → 400; capital key → 200 + value written.
- `dashboard/main.py` `CAPITAL_SETTING_KEYS` is the single source of truth for
  the settings-vs-capital split.
