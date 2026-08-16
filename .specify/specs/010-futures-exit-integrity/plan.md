# Plan: Futures Exit Integrity

**Feature**: 010-futures-exit-integrity | **Date**: 2026-08-15 | **Spec**: `specs/010-futures-exit-integrity/spec.md`
**Status**: Implemented 2026-08-15 — 17/17 tasks, 102/102 tests green
**Branch**: `main` (repo convention — no feature branches)

**Input**: Feature spec (Clarified, Q1–Q6 resolved, 16 acceptance criteria) and
Constitution v1.1.0. Q1 was resolved **empirically** against `ccxt/woofipro.py`
(same base URL as `OrderlyFutures`), so this plan pins a verified API contract
rather than an assumed one.

## Summary

Three of the five paths in `futures_scalper.manage_open_positions` violate
Constitution III or V. This repairs all three plus two supporting defects:

1. **Time stop** cancels both brackets, deletes the DB row, and never closes the
   position → replaced by cancel → reduce-only market close → **verify** →
   record from the real fill. Failure keeps the row and re-places the SL.
2. **Regime exit** writes a breakeven TP to the DB only → replaced by cancel +
   re-place, DB updated only on exchange success.
3. **TP/SL exits** record intended prices at a flat `0.0003` → replaced by real
   `average_executed_price` / `total_fee`, with a logged settings-based fallback.
4. **`opened_at=0`** → the real value (already loaded two lines above).
5. **`cancel_order`** fires a junk `POST /v1/order` with `side: "CANCEL"` on
   every call → removed.

`OrderlyFutures` gains `market_close` and `get_order_fills`. DEX is off
throughout, so there is no live exposure; correctness comes from fakes plus a
manual `dry_run` checklist.

## Technical Context

**Language/Version**: Python 3.11 (`./venv/bin/python`)

**Primary Dependencies**: stdlib + `requests` (already used). **No new
dependency** — `ccxt` was consulted as documentation only and is **not**
imported by the bot.

**Storage**: SQLite — **no schema change, no migration**. `closed_trades`
contains binance rows only, so nothing to backfill.

**Testing**: `./venv/bin/python -m pytest tests/test_futures_exit_integrity.py --basetemp=.pytest_tmp -q`
plus the full suite (88 green before this feature).

**Target Platform**: Linux server (Docker → Watchtower)

**Performance Goals**: no change to cycle cost on the happy path — the TP/SL
branches add one `get_order_fills` call **only when an order has already
filled** (i.e. at most once per position, on the cycle it closes).

**Constraints**: Constitution III is the acceptance bar; VII budget already
exceeded (2,495 lines); `dry_run` honoured on every new order path; settings not
constants; structured single-line logs.

**Scale/Scope**: 1 venue, ≤ ~20 positions; 16 acceptance criteria; 2 source
files + 1 new test file + 2 docs.

## Constitution Check

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy | ✅ | Exit mechanics only; no signal, threshold or direction logic touched. |
| **II** Reward Must Exceed Cost (v1.1.0) | ✅ | Entry gate untouched. |
| **III** No Leveraged Position Without a Confirmed Stop (**NON-NEGOTIABLE**) | ✅ **restored** | Currently violated by the time-stop path. After this change every cycle end leaves the position either **closed and verified**, or **stop-protected** (SL re-placed), or — if both fail — **alerted and the venue disabled** (Q3). |
| **IV** Unknown State = No Trading | ✅ **strengthened** | Closure is verified via `get_open_positions` before the DB row is deleted; an unverifiable close keeps the row. Today's blind delete is the defect. |
| **V** Real Fills Only | ✅ **restored** | `average_executed_price` / `total_fee` replace intended prices and the `0.0003` constant; fallbacks are logged; `opened_at` becomes real. |
| **VI** Restart Safety | ✅ **improved** | A position that fails to close keeps its DB row, so `_reconcile_startup` can still see it. |
| **VII** Simplicity | ⚠️ justified | Budget already exceeded at **2,495** lines before this feature (see 009 converge). Adds ≈70: `market_close` ~30, `get_order_fills` ~18, exit-path rework ~20, `cancel_order` **−6**. No new module, no new dependency. Re-baselining remains spec 013. |
| **VIII** The Bot Trades | ✅ | Exits only; entries unaffected. The Q3 escalation disables entries on a venue that has *already* failed twice on a safety-critical operation — a correct trade-off, not over-blocking. |

