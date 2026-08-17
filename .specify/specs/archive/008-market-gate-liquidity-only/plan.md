# Plan: Market Gate — Liquidity-Only Suspension

**Feature**: 008-market-gate-liquidity-only | **Date**: 2026-08-12 | **Spec**: `specs/008-market-gate-liquidity-only/spec.md`
**Status**: Draft — awaiting implementation authorization
**Branch**: `main` (per spec Q6 — implementation directly on `main`; the repo tracks only main, no feature branches)

**Input**: Feature spec (Draft, Q1 resolved, 11 acceptance criteria). This plan
does not re-litigate resolved decisions; it verifies them against the code and
**pins the one mechanism the spec delegated to planning** — how `_gate_apply`
knows a WARN is strong for the ⚠️/✅ notification gate (clarify Q1).

## Summary

One small, setting-driven change to the feature-005 market gate: **the gate
keeps only its liquidity half for suspension.** `_warn_is_strong` — the pure
escalation classifier in `trade/market_check.py` — stops treating
`regime_trending=`/`regime_unknown=` WARNs as strong by default; they become
**mild** (informational): the verdict still downgrades PASS→WARN (operator
still sees `verdict=WARN` in the structured `[GATE]` log), but regime WARNs no
longer count toward `bad_streak`, never suspend, and no longer fire the ⚠️
Telegram notification. A new bool setting `market_gate_regime_escalates`
(default `false`) lets the operator restore the feature-005 regime escalation
from the DB without a code change. **Pinned mechanism (clarify Q1)**: the ⚠️/✅
notification pair fires **only for strong/escalating WARNs**, and `_gate_apply`
reuses the same `_warn_is_strong(reasons, settings)` classification the state
machine uses (no new report field) — so the notification trigger and the
suspension classification are structurally identical. Liquidity escalation
(`FAIL liquidity_fail_share`, strong `WARN liquidity_partial` ≥
`market_gate_warn_liquidity_share`) is byte-for-byte unchanged. No
`_evaluate` verdict change, no cadence/streak/debounce change, no DB
migration, no `settings_rules.py` change. Restores throughput after the 08-11
over-blocking (3,709 entry skips) — Constitution VIII.

## Technical Context

**Language/Version**: Python 3.11 (repo venv; `./venv/bin/python`)

**Primary Dependencies**: stdlib only — **no new dependencies** (pure logic +
one `get_setting_bool` read already imported in bot.py)

**Storage**: SQLite `data/trading.db` — **no schema change / no migration**;
the new default lives in the `get_setting_bool("market_gate_regime_escalates",
False)` fallback (established Amendment 003/005/006 pattern). Gate state stays
in-memory.

**Testing**: pytest — `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`
(plus the full `tests/` suite for regressions)

**Target Platform**: Linux server (bot.py + telegram.py, supervised by forever.py)

**Project Type**: trading bot, single-repo Python (no framework)

**Performance Goals**: zero new network traffic; the change is one extra pure
boolean read per gate evaluation.

**Constraints**: Constitution VII line budget (small edits to existing
functions, no new module); settings read fresh each cycle; minimum modification
(minimal-change policy, `.github/instructions/minimal-change.instructions.md`);
no import from `trade/main.py`/`trade/signal_agent/`; `dry_run` unchanged;
structured single-line `[GATE]` logs, no emoji in logs.

**Scale/Scope**: 2 venues (binance, orderly) × ≤ ~50 universe members; 11
acceptance criteria; 4 source files + 2 docs.

## Constitution Check

