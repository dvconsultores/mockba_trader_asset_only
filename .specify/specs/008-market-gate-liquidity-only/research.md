# Research: Market Gate — Liquidity-Only Suspension (008)

**Feature**: 008-market-gate-liquidity-only | **Date**: 2026-08-12

Code-verified facts from the live repo (`trade/market_check.py`, `bot.py`,
`trade/settings_schema.py`, `trade/settings_rules.py`,
`tests/test_market_check.py`, `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`).
Every signature and line number below was read from source, not assumed. This
resolves the two items the spec delegated to planning: (1) how `_gate_apply`
knows a WARN is **strong** (clarify Q1 — pinned in `plan.md`), and (2) the
exact test rows that change.

## 1. `trade/market_check.py` — escalation classification

### 1.1 `_warn_is_strong` (def line 311; docstring 312–316)

```python
def _warn_is_strong(reasons, settings):
    """A WARN escalates to suspension only for a BROAD problem: a liquidity
    fail_share at/above market_gate_warn_liquidity_share, or a trending /
    unknown regime mix (those verdicts only fire at high shares already). A
    lone bad asset (small liquidity_partial) is mild — it must not block the
    whole venue on a small universe (005 follow-up: gate was too strict)."""
    if not reasons:
        return False
    share_thr = settings.get("market_gate_warn_liquidity_share", 0.25)
    for r in reasons:
        if r.startswith("liquidity_partial="):          # line 321
            try:
                return float(r.split("=", 1)[1]) >= share_thr
            except ValueError:
                return False
        if r.startswith("regime_trending=") or r.startswith("regime_unknown="):  # line 326
            return True                                  # line 327
    return False
```

- **The regime branch to change is lines 326–327**: `if
  r.startswith("regime_trending=") or r.startswith("regime_unknown="): return
  True`. It is **unconditional** today — a regime WARN is always strong.
- The liquidity branch (321–325) stays: `liquidity_partial=` ≥
  `market_gate_warn_liquidity_share` (default 0.25) ⇒ strong.
- Only two references in the whole repo (grep `_warn_is_strong`): the def
  (311) and the call inside `update_gate_state` (365). No other consumer — the
  new bot.py consumer is additive.

### 1.2 `update_gate_state` (def line 331; WARN branch 361–373)

```python
    elif verdict == "WARN":                              # line 361
        # Strong (broad) WARN → counts toward suspension like FAIL. Mild WARN
        # (a lone bad asset) → informational only, resets streaks so a blip
        # never trips the gate (see _warn_is_strong).
        if _warn_is_strong(reasons, settings):           # line 365
            new_state["bad_streak"] += 1
            new_state["good_streak"] = 0
            if not new_state["suspended"] and new_state["bad_streak"] >= settings["market_gate_bad_streak"]:
                new_state["suspended"] = True
                transition = {"type": "suspend"}
        else:                                            # line 371
            new_state["bad_streak"] = 0
            new_state["good_streak"] = 0
```

- **WARN branch shape is UNCHANGED**: strong → `bad_streak`+1, suspend at
  `market_gate_bad_streak`; mild → reset both streaks, never suspends. Only
  what `_warn_is_strong` classifies as strong changes (spec Part 1).
- The WARN-branch comment (362–364) and the `update_gate_state` docstring WARN
  line (338–341, "strong (broad) warnings count toward bad_streak … (005
  follow-up)") describe the old "regime always strong" behavior and are
  updated.

### 1.3 `_evaluate` (def line 27; verdict rules 36–63) — UNCHANGED (confirmed)

```python
    if not scan_fresh:                                   # stale ⇒ FAIL
        return "FAIL", ["scan_stale"]
    fail_share = ...
    if fail_share >= settings.get("market_gate_fail_share", 0.5):   # line 46
        return "FAIL", [f"liquidity_fail_share={fail_share:.2f}"]
    if fail_share > 0:                                   # line 55
        return "WARN", [f"liquidity_partial={fail_share:.2f}"]
    trend_share = ...
    if trend_share >= settings.get("market_gate_trend_share", 0.6):  # line 60
        return "WARN", [f"regime_trending={trend_share:.2f}"]
    if unknown_share >= settings.get("market_gate_unknown_share", 0.5):  # line 62
        return "WARN", [f"regime_unknown={unknown_share:.2f}"]
    return "PASS", []
```