**Post-design re-check**: the escalation ladder (close → verify → re-place SL →
alert + disable) has no branch that ends a cycle with an unprotected position and
no branch that deletes an unverified row. III and IV hold by construction.

## Research Summary — verified API contract (Q1)

Source: `venv/lib/python3.13/site-packages/ccxt/woofipro.py`, which declares
`'public'/'private': 'https://api-evm.orderly.org'` (lines 137–138) — the same
host `OrderlyFutures.__init__` defaults to (`executor.py:404`) — and links the
Orderly EVM REST docs in its method docstrings.

| Need | Field / endpoint | Evidence |
|---|---|---|
| Reduce-only close | `reduce_only: True` in the POST body | `woofipro.py:1464` |
| Market order shape | `order_type: "MARKET"`, `order_quantity`, `side`, `symbol` | `woofipro.py:1455-1470`; identical to `executor.py:519-524` |
| Real fill price | `average_executed_price` | `woofipro.py:1342`, `1918` |
| Real fee | `total_fee` (+ `fee_asset`) | `woofipro.py:1344`, `1915` |
| Filled status | `status == "FILLED"` | `woofipro.py:1914` |
| Read one order | `GET /v1/order/{oid}` | `woofipro.py:215`; already used at `executor.py:603` |
| Cancel | `DELETE /v1/order?order_id=&symbol=` | already correct at `executor.py:616-620` |

`place_entry` already reads `average_executed_price` and `total_fee`
(`executor.py:531-533`), so `get_order_fills` reuses a path proven in
production, not a new guess.

**Current code anchors** (read at plan time):
`futures_scalper.manage_open_positions` def **76**; TP branch **86-88**; SL
branch **90-92**; time stop **94-98**; regime exit **100-106**; `_close` def
**108-113** (`opened_at=0` at 112, `fr` fee-rate param). `OrderlyFutures`:
`_post` **440**, `place_entry` **482**, `get_open_positions` **590**,
`get_order_status` **601**, `cancel_order` **608** (junk POST at **610**).

## Pinned Mechanisms

### M1 — The exit ladder (Constitution III by construction)

```
time stop reached
  ├─ cancel TP, cancel SL
  ├─ market_close(reduce_only=True)
  │    ├─ fill  → verify get_open_positions() empty
  │    │           ├─ empty      → record real fill, delete row      [AC1,5,6]
  │    │           └─ NOT empty  → keep row, re-place SL, ERROR      [AC2,3]
  │    └─ None  → keep row, re-place SL, ERROR                       [AC2,3]
  └─ if SL re-placement ALSO fails → Telegram alert
                                   + auto_trade_orderly=false        [AC4]
```

No branch deletes an unverified row, and no branch ends without either closure
or a stop. Re-placement reuses the existing `sl_body` shape from `place_entry`
(`executor.py:562-567`) — a `STOP_MARKET` at the stored `sl_price`.

### M2 — Real fills with a logged fallback (Constitution V)

```python
def _real_exit(exchange, order_id, fallback_price, qty):
    """(price, fee) from the exchange; falls back to the stored price and the
    dex_round_trip_fee_pct setting, logging that the numbers are estimates."""
    res = exchange.get_order_fills(order_id) if order_id else None
    if res and res[0] > 0:
        return res
    fee = fallback_price * qty * (get_setting_float("dex_round_trip_fee_pct", 0.06) / 100) / 2
    logger.warning(f"[EXIT] {order_id}: no fill data — recording estimate price={fallback_price}")
    return fallback_price, fee
```

The `/2` converts the round-trip setting to one leg. `_close` keeps its
signature shape but takes explicit `fee_entry`/`fee_exit` instead of the `fr`
rate, matching `spot_scalper._close`.

