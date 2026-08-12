# Quickstart: Market Gate — Liquidity-Only Suspension (008)

**Feature**: 008-market-gate-liquidity-only | **Date**: 2026-08-12

Runnable validation scenarios proving the feature end-to-end. All commands run
from the project root with the venv Python. The escalation/notification
contract lives in `contracts/warn-escalation.md` and the data model in
`data-model.md` — this guide does not repeat them. Implementation details
belong to the tasks phase, not here.

## Prerequisites

- Bot supervised by `forever.py` (bot.py + telegram.py); exchange credentials
  in `.env`.
- The market gate **enabled** for the venue under test:
  `./venv/bin/python -c "from db.db_ops import upsert_setting;
  upsert_setting('market_gate_enabled', 'true')"` (or via Telegram/UI).
- The new setting registered (Scenario 1).
- Unit tests: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`
  (plus the full `tests/` suite for regressions).

---

## Scenario 1: Setting Registered & Validated

**Goal**: `market_gate_regime_escalates` is the 9th `market_gate_*` key, is a
bool in group `gate`, falls back to `false` when unset, and validates `ok` for
both `True` and `False` (no ranges → no hard-range violations).

```bash
./venv/bin/python -c "
from trade.settings_schema import BY_KEY
from trade.settings_rules import validate
from db.db_ops import get_setting_bool
keys = sorted(k for k in BY_KEY if k.startswith('market_gate_'))
print(len(keys), 'market_gate_* keys:', keys)
s = BY_KEY['market_gate_regime_escalates']
print(s.type.__name__, s.group, 'default', get_setting_bool('market_gate_regime_escalates', False))
print(validate('market_gate_regime_escalates', True).level)   # ok
print(validate('market_gate_regime_escalates', False).level)  # ok
"
```

**Expected**: `9 market_gate_* keys` (the 8 existing + `market_gate_regime_escalates`);
`bool gate default False`; `ok` / `ok`. No DB migration needed — the default
lives in the `get_setting_bool(..., False)` fallback.

---

## Scenario 2: Regime WARNs Are Mild by Default (No Suspension, No ⚠️)

**Goal**: with `market_gate_regime_escalates` unset/false, a venue stuck on a
`regime_trending=`/`regime_unknown=` WARN never suspends and never fires the ⚠️
Telegram message — the structured `[GATE]` log is the only signal.

1. Leave `market_gate_regime_escalates` unset (or set `"false"`).
2. Trigger repeated regime WARNs (e.g. a small universe whose trend share ≥
   `market_gate_trend_share`).
3. Watch the logs for `[GATE] venue=… verdict=WARN reason=regime_trending=… action=hold`
   — repeated evaluations keep `action=hold`; **no** ⚠️ message and **no**
   suspension.

**Expected**: the venue's `_gate_state` shows `suspended=false` and both
streaks at 0 after any number of regime WARNs; no ⚠️/✅ Telegram messages;
`signals` shows no `market_gate_suspended` skips.

---

## Scenario 3: Operator Re-enables Regime Escalation

**Goal**: setting `market_gate_regime_escalates=true` restores the feature-005
behavior — regime WARNs count toward `bad_streak` and suspend after
`market_gate_bad_streak`, and the ⚠️ notification fires.

```bash
./venv/bin/python -c "from db.db_ops import upsert_setting;
upsert_setting('market_gate_regime_escalates', 'true')"
```

1. Repeat regime WARNs as in Scenario 2.
2. **Expected**: the 2nd consecutive strong regime WARN suspends the venue
   (`[GATE] … action=suspend`, 🛑 message), the 1st strong WARN fired one ⚠️
   message, and entries are skipped with `market_gate_suspended` recorded in
   `signals`. Set back to `"false"` (or delete the row) to return to
   Scenario-2 behavior — no restart needed (setting read fresh each cycle).

---

## Scenario 4: Liquidity Escalation Unchanged

**Goal**: the gate's liquidity half behaves exactly as feature 005.

1. **FAIL still suspends**: a `liquidity_fail_share=` FAIL (fail share ≥
   `market_gate_fail_share`) counts toward `bad_streak` and suspends after
   `market_gate_bad_streak` — unchanged.
2. **Strong partial still suspends**: a `liquidity_partial=` WARN at/above
   `market_gate_warn_liquidity_share` (default 0.25) counts toward
   `bad_streak` and suspends — unchanged, and it **still fires ⚠️/✅**.
3. **Mild partial stays mild**: a `liquidity_partial=` below the share resets
   both streaks and never suspends — unchanged.
4. **PASS resumes**: a suspended venue resumes only after
   `market_gate_good_streak` consecutive PASS — unchanged.

**Expected**: identical to feature 005 (covered by the unchanged liquidity
tests in `tests/test_market_check.py`).

---

## Scenario 5: ⚠️/✅ Only for Strong WARNs (clarify Q1)

**Goal**: the debounced ⚠️ WARNING / ✅ warning-cleared pair fires **only** for
strong/escalating WARNs.

| WARN | `market_gate_regime_escalates` | ⚠️ / ✅ |
|---|---|---|
| `liquidity_partial=0.33` (≥ share) | any | fires (strong) |
| `liquidity_partial=0.11` (< share) | any | none (mild) |
| `regime_trending=1.00` / `regime_unknown=1.00` | unset/false | **none** — `[GATE] … action=hold` log only |
| `regime_trending=1.00` / `regime_unknown=1.00` | true | fires (strong) |

**Expected**: covered by the new `test_warn_notifications_strong_only` test —
a mild regime WARN (default) makes zero `send_message` calls and never sets
`warn_active`; with the setting true, exactly one ⚠️ on start and one ✅ on a
following PASS. The unchanged `test_transition_notifications_once` /
`test_warn_lifecycle_notifications` guard the liquidity-WARN paths.

---

## Scenario 6: Test Suite

```bash
./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q
```

**Expected**: all tests pass, including the updated `test_debounce_transitions`
(mild-by-default regime block + re-enable regression), the updated
`test_settings_validation` (9-key list + `regime_escalates` validation), and
the new `test_warn_notifications_strong_only`. Then run the full `tests/`
suite to confirm no regressions.
