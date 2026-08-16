# Feature Specification: 010 — Futures Exit Integrity

**Feature Branch**: `010-futures-exit-integrity` *(implementation on `main` per repo convention — the repo tracks only main, no feature branches)*

**Created**: 2026-08-15

**Status**: Clarified — Q1–Q6 resolved. Ready for `/speckit.plan`.

**Flow**: constitution → specify → **clarify** → plan → checklist → tasks → analyze → implement (AUTHORIZATION REQUIRED) → converge

**Constitution**: v1.1.0. This feature is a **compliance repair** — three of the five paths in `futures_scalper.manage_open_positions` currently violate Principle III and/or Principle V.

---

## What

Make every Orderly futures exit path do what it claims: **actually close the position on the exchange**, and **record the real fill** rather than a computed number.

### Part 1 — Time stop must close the position (Constitution III)

`futures_scalper.manage_open_positions` lines 94–98 currently:

```python
if (now-op)>mh:
    if tpid: exchange.cancel_order(sym,tpid)
    if slid: exchange.cancel_order(sym,slid)
    _close(asset,"orderly",side,ep,ep,sp,q,0.0003,pid,si,"time_stop"); continue
```

It cancels the take-profit **and the stop-loss**, deletes the local record, and
**never sends a closing order**. The result is a live leveraged position on
Orderly with no stop, no take-profit, and no local record of its existence —
invisible to reconciliation, to the dashboard, and to every kill switch. It also
records the exit at `ep` (the entry price), so the trade books a fabricated PnL
of −fees regardless of where the market actually was.

New behaviour: cancel the brackets, send a **reduce-only market close**, verify
the position is gone, and only then record the trade from the **real fill**. If
the close fails, the position **stays in the DB** and its stop-loss is
**re-placed** before the cycle ends (Constitution III), with an error logged.

### Part 2 — Regime exit must reach the exchange, or not happen at all

Lines 100–106 compute a breakeven take-profit and write it to the DB with
`update_position(pid, tp_price=new_tp)` — the **exchange order is never
amended**. Two failures compound: the intended protection does not exist, and
when the original (unmoved) TP later fills, `_close` records `pd["tp_price"]` —
now the fabricated breakeven price — as the exit.

New behaviour: cancel the live TP and place a replacement at the breakeven
price. The DB is updated **only** if the exchange accepted the replacement. If
the cancel succeeds but the replacement fails, the position is left with no TP
and the failure is logged — the SL still protects it (Constitution III), and the
next cycle retries.

### Part 3 — TP/SL exits must record real fills (Constitution V)

Lines 86–92 record `float(pd["tp_price"])` / `float(pd["sl_price"])` — the
*intended* prices — with a flat `0.0003` fee rate applied to both legs. A
STOP_MARKET fill can slip well past its trigger, so recorded futures losses are
systematically optimistic, and every fee is an assumption. Constitution V
requires "the exchange's actual fill price and actual fee, never … an assumed
rate".

New behaviour: fetch the real fill for the filled order and record that. Fall
back to the stored price **only** when the fill query fails, and log when that
fallback is used so it is auditable.

### Part 4 — `opened_at` must be real

`_close` (line 112) passes `opened_at=0`, so every futures `closed_trades` row
loses its hold duration. `pd["opened_at"]` is already loaded at line 84. Pass it.

*(The identical defect in `spot_scalper._close` is **out of scope** — see Scope.)*

### Part 5 — Executor primitives

`OrderlyFutures` has no way to close a position or read a fill. Add:

- `market_close(asset, side, qty) -> Fill | None` — reduce-only market order in
  the closing direction, returning the real fill price and fee.
- `get_order_fills(order_id) -> tuple[float, float] | None` — `(average_price,
  total_fee)` for a filled order, mirroring `BinanceSpot.get_order_fills`.
- **Fix `cancel_order`** (line 608): it currently fires a junk
  `POST /v1/order` with `side: "CANCEL"` — a malformed order request sent to the
  exchange on **every cancel** — before doing the real `DELETE /v1/order`. Delete
  the dead POST.

---

## Why

DEX trading is currently off (`auto_trade_orderly=False`), so none of this is
live today. That is exactly why it should be fixed now: the defects are
**latent**, and the first thing that arms DEX will arm all three at once.

Severity if armed:

| Defect | Consequence |
|---|---|
| Time stop never closes | An unmonitored leveraged position with **no stop-loss**, unknown to the bot. On a 3× position this is an uncapped loss until liquidation. |
| Regime exit is a no-op | The "move to breakeven" protection does not exist; the fabricated price is then booked as the exit price. |
| Fabricated TP/SL fills | Recorded PnL diverges from the account. Because the daily-loss and consecutive-loss kill switches read `closed_trades.pnl_net`, understated losses mean the **kill switches fire late or not at all**. |