- Verdict rules are **untouched**: `regime_trending=`/`regime_unknown=` still
  downgrade PASS→WARN (lines 60, 62); FAIL still comes from stale
  (line 46) / `liquidity_fail_share` (lines 47–48). The operator still sees the
  WARN verdict in the structured `[GATE] venue=… verdict=WARN …` log (AC2).

### 1.4 `_gate_share_settings` (def line 240) — UNCHANGED (confirmed)

Returns only the verdict-aggregation shares
(`market_gate_fail_share`/`trend_share`/`unknown_share`) consumed by
`_evaluate`. It does **not** feed `_warn_is_strong` and carries no
`market_gate_warn_liquidity_share` — the escalation setting travels separately
in the `settings` dict built by `bot._gate_apply`. No change.

## 2. `bot.py` — `_gate_apply` (def line 676)

```python
def _gate_apply(venue: str, report: dict) -> dict:       # line 676
    """Apply one gate evaluation: … WARN also notifies once when it starts
    and once when it clears, always with the reason (005 follow-up). …"""
    settings = {                                         # line 683
        "market_gate_bad_streak": get_setting_int("market_gate_bad_streak", 2),
        "market_gate_good_streak": get_setting_int("market_gate_good_streak", 2),
        "market_gate_warn_liquidity_share": get_setting_float("market_gate_warn_liquidity_share", 0.25),
    }                                                    # line 687
    state = _gate_state.get(venue, {"suspended": False, "bad_streak": 0, "good_streak": 0})
    new_state, transition = update_gate_state(state, report["verdict"], settings,
                                              report.get("reasons"))   # lines 689–690
    …
    # WARN lifecycle — one notification when a WARN starts, one when it clears.  # 701
    # FAIL silently clears the flag (its own suspend/hold message covers it).    # 702
    if report["verdict"] == "WARN" and not state.get("warn_active"):  # line 703
        new_state["warn_active"] = True
        send_message(f"⚠️ [GATE] {label} WARNING — {reason}")          # line 705
        logger.info(f"[GATE] venue={venue} verdict=WARN reason={reason} action=warn_start")  # 706
    elif report["verdict"] != "WARN" and state.get("warn_active"):     # line 707
        new_state["warn_active"] = False
        if report["verdict"] == "PASS":
            send_message(f"✅ [GATE] {label} warning cleared — {reason}")  # line 710
            logger.info(f"[GATE] venue={venue} verdict=PASS reason={reason} action=warn_clear")  # 711
    _gate_state[venue] = new_state                           # line 712
    return new_state
```

- **The settings dict (683–687) already carries `market_gate_warn_liquidity_share`
  and is passed to `update_gate_state` (689–690), which internally calls
  `_warn_is_strong(reasons, settings)`.** This is the reuse point: `_gate_apply`
  can compute the identical strong-ness with the identical inputs.
- **`get_setting_bool` is already imported** (bot.py line 23) — the new
  `market_gate_regime_escalates` read needs no new import.
- bot.py imports `from trade.market_check import check_venue_observed,
  update_gate_state` at **line 34** — `_warn_is_strong` is added there.
- The WARN lifecycle block (701–712) fires ⚠️/✅ **for ANY WARN** today
  (no strong/mild distinction) — this is the exact block clarify Q1 scopes to
  strong/escalating WARNs only.
- **Pinned mechanism (clarify Q1)**: `_gate_apply` reuses
  `_warn_is_strong(report.get("reasons"), settings)` (same dict, same reasons
  as the `update_gate_state` call) and gates the ⚠️/`warn_active` branch on it.
  No new report field — the report contract and its producers stay untouched.
  Rationale and the full mechanism: `plan.md` "Pinned mechanism".

## 3. `trade/settings_schema.py` — gate group (lines 197–216)

`SettingSpec` dataclass (line 13): `key, type, group, unit, hard_min, hard_max,
soft_min, soft_max, short, depends_on=()`. The bool-with-no-ranges shape
template is `market_gate_enabled` (line 199):

```python
    SettingSpec("market_gate_enabled", bool, "gate", None, None, None, None, None,
                "Opt-in master switch for the market-conditions gate (default off — zero behavior change)"),
```

The gate group currently registers **8** `market_gate_*` keys
(enabled 199, interval_min 201, bad_streak 203, good_streak 205, fail_share
207, trend_share 209, unknown_share 211, warn_liquidity_share 213). The new
`market_gate_regime_escalates` row is inserted **after line 213** (after
`market_gate_warn_liquidity_share`, before `market_filter_enabled` at 215) →
**9** keys. `BY_KEY`/`GROUPS` derive from `ALL`; no other registry edit.

