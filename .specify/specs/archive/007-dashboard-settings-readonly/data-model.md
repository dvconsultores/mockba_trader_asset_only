# Data Model: Dashboard Settings Read-Only (feature 007)

**Date**: 2026-08-12 | **Branch**: `main`

This feature makes the dashboard's Settings page read-only. It changes **no
database schema**; the only data-model-relevant changes are (a) the UI state
model of `MiniSettings` and (b) the HTTP contract of `POST /api/miniapp` (see
`contracts/miniapp-api.md`).

---

## 1. `settings` table — UNCHANGED

`db/schema_v2.sql` lines 5–10 (idempotent):

```sql
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- **No schema change, no migration, no default change.**
- Write paths that remain: `bot.py` / `telegram.py` (upsert), the dashboard
  capital-key write path (retained — §2), and `push-db.sh` (operator DB push).
- The bot still reads settings fresh each cycle (Constitution operational
  rule) — untouched.

## 2. Settings-key families (behavioral split introduced by 007)

| Family | Keys | Dashboard behavior after 007 |
|---|---|---|
| Settings-page keys (MiniSettings) | `dip_min_pct`, `pump_min_pct`, `tp_min_pct`, `sl_min_pct`, `sl_min_pct_spot`, `cooldown_sec`, `max_concurrent_positions`, `min_entry_spacing_pct`, `leverage`, `daily_loss_limit_pct`, `max_consecutive_losses`, `adaptive_enabled`, `dip_k`, `tp_k`, `atr_period`, `max_hold_minutes_spot`, `max_hold_minutes_futures`, `max_leverage`, `slope_threshold`, `assumed_slippage_pct`, `min_net_edge_pct`, `binance_blocklist` (+ any unknown key) | **Read-only**: `POST /api/miniapp` → 403 `"settings are read-only — manage via local DB push"`, no write. Display via `GET /api/miniapp` unchanged. |
| Capital keys (CapitalManager) | `capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct` | **Editable (unchanged)**: `POST /api/miniapp` keeps auth → upsert → post-save validation → Telegram alert → 200. |
| Probe key | `__ping__` | **Unchanged**: no write; 403 `"Invalid auth"` unauthenticated, `{"ok": True}` authenticated. |

The two families are **disjoint** (verified against `MiniSettings.tsx`
PRESETS/FREE_INPUTS/TEXT_INPUTS and `CapitalManager.tsx`
`DECLARED_KEY`/`SLOT_PCT_KEY` — `research.md` §1.6/§2.3).

## 3. UI state model — `MiniSettings.tsx`

| Symbol | Before | After |
|---|---|---|
| `isTelegram` (module global, line 7) | `!!TG?.initData` — enabled editing in Telegram | **removed** |
| `browserSessionChecked` / `browserSessionValid` (lines 8–9) | browser probe state | **removed** |
| `editable` (line 350) | `useState(isTelegram)`, settable by the `__ping__` probe (lines 367–373) | **`const editable = false`** |
| Inputs (lines 473/496/507) | `disabled={!editable}` | unchanged — permanently `disabled` |
| Header subtitle (line 453) / footer (line 515) | `'Read-only outside Telegram'` / `'Open via Telegram bot to edit'` | `'Settings are read-only — managed via DB'` / `'Settings are read-only — manage via local DB push'` |
| `saveSetting`/`debouncedSave`/`selectAndSave`, `saving`/`errors`, `timers` | active edit plumbing | **inert** (unreachable; kept per minimal-change policy) |

`CapitalManager.tsx` state model is **untouched** (`editable` still derived
from `isTelegram` + `__ping__` probe; Declared Capital / Slot % still writable).

## 4. Server-side constant (new)

`dashboard/main.py` gains one module-level constant (design-level):

```
CAPITAL_SETTING_KEYS = {"capital_cex_usdt", "capital_dex_usdc",
                        "cex_slot_pct", "dex_slot_pct"}
```

Used only by `api_miniapp_update` to decide 403-vs-write for real keys.

## 5. State transitions

None — this feature introduces no new persisted state and no state machine.
The only "transition" is behavioral: a real settings key POST that previously
transitioned the DB value now terminates with 403 and leaves the stored value
unchanged.

## Validation

- No new settings-rules entries (settings validation in `trade/settings_rules.py`
  is untouched; the dashboard's capital-key write path still runs the existing
  post-save `validate_all`).
- Tests assert the split: settings key → 403 + stored value unchanged;
  `__ping__` → unchanged probe; capital key → still written (AC4/AC5/AC7).