The spot side of the same audit produced measurable damage from a related
accounting bug (a corrupted `fee_entry` booked a −44% trade that never happened
and poisoned the kill-switch inputs). The futures paths carry the same class of
defect with leverage attached.

**Constitution III is NON-NEGOTIABLE** and states: "No cycle ends with an
unprotected leveraged position." The time-stop path ends the cycle with exactly
that. **Constitution V** requires real fills; three futures paths use computed
numbers. This spec closes both.

---

## Constitution impact

| Principle | Impact |
|---|---|
| **I** One Strategy | None — exit mechanics only, no signal change. |
| **II** Reward Must Exceed Cost (v1.1.0) | None — entry gate untouched. |
| **III** No Leveraged Position Without a Confirmed Stop (**NON-NEGOTIABLE**) | **Restored.** The time-stop path currently violates it; Part 1 makes the position either closed or stop-protected at every cycle end. |
| **IV** Unknown State = No Trading | **Strengthened.** Closure is *verified* via `get_open_positions` rather than assumed; a failed or unverifiable close keeps the DB record instead of deleting it. |
| **V** Real Fills Only | **Restored.** Parts 3–4 replace computed exit prices, assumed fee rates and `opened_at=0` with exchange data. |
| **VI** Restart Safety | **Improved.** A position that fails to close keeps its DB row, so startup reconciliation can still see it. Today's silent delete is precisely what breaks restart safety. |
| **VII** Simplicity | Hot-path budget already exceeded (2,495 lines, see 009 converge). This adds ~60 lines (three executor methods ~35, exit-path rework ~25). Justification required in the plan; re-baselining remains a separate recommendation. |
| **VIII** The Bot Trades | Neutral — exits only. A failed close blocks nothing; entries are unaffected. |

---

## Clarifications

### Session 2026-08-15

- **Q1 — Reduce-only flag → RESOLVED EMPIRICALLY, not assumed.** Verified against
  `ccxt/woofipro.py` in the repo venv, which targets **`https://api-evm.orderly.org`**
  — the same base URL `OrderlyFutures` uses — and links the Orderly EVM REST docs.
  The contract:
  - Order body takes **`reduce_only: true`** (bool) — `woofipro.py:1464`.
  - A non-conditional MARKET order uses `order_type: "MARKET"`, `order_quantity`,
    `side: "BUY"|"SELL"`, `symbol`, optional `client_order_id` — the exact shape
    `place_entry` already posts (`executor.py:519-524`).
  - A filled order carries **`average_executed_price`**, **`total_fee`**,
    `fee_asset`, `executed_quantity`, `status: "FILLED"` (`woofipro.py:1911-1918`,
    `parse_order`). `place_entry` already reads the first two (`executor.py:531-533`),
    so `get_order_fills` reuses a proven path.
  - Cancel is `DELETE /v1/order` with `order_id` + `symbol` — already correct in
    the repo; only the junk POST preceding it is removed.
  No fallback design is needed: reduce-only is confirmed available. Post-close
  verification via `get_open_positions` is kept regardless (Constitution IV).
- **Q2 — Close-failure retry → next cycle, not in-cycle.** Re-place the SL, log
  an ERROR, keep the DB row, and let the next 30s cycle retry. No tight loop
  against an exchange that is already failing.
- **Q3 — Emergency escalation → yes, alert and disable.** If the close fails
  **and** the SL re-placement fails, the position is unprotected and
  Constitution III (NON-NEGOTIABLE) is breached. Send a Telegram alert and set
  `auto_trade_orderly=false`. Rationale: III admits no exception, and the repo
  already precedents both halves — the `place_entry` emergency market-close and
  the venue-failure `upsert_setting(f"auto_trade_{venue}", "false")`. Disabling
  blocks **entries only**; exits keep running, so the stranded position is still
  managed.
- **Q4 — Fee fallback → `dex_round_trip_fee_pct`, logged.** Prefer the
  exchange's `total_fee`. When absent or zero, fall back to the setting (never
  the hardcoded `0.0003`) and log that the number is an estimate, so
  Constitution V's audit trail stays honest.
- **Q5 — Regime exit → fix in place, do not remove.** It has no supporting
  evidence, but deleting a risk control is a strategy change that needs its own
  spec and its own evidence. This spec makes it do what it already claims to do.
  Recorded as a converge candidate for a future evidence review.
- **Q6 — Test depth → fakes plus a manual checklist.** CI cannot reach Orderly.
  Correctness is established with a fake `OrderlyFutures` (the
  `test_spot_exit_hardening.py` pattern), and the plan carries a manual `dry_run`
  checklist to run before DEX is ever armed.

---

## Scope

**In scope**

- `trading_bot/futures_scalper.py` — `manage_open_positions` (TP, SL, time-stop
  and regime-exit branches) and `_close` (real `opened_at`, real fees).
