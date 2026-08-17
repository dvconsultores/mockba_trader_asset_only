# Feature Specification: 008 — Market Gate: Liquidity-Only Suspension

**Feature Branch**: `008-market-gate-liquidity-only` *(implementation on `main` per repo convention — the repo tracks only main, no feature branches)*

**Created**: 2026-08-12

**Status**: Draft — awaiting implementation authorization

**Flow**: constitution → specify → clarify → plan → checklist → tasks → analyze → **implement (AUTHORIZATION REQUIRED)** → converge

---

## What

One small, setting-driven change to the automatic market gate (feature 005): the gate keeps only its **liquidity half** for suspension. Aggregate-liquidity verdicts (`FAIL liquidity_fail_share`, strong `WARN liquidity_partial`) keep counting toward suspension exactly as today; regime-based WARNs (`regime_trending`, `regime_unknown`) become **informational** — they still downgrade the verdict PASS→WARN, but they **no longer count toward suspension** and, being mild, no longer fire the ⚠️ WARN Telegram notification. The ⚠️/✅ notification pair now fires only for **strong/escalating** WARNs (clarify Q1); mild regime WARNs are visible only in the structured `[GATE] verdict=WARN` log. A new bool setting `market_gate_regime_escalates` (default `false`) lets the operator re-enable regime-based suspension from the DB without a code change (repo convention: settings, never constants).

### Part 1 — Escalation rule change (`trade/market_check.py`)

- `_warn_is_strong` currently returns `True` for any reason starting with `regime_trending=` or `regime_unknown=`, and for `liquidity_partial=` when the share >= `market_gate_warn_liquidity_share` (default 0.25). The two regime branches are **removed**: from now on only `liquidity_partial=` (>= `market_gate_warn_liquidity_share`) escalates by default.
- The regime escalation becomes **conditionally re-enabled** via the new `market_gate_regime_escalates` setting: when `true`, `regime_trending=` / `regime_unknown=` WARNs count toward `bad_streak` again (exact feature-005 behavior restored); when `false` (default), they are mild.
- **The verdict rules in `_evaluate` are untouched.** `regime_trending` / `regime_unknown` still downgrade PASS→WARN, so the operator still sees the WARN verdict in the structured `[GATE] venue=… verdict=WARN …` log. Only the escalation classification changes — and with it the ⚠️ WARN notification trigger, which now fires only for strong/escalating WARNs (clarify Q1).
- `update_gate_state`'s WARN branch is unchanged in shape: strong WARN → `bad_streak`+1, suspends at `market_gate_bad_streak`; mild WARN → resets both streaks, never suspends. Only what `_warn_is_strong` classifies as strong changes.
- The function docstring and the module/gate comments that describe the regime-WARN-suspends behavior are updated to match.

### Part 2 — Setting & validation (`trade/settings_schema.py`, `bot.py`)

- NEW setting `market_gate_regime_escalates` (bool, group `gate`, default `false`, no hard/soft ranges — mirrors `market_gate_enabled`).
- `bot.py` `_gate_apply` reads it fresh via `get_setting_bool("market_gate_regime_escalates", False)` into the settings dict passed to `update_gate_state` — the same dict that already carries `market_gate_warn_liquidity_share`. It must also know whether a WARN is **strong** to gate the ⚠️/✅ notification pair (strong → notify, mild → log only); the report dict already carries `reasons`, so `_gate_apply` can reuse the same `_warn_is_strong`-style classification, or the report can carry a `strong_warn`/escalation flag — mechanism pinned in the plan, minimal change. No other bot.py change; cadence, streaks, debounce mechanics, cold-start warmup untouched.

### Part 3 — Tests (`tests/test_market_check.py`)

- The tests asserting `regime_trending`/`regime_unknown` WARNs lead to suspension are **updated** to assert they do **NOT** suspend by default (mild: reset both streaks), plus a regression case asserting that `market_gate_regime_escalates=true` restores the 005 escalation (the operator re-enable path).
- Existing liquidity tests are kept and asserted unchanged: `liquidity_fail_share` FAIL and `liquidity_partial` >= `market_gate_warn_liquidity_share` still suspend after `market_gate_bad_streak`; mild `liquidity_partial` below the share never suspends; PASS still resumes after `market_gate_good_streak`.
- `test_settings_validation` key-list assertion gains the 9th `market_gate_*` key.

### Part 4 — Docs

- `docs/CURRENT_STATE.md` — update the feature-005 gate section (WARN escalation rule) + a feature-008 section (this feature).
- `docs/CHANGELOG.md` — `fix:` entry.

