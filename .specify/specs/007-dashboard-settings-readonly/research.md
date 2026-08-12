# Research: Dashboard Settings Read-Only (feature 007)

**Date**: 2026-08-12 | **Branch**: `main` | **Spec**: `spec.md`

Every anchor below was verified against the actual repository code
(2026-08-12). Line numbers refer to the current files; recount at edit time.

---

## 1. `dashboard-ui/src/MiniSettings.tsx` — edit-enablement and copy

### 1.1 Module globals (lines 6–9)

```ts
const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData
let browserSessionChecked = false
let browserSessionValid = false
```

- `TG` (line 6) is **kept**: used at line 362 (`if (TG) { TG.ready(); TG.expand() }`)
  and line 386 (`if (TG?.initData) headers['X-Telegram-InitData'] = TG.initData`
  inside `saveSetting`).
- `isTelegram` (line 7), `browserSessionChecked` (line 8),
  `browserSessionValid` (line 9) are **removed**. `MiniSettings` defines its own
  `isTelegram` — it does **not** import from `./TelegramProvider` (unlike
  `CapitalManager.tsx` line 3), so removing it has no cross-file impact.
  `browserSessionValid` is only ever *written* (line 371) and never read — it is
  dead today.

### 1.2 `editable` state (line 350)

```ts
const [editable, setEditable] = useState(isTelegram)
```

Becomes `const editable = false` (spec: "no `useState`"). `setEditable` is only
used by the probe effect (lines 371–372), so it disappears with the state.
`useState` remains imported (settings/saving/errors/loaded states at lines
346–349 stay). `useEffect` remains imported (the settings-load effect at line
356 stays).

### 1.3 `__ping__` probe effect (lines 367–373)

```ts
useEffect(() => {
  if (isTelegram || browserSessionChecked) return
  browserSessionChecked = true
  fetch('/api/miniapp', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: '__ping__', value: '' }) })
    .then(r => { browserSessionValid = r.ok; setEditable(r.ok) })
    .catch(() => setEditable(false))
}, [])
```

**Removed** entirely (7 lines). This is the only place MiniSettings POSTs
`__ping__`; after removal MiniSettings never POSTs to `/api/miniapp`.

### 1.4 Read-only rendering (already exists — keep unchanged)

- Line 473: `<ComboList … onChange={v => selectAndSave(p.key, v)} disabled={!editable} />`
- Line 496: `<NumberField … disabled={!editable} … onChange={v => debouncedSave(f.key, v)} />`
- Line 507: `<TextField value={val} disabled={!editable} onChange={v => debouncedSave(f.key, v)} />`

All three already render `disabled={!editable}`. With `editable === false` they
are permanently disabled — the read-only look already exists, no new styling
needed. Disabled inputs never fire `onChange`, so `selectAndSave` /
`debouncedSave` / `saveSetting` become **unreachable**.

### 1.5 Copy anchors

- Line 453 (header subtitle):
  `{editable ? 'Tap a value to change it' : 'Read-only outside Telegram'}`
  inside `<p className="text-xs text-[#7a7090] mt-0.5">` → static
  **`Settings are read-only — managed via DB`**.
- Lines 455–457 (badge):
  `<span className={\`text-[10px] px-2.5 py-1 rounded-full border ${editable ? '…' : 'text-[#4a4060] border-[#2a2240] bg-[#1a1528]'}\`}>{editable ? '⚡ auto-save' : 'read-only'}</span>`
  — the false branch already renders `'read-only'` with the read-only styling;
  **kept unchanged** (no edit needed).
- Line 515 (footer):
  `{editable ? 'Changes save automatically on selection or after typing' : 'Open via Telegram bot to edit'}`
  → static **`Settings are read-only — manage via local DB push`**.

### 1.6 Inert code decision

`saveSetting` (377–392), `debouncedSave` (394–400), `selectAndSave` (402–406),
the `saving`/`errors` states (347–348) and `timers` ref (351) remain referenced
after the change and become unreachable. **Decision: keep inert** — removing
them is extra churn against the minimal-change policy
(`.github/instructions/minimal-change.instructions.md`), and
`dashboard-ui/tsconfig.json` has `noUnusedLocals: false` /
`noUnusedParameters: false`, so `tsc` reports no unused-symbol errors. Deferred
cleanup is listed as out of scope in the plan.

### 1.7 UI build / test surface

`dashboard-ui/package.json` scripts: `dev` (vite), `build` (`tsc && vite
build`), `preview`. **No UI test command exists.** `tsconfig.json`:
`strict: true`, `noEmit: true`, `noUnusedLocals: false`,
`noUnusedParameters: false`. → UI verification = `npm run build` (type-check +
bundle); the **pytest** backend test is the required automated verification.