*GATE: evaluated before Phase 0 research and re-checked after Phase 1 design
(below).*

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy (mean reversion) | ✅ | No strategy change. The gate is still a venue-level "should this venue trade now" filter; only the WARN escalation *classification* changes. No ML/LLM. |
| **II** Reward Exceeds Risk (NON-NEGOTIABLE) | ✅ | Untouched. `tp_pct > sl_pct`, net-edge and breakeven gates unchanged; no settings_rules change. |
| **III** No Position Without a Stop (NON-NEGOTIABLE) | ✅ | The gate blocks **new entries only**; stops/exits and position management are untouched (spec explicitly out of scope). Exit management still runs before the gate guard. |
| **IV** Unknown State = No Trading (NON-NEGOTIABLE) | ✅ | Unchanged semantics: `regime_unknown` WARN remains "not good" (still downgrades PASS→WARN); UNKNOWN never counts toward a good verdict; mild WARNs reset streaks (never accidentally "good"); stale/missing data still FAILs closed. |
| **V** Real Fills Only | ✅ | Untouched — the gate reads no PnL and writes no trades; the change is verdict-classification + notification scope only. |
| **VI** Restart Safety | ✅ | Gate state is in-memory and unchanged; restart starts unsuspended and re-establishes within `bad_streak × interval`; no new persistent state. |
| **VII** Simplicity Is a Constraint | ✅ | Minimal lines: `_warn_is_strong` ±3 lines + docstring; `update_gate_state` WARN comment +1; `_gate_apply` one settings-dict key + one boolean gate + one import name; `settings_schema.py` +1 row; no new module; no import from `trade/main.py`/`trade/signal_agent/` (they do not exist in this repo). |
| **VIII** The Bot Trades | ✅ | **Restores throughput**: the redundant regime-trending escalation (3,709 skips on 08-11 on the small spot universe) is demoted to informational, so the gate suspends only on genuine aggregate liquidity collapse. Defaults stay lenient; the re-enable path is operator opt-in (`market_gate_regime_escalates=true`), and liquidity protection (the gate's unique value) is fully preserved. |

**Post-design re-check**: the design below satisfies IV (regime_unknown WARN
still "not good", mild never counts good), VIII (liquidity-only default
removes the redundant regime suspension; re-enable is opt-in), III (entries
only), V (untouched). No gate violation.

## Project Structure

### Documentation (this feature)

```text
.specify/specs/008-market-gate-liquidity-only/
├── spec.md              # authoritative feature spec (Draft, Q1 resolved)
├── plan.md              # this file
├── research.md          # verified line anchors / pinned mechanism evidence
├── data-model.md        # the new setting + escalation classification contract
├── contracts/
│   └── warn-escalation.md  # strong/mild WARN + ⚠️/✅ notification contract
├── quickstart.md        # validation scenarios
└── tasks.md             # Phase 2 — NOT created by this plan
```

### Source Code (repository root)

```text
trade/market_check.py        # _warn_is_strong ±3 lines (regime branch conditioned
                             #   on the new setting) + docstring; update_gate_state
                             #   WARN comment (+1 line). _evaluate / _gate_share_settings
                             #   / update_gate_state logic UNCHANGED.
bot.py                       # _gate_apply: settings dict +1 key (regime_escalates),
                             #   strong-WARN notification gating (⚠️/✅ strong-only),
                             #   docstring + lifecycle comment; import line +_warn_is_strong
trade/settings_schema.py     # +1 SettingSpec ("market_gate_regime_escalates", bool,
                             #   group "gate", default false) — 9th market_gate_* key
tests/test_market_check.py   # test_debounce_transitions regime block (mild default +
                             #   re-enable regression); test_settings_validation 9-key +
                             #   validate; NEW test_warn_notifications_strong_only
docs/CURRENT_STATE.md        # feature-005 gate section escalation sentence + new
                             #   feature-008 section (## 0. convention)
docs/CHANGELOG.md            # +fix: entry (2026-08-12)
```

**Structure Decision**: single-project repo; no new module. The change is
three small edits in existing functions + one schema row + tests/docs —
mirrors the 005 minimal-footprint approach. `trade/main.py`/`trade/signal_agent/`
do not exist in this repo — the never-import constraint is trivially satisfied.

## Research Summary

See `research.md` for the full code-verified inventory. Key findings (all line
numbers read from source):

- **`_warn_is_strong`** (`trade/market_check.py` **line 311**, body 311–328):
  the unconditional regime branch is at **lines 326–327**
  (`if r.startswith("regime_trending=") or r.startswith("regime_unknown="):
  return True`); the liquidity branch (lines 321–325, `>=`
  `market_gate_warn_liquidity_share`, default 0.25) is kept; docstring
  312–316. Only referenced by the def and the `update_gate_state` call
  (line 365) — no other consumer in the repo.
- **`update_gate_state`** (def **line 331**): WARN branch **lines 361–373** —
  strong → `bad_streak`+1 (365–370), mild → reset both (371–373). **Shape
  unchanged**; only the classifier input changes. WARN-branch comment
  (362–364) and docstring WARN line (338–341) updated.
- **`_evaluate`** (def **line 27**; rules 36–63): **verdict rules UNCHANGED** —
  `regime_trending=` (line 60) / `regime_unknown=` (line 62) still downgrade
  PASS→WARN; FAIL still from stale (line 46) / `liquidity_fail_share`
  (47–48). Confirmed: no change (AC2).
- **`_gate_share_settings()`** (def **line 240**): verdict shares only
  (fail/trend/unknown), consumed by `_evaluate` — **UNCHANGED**; it does not
  feed `_warn_is_strong`.
- **`_gate_apply`** (`bot.py` **line 676**): settings dict **lines 683–687**
  already carries `market_gate_warn_liquidity_share` (the exact key
  `_warn_is_strong` needs) and is passed to `update_gate_state`
  (**lines 689–690**) with `report.get("reasons")` — **this is the reuse
  point**. WARN lifecycle block **lines 701–712** (⚠️ at 703–706, ✅ at
  708–711, `warn_active` lifecycle, `_gate_state` write at 712) currently
  fires for ANY WARN. `get_setting_bool` already imported (bot.py **line 23**);
  the `trade.market_check` import line is **bot.py line 34**.
- **`trade/settings_schema.py`**: `SettingSpec` dataclass **line 13**
  (`key, type, group, unit, hard_min, hard_max, soft_min, soft_max, short,
  depends_on=()`); bool-with-no-ranges template is `market_gate_enabled`
  (**line 199**); gate group lines 197–216; `market_gate_warn_liquidity_share`
  at **line 213** (new row goes after it, before `market_filter_enabled`
  line 215). 8 → **9** `market_gate_*` keys.
- **`trade/settings_rules.py`: NO change (confirmed)** — no `market_gate`
  cross-check exists; a bool with no ranges passes the generic Amendment 002
  validator exactly like `market_gate_enabled`.
- **Tests** (`tests/test_market_check.py`): `test_debounce_transitions`
  **line 403** (regime block **lines 449–454**, currently "regime WARNs are
  always strong"; its local `settings` dict has no `regime_escalates` key →
  default `False` applies); `test_transition_notifications_once` **line 510**
  and `test_warn_lifecycle_notifications` **line 546** both use **strong**
  liquidity WARNs (`liquidity_partial=0.25`/`0.33`) → unchanged, they double
  as the liquidity regression guards; `test_settings_validation` **line 659**
  (key list 663–668 = 8 keys; `validate_all` dict ≈681–687);
  `test_verdict_correctness` **line 292** exercises `_evaluate` only →
  unchanged.
- **Docs**: `docs/CURRENT_STATE.md` feature-005 gate section lines 81–189; the
  "Debounce state machine" bullet (lines 140–143) still says **"WARN = neutral
  hold"** (predates the 005-follow-up strong-WARN code) — it is the bullet to
  rewrite. Settings table lines 180–186 (8 rows) → 9 rows. `## 0.` section
  convention confirmed (006 at lines 14–79, 007 at 9–12). `docs/CHANGELOG.md`
  has `## 2026-08-12` at the top; `fix:` entries under 2026-08-09 set the
  convention.

## Pinned Mechanism (clarify Q1) — strong-WARN notification gating

**Decision: `_gate_apply` reuses the existing pure classifier —
`trade.market_check._warn_is_strong(report.get("reasons"), settings)` — no new
report field.**

- **Reuse point (verified)**: `_gate_apply` already builds a `settings` dict
  (bot.py 683–687) and passes it + `report.get("reasons")` into
  `update_gate_state` (689–690), which internally calls
  `_warn_is_strong(reasons, settings)` (market_check.py 365). The same two
  inputs (`report["reasons"]`, the same `settings` dict) give `_gate_apply`
  the identical strong-ness: `warn_strong = _warn_is_strong(report.get("reasons"), settings)`.
- **Notification gate**: the WARN lifecycle branch (703) becomes
  `if report["verdict"] == "WARN" and warn_strong and not state.get("warn_active"):`
  — ⚠️ + `warn_active` are set **only** for strong WARNs. The clearing branch
  (707–711) is unchanged: a previously-notified strong WARN still gets its ✅
  "warning cleared" on PASS (and FAIL silently clears the flag). A mild WARN
  never sets `warn_active`, never sends ⚠️/✅, and is visible only in the
  structured `[GATE] venue=… verdict=WARN … action=hold` log line.
- **Why reuse over a report flag**: (a) `_warn_is_strong` is already pure and
  already the single source of truth for `update_gate_state` — reusing it makes
  the notification trigger and the suspension classification **structurally
  identical (cannot diverge)**; (b) no change to the report contract
  (`contracts/market-report.md`), its producers (`check_venue_observed`/
  `check_venue_live`), or `format_report`; (c) minimal diff — one import name
  on bot.py line 34, one boolean, one `and` in the lifecycle condition. A
  `strong_warn` report flag would require contract + producer changes and could
  drift from the state machine's classification.
- **Consistency guarantee**: both call sites read the same `settings` dict
  (which gains `market_gate_regime_escalates` at 683–687) and the same
  `report["reasons"]`, so strong-ness is identical in the state machine and in
  the notification gate **by construction**.

## Data Model / Contracts

- **One new bool setting** (`data-model.md` §1):
  `market_gate_regime_escalates` — bool, group `gate`, default `false`, no
  unit, no hard/soft ranges, no `depends_on`. Default lives in the
  `get_setting_bool("market_gate_regime_escalates", False)` fallback at the
  two read sites — **no DB migration** (existing DBs behave as `false` until
  the operator sets it). Registered as a `SettingSpec` so the Amendment 002
  validator / UI / Telegram pick it up; `validate(True/False)` → `ok` (same
  shape as `market_gate_enabled`). 9th `market_gate_*` key.
- **WARN escalation contract** (`contracts/warn-escalation.md`): strong =
  `liquidity_partial=` ≥ `market_gate_warn_liquidity_share`, or
  `regime_trending=`/`regime_unknown=` when `market_gate_regime_escalates`
  truthy; mild = `liquidity_partial=` < share, or regime WARNs by default.
  Single source of truth: `_warn_is_strong(reasons, settings)`. Strong →
  `bad_streak`/suspend + ⚠️/✅; mild → reset streaks, log-only.
- **Report contract unchanged** — no new report field (mechanism pinned
  above); `contracts/market-report.md` stays valid.
- **Gate state machine unchanged** — in-memory per-venue dict (`suspended`,
  `bad_streak`, `good_streak`, `warn_active`); transitions identical; only the
  classifier's regime output changes.

## Detailed Design

### Part 1 — `trade/market_check.py`: escalation classification only

**`_warn_is_strong`** (line 311):
- Add `regime_escalates = settings.get("market_gate_regime_escalates", False)`
  next to `share_thr` (line 319).
- Change the regime branch (lines 326–327) from unconditional to
  setting-gated:
  `if (r.startswith("regime_trending=") or r.startswith("regime_unknown=")) and regime_escalates: return True`
- Rewrite the docstring (312–316): strong = liquidity `liquidity_partial=` ≥
  `market_gate_warn_liquidity_share`, or regime WARNs only when
  `market_gate_regime_escalates` is true (default false — liquidity-only
  suspension; the broad-market filter owns macro trends); a lone bad asset
  (small `liquidity_partial`) is mild.
- The liquidity branch (321–325) is untouched.

**`update_gate_state`** (line 331): **no logic change**. Update the WARN-branch
comment (362–364) and the docstring WARN line (338–341) to describe
liquidity-only by default + the `market_gate_regime_escalates` re-enable path.

**`_evaluate`** (line 27) and **`_gate_share_settings`** (line 240): **untouched**
(verdict rules and verdict-share reads unchanged — AC2).

### Part 2 — `bot.py`: read the setting + strong-only notifications

- **Import** (line 34): `from trade.market_check import check_venue_observed,
  update_gate_state, _warn_is_strong`.
- **Settings dict** (683–687): add
  `"market_gate_regime_escalates": get_setting_bool("market_gate_regime_escalates", False),`
  — read fresh each evaluation alongside `market_gate_warn_liquidity_share`
  (`get_setting_bool` already imported, line 23). This single dict feeds both
  `update_gate_state` and the notification gate.
- **WARN lifecycle** (701–712): compute
  `warn_strong = _warn_is_strong(report.get("reasons"), settings)` before the
  block; gate the ⚠️/`warn_start` branch (703–706) on `warn_strong`; the
  clearing branch (707–711) is unchanged. Update the `_gate_apply` docstring
  (677–682) and the lifecycle comment (701–702) to state that ⚠️/✅ fires only
  for strong/escalating WARNs (mild regime WARNs are log-only).
- **Nothing else in bot.py changes** — cadence, streaks, debounce mechanics,
  cold-start warmup, observation recording, entry blocking, `_record_gate_skip`,
  `_broad_market_downtrend` all untouched.

### Part 3 — `trade/settings_schema.py`: register the setting

Insert after `market_gate_warn_liquidity_share` (line 213), before
`market_filter_enabled` (line 215):

```python
    SettingSpec("market_gate_regime_escalates", bool, "gate", None, None, None, None, None,
                "Regime-trending/unknown WARNs escalate to suspension when true (default false — liquidity-only suspension; broad-market filter owns macro trends)"),
```

- 8 → 9 `market_gate_*` keys; `BY_KEY`/`GROUPS` derive automatically.
- **No `settings_rules.py` change (confirmed)**: bool with no ranges, no
  `depends_on` — the generic Amendment 002 validator accepts it exactly like
  `market_gate_enabled`. No hard-range violation cases exist for it.

### Part 4 — Tests (`tests/test_market_check.py`): see Testing Strategy

### Part 5 — Docs: see Docs Update

## Edge Cases

| Edge case | Handling |
|---|---|
| `market_gate_regime_escalates` unset (existing DBs) | `get_setting_bool(..., False)` fallback → regime WARNs mild; existing DBs unaffected until the operator sets the setting (spec Assumptions). |
| Repeated regime WARNs (default) | Mild path: reset both streaks, never suspend — flapping-proof; no venue-wide block from a trending mix on a small universe (the 08-11 event). |
| Operator toggles mid-run | Setting read fresh each `_gate_apply` evaluation (and each `update_gate_state` call) → takes effect without restart (Constitution: settings fresh each cycle). |
| Strong WARN active, then mild WARN | `warn_active` stays True (verdict is still WARN); no duplicate ⚠️ (debounced, one per lifecycle); a later PASS still sends ✅ "warning cleared" — the originally-notified strong WARN is cleared. |
| Mild WARN (never notified) → PASS | `warn_active` never set → no ✅ message (nothing was shown) — correct. |
| Strong WARN → FAIL | FAIL silently clears `warn_active` (its own suspend/hold message covers it) — unchanged. |
| `liquidity_partial` exactly at share (0.25) | `>=` boundary → strong — unchanged from 005. |
| `liquidity_partial` below share | mild — unchanged from 005. |
| FAIL / PASS | untouched — unchanged from 005. |
| `reasons` empty/None | `_warn_is_strong` returns False → mild (defensive; `_evaluate` never produces a bare WARN). |
| Cold start / debounce / cadence | untouched (`_gate_observations_warm`, intervals, streaks, warm-up unchanged). |
| Restart | in-memory state resets; re-establishes within `bad_streak × interval` — unchanged (Constitution VI). |

## Out of Scope

- `_evaluate` verdict rules, gate cadence, streak/debounce thresholds,
  cold-start warmup, observation recording, entry-block mechanics, `/market`
  report, `format_report`.
- Broad-market filter (`_broad_market_downtrend`), per-asset regime gating,
  stale-universe guard, spread-degradation guard, kill switches.
- `market_gate_fail_share`/`trend_share`/`unknown_share`/`warn_liquidity_share`
  defaults and semantics; `trade/settings_rules.py`; DB schema/migration; new
  process/thread/external service.
- Notification **debounce mechanics** (one message per transition/lifecycle) —
  unchanged; only the WARN **trigger scope** narrows to strong/escalating
  WARNs (clarify Q1).

## Testing Strategy (11 acceptance criteria)

Existing `tests/test_market_check.py` (pytest; tmp-DB fixture via
`db.db_ops.DB_PATH` monkeypatch; notification assertions via
`mock.patch("trading_bot.send_bot_message.send_message")` — the established
pattern in `test_transition_notifications_once`/`test_warn_lifecycle_notifications`).

**Updated:**

| Test | Line | Change | AC |
|---|---|---|---|
| `test_debounce_transitions` | 403 (regime block 449–454) | Replace "regime WARNs are always strong" with: (a) **mild by default** — repeated `regime_trending=`/`regime_unknown=` WARNs with the existing local `settings` (no `regime_escalates` key → default `False`) reset both streaks and never suspend; (b) **re-enable regression** — `settings_regime = dict(settings, market_gate_regime_escalates=True)`: `regime_trending=1.00` → `bad_streak` 1, then `regime_unknown=1.00` → suspend (005 behavior restored). The liquidity strong (`liquidity_partial=0.33`) / mild (`0.11`) blocks above stay byte-for-byte (no regime reasons involved). | AC1, AC3–AC7 |
| `test_settings_validation` | 659 | Key list (663–668) gains `"market_gate_regime_escalates"` (9 keys, all group `gate`); add `validate("market_gate_regime_escalates", True).level == "ok"` and `False → ok` (bool, no ranges → no hard-range cases); add `"market_gate_regime_escalates": "false"` to the `validate_all` dict. | AC8 |

**New:**

| Test | Verifies | AC |
|---|---|---|
| `test_warn_notifications_strong_only` | (a) mild regime WARN default: `_gate_apply("binance", {"verdict": "WARN", "reasons": ["regime_trending=1.00"]})` with the setting unset → `send_message` **never** called, `warn_active` stays unset; a subsequent PASS fires **no** ✅ "warning cleared"; (b) `db.upsert_setting("market_gate_regime_escalates", "true")` → the same regime WARN fires exactly one ⚠️ WARNING (message contains `WARNING` + the reason) and sets `warn_active`; a second identical WARN fires nothing (debounced); a following PASS fires exactly one ✅ "warning cleared" and clears `warn_active`; (c) a strong liquidity WARN (`liquidity_partial=0.25`) still notifies (unchanged-path regression guard). | AC11 |

**Unchanged (regression guards):** `test_transition_notifications_once` (line 510)
and `test_warn_lifecycle_notifications` (line 546) use **strong** liquidity
WARNs (`liquidity_partial=0.25`/`0.33`) — they still pass and prove liquidity
WARNs still notify/suspend (AC3–AC5, AC9, AC11). `test_verdict_correctness`
(line 292) — verdict rules untouched (AC2). All other tests
(`test_check_uses_shared_functions`, `test_live_and_observed_same_contract`,
`test_stale_*`, `test_observed_mode_no_api_load`, `test_not_near_zero_trade`,
`test_observed_live_spread_degradation_fails`, `test_disabled_default_no_behavior_change`,
`test_entries_only_never_exits`, `test_manual_report_compact`,
`test_observations_flow_during_suspension`, `test_gate_suspended_records_signal`,
`test_global_daily_loss_pct`, `test_universe_rotate_*`,
`test_broad_market_downtrend_gate`, `test_global_block_records_signal`) are
untouched — none reference regime-WARN suspension or the regime WARN
notification.

Run: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`
(plus the full `tests/` suite for regressions).

## Docs Update

- **`docs/CURRENT_STATE.md`**:
  - feature-005 gate section (lines 81–189): rewrite the "Debounce state
    machine" bullet (lines 140–143, currently the pre-follow-up "WARN =
    neutral hold") to state: strong WARNs (`liquidity_partial` ≥
    `market_gate_warn_liquidity_share`) count toward `bad_streak` and suspend;
    **regime WARNs are informational by default (feature 008 — liquidity-only
    suspension)**; `market_gate_regime_escalates=true` re-enables the 005
    regime escalation; ⚠️/✅ fire only for strong/escalating WARNs. Add the
    `market_gate_regime_escalates` row to the settings table (lines 180–186).
  - New `## 0. Market Gate: Liquidity-Only Suspension (feature 008,
    2026-08-12)` top-level section (following the 006/007 `## 0.` convention):
    the escalation-rule change (`_warn_is_strong` regime branch conditioned on
    the new setting), the new setting (bool, default false, no migration), the
    strong-only ⚠️/✅ notification scope, motivation (08-11, 3,709 skips),
    constitution notes (VIII restores throughput; III/IV/V unaffected).