### M3 — Regime exit is transactional against the exchange

Cancel the live TP, place the replacement, and call `update_position` **only**
if the new order id comes back. On cancel-success/place-failure the position has
no TP but still has its SL; an ERROR is logged and the next cycle retries. The
DB never records a price the exchange does not hold.

## Detailed Design

### Part 1 — `trading_bot/executor.py` (`OrderlyFutures` only)

- **`market_close(asset, side, qty) -> Fill | None`** — `dry_run` short-circuits
  with a synthetic `Fill` (AC14, same shape as `place_entry`/`market_sell`).
  Otherwise POST `/v1/order` with `symbol`, `side` = `"SELL"` for a long /
  `"BUY"` for a short, `order_type: "MARKET"`, `order_quantity` (tick-formatted),
  `reduce_only: True`, `client_order_id`. Parse `average_executed_price`,
  `executed_quantity`, `total_fee`. Non-200 → `None`.
- **`get_order_fills(order_id) -> tuple[float, float] | None`** — GET
  `/v1/order/{order_id}`; return `(average_executed_price, total_fee)` when both
  are present and the price is > 0, else `None`. Mirrors
  `BinanceSpot.get_order_fills`.
- **`cancel_order`** — delete the junk `POST /v1/order` with `side: "CANCEL"`
  (line 610) and the stale comment; keep the `DELETE` (AC13).

### Part 2 — `trading_bot/futures_scalper.py`

- **TP branch**: `xp, fee_xp = _real_exit(exchange, tpid, pd["tp_price"], q)` (AC9).
- **SL branch**: same with `slid` / `pd["sl_price"]` (AC10).
- **Time stop**: the M1 ladder.
- **Regime exit**: M3.
- **`_close`**: takes `fee_ep`/`fee_xp` and `opened_at`; passes the real
  `opened_at` (AC11) and real fees (AC12). Entry fee comes from
  `pd.get("fee_entry")` when present, else the settings estimate — note
  `futures_scalper._save_open` does **not** store `fee_entry` today, so the
  estimate is the normal path until a later spec fixes that. *(Recorded as a
  converge item, not fixed here — it is an entry-side defect.)*

### Part 3 — Tests

New `tests/test_futures_exit_integrity.py`; a fake `OrderlyFutures` records
every call and can be scripted to fail any of `market_close`, `get_open_positions`,
`cancel_order`, `place_entry` (for SL re-placement).

### Part 4 — Docs

`docs/CURRENT_STATE.md` feature-010 section + `docs/CHANGELOG.md` `fix:` entry.

## Edge Cases

| Edge case | Handling |
|---|---|
| Close returns a fill but the position is still open (partial) | Verification fails ⇒ keep the row, re-place SL, ERROR. Next cycle retries with the remaining qty from the DB. |
| `get_open_positions` itself fails | Treated as "not verified" ⇒ keep the row (Constitution IV). |
| Cancel TP fails before the close | Proceed with the reduce-only close anyway (it cannot flip the position), then verify; a dangling TP on a flat position is logged. |
| SL already filled between cycles | The SL branch runs before the time stop and records the real fill — unchanged ordering. |
| `dry_run` | `market_close` returns a synthetic fill and places nothing; the ladder proceeds so the logic is exercisable without an exchange (AC14). |
| Regime exit while TP is missing (`tpid` None) | Branch is skipped, as today. |
| Fee absent/zero | Settings-based estimate, logged (AC12). |
| Position never had an SL (`slid` None) | Re-placement uses the stored `sl_price`; if that is also absent, escalate per Q3 — a leveraged position with no recoverable stop is exactly the III breach the alert exists for. |

## Out of Scope

- **Spot** exit paths (`spot_scalper`), including its own `opened_at=0` and
  theoretical-SL-price defects — audit items 6 and 7, deliberately deferred.
- `BinanceSpot`; entry logic; thresholds; sizing; leverage; regime detection;
  toxicity; kill switches; market gate; universe scanner; feature 009.