---

## 2. `dashboard/main.py` — `POST /api/miniapp` write path

### 2.1 Endpoint skeleton (verified)

| Lines | Content | Action |
|---|---|---|
| 536–549 | `@app.get("/api/miniapp")` `api_miniapp_get` — returns `{"ok": True, "settings": {…}}`, no auth | **Unchanged** |
| 552–553 | `@app.post("/api/miniapp")` `api_miniapp_update` | — |
| 554 | docstring `"""Update a single setting. Requires Telegram initData or a valid admin session."""` | Update |
| 555–560 | `body = await request.json()`; `key`/`value` strip; `if not key: raise 400 "Missing key"` | **Keep** |
| 561–572 | `if key == "__ping__":` no-write auth probe (403 `"Invalid auth"` unauthenticated; `{"ok": True}` authenticated via `_validate_telegram_init_data` / `_validate_admin_session`) | **Keep byte-for-byte** |
| 574–620 | real-key path: Telegram-initData auth (576–587) + admin session (588); `db = _get_db_rw()`; `INSERT INTO settings … ON CONFLICT(key) DO UPDATE` (594); `validate_all` post-save loop (597–608); `_send_validation_alert` (618); `return {"ok": True, "key":…, "value":…, "alerts":…}` (620) | **Replace with 403** |

### 2.2 Consumers of `POST /api/miniapp` (all UI files — verified by grep)

| File | Lines | Payload | After 007 |
|---|---|---|---|
| `MiniSettings.tsx` | 370 | `__ping__` probe | probe removed (component no longer POSTs) |
| `MiniSettings.tsx` | 380 | **real settings keys** (`saveSetting`) | unreachable (`editable === false`) |
| `CapitalManager.tsx` | 167–173 | `__ping__` probe | untouched — keeps working |
| `CapitalManager.tsx` | 179 (via 249) | **real capital keys** (`saveSetting`) | untouched — **must keep working (AC7)** |
| `StatusBar.tsx` | 79 | `__ping__` probe | untouched — keeps working |

### 2.3 CRITICAL FINDING — CapitalManager writes real keys through `/api/miniapp`

`CapitalManager.tsx` defines per-venue **settings-table keys** (lines 29–30):

```ts
const DECLARED_KEY: Record<string, string> = { binance: 'capital_cex_usdt', orderly: 'capital_dex_usdc' }
const SLOT_PCT_KEY: Record<string, string> = { binance: 'cex_slot_pct', orderly: 'dex_slot_pct' }
```

and its `saveSetting` (lines 176–194) POSTs `{ key, value }` to
**`/api/miniapp`** (line 179), called from `renderEditable`'s `commit` (line
249) for "Declared Capital" and "Slot %". `dashboard/main.py` has **no
dedicated `POST /api/capital`** — only `GET /api/capital` (line 627). The
Capital page's `toggleVenue` writes `auto_trade_*` via `POST /api/bot/control`
(line 761) and `toggleBlacklist` via `PUT /api/universe/…/blacklist` (line
712) — those are separate endpoints, but **Declared Capital and Slot % go
through `POST /api/miniapp`**.

