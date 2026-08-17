# Data Model: Market Gate — Liquidity-Only Suspension (008)

**Feature**: 008-market-gate-liquidity-only | **Date**: 2026-08-12

No DB schema change, no migration. This feature adds: **one bool setting**
(default in the `get_setting_bool` fallback) and **a WARN escalation
classification change** — no new report fields, no new state, no new tables.
Details cross-ref `research.md` (line references) and
`contracts/warn-escalation.md` (the escalation/notification contract).

## §1 — New setting (registered in `trade/settings_schema.py`)

| Key | Type | Group | Default | Hard range | Soft range | Unit | `depends_on` |
|---|---|---|---|---|---|---|---|
| `market_gate_regime_escalates` | bool | gate | false | — | — | — | — |

- **Default lives in the `get_setting_bool("market_gate_regime_escalates",
  False)` fallback** at the two read sites (bot.py `_gate_apply` settings dict;
  via the `settings` dict in `trade.market_check._warn_is_strong`) — **no DB
  migration**, read fresh each evaluation (Constitution: settings read fresh
  each cycle).
- Registered as a `SettingSpec` (bool, group `"gate"`, all ranges `None` —
  the exact `market_gate_enabled` shape) so the Amendment 002 deterministic
  validator (`trade/settings_rules.py`) and UI/Telegram pick it up
  automatically (`BY_KEY`/`GROUPS` derive from `ALL`). No `settings_rules.py`
  cross-check: bool with no ranges, nothing to validate (confirmed in
  `research.md` §3).
- `market_gate_*` key count: **8 → 9** (asserted by `test_settings_validation`).

## §2 — WARN escalation classification (the core change)

The gate's WARN branch is driven by one pure classifier,
`_warn_is_strong(reasons, settings)` (`trade/market_check.py` line 311),
consumed by **both** the state machine (`update_gate_state`, line 365) and the
notification gate (`bot._gate_apply`, new consumer). Full contract:
`contracts/warn-escalation.md`.

| Reason class | `market_gate_regime_escalates` | Classified | Effect on state machine | Telegram |
|---|---|---|---|---|
| `liquidity_partial=` ≥ `market_gate_warn_liquidity_share` (0.25) | any | **strong** | `bad_streak`+1, suspend at `market_gate_bad_streak` | ⚠️ on start, ✅ on clear |
| `liquidity_partial=` < share | any | **mild** | resets both streaks, never suspends | none (log only) |
| `regime_trending=` / `regime_unknown=` | `false` (default) | **mild** | resets both streaks, never suspends | none (log only) |
| `regime_trending=` / `regime_unknown=` | `true` | **strong** | `bad_streak`+1, suspend at `market_gate_bad_streak` (005 behavior restored) | ⚠️ on start, ✅ on clear |

- Verdict **reasons are unchanged** — `_evaluate` still emits
  `liquidity_partial=`/`regime_trending=`/`regime_unknown=` and still
  downgrades PASS→WARN for the regime classes (AC2). Only the
  escalation classification of regime reasons changes.
- `update_gate_state`'s WARN branch shape is unchanged (strong → streak,
  mild → reset); the mild path can never suspend, so repeated regime WARNs on a
  small universe can no longer block a venue (the 08-11 over-blocking fix).

## §3 — Gate state machine (unchanged)

Per-venue in-memory dict (`_gate_state[venue]` in bot.py):
`suspended: bool, bad_streak: int, good_streak: int, warn_active: bool`.

- PASS → `good_streak`+1 / reset `bad_streak`; resume after
  `market_gate_good_streak`.
- FAIL → `bad_streak`+1 / reset `good_streak`; suspend after
  `market_gate_bad_streak`.
- WARN → strong (see §2) counts like FAIL; mild resets both streaks.
- `warn_active` semantics: set only when a **strong** WARN is notified; cleared
  on a non-WARN verdict (PASS → ✅ "warning cleared"; FAIL → silently, its own
  message covers it). A mild WARN never touches `warn_active` (nothing was
  notified, so nothing to clear). All in-memory, restart resets it (Constitution
  VI, unchanged).

## §4 — Report contract (unchanged — no new fields)

The `check_venue_live`/`check_venue_observed` report
(`contracts/market-report.md`) is **unchanged**: no `strong_warn` flag is
added. Strong-ness is computed at the consumer (`bot._gate_apply`) from the
already-carried `reasons` + the settings dict — see `plan.md` "Pinned
mechanism". Producers (`check_venue_observed`), `format_report`, and the
Telegram renderer are untouched.

## §5 — Notification contract (narrowed trigger scope, clarify Q1)

| Message | Fires when | Text |
|---|---|---|
| ⚠️ WARNING | a **strong** WARN starts (`warn_active` False→True) | `⚠️ [GATE] {CEX|DEX} WARNING — {reason}` |
| ✅ warning cleared | a previously-notified strong WARN ends with PASS (`warn_active` True→False) | `✅ [GATE] {CEX|DEX} warning cleared — {reason}` |
| 🛑 suspended / ✅ recovered | suspend/resume transition (unchanged) | `🛑 [GATE] … suspended — …` / `✅ [GATE] … recovered — …` |

Mild WARNs (default regime, or `liquidity_partial` < share): **no Telegram
message** — visible only in the structured `[GATE] venue=… verdict=WARN …
action=hold` log line (the `action=warn_start`/`action=warn_clear` lines
belong to the strong lifecycle). Debounce mechanics (one message per
transition/lifecycle) unchanged.