- `place_entry`'s dangling-TP-on-emergency-close defect (related, separate).
- `futures_scalper._save_open` not storing `fee_entry` (entry-side; converge item).
- Arming DEX — operator decision.

## Testing Strategy (16 acceptance criteria)

| Test | Verifies | AC |
|---|---|---|
| `test_time_stop_closes_position` | `market_close` called with the closing side and qty; row deleted; `closed_trades` written | AC1 |
| `test_time_stop_records_real_fill` | Exit price is the close fill, **not** `entry_price` | AC6 |
| `test_time_stop_failed_close_keeps_position` | `market_close` → None ⇒ row survives, no `closed_trades` row | AC2 |
| `test_time_stop_failed_close_replaces_sl` | SL re-placed after a failed close | AC3 |
| `test_time_stop_unverified_close_keeps_position` | Fill returned but `get_open_positions` still non-empty ⇒ row survives | AC5 |
| `test_double_failure_escalates` | Close **and** SL re-placement fail ⇒ `send_message` called, `auto_trade_orderly` == false | AC4 |
| `test_regime_exit_amends_exchange` | TP cancelled and re-placed; `tp_price` updated | AC7 |
| `test_regime_exit_failure_leaves_db` | Replacement fails ⇒ `tp_price` unchanged, ERROR logged | AC8 |
| `test_tp_exit_uses_real_fill` | TP exit price/fee from `get_order_fills`, not `pd["tp_price"]` | AC9 |
| `test_sl_exit_uses_real_fill` | Same for SL — slippage past the trigger captured | AC10 |
| `test_opened_at_is_real` | `closed_trades.opened_at` == the position's `opened_at` | AC11 |
| `test_fee_fallback_uses_setting` | No exchange fee ⇒ `dex_round_trip_fee_pct`, never `0.0003` | AC12 |
| `test_cancel_order_single_request` | `cancel_order` issues **one** request, no POST | AC13 |
| `test_dry_run_places_nothing` | `dry_run=true` ⇒ no HTTP, synthetic fill | AC14 |

**Regression**: full suite (88) must stay green.

### Manual `dry_run` checklist (Q6) — before DEX is ever armed

1. `dry_run=true`, `auto_trade_orderly=Automatic`, seed one futures position with
   a past `opened_at`.
2. Confirm the log shows `market_close` → verification → `closed_trades` row with
   a non-entry exit price and a real `opened_at`.
3. Force an adverse regime; confirm cancel + re-place appear in the log and
   `tp_price` moves only after success.
4. Only then consider `dry_run=false`.

## File Manifest

| File | Action |
|---|---|
| `trading_bot/executor.py` | `OrderlyFutures`: +`market_close`, +`get_order_fills`, `cancel_order` junk POST removed |
| `trading_bot/futures_scalper.py` | `manage_open_positions` TP/SL/time-stop/regime branches; `_close` signature (real fees + `opened_at`) |
| `tests/test_futures_exit_integrity.py` | NEW — 14 tests |
| `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md` | feature-010 section + `fix:` entry |
| `bot.py`, `spot_scalper.py`, `BinanceSpot`, `db/*`, `trade/*` | **Unchanged** |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `reduce_only` rejected by the live API | Low | High | Verified against ccxt's Orderly implementation (same host); post-close verification catches it regardless; manual `dry_run` checklist before arming |
| Verification false-negative strands a closed position in the DB | Low | Medium | Safe direction — a stale row is visible to reconciliation and the dashboard; the alternative (blind delete) is the defect being fixed |
| Q3 escalation disables the venue on a transient blip | Low | Medium | Requires **two** failures (close *and* SL re-placement) on a safety-critical path; disables entries only, exits keep running |
| Fee estimate mistaken for a real fill | Low | Low | Every fallback logs a WARNING naming the order (Constitution V audit trail) |
| No live validation possible in CI | Certain | Medium | Fakes + the manual `dry_run` checklist; DEX stays off until it passes |
| Line budget (VII) | — | — | +70 net; pre-existing overrun, spec 013 |