**Consequence**: a blanket "any real key → 403" (spec Q2's literal wording)
would break AC7 ("CapitalManager.tsx still edits declared capital"). The
spec's own AC7 is non-negotiable, so the backend must distinguish the two
real-key families:

- **Capital keys** (keep the existing auth + write + validate + alert path):
  `capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct`.
- **All other real keys** (the Settings-page keys in §1.6, plus any unknown
  key): **403 `settings are read-only — manage via local DB push`**, no write,
  no validation, no alert.

This is a **required, documented deviation** from the literal "any real setting
key returns 403" wording (spec Q2 / feature description), introduced to satisfy
AC7 and the spec's own "Capital management … remains fully editable" scope
line. It changes the diff shape: instead of deleting lines 574–620, the write
path is retained behind a capital-key guard with a small module-level
allow-list.

### 2.4 Helpers that must stay

- `_get_db_rw` (line 529) — still used by `_upsert_setting` (line 56), the
  asset-blacklist endpoint (line 726), `/api/bot/control` (line 797) **and by
  the retained capital write path**. **No dead-code cleanup.**
- `_validate_telegram_init_data` (line 450), `_validate_admin_session` (line
  473), `_send_validation_alert` (line 394) — still used by other endpoints
  (`/api/admin/login`, `/api/bot/control`, `_check_auth`, blacklist) **and by
  the retained capital write path**. **Keep.**

### 2.5 Pinned 403 message

`settings are read-only — manage via local DB push` — spec-delegated wording;
user-actionable, distinct from `"Invalid auth"`. Returned for any real
**non-capital** key **before** the auth check (the endpoint writes nothing for
those keys, so auth is meaningless and the 403 test becomes deterministic).

---

## 3. Test conventions (`tests/test_closed_trades_page.py`)

- Module docstring listing the acceptance criteria covered.
- `ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` +
  `sys.path.insert(0, ROOT)`.
- `import dashboard.main as api`; `from fastapi.testclient import TestClient`.
- `client` fixture: `db_path = str(tmp_path / "test.db")`; create the table;
  `monkeypatch.setattr(api, "DB_PATH", db_path)`; `with TestClient(api.app) as
  c: yield c, db_path`.
- Seed helper writes rows directly via `sqlite3.connect(db_path)`.
- **No existing test covers `/api/miniapp` or the `settings` table** (grep
  across `tests/` found none) — the new test file is the first.

`settings` DDL for the fixture (mirrors `db/schema_v2.sql` lines 5–10):

```sql
CREATE TABLE settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Run command (sandbox tmpdir quirk — repo memory): `--basetemp=.pytest_tmp`.

### 3.1 Deterministic auth

- Settings-key 403 test: **no auth needed** — the new code raises 403 before
  any auth check.
- Unauthenticated `__ping__`: no auth — deterministic 403 `"Invalid auth"`.
- Authenticated `__ping__` **and the capital-key write test**: monkeypatch
  `api._validate_admin_session` with `async def _ok(request): return True`
  (the `__ping__` branch awaits it at line 569; the capital write path awaits
  it at line 588). This avoids crafting a valid HMAC Telegram initData or a
  signed session cookie (both require `BOT_TOKEN`, which may be unset in the
  test env). `_validate_telegram_init_data` is not reached because the request
  carries no `X-Telegram-InitData` header.

---

## 4. Docs conventions

- `docs/CHANGELOG.md`: `type: short description` (per `how-to-work-with-specs.md`);
  a `## 2026-08-12` heading already exists (feature 006) — append the 007
  `ux:` entry under it.
- `docs/CURRENT_STATE.md`: header carries `> Updated: 2026-08-12 | Feature 006
  — Spot Exit Hardening`; newest feature sections sit at the top
  (`## 0. Spot Exit Hardening …`, `## 0. Market Conditions Check …`). For 007:
  bump `> Updated:` and insert a `## 0. Dashboard Settings Read-Only (feature
  007, 2026-08-12)` section right after the header.

---

## 5. Decisions (Decision / Rationale / Alternatives)

- **Decision**: `editable` becomes `const editable = false` (no `useState`).
  **Rationale**: the only writers were the initializer and the probe effect;
  both are removed; spec requires "no `useState`". **Alternatives**: keeping
  `useState(false)` (extra churn for no behavior) — rejected.
- **Decision**: remove the `__ping__` probe from MiniSettings but keep the
  backend `__ping__` branch byte-for-byte. **Rationale**: CapitalManager and
  StatusBar still probe; spec AC5/AC7. **Alternatives**: keeping MiniSettings'
  probe (contradicts AC2) or removing the backend branch (breaks CapitalManager
  browser-session detection) — both rejected.
- **Decision**: **allow-list the four capital keys** — `POST /api/miniapp`
  keeps the write path only for `capital_cex_usdt`, `capital_dex_usdc`,
  `cex_slot_pct`, `dex_slot_pct`; **all other real keys → 403**
  `"settings are read-only — manage via local DB push"` before any auth.
  **Rationale**: CapitalManager writes these four real keys through this same
  endpoint (§2.3) and AC7 requires capital editing to keep working; the 403
  (no write) satisfies AC4 for the Settings-page keys. **Alternatives**:
  blanket 403 for every real key (breaks AC7 — rejected); a separate
  `POST /api/capital` endpoint (bigger change; CapitalManager rewrite — out of
  scope — rejected). This is the **documented deviation** from the literal
  "any real setting key returns 403" wording.
- **Decision**: leave `saveSetting`/`debouncedSave`/`selectAndSave`/
  `saving`/`errors`/`timers` inert rather than deleting. **Rationale**:
  minimal-change policy; unreachable; `tsconfig` won't flag them. **Alternatives**:
  delete them (cleaner but a much larger diff touching working code; risk of
  subtle breakage) — deferred, out of scope.
- **Decision**: UI verification = `npm run build` (`tsc && vite build`);
  automated verification = pytest. **Rationale**: no UI test command exists
  (package.json has no `test` script). **Alternatives**: adding a UI test
  harness (new dependency/tooling, out of scope for this feature).
- **Decision**: branch `main`. **Rationale**: repo convention — the repo tracks
  only main, no feature branches (spec Q6).