## Why

On 2026-08-11 the 005 gate's regime-trending WARN over-blocked on the small spot universe: **3,709 entry skips**. The regime half of the gate is redundant with two protections that already exist:

1. **The broad-market filter** (`_broad_market_downtrend` in `bot.py`, on BTC/ETH/SOL/BNB) already owns macro trend protection — a global red day blocks all new entries at the venue level.
2. **The per-asset `regime` filter** already blocks TREND_DOWN / UNKNOWN per asset (`spot_scalper.py` skips `regime=TREND_DOWN` and non-RANGE/TREND_UP regimes; `bot.py` skips UNKNOWN before entry; `futures_scalper.py` direction-gates long/short by regime).

On a small universe a handful of trending assets can push the trend share past `market_gate_trend_share` and suspend the whole venue — a near-zero-trade outcome that violates Constitution VIII. Aggregate liquidity collapse, by contrast, is the gate's **unique value**: no other guard reacts to a venue-wide liquidity failure. So the gate keeps its liquidity half for suspension and demotes the regime half to informational.

This restores throughput (Constitution VIII), touches no exit/stops behavior (Constitution III — the gate blocks entries only), and keeps the operator informed (the WARN verdict remains in the structured `[GATE]` log; the ⚠️/✅ Telegram pair now fires only for strong/escalating WARNs — clarify Q1).

---

## Clarifications

### Session 2026-08-12

- Q: When should the ⚠️ WARN Telegram notification (and its ✅ "warning cleared" counterpart) fire after the change? → A: Only for **strong/escalating** WARNs — `liquidity_partial` ≥ `market_gate_warn_liquidity_share`, `liquidity_fail_share` FAIL, or regime WARNs when `market_gate_regime_escalates=true`. Mild regime WARNs (default) are visible only in the structured `[GATE] venue=… verdict=WARN … action=hold/warn_start` log — no Telegram message. `_gate_apply` must therefore know whether a WARN is strong; the report already carries `reasons` (reuse the `_warn_is_strong`-style classification) or can carry a `strong_warn`/escalation flag — mechanism pinned in the plan, minimal change.

---

## Resolved decisions (from feature description)

| # | Decision |
|---|---|
| Q1 | Settings vs hardcode → **NEW bool `market_gate_regime_escalates` (default `false`)**. The operator can re-enable regime-based suspension from the DB without a code change — consistent with the "settings, never constants" convention (the 005 gate is entirely settings-driven) and with the constitution's "settings read fresh each cycle". Proportionate: one schema row + one settings read. |
| Q2 | Default behavior: regime WARNs are **mild** — informational only; they never count toward `bad_streak`/suspension. The verdict downgrade (PASS→WARN) is preserved; the ⚠️ WARN notification is **not** (mild regime WARNs are structured-log only — clarify Q1). |
| Q3 | Liquidity escalations **unchanged**: `FAIL liquidity_fail_share` and `WARN liquidity_partial` >= `market_gate_warn_liquidity_share` keep counting toward `bad_streak`/suspend; mild `liquidity_partial` (< share) stays informational. |
| Q4 | `_evaluate` verdict rules **unchanged** — only `_warn_is_strong`'s escalation classification changes (plus docstrings/comments). |
| Q5 | The setting is read at the gate application site (`bot.py` `_gate_apply`, alongside `market_gate_warn_liquidity_share`) and passed through `settings` to `update_gate_state`/`_warn_is_strong`. |
| Q6 | Implementation on `main` directly — no feature branches (repo convention: the repo tracks only main). |
| Q7 | Docs: `fix:` CHANGELOG entry (over-blocking corrected); CURRENT_STATE feature-005 gate section updated + new feature-008 section. |

---

## Layout

### 1. `trade/market_check.py` — escalation classification only

- `_warn_is_strong(reasons, settings)`: remove the `if r.startswith("regime_trending=") or r.startswith("regime_unknown="): return True` branch; a regime reason is strong **only when** `settings.get("market_gate_regime_escalates", False)` is true. The `liquidity_partial=` branch (share >= `market_gate_warn_liquidity_share`, default 0.25) is unchanged.
- `update_gate_state`: WARN logic unchanged in shape (strong → `bad_streak`+1, suspend at threshold; mild → reset both streaks, never suspends). Only the inputs to `_warn_is_strong` change.
- Update the `_warn_is_strong` docstring and the `update_gate_state` WARN-branch comments to describe the liquidity-only default and the `market_gate_regime_escalates` re-enable path.

### 2. `bot.py` — read the new setting + strong-WARN notification gating