**`trade/settings_rules.py`: no change (confirmed).** Grep found no
`market_gate` cross-check in the file (only `get_setting_bool` on
`adaptive_enabled`, line 169). A bool setting with no ranges passes the
generic Amendment 002 validator exactly like `market_gate_enabled`
(`test_settings_validation` already asserts `validate("market_gate_enabled",
True).level == "ok"`). No `depends_on`, no hard/soft ranges → nothing for
`validate` to cross-check.

## 4. `tests/test_market_check.py` — exact rows to touch

| Test | Line | Current assertion | Change |
|---|---|---|---|
| `test_debounce_transitions` | 403 (regime block **449–454**) | "regime WARNs (trending/unknown) are always strong" — `regime_trending=1.00` → `bad_streak` 1; `regime_unknown=1.00` → suspend | Replace with mild-by-default (5× regime WARNs reset both streaks, never suspend) **+** re-enable regression (`market_gate_regime_escalates=True` restores the 005 escalation). Local `settings` here is `{"market_gate_bad_streak": 2, "market_gate_good_streak": 2}` (no `regime_escalates` key) → default `False` applies. |
| `test_transition_notifications_once` | 510 | `warn = {…"reasons": ["liquidity_partial=0.25"]}` — **strong** (0.25 ≥ default 0.25) | **Unchanged** — stays the liquidity-WARN regression guard. |
| `test_warn_lifecycle_notifications` | 546 | `warn = {…"reasons": ["liquidity_partial=0.33"]}` — **strong** | **Unchanged** — stays the liquidity-WARN lifecycle regression guard. |
| `test_settings_validation` | 659 (key list 663–668) | 8-key `market_gate_*` list; no `regime_escalates` validate; `validate_all` dict (≈681–687) has 7 entries | Add the 9th key; `validate("market_gate_regime_escalates", True/False).level == "ok"`; add `"market_gate_regime_escalates": "false"` to the `validate_all` dict. |
| `test_verdict_correctness` | 292 | exercises `_evaluate` only (SHARE_SETTINGS = fail/trend/unknown shares) | **Unchanged** — verdict rules untouched (AC2/AC4). |

New test to add: `test_warn_notifications_strong_only` (AC11) — mild regime
WARN (default) fires **no** ⚠️/✅ and never sets `warn_active`;
`market_gate_regime_escalates=true` restores exactly one ⚠️ on start and one ✅
on PASS. Notification assertions follow the established pattern
`mock.patch("trading_bot.send_bot_message.send_message")`
(see `test_transition_notifications_once`, line 510, and
`test_warn_lifecycle_notifications`, line 546).

## 5. Docs

- **`docs/CURRENT_STATE.md`** — feature-005 gate section is lines 81–189. The
  "Debounce state machine" bullet (lines 140–143) still says **"WARN = neutral
  hold (resets both streaks, never suspends)"** — this predates the 005
  follow-up strong-WARN escalation that is in the code; it is the bullet to
  rewrite for the liquidity-only rule. Settings table lines 180–186 (8 rows) →
  9 rows. New `## 0.` top-level section convention confirmed (feature 006 at
  lines 14–79, feature 007 at 9–12).
- **`docs/CHANGELOG.md`** — `## 2026-08-12` section at the top (feature 007
  `ux:` + feature 006 `feat:` entries); `fix:` entries appear under
  2026-08-09 — the 008 entry is a `fix:` under 2026-08-12.

## 6. Resolved unknowns (plan decisions)

| Unknown (spec delegation) | Resolution |
|---|---|
| How `_gate_apply` knows a WARN is strong (clarify Q1) | **Reuse `trade.market_check._warn_is_strong(report.get("reasons"), settings)`** — already pure, already the single source of truth in `update_gate_state`, and `_gate_apply` already builds the exact `settings` dict it needs. No new report field. Full rationale in `plan.md`. |
| Settings read site | `bot.py` `_gate_apply` settings dict (683–687) gains `"market_gate_regime_escalates": get_setting_bool("market_gate_regime_escalates", False)` — read fresh each evaluation, same dict passed to `update_gate_state`/`_warn_is_strong`. |
| Validation needed? | **None** — bool with no ranges, no `depends_on`; generic Amendment 002 validator (same as `market_gate_enabled`); `settings_rules.py` untouched. |
| Migration? | **None** — default `false` lives in the `get_setting_bool` fallback; unset DB rows behave as `false` (spec Assumptions). |
