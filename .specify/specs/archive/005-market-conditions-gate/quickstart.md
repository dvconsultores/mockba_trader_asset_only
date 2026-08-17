# Quickstart: Market Conditions Check & Auto-Gate

**Feature**: 005-market-conditions-gate | **Date**: 2026-08-09

Runnable validation scenarios proving the feature end-to-end. All commands run
from the project root with the venv Python. Contract details live in
`contracts/market-report.md` and `data-model.md` — this guide does not repeat
them.

## Prerequisites

- Bot running in dry-run (`dry_run=true`); exchange credentials in `.env`
- Telegram bot configured (`API_TOKEN`, `TELEGRAM_CHAT_ID`, `BOT_LANGUAGE`)
- A stored universe scan for at least one venue (`/universe` or the scanner)
- `market_gate_*` settings registered (Scenario 1)

---

## Scenario 1: Settings Registered & Validated

**Goal**: the seven `market_gate_*` settings exist in the schema and pass the
Amendment 002 deterministic validator.

```bash
./venv/bin/python -c "
from trade.settings_schema import BY_KEY
from trade.settings_rules import validate
keys = [k for k in BY_KEY if k.startswith('market_gate_')]
print(sorted(keys))
for k in sorted(keys):
    s = BY_KEY[k]
    print(k, s.type.__name__, 'hard', s.hard_min, s.hard_max, 'soft', s.soft_min, s.soft_max)
# valid + invalid values
print(validate('market_gate_interval_min', 5).level)   # ok
print(validate('market_gate_interval_min', 0).level)   # error (hard_min 1)
print(validate('market_gate_bad_streak', 1).level)     # ok
print(validate('market_gate_bad_streak', 0).level)     # error
"
```

**Expected**: 7 keys (`enabled`, `interval_min`, `bad_streak`, `good_streak`,
`fail_share`, `trend_share`, `unknown_share`); valid values `ok`, out-of-range
`error`.

---

## Scenario 2: Manual `/market` Report (live mode)

**Goal**: the manual button/command renders a compact per-venue verdict.

1. Start `telegram.py` (with the bot).
2. Send `/market` in the private chat, or open `/list` and press the **Market
   check** button.

**Expected**:
- One compact block per venue (CEX/DEX): `Verdict: PASS|WARN|FAIL` (verbatim
  token), scan age/freshness, regime mix counts, liquidity pass count, reasons.
- Message stays within 4096 chars (chunked if ever longer).
- Unauthorized chat → `🔍 Not authorized` (same as `/list`).
- While the gate is **disabled** this only runs on demand — it is the operator's
  escape-hatch view.

---

## Scenario 3: Automatic Gate — Suspend & Resume (observed mode)

**Goal**: with `market_gate_enabled=true`, a poor venue suspends new entries
after `bad_streak` evaluations and resumes after `good_streak` good ones.

Setup in a **dry-run** environment:

```bash
./venv/bin/python -c "
from db.db_ops import upsert_setting
for k, v in {
  'market_gate_enabled': 'true',
  'market_gate_interval_min': '1',     # fast cadence for testing
  'market_gate_bad_streak': '2',
  'market_gate_good_streak': '2',
}.items():
    upsert_setting(k, v)
"
```

1. Restart `bot.py` so the gate block picks up the settings.
2. Force poor conditions (e.g., set `universe_spread_ratio_max` very low so
   assets fail `spread_ok`, or wait for a genuinely thin market) — or, for a
   deterministic check, rely on `tests/test_market_check.py` debounce tests.
3. Observe the log: after 2 consecutive `FAIL` evaluations,
   `[GATE] venue=... verdict=FAIL reason=... action=suspend` and **one**
   Telegram notification "suspended — poor market conditions".
4. Restore normal conditions; after 2 consecutive `PASS` evaluations,
   `action=resume` and **one** "recovered" notification.

**Expected**:
- While suspended: no new entries (skips logged at DEBUG with reason recorded in
  `signals`), but open positions keep being managed to TP/SL/time-stop.
- No per-evaluation notifications — only the two transition messages.
- The two venues suspend/resume **independently**.
- With `market_gate_enabled=false` (default) none of the above happens — the bot
  behaves exactly as before (Scenario 4).

---

## Scenario 4: Disabled-by-Default (zero behavior change)

**Goal**: with the setting unset, byte-for-byte behavior is unchanged.

```bash
./venv/bin/python -c "
from db.db_ops import get_setting_bool
print(get_setting_bool('market_gate_enabled', False))  # False
"
```

Run the bot normally (gate disabled). **Expected**: no `[GATE]` logs beyond a
single startup "disabled" line, no entry blocking, no notifications. Entry
behavior identical to before the feature.

---

## Scenario 5: Unit Tests

```bash
./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q
```

**Expected**: all pass — shared-function non-divergence patch test, same-contract
parity, freshness trigger/fail-closed, verdict correctness matrix, disabled
default, debounce transitions, entries-only blocking, exactly-one transition
notification, zero observed-mode API load, compact report, not-near-zero-trade,
settings validation.