- `_gate_apply`: add `"market_gate_regime_escalates": get_setting_bool("market_gate_regime_escalates", False)` to the settings dict passed to `update_gate_state` — the same dict that already carries `market_gate_warn_liquidity_share`.
- **Strong-WARN awareness (clarify Q1)**: `_gate_apply` must know whether a WARN is **strong** before firing the ⚠️ WARN / ✅ warning-cleared Telegram pair — it fires only for strong/escalating WARNs; mild regime WARNs get the structured `[GATE] … verdict=WARN … action=hold/warn_start` log line only. The report dict already carries `reasons`: either `_gate_apply` calls the same `_warn_is_strong`-style classification, or the report carries a `strong_warn`/escalation flag. **Mechanism pinned in the plan** (minimal change — prefer reusing the existing classification over a new report field unless the plan shows otherwise).
- Nothing else changes (cadence, streaks, debounce mechanics, cold-start warmup, observation recording untouched).

### 3. `trade/settings_schema.py` — register the setting

| Key | Type | Group | Default | Hard range | Soft range |
|---|---|---|---|---|---|
| `market_gate_regime_escalates` | bool | gate | false | — | — |

- Default lives in the `get_setting_bool` fallback — **no DB migration**; accepted by the Amendment 002 deterministic validator (bool with no ranges, same pattern as `market_gate_enabled`).

### 4. `tests/test_market_check.py` — update regime-WARN expectations

- `test_debounce_transitions`: the "regime WARNs (trending/unknown) are always strong" block becomes "mild by default": repeated `regime_trending=` / `regime_unknown=` WARNs reset both streaks and never suspend; add the re-enable case (settings with `market_gate_regime_escalates=True`) asserting the 005 escalation returns.
- `test_warn_lifecycle_notifications` / `test_transition_notifications_once`: add the strong-only notification cases — a mild `regime_trending=`/`regime_unknown=` WARN (default settings) fires **no** ⚠️ message; with `market_gate_regime_escalates=True` it does. Existing liquidity-WARN notification cases stay unchanged (they are strong).
- Keep and assert unchanged: `liquidity_fail_share` FAIL suspends; `liquidity_partial` >= `market_gate_warn_liquidity_share` suspends; mild `liquidity_partial` never suspends; PASS resumes.
- `test_settings_validation`: the `market_gate_*` key list grows to 9 (`market_gate_regime_escalates` added) and the new key validates `ok` (bool, no ranges).

### 5. Docs

- `docs/CURRENT_STATE.md` — feature-005 gate section: the WARN-escalation sentence changes to liquidity-only (regime WARNs informational by default, `market_gate_regime_escalates` re-enables); new "## 0. Market Gate: Liquidity-Only Suspension (feature 008, 2026-08-12)" section.
- `docs/CHANGELOG.md` — `fix:` entry (regime-WARN over-blocking corrected; 3,709-skips event on 08-11).

---

## Scope

**In scope**