- **`docs/CHANGELOG.md`** — `fix:` entry under `## 2026-08-12`: "Market gate:
  liquidity-only suspension (008) — regime-trending/unknown WARNs are
  informational by default (mild: never count toward bad_streak, no ⚠️
  notification); new `market_gate_regime_escalates` (default false) re-enables
  the 005 regime escalation; ⚠️/✅ notification pair fires only for
  strong/escalating WARNs. Corrects the 08-11 over-blocking (3,709 entry
  skips) — Constitution VIII."

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/008-market-gate-liquidity-only/` | ✅ spec, plan, research, data-model, contracts/warn-escalation, quickstart (this phase) |
| `trade/market_check.py` | `_warn_is_strong` regime branch gated on `market_gate_regime_escalates` + docstring; `update_gate_state` WARN comment; verdict logic untouched |
| `bot.py` | `_gate_apply`: settings dict +`market_gate_regime_escalates`; strong-only ⚠️/✅ gating via `_warn_is_strong`; import line +`_warn_is_strong`; docstrings |
| `trade/settings_schema.py` | +1 `SettingSpec` (`market_gate_regime_escalates`, bool, group `gate`) |
| `trade/settings_rules.py` | **No change** (bool with no ranges; no cross-check needed) |
| `tests/test_market_check.py` | `test_debounce_transitions` regime block updated; `test_settings_validation` 9-key + validate; new `test_warn_notifications_strong_only` |
| `docs/CURRENT_STATE.md` | feature-005 gate section + new feature-008 section |
| `docs/CHANGELOG.md` | `fix:` entry (2026-08-12) |
| `db/*`, `trading_bot/*`, `telegram.py`, `dashboard/*` | Unchanged |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Notification trigger diverges from suspension classification (⚠️ fires for a WARN that doesn't escalate, or vice versa) | Low | Medium | Single classifier reused by both call sites with identical inputs (`_warn_is_strong` + same `settings`/`reasons`) — structurally identical by construction; new `test_warn_notifications_strong_only` covers all four strong/mild × regime/liquidity combinations |
| Regime WARNs silently disappear from operator view | Low | Medium | Verdict unchanged (PASS→WARN, same reasons); structured `[GATE] … verdict=WARN … action=hold` log line remains the informational signal (AC2); only the ⚠️ Telegram message stops |
| Accidental relaxation of the liquidity half | Low | High | Liquidity branches in `_warn_is_strong` untouched; unchanged liquidity tests (`test_debounce_transitions` strong/mild blocks, `test_transition_notifications_once`, `test_warn_lifecycle_notifications`) act as regression guards (AC3–AC5) |
| Re-enable path broken (operator cannot restore 005 behavior) | Low | Medium | Explicit regression in `test_debounce_transitions` (`market_gate_regime_escalates=True` → suspend) + notification case in the new test (AC7, AC11) |
| Setting missing from schema (validation/UI drift) | Low | Low | `test_settings_validation` 9-key assertion + `validate`/`validate_all` checks (AC8); `BY_KEY` derives from `ALL` |
| Near-zero-trade returns (Constitution VIII) | Low | High | Default is liquidity-only (regime escalation off); liquidity protection preserved; re-enable is operator opt-in; default settings remain lenient |
| Unintended verdict change | Low | Medium | `_evaluate`/`_gate_share_settings` explicitly untouched; `test_verdict_correctness` unchanged (AC2) |
| Line growth / hot-path budget (Constitution VII) | Low | Medium | ±3 lines in `_warn_is_strong`, one settings-dict key + one boolean in `_gate_apply`, one schema row — no new module |
| Regression in the ⚠️/✅ lifecycle edge cases (strong→mild→PASS) | Low | Medium | Edge cases enumerated (strong WARN stays flagged through a mild WARN; ✅ only clears a notified WARN) and covered in `test_warn_notifications_strong_only` |
