# Quickstart: Spot Exit Hardening (gap/crash protection)

**Feature**: 006-spot-exit-hardening | **Date**: 2026-08-12

Runnable validation scenarios proving the feature end-to-end. All commands run
from the project root with the venv Python. Contract details live in
`contracts/exit-reasons.md` and `data-model.md` — this guide does not repeat
them. Implementation details belong to the tasks phase, not here.

## Prerequisites

- Bot supervised by `forever.py` (bot.py + telegram.py); exchange credentials
  in `.env`; **`dry_run=true`** for the crash-guard scenarios.
- A stored universe scan for binance (`/universe` or the scanner).
- Both new settings registered (Scenario 1).

---

## Scenario 1: Settings Registered & Validated

**Goal**: `universe_max_atr_pct` and `max_loss_per_position_pct` exist in the
schema with the specified ranges, fall back to defaults, and the cross-checks
behave (hard error strictly inside the spot SL floor; equality allowed).

```bash
./venv/bin/python -c "
from trade.settings_schema import BY_KEY
from trade.settings_rules import validate
from db.db_ops import get_setting_float
for k in ('universe_max_atr_pct', 'max_loss_per_position_pct'):
    s = BY_KEY[k]
    print(k, s.type.__name__, 'default', get_setting_float(k, 1.5 if 'atr' in k else 3.0),
          'hard', s.hard_min, s.hard_max, 'soft', s.soft_min, s.soft_max)
# sl_min_pct_spot is 0.6 in the live DB → 0.5 is strictly inside → error; 0.6 == → ok
print(validate('max_loss_per_position_pct', 0.5).level)   # error
print(validate('max_loss_per_position_pct', 0.6).level)   # ok (equality)
print(validate('max_loss_per_position_pct', 3.0).level)   # ok
print(validate('universe_max_atr_pct', 1.5).level)        # ok
"
```

**Expected**: both keys listed with `float`, hard 0.1–20, soft 0.5–5 / 1–5;
`0.5 → error`, `0.6 → ok`, `3.0 → ok`, `1.5 → ok`.

---

## Scenario 2: Universe Cap Rejects High-ATR Names (Spot Only)

**Goal**: after a binance scan, BICO-class names (`atr_pct_median` > cap) are
gone from `/universe cex` while PUMP/MMT remain; the Orderly universe is
untouched.

1. Set the cap: `./venv/bin/python -c "from db.db_ops import upsert_setting;
   upsert_setting('universe_max_atr_pct', '1.5')"` (or via Telegram/UI).
2. Trigger a rescan: `/universe` or wait for the scanner tick.
3. Check the notification / `_scan_summary_message`: candidates dropped count.
4. `/universe cex` → BICO absent, PUMP + MMT present; `/universe dex` →
   unchanged (venue-branch: orderly unaffected).

**Expected**: exactly the names with `atr_pct_median > 1.5` are excluded on
`binance` only; the scan summary reports how many were dropped by the cap.

---

## Scenario 3: Crash Guard Fires Below the Floor (Dry-Run)

**Goal**: a spot position whose live price crashes below
`entry × (1 − max_loss_per_position_pct/100)` is cancelled + market-sold in
the same cycle and recorded with `exit_reason='crash_guard'`, with the real
(dry-run) fill price, and the `(asset, side)` is blocked from re-entry for the
SL cooldown.

1. `dry_run=true`; hold a spot position (entry ≈ $1.00).
2. Set `max_loss_per_position_pct` to `3.0`; let the price gap/crash below
   `$0.97` (or set a value that triggers on the current price in a test
   harness with a mocked exchange).
3. Watch the structured `[EXIT] asset=… reason=crash_guard …` log (INFO, no
   emoji) within the next ~30s cycle.
4. Check the Closed Trades page (or the DB):
   `SELECT exit_reason, exit_price, pnl_net FROM closed_trades ORDER BY closed_at DESC LIMIT 5`
   → a `crash_guard` row with the real (dry-run) fill price.
5. Re-entry: the same asset does not re-enter for `cooldown_sec × 10`.

**Expected**: guard-first behavior — the exit happens in the same cycle as the
floor breach, reason `crash_guard`, real fill, cooldown stamped.

---

## Scenario 4: Fill-Aware Ordering (No Phantom Double-Close)

**Goal**: when price is below the floor but the TP or SL order already filled,
the real fill is recorded with its real reason (`tp`/`sl`) and **no** market
sell occurs.

Simulate with the unit tests (mocked exchange sets `get_order_status` to
`FILLED` while `get_price` is below the floor) — see Scenario 5.

**Expected**: `closed_trades` gets the real reason; `market_sell` is never
called; the position is not double-closed.

---

## Scenario 5: Automated Tests

**Goal**: the full acceptance-criteria suite passes.

```bash
./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q
# plus the existing suites (no regressions)
./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q
```

**Expected**: all tests green — universe cap (spot-only, additive), crash-guard
fire + fill-aware ordering + `None`-price no-action + cooldown stamping +
no-`sl_price` positions + normal-exit invariance + validation error-vs-equality
+ dry-run unchanged + settings registered with defaults.

---

## Scenario 6: Dashboard Label

**Goal**: the Closed Trades page renders `crash_guard` as "Crash guard".

1. Open the Mini App → **More options → Closed trades**.
2. Find a `crash_guard` trade (dry-run Scenario 3).

**Expected**: reason column shows `Crash guard` (not `CRASH_GUARD`).