- `trade/market_check.py` — `_warn_is_strong` regime branches (removed / conditioned on the new setting), docstrings + comments.
- `bot.py` — `_gate_apply` reads `market_gate_regime_escalates`.
- `trade/settings_schema.py` — register `market_gate_regime_escalates` (group `gate`, bool, default false).
- `tests/test_market_check.py` — update regime-WARN suspension expectations + settings key list.
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md`.

**Out of scope**

- No change to `_evaluate` verdict rules — `regime_trending`/`regime_unknown` still downgrade PASS→WARN (informational).
- No change to the gate's cadence, streak settings, debounce thresholds, notification **debounce mechanics** (one message per transition), cold-start warmup, observation recording, entry-blocking mechanics, or `/market` report. The ⚠️/✅ notification **trigger scope** narrows to strong/escalating WARNs only (clarify Q1); mild regime WARNs stop notifying.
- No change to the broad-market filter (`_broad_market_downtrend`), per-asset regime gating, stale-universe guard, spread-degradation guard, or kill switches.
- No change to `market_gate_fail_share` / `market_gate_trend_share` / `market_gate_unknown_share` / `market_gate_warn_liquidity_share` defaults or semantics.
- No DB schema change / migration; no new process, thread, or external service.

---

## Constraints

- **Minimum modification.** Only the escalation rule and the ⚠️/✅ notification trigger scope change (strong-only, clarify Q1); verdicts, debounce mechanics, and cadence are preserved. Everything unrelated is untouched.
- **Constitution III** — the gate still blocks entries only; stops/exits and position management are unaffected.
- **Constitution IV** — unchanged: unknown state never counts toward a good verdict; `regime_unknown` WARN remains "not good" (it just no longer escalates).
- **Constitution VII** — minimal lines: a small change to `_warn_is_strong` + one settings read in `_gate_apply` + one schema row; no new module; no import from `trade/main.py` or `trade/signal_agent/`.
- **Constitution VIII** — restores throughput: the redundant regime-trending escalation (3,709 skips on 08-11) is demoted to informational, so the gate only suspends on genuine aggregate liquidity collapse.
- **Settings, never constants** — the re-enable path is a setting (`market_gate_regime_escalates`, default false), read fresh each cycle; no hardcoded behavior toggle.
- **Settings validation** — the new setting passes the Amendment 002 deterministic validator (registered in `settings_schema.py`).
- **Structured single-line logs, no emoji** — unchanged `[GATE] ...` logs.
- **`dry_run` unchanged** — every order path and its checks are untouched.
- **Implementation on `main` directly** — no feature branches (repo convention).

---

## Assumptions

- The recommended design (bool setting `market_gate_regime_escalates`, default `false`) is adopted — the operator can re-enable regime-based suspension from the DB without a code change; the change is proportionate (one schema row + one settings read).
- Regime-based WARNs remain visible to the operator via the structured `[GATE] venue=… verdict=WARN …` log — informational only; mild regime WARNs no longer fire the ⚠️ Telegram notification (clarify Q1).
- Liquidity collapse is the gate's unique value and its escalation behavior (FAIL + strong partial) is the intended core of the gate going forward.
- The 08-11 event (3,709 skips attributed to regime-trending WARN on a small universe) is the motivating over-blocking observation.
- With `market_gate_regime_escalates` unset (no DB row), the default `false` applies — existing DBs are unaffected until the operator sets it.

---

## Acceptance criteria

1. **Regime WARNs are mild by default** — with `market_gate_regime_escalates` unset/false, a `regime_trending=` or `regime_unknown=` WARN never increments `bad_streak` and never suspends; repeated regime WARNs reset both streaks (mild path).
2. **Verdicts unchanged** — `_evaluate` still downgrades PASS→WARN for `regime_trending`/`regime_unknown` with identical reasons; the operator still sees the WARN verdict in the structured `[GATE] venue=… verdict=WARN …` log.
3. **Liquidity FAIL still suspends** — `liquidity_fail_share` FAILs count toward `bad_streak` and suspend after `market_gate_bad_streak`, unchanged from feature 005.
4. **Strong liquidity_partial still suspends** — a `liquidity_partial=` WARN at/above `market_gate_warn_liquidity_share` counts toward `bad_streak` and suspends after `market_gate_bad_streak`, unchanged.
5. **Mild liquidity_partial still mild** — a `liquidity_partial=` WARN below `market_gate_warn_liquidity_share` stays informational (resets both streaks, never suspends), unchanged.
6. **PASS still resumes** — a suspended venue resumes only after `market_gate_good_streak` consecutive PASS, unchanged.
7. **Operator re-enable** — with `market_gate_regime_escalates=true`, `regime_trending=`/`regime_unknown=` WARNs count toward `bad_streak` and suspend after `market_gate_bad_streak` (exact feature-005 behavior restored).
8. **Setting registered & fresh** — `market_gate_regime_escalates` (bool, group `gate`, default false) is registered in `trade/settings_schema.py`, accepted by the Amendment 002 validator, read fresh each cycle by `_gate_apply`, and needs no DB migration.
9. **Tests updated** — `tests/test_market_check.py` asserts the new default (regime WARNs do not suspend, AC1), the re-enable path (AC7), the unchanged liquidity escalation (AC3–AC5), PASS resume (AC6), the strong-only ⚠️/✅ notification scope (AC11), and the 9-key `market_gate_*` list.
10. **Docs** — `docs/CURRENT_STATE.md` reflects the liquidity-only escalation in the feature-005 gate section and gains a feature-008 section; `docs/CHANGELOG.md` gains a `fix:` entry.
11. **⚠️/✅ notification only for strong WARNs** — the debounced ⚠️ `[GATE] WARNING` and ✅ `warning cleared` Telegram messages fire only for **strong/escalating** WARNs: `liquidity_partial` ≥ `market_gate_warn_liquidity_share`, `liquidity_fail_share` FAIL, or regime WARNs when `market_gate_regime_escalates=true`. A mild regime WARN (default) fires no Telegram message — the structured `[GATE] venue=… verdict=WARN … action=hold/warn_start` log line is the only signal (clarify Q1).