- `trading_bot/executor.py` — `OrderlyFutures.market_close`,
  `OrderlyFutures.get_order_fills`, and the `cancel_order` dead-POST removal.
- `tests/test_futures_exit_integrity.py` — new.
- `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`.

**Out of scope**

- **Spot exit paths** — `spot_scalper.manage_open_positions` is untouched. Its
  own `opened_at=0` and theoretical-SL-price defects (audit items 6 and 7) stay
  open for a later spec; mixing venues would make this diff unreviewable.
- Entry logic, thresholds, sizing, leverage, regime detection, toxicity, kill
  switches, the market gate, the universe scanner, feature 009's confirmation.
- `BinanceSpot` (only `OrderlyFutures` changes in `executor.py`).
- Arming DEX (`auto_trade_orderly`) — an operator decision, and explicitly **not**
  part of this spec.
- Any new dependency, thread, process, or DB migration.

---

## Constraints

- **Constitution III is the acceptance bar**: no code path may end a cycle with
  a position that is neither closed nor stop-protected.
- **Verify, never assume** (IV): closure is confirmed against
  `get_open_positions` before the DB row is deleted.
- **Real fills** (V): computed prices only as a logged fallback.
- **`dry_run` unchanged** — every new order path checks it, exactly like
  `place_entry`/`market_sell`.
- **Fail safe, not silent**: a failed close keeps the DB record. Deleting a row
  the bot could not verify as closed is the defect being fixed.
- **Settings, never constants** — the fee fallback reads
  `dex_round_trip_fee_pct` (Q4).
- **Minimum modification** — no restructuring of `manage_open_positions`; the
  branch order (TP → SL → time stop → regime) is preserved.
- **Structured single-line logs, no emoji**; exits at INFO, failures at ERROR.
- **Implementation on `main`** — no feature branches.

---

## Assumptions

- Orderly supports a reduce-only market order and exposes
  `average_executed_price` / `total_fee` on a filled order, consistent with the
  fields `place_entry` already reads (lines 531–533). **Q1 flags this as
  unverified.**
- DEX stays off (`auto_trade_orderly=False`) throughout implementation, so there
  is no live exposure and no migration or backfill of existing futures trades
  (there are none — `closed_trades` contains binance rows only).
- CI cannot reach Orderly; correctness is established with fakes plus a manual
  `dry_run` checklist (Q6).
- The existing `place_entry` emergency path (SL failure → market close → return
  `None`) is the precedent for Q3's escalation shape. *Note: that path leaves a
  dangling TP order — related, but a separate defect, not fixed here.*

---

## Acceptance criteria

1. **Time stop closes the position** — the time-stop branch sends a closing
   order and, only after the close is confirmed, records the trade and deletes
   the DB row.
2. **Failed close keeps the position** — if the close order fails or cannot be
   verified, the DB row is **not** deleted, no `closed_trades` row is written,
   and an ERROR is logged.
3. **Failed close re-protects** — after a failed close, the stop-loss is
   re-placed before the cycle ends (Constitution III).
4. **Emergency escalation** — if the close and the SL re-placement both fail, a
   Telegram alert fires and `auto_trade_orderly` is disabled (subject to Q3).
5. **Closure is verified** — the position is confirmed absent from
   `get_open_positions` before the DB row is deleted (Constitution IV).
6. **Time-stop PnL is real** — the recorded exit price is the close fill, never
   the entry price.
7. **Regime exit reaches the exchange** — the breakeven TP is cancelled and
   re-placed; `update_position` runs **only** on exchange success.
8. **Regime-exit failure is safe** — a failed replacement leaves the DB
   unchanged, logs an ERROR, and leaves the SL in place.
9. **TP exits record real fills** — the exit price and fee come from
   `get_order_fills`; the stored price is used only as a logged fallback.
10. **SL exits record real fills** — same, so slippage past the trigger is
    captured.
11. **`opened_at` is real** — every futures `closed_trades` row carries the
    position's true `opened_at`.
12. **Fees are real** — the exchange fee is recorded; the fallback is
    `dex_round_trip_fee_pct`, logged as an estimate (subject to Q4).
13. **`cancel_order` sends one request** — the junk `POST /v1/order` with
    `side: "CANCEL"` is gone; only the `DELETE` remains.
14. **`dry_run` is honoured** — `market_close` places no order under `dry_run`
    and returns a synthetic fill, exactly like the other order paths.
15. **Tests** — `tests/test_futures_exit_integrity.py` covers AC1–AC14 with a
    fake `OrderlyFutures`; the existing 88 tests continue to pass.
16. **Docs** — `docs/CURRENT_STATE.md` gains a feature-010 section;
    `docs/CHANGELOG.md` gains a `fix:` entry recording the Constitution III/V
    repair.
