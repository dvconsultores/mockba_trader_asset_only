# Plan: Spot Exit Hardening (gap/crash protection)

**Feature**: 006-spot-exit-hardening | **Date**: 2026-08-12 | **Spec**: `specs/006-spot-exit-hardening/spec.md`
**Status**: Draft — awaiting implementation authorization
**Branch**: `develop` (per spec Q7 — implementation directly on `develop`, no feature branches)

**Input**: Feature spec (Draft, Q1–Q4 resolved, 12 acceptance criteria). This plan
does not re-litigate resolved decisions; it verifies them against the code, pins
the ATR source the spec explicitly delegated to planning, and fixes the design.

## Summary

Two small, setting-driven protections that hard-cap the worst-case loss of a
spot position when price gaps or crashes through the stop, plus the settings
and docs that ship with them. **Part 1**: a spot-only, strictly-additive
volatility cap (`universe_max_atr_pct`, default 1.5) in `trade/universe.py`
`scan_venue` that rejects crash-prone names — pinned to the Stage-4 replay
`atr_pct_median` (calibrated: removes BICO-class ≈1.9–2.1%, keeps PUMP ≈0.6% /
MMT ≈0.87%, verified against the live DB), applied after the replay and before
ranking/storage under a `venue == "binance"` branch, with a `dropped_by_max_atr`
summary count. **Part 2**: a catastrophic-move guard (`max_loss_per_position_pct`,
default 3.0) as the **first**, **fill-aware** check in
`trading_bot/spot_scalper.py` `manage_open_positions` — verifies TP/SL fill
status before acting (already-FILLED → real fill with real reason, never a
market sell), otherwise cancels TP/SL + `market_sell` + `_close(rsn='crash_guard')`
when `live < entry × (1 − pct/100)`; `live is None` → no action; `crash_guard`
stamps the `_last_sl` re-entry cooldown exactly like `sl`. **Part 3**: the two
`SettingSpec`s in `trade/settings_schema.py` (defaults in `get_setting_*`
fallbacks, no migration), cross-checks in `trade/settings_rules.py` (hard error
when `max_loss_per_position_pct < sl_min_pct_spot`, equality benign; optional
empty-universe warn for the ATR cap), the `crash_guard` Closed-Trades label in
`dashboard/main.py`, docs, and a new test file. Normal TP/SL/time-stop behavior,
entry logic, the futures/DEX path, and the executor are untouched.

## Technical Context

**Language/Version**: Python 3.11 (repo venv; `./venv/bin/python`)

**Primary Dependencies**: `requests`, stdlib `sqlite3` — **no new dependencies**

**Storage**: SQLite `data/trading.db` — **no schema change / no migration**;
new settings defaults live in `get_setting_float` fallbacks (established
Amendment 003/005 pattern); `asset_universe.atr_pct_median` is read as the
calibration source (already stored).

**Testing**: pytest — `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q`

**Target Platform**: Linux server (bot.py + telegram.py, supervised by forever.py)

**Project Type**: trading bot, single-repo Python (no framework)

**Performance Goals**: Part 1 adds zero API calls (reuses the already-computed
replay `atr_pct_median`); Part 2 adds at most one `get_price` call per managed
asset per cycle (needed for the floor) — no other new network traffic.

**Constraints**: Constitution VII line budget (small additions to existing
functions, no new module); settings read fresh each cycle; minimum modification
(minimal-change policy, `.github/instructions/minimal-change.instructions.md`);
no import from `trade/main.py`/`trade/signal_agent/`; `dry_run` behavior
unchanged.

**Scale/Scope**: 2 venues (binance, orderly) × ≤ ~50 universe members; spot exit
path ≤ 2 concurrent positions; 12 acceptance criteria.

## Constitution Check

*GATE: evaluated before Phase 0 research and re-checked after Phase 1 design
(below).*

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy (mean reversion) | ✅ | Both additions are exit-safety / universe-screening, not a new strategy. The max-ATR cap removes crash-prone names from the *same* mean-reversion universe; the crash guard is an emergency exit on a position already crashed through a hard floor. No ML/LLM, no entry-rule change. |
| **II** Reward Exceeds Risk (NON-NEGOTIABLE) | ✅ | Untouched. `tp_pct > sl_pct`, net-edge and breakeven startup gates unchanged. The new validator cross-check (`max_loss_per_position_pct` vs `sl_min_pct_spot`) is an *additional* hard startup gate in the same family (Constitution II gates, not warnings). |
| **III** No Position Without a Stop (NON-NEGOTIABLE) | ✅ | Stops are never removed or relaxed. The guard only **adds** an emergency exit that fires when price has already crashed **below** the configured SL — and only after the TP/SL fill status is verified (never market-sells a position the exchange already closed). Validation prevents the guard from becoming the primary stop (hard error when it sits strictly inside `sl_min_pct_spot`). |
| **IV** Unknown State = No Trading (NON-NEGOTIABLE) | ✅ | `live is None` → the guard takes **no action** and keeps the position for the next cycle (no guessing, no assumed price). The universe cap fails closed (reject on missing ATR data is handled by the existing `select_ranked` `m is None` path). |
| **V** Real Fills Only | ✅ | Every crash-guard exit records the actual `market_sell` fill price and fee through the existing `_close` → `record_closed_trade` path (`exit_reason='crash_guard'`); already-filled TP/SL orders record their real fills with real reasons. No assumed prices except the pre-existing documented dry-run fallback to the entry price for simulated fills (`fill_price == 0.0`). |
| **VI** Restart Safety | ✅ | No new persistent state. The crash floor is recomputed from `open_positions` each cycle; the cooldown is in-memory (`_last_sl`) exactly like today. Startup reconciliation (`_reconcile_startup`) untouched. |
| **VII** Simplicity Is a Constraint | ✅ | No new module. `scan_venue` grows ~8–10 lines; `manage_open_positions` grows ~30 lines (guard block mirrors the existing `sl` branch structure, including the no-balance/orphan recovery); `_close` one-line condition change; `settings_schema.py` +2 specs; `settings_rules.py` +2 blocks; `dashboard/main.py` 1 line. Line budgets hold (see 005 precedent — hot-path total already justified historically). |
| **VIII** The Bot Trades | ✅ | Verified against the live DB (2026-08-12): cap 1.5 removes only BICO (1.86) and keeps MMT (0.87), PUMP (0.60), GIGGLE, RE, CRV, ZAMA — trade frequency preserved. The floor is a rare emergency backstop (default 3.0 ≫ effective SL ≈0.6–0.8%), not a trade-suppressor. |

**Post-design re-check**: the design below satisfies III (guard never relaxes
stops; validation keeps it a pure gap-catcher), IV (no-action on `None`),
V (real fill + real reason in all three crash-guard sub-paths), VIII (calibrated
cap removes only BICO-class names). No gate violation.

## Project Structure

### Documentation (this feature)

```text
.specify/specs/006-spot-exit-hardening/
├── spec.md              # authoritative feature spec (Draft, Q1–Q4 resolved)
├── plan.md              # this file
├── research.md          # verified signatures / call sites / calibration evidence
├── data-model.md        # settings, exit-reason enum, floor state, summary contract
├── contracts/
│   └── exit-reasons.md  # closed_trades.exit_reason contract (+ crash_guard)
├── quickstart.md        # validation scenarios
└── tasks.md             # Phase 2 — NOT created by this plan
```

### Source Code (repository root)

```text
trade/universe.py               # scan_venue +~8–10 lines — post-replay spot-only
                                #   max-ATR hard filter + dropped_by_max_atr summary
trading_bot/spot_scalper.py     # manage_open_positions +~30 lines — crash-guard
                                #   first check (fill-aware); always fetch price;
                                #   _close: crash_guard stamps _last_sl (1 line)
trade/settings_schema.py        # +2 SettingSpecs (groups "universe" and "exit")
trade/settings_rules.py         # +2 cross-check blocks (hard error; empty-universe warn)
dashboard/main.py               # REASON_LABELS + "crash_guard": "Crash guard" (1 line)
tests/test_spot_exit_hardening.py  # NEW — ~14 tests (AC1–AC12)
docs/CURRENT_STATE.md           # +feature 006 section
docs/CHANGELOG.md               # +feat: entry (2026-08-12)
```

**Structure Decision**: single-project repo; both additions live inside existing
functions with no new module, mirroring the 005 minimal-footprint approach.
`trade/main.py`/`trade/signal_agent/` do not exist in this repo — the
never-import constraint is trivially satisfied.

## Research Summary

See `research.md` for the full code-verified inventory. Key findings:

- **`_hard_filters_pass`** (`trade/universe.py` line 299) takes **no venue** —
  venue-branching lives in `scan_venue` (precedent: `hold_key =
  "max_hold_minutes_spot" if venue == "binance" else ...`, line 651). The new
  cap is added in `scan_venue`, not inside `_hard_filters_pass`.
- **Stage-1 has no usable ATR**: `_fetch_binance_24hr` (line 162) discards
  `highPrice`/`lowPrice` and returns only `quoteVolume`; the candidate dict
  (line 246) has no volatility field. The 24h high–low range is not currently
  available without a return-type change.
- **The calibrated measure is Stage-4 `atr_pct_median`** (`replay_symbol`,
  line 415, key at line 479; stored at line 711; ranking tiebreak at line 716).
  Live-DB evidence (2026-08-12): binance stores BICO 1.86, MMT 0.87, PUMP 0.60,
  GIGGLE 0.61, RE 0.59, CRV 0.40, ZAMA 0.33 → cap 1.5 removes exactly BICO.
  → **Pinned decision: ATR source = `atr_pct_median`.** Placement follows the
  data: the filter runs **after the Stage-4 replay loop and before
  `select_ranked`** (not inside Stage 2). This is the documented, deliberate
  deviation from the spec's "before the depth stage" preference — a 1.5% cap on
  the 24h range would empty the universe (Constitution VIII), and the
  calibrated measure only exists at Stage 4 (rationale in `research.md` §1.5).
- **`manage_open_positions`** (`spot_scalper.py` line 75): live price fetched
  only when some position has an `sl_price` (line 80) — must become
  unconditional; exchange-fill checks run first (lines 91–95); the `sl` branch
  (lines 94–123) contains the exact cancel + market-sell + no-balance/orphan
  recovery pattern the crash guard mirrors; `_close` (line 164) stamps
  `_last_sl` only for `rsn == "sl"` (line 166) — extended to `crash_guard`.
- **Exchange interface** (executor.py): `get_price` (164), `get_asset_balance`
  (153), `market_sell` (300, dry-run `Fill(fill_price=0.0)`, line 304),
  `get_order_status` (346), `get_order_fills` (353), `cancel_order` (376) — all
  reused, nothing new.
- **Validator precedents** (`settings_rules.py`): hard-error pattern
  `tp_min_pct <= sl_min_pct` (lines 85–90) and empty-universe warn for depth
  (lines 228–257) — both mirrored for the two new cross-checks.
- **No bot.py change needed**: `spot_manage(asset, binance)` (bot.py line 406)
  already runs exits first in the per-asset loop; the guard lives entirely in
  `manage_open_positions`.
- **Dashboard**: `REASON_LABELS` (dashboard/main.py line 823) needs exactly one
  new entry; used at line 935 with an uppercased raw fallback otherwise.

## Data Model / Contracts

- **Settings** (`data-model.md` §1): `universe_max_atr_pct` (float, group
  `universe`, default 1.5, hard 0.1–20, soft 0.5–5) and
  `max_loss_per_position_pct` (float, group `exit`, default 3.0, hard 0.1–20,
  soft 1–5, `depends_on=("sl_min_pct_spot",)`). Defaults in `get_setting_*`
  fallbacks; no migration.
- **Exit-reason contract** (`contracts/exit-reasons.md`): `closed_trades`
  gains `crash_guard`; fill semantics + cooldown-stamping rules per value;
  `dashboard` label contract.
- **Crash-floor state** (`data-model.md` §3): `floor = entry_price × (1 −
  max_loss_per_position_pct/100)` per open spot position; `_last_sl` cooldown
  stamping for `("sl", "crash_guard")`.
- **Scan summary** (`data-model.md` §4): `scan_venue` gains `dropped_by_max_atr`
  (int, binance only).

## Detailed Design

### Part 1 — `trade/universe.py`: spot-only max-ATR hard filter

**Where**: in `scan_venue`, immediately after the Stage-4 replay loop (metrics
dict built at lines 656–671) and **before** Stage 5 `select_ranked` (call at
line 677) / `replace_universe` (line 679). Read `universe_max_atr_pct` fresh
via `get_setting_float`; apply under a `venue == "binance"` branch (the
`hold_key` venue-branch precedent, line 651) so the Orderly/futures universe
is untouched.

**Exact change (design-level, no code in this phase)**:

```python
# ── Stage 4 — replay (the ranking key) ──
metrics = { ... }                                   # existing loop (lines 656–671)

# ── NEW: spot-only max-ATR cap (crash-prone names never stored) ──
if venue == "binance":
    max_atr = get_setting_float("universe_max_atr_pct", 1.5)
    _before = len(checked)
    # Keep candidates with missing/None ATR (replay failed) — the existing
    # select_ranked `m is None` path handles those; only genuinely high-ATR
    # names are dropped here, so dropped_by_max_atr counts real drops only.
    checked = [c for c in checked
               if (m := metrics.get(c["asset"])) is None
               or m.get("atr_pct_median") is None
               or m["atr_pct_median"] <= max_atr]
    summary["dropped_by_max_atr"] = _before - len(checked)

# ── Stage 5 — rank & store ── (unchanged)
rows = select_ranked(checked, metrics, min_signals, min_rec, size, started)
```

- **Semantics**: a candidate with a numeric `atr_pct_median > max_atr` is
  removed from `checked` and can never reach `select_ranked`/`replace_universe`
  (never stored). Candidates whose replay failed (`m is None`) or produced no
  ATR (`atr_pct_median is None`) are **left to the existing `select_ranked`**
  exclusion (`if m is None: continue` and the missing-ATR storage path) — the
  cap never *loosens* anything and never changes failure semantics.
- **Strictly additive** (AC2): the filter only removes names; the Stage-2
  volume/spread/rank/fundability filters (`_hard_filters_pass`, lines 299–316)
  are untouched and still run first.
- **Setting-driven & fresh** (AC3): read via `get_setting_float` every scan.
- **Observability** (user requirement): `summary["dropped_by_max_atr"]` is the
  count of cap-dropped names; `_scan_summary_message` (line 782) surfaces it in
  the Telegram scan notification ("dropped_by_max_atr=N") so the operator sees
  when crash-prone names are excluded.
- **No `_hard_filters_pass` signature change** (minimal change).

**Rationale for placement (pinned decision)**: the spec delegated the ATR
source to planning and requires a default calibrated so only BICO-class names
are removed while PUMP/MMT remain. Only `atr_pct_median` reproduces that
calibration (verified against the live DB — `research.md` §1.4). Because that
measure is produced by the Stage-4 replay, the filter cannot run in Stage 2
"before the depth stage"; applying a non-calibrated proxy (24h range) with a
1.5% cap would empty the universe (Constitution VIII). This is the documented
deviation, with the budget trade-off: the cap saves the Stage-5 ranking/storage
work and keeps crash-prone names out of the stored universe (the acceptance
criterion that matters — "never stored").

### Part 2 — `trading_bot/spot_scalper.py`: catastrophic-move guard

**Where**: `manage_open_positions` (line 75). Two structural changes plus the
guard block:

**Change A — always fetch the live price** (line 80):

```python
# Before: live = exchange.get_price(asset) if any(pd.get("sl_price") for pd in positions) else None
live = exchange.get_price(asset)
```

The crash floor applies to **all** positions (including ones stored with
`sl_price = None`, `_save_open` line 243). `live is None` (API failure) →
no action anywhere (existing `sl` check already guards on `None`; the new guard
guards on `None` too). Intended cost: one `get_price` call per managed asset per
cycle even when no `sl_price` is stored.

**Change B — guard-first, fill-aware crash-guard block** placed as the **first
per-position check** inside the `for pd in positions:` loop (before the existing
exchange-fill checks at lines 91–95):

```python
mlp = get_setting_float("max_loss_per_position_pct", 3.0)   # read once, before the loop

# ── Crash guard (gap/crash floor) — FIRST check, fill-aware ──
if live is not None and live < ep * (1 - mlp / 100):
    # Already-filled TP/SL → record the REAL fill with its REAL reason;
    # never market-sell a position the exchange already closed.
    if tpid and exchange.get_order_status(sym, tpid) == "FILLED":
        xp, fee_xp = _real_fill(exchange, sym, tpid, float(pd["tp_price"]))
        _close(asset, "binance", "long", ep, xp, sp, q, pid, si, "tp", fee_ep, fee_xp); continue
    if slid and exchange.get_order_status(sym, slid) == "FILLED":
        _close(asset, "binance", "long", ep, float(pd.get("sl_price", ep)), sp, q, pid, si,
               "sl", fee_ep, 0.0); continue
    # Free the coins held by the open TP/SL orders first (existing pattern).
    tp_cancel_ok = (not tpid) or exchange.cancel_order(sym, tpid)
    if slid: exchange.cancel_order(sym, slid)
    if not tp_cancel_ok:
        logger.error(f"[EXIT] {asset} crash_guard: TP cancel failed — keeping position to retry")
        continue
    sell = exchange.market_sell(asset, q)
    if sell is None:
        bal = exchange.get_asset_balance(asset)
        if bal is not None and bal < q:
            if tpid:
                fill_info = exchange.get_order_fills(sym, tpid)
                if fill_info:
                    xp, fee_xp = fill_info
                    logger.warning(f"[EXIT] {asset} crash_guard: no balance ({bal}) — closing as TP via real fill {xp}")
                    _close(asset, "binance", "long", ep, xp, sp, q, pid, si, "tp", fee_ep, fee_xp)
                    continue
            xp = live if live else ep
            logger.warning(f"[EXIT] {asset} crash_guard: no balance ({bal}) — closing orphan position at {xp}")
            _close(asset, "binance", "long", ep, xp, sp, q, pid, si, "orphan", fee_ep, 0.0)
        else:
            logger.error(f"[EXIT] {asset} crash_guard: market sell failed — keeping position to retry")
        continue
    xp = sell.fill_price if sell.fill_price > 0 else ep
    _close(asset, "binance", "long", ep, xp, sp, q, pid, si, "crash_guard", fee_ep, sell.fee_amount)
    continue
```

- **Guard-first** (AC5): the floor check is the first per-position check, so a
  gap is caught in the same ~30s cycle.
- **Fill-aware** (AC5/AC8/AC9): TP/SL fill status is verified *inside* the guard
  before any cancel/sell — an already-filled order is recorded with its real
  reason (`tp`/`sl`) and never market-sold.
- **`None` price → no action** (AC6, Constitution IV): `live is None` skips the
  block entirely; the position is kept for the next cycle.
- **Real fills** (AC9, Constitution V): the `crash_guard` close uses
  `sell.fill_price` / `sell.fee_amount` (dry-run `fill_price == 0.0` falls back
  to `ep`, preserving the existing dry-run convention used by the `sl`/
  `time_stop` branches).
- **No-balance / orphan recovery** (AC8): mirrors the existing `sl`/`time_stop`
  recovery (lines 105–121) so a position already closed by the exchange is
  never phantom-sold or mis-recorded.
- **Above-floor positions** (AC7): fall through to the existing exchange-fill /
  `sl` / time-stop logic byte-for-byte unchanged.

**Change C — `_close` cooldown stamping** (line 166):

```python
# Before: if rsn == "sl":
if rsn in ("sl", "crash_guard"):
    _last_sl[f"{v}:{a}:{s}"] = time.time()
```

A crash-guard exit counts as a stop-loss against the existing re-entry cooldown
(`cooldown_sec × SL_COOLDOWN_MULT`, ~10 min) — identical to an `sl` exit
(AC4, clarified Q2).

### Part 3 — settings, validation, dashboard, docs

**`trade/settings_schema.py`** (+2 `SettingSpec`s):

| Key | SettingSpec |
|---|---|
| `universe_max_atr_pct` | `float, group "universe", unit "%", hard 0.1–20, soft 0.5–5, short "Max replay median ATR% for a spot universe candidate — crash-prone names above the cap never enter the universe"` |
| `max_loss_per_position_pct` | `float, group "exit", unit "%", hard 0.1–20, soft 1–5, short "Crash-guard floor — market-sell a spot position when live price falls below entry × (1 − pct/100)", depends_on=("sl_min_pct_spot",)` |

`BY_KEY`/`GROUPS` derive automatically; `get_setting_float` fallbacks supply
1.5 / 3.0 (AC3/AC10, no migration).

**`trade/settings_rules.py`** (+2 cross-check blocks in `validate`):

1. **Hard error (mirrors `tp_min_pct <= sl_min_pct`, lines 85–90)** — when
   `max_loss_per_position_pct` is set, error if `value <
   get_setting_float("sl_min_pct_spot", get_setting_float("sl_min_pct", 0.5))`.
   Equality (`==`) is **allowed** (AC10, clarified Q4): guard and exchange stop
   trigger at the same level, so the guard stays a pure gap-catcher and never
   becomes the primary stop (Constitution III). Suggested value: the spot SL
   floor. Message states the guard would pre-empt the stop.
2. **Optional empty-universe warn (mirrors the depth-multiple warn, lines
   228–257)** — for `universe_max_atr_pct`: query the stored binance universe
   `MIN(atr_pct_median)`; warn "universe will be empty" when the cap is below it
   (a cap that low excludes every currently stored spot name — Constitution VIII
   guardrail).

**`dashboard/main.py`** (1 line, line 823):
`REASON_LABELS = {"tp": "TP", "sl": "SL", "time_stop": "Time stop", "orphan": "Orphan", "crash_guard": "Crash guard"}`

**Docs** — see Docs Update.

## Edge Cases

| Edge case | Handling |
|---|---|
| `atr_pct_median` missing / replay failed | Left to the existing `select_ranked` `m is None` / missing-ATR handling — never stored, cap never loosens anything. |
| Cap set below every stored name | `settings_rules` empty-universe **warn** (binance `MIN(atr_pct_median)` vs cap) — operator-visible guardrail (Constitution VIII). |
| Orderly/futures scan | Venue-branch: the cap block runs only for `venue == "binance"`; `dropped_by_max_atr` is not set for orderly. DEX universe untouched (spec Q3). |
| `live is None` (price API failure) | Guard skipped; position kept to retry (Constitution IV, AC6). Existing `sl` check already no-ops on `None`. |
| Below floor + TP order already FILLED | Guard records the real TP fill (`_real_fill`) with reason `tp`; no market sell (AC5/AC8/AC9). |
| Below floor + SL order already FILLED | Guard records the real reason `sl` at the stored `sl_price` (existing convention); no market sell (AC8). |
| Below floor + market sell returns `None` | No-balance/orphan recovery (real TP fill if filled, else `orphan`); on balance ≥ qty, keep position to retry — same as `sl`/`time_stop`. |
| `max_loss_per_position_pct` strictly inside `sl_min_pct_spot` | **Hard validation error** at startup/setting-change (Constitution II); equality allowed. Guard never becomes the primary stop. |
| Dry-run | `market_sell` returns a simulated `Fill(fill_price=0.0, fee_amount=0.0)` → `xp = ep` fallback, exactly like the existing `sl`/`time_stop` dry-run path; no new order path, no bypass (AC12). |
| Position with no `sl_price` (SL disabled) | Price is now fetched and the floor applies — the crash guard covers it (Change A). |
| Above floor | Guard block skipped; exchange-fill / `sl` / time-stop logic runs unchanged (AC7). |
| Crash-guard exit then immediate recovery | Same `(asset, side)` is blocked from re-entry by `_last_sl` for `cooldown_sec × SL_COOLDOWN_MULT` (~10 min); longer-horizon exclusion from `universe_max_atr_pct` at the next scan (clarified Q2). |
| Restart | Floor recomputed from `open_positions`; cooldown in-memory (resets) — no persistent state (Constitution VI). |

## Out of Scope

- Entry logic, TP/SL sizing, adaptive thresholds, and the OCO placement itself
  (`trading_bot/executor.py` untouched).
- The futures/DEX path (`futures_scalper.py`, Orderly brackets) — no futures-side
  ATR cap (spec Q3; DEX uses exchange-side bracket stops).
- DB schema change / migration; new process/thread/external service.
- The PUMP closed-trade **fee anomaly** (43% fee, −44% PnL) remains a separate
  execution/fill investigation (feature 005 Q8) — out of scope; this feature
  targets the gap/crash-slippage mechanism.
- Dashboard/UI exposure of the two new settings (operator manages settings
  locally).

## Testing Strategy (12 acceptance criteria)

`tests/test_spot_exit_hardening.py` mirrors `tests/test_amendment003.py`
(tmp-DB fixture via `db.db_ops.DB_PATH` monkeypatch; network isolation via
`mock.patch.object`). The scalper tests use a fake `BinanceSpot`-shaped exchange
(`get_price`, `get_order_status`, `cancel_order`, `market_sell`,
`get_asset_balance`, `get_order_fills`) and seed positions via `save_position`;
an autouse fixture clears `spot_scalper._last_sl`/`_last_entry` between tests.
Suggested tests:

| # | Test | Verifies AC |
|---|---|---|
| 1 | `test_settings_registered_with_defaults` — both keys in `BY_KEY` with type/group/ranges; `get_setting_float(k, default)` returns 1.5/3.0 when unset | AC3, AC10 |
| 2 | `test_universe_cap_rejects_high_atr_spot_only` — `scan_venue("binance")` with patched `_fetch_candidates`/`_fetch_depth`/`replay_symbol`/`replace_universe`; two candidates (atr 2.1 / 0.6), cap 1.5 → only low-ATR stored; `summary["dropped_by_max_atr"] == 1` | AC1, AC3 |
| 3 | `test_universe_cap_venue_branch_orderly_untouched` — same candidates via `scan_venue("orderly")` → both stored, no drop key | AC1 |
| 4 | `test_universe_cap_additive_only` — low-ATR candidate failing `min_volume` still rejected (pre-existing filter intact); cap never loosens | AC2 |
| 5 | `test_crash_guard_fires_below_floor` — entry 100, `max_loss_per_position_pct=3`, live 96 (floor 97), orders open → TP+SL cancelled, `market_sell` called, `closed_trades` row with `exit_reason='crash_guard'` and real fill price | AC4, AC9 |
| 6 | `test_crash_guard_fill_aware_sl_filled` — below floor, SL status `FILLED` → `closed_trades` reason `sl`, `market_sell` **not** called | AC5, AC8 |
| 7 | `test_crash_guard_fill_aware_tp_filled` — below floor, TP status `FILLED` → reason `tp` with real fill via `get_order_fills`, no market sell | AC5, AC8, AC9 |
| 8 | `test_crash_guard_none_price_no_action` — `get_price → None` → no cancel, no sell, position kept | AC6 |
| 9 | `test_crash_guard_stamps_cooldown` — after a `crash_guard` close, `spot_scalper._last_sl["binance:AAA:long"]` is set and `_cooldown_ok(..., cs)` is False within `cs × SL_COOLDOWN_MULT` | AC4 |
| 10 | `test_crash_guard_applies_to_no_sl_price_positions` — position with `sl_price=None`, live below floor → crash guard fires (Change A) | AC4 |
| 11 | `test_above_floor_normal_exits_unchanged` — above floor, live ≤ `sl_price` → existing `sl` path (reason `sl`); TP FILLED → reason `tp` | AC7 |
| 12 | `test_validation_hard_error_vs_equality` — `sl_min_pct_spot=0.6`: `validate("max_loss_per_position_pct", 0.5)` → error; `0.6` → ok; `3.0` → ok; hard-range `0.05`/`25` → error; `validate_all` passes with defaults | AC10 |
| 13 | `test_universe_cap_empty_universe_warn` — seeded binance universe `MIN(atr_pct_median)=0.5`; `validate("universe_max_atr_pct", 0.3)` → warn | AC10 |
| 14 | `test_dry_run_unchanged` — dry-run `market_sell` returns `Fill(fill_price=0.0)` → crash-guard close falls back to `xp = ep` (same convention as `sl`/`time_stop`); no new order path | AC12 |

Run: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q`
(plus the full `tests/` suite for regressions).

## Docs Update

- **`docs/CURRENT_STATE.md`** — new top-level section (feature 006, dated
  2026-08-12) describing: the spot-only `universe_max_atr_pct` cap (pinned to
  replay `atr_pct_median`, venue-branch, `dropped_by_max_atr` summary,
  calibration impact BICO vs PUMP/MMT); the `max_loss_per_position_pct` crash
  guard (floor formula, guard-first/fill-aware ordering, `None`-price no-action,
  `crash_guard` exit reason + `_last_sl` cooldown stamping, no-balance/orphan
  recovery); the two new settings table (defaults, ranges); the new
  `REASON_LABELS` entry.
- **`docs/CHANGELOG.md`** — `feat:` entry (dated 2026-08-12): "Spot exit
  hardening — universe max-ATR cap + crash-guard floor (006)" summarizing the
  three parts, the two settings, the validator cross-checks, and the test file.

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/006-spot-exit-hardening/` | ✅ spec, plan, research, data-model, contracts, quickstart (this phase) |
| `trade/universe.py` | `scan_venue`: post-replay spot-only max-ATR filter + `dropped_by_max_atr` summary |
| `trading_bot/spot_scalper.py` | `manage_open_positions`: always fetch live price, crash-guard first check (fill-aware); `_close`: stamp `_last_sl` for `crash_guard` |
| `trade/settings_schema.py` | +2 `SettingSpec`s (`universe_max_atr_pct`, `max_loss_per_position_pct`) |
| `trade/settings_rules.py` | +hard error (`max_loss` vs `sl_min_pct_spot`), +empty-universe warn (`universe_max_atr_pct`) |
| `dashboard/main.py` | `REASON_LABELS` + `"crash_guard": "Crash guard"` |
| `tests/test_spot_exit_hardening.py` | New — ~14 tests covering AC1–AC12 |
| `docs/CURRENT_STATE.md` | Updated |
| `docs/CHANGELOG.md` | Updated (`feat:`) |
| `db/*`, `trading_bot/executor.py`, `futures_scalper.py`, `bot.py`, `telegram.py` | Unchanged |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ATR cap empties/shrinks the universe (Constitution VIII) | Low | High | Pinned to the calibrated `atr_pct_median`; verified against live DB (removes only BICO at 1.5); validator warn when the cap would empty the stored spot universe; settings-rules guardrail |
| Cap placement deviates from spec's "before depth stage" | Medium | Low | Documented pinned decision: calibrated measure only exists at Stage 4; 24h-range proxy rejected (would empty the universe). Cap still guarantees "never stored" — the AC1 outcome |
| Crash guard market-sells a position the exchange already closed | Low | High | Fill-aware: TP/SL status verified first; no-balance/orphan recovery reused; never a phantom sell (AC8) |
| Guard fires spuriously on a stale `get_price` | Low | Medium | Floor is 3% ≫ effective SL (~0.6–0.8%); `None` price → no action; guard mirrors the existing `sl` price path |
| `max_loss_per_position_pct` becomes the primary stop (Constitution III) | Low | High | Hard validation error when strictly inside `sl_min_pct_spot`; equality allowed and benign; startup/setting-change gate re-runs |
| Line growth / hot-path budget (Constitution VII) | Low | Medium | ~40 lines across existing functions, no new module; guard reuses the `sl` branch structure verbatim |
| Dry-run / normal-exit regression | Low | Medium | Above-floor positions byte-for-byte unchanged (AC7); dry-run fill fallback identical to existing paths (AC12); full `tests/` suite re-run |
| Cooldown not applied to crash-guard exits | Low | Medium | `_close` stamps `_last_sl` for `rsn in ("sl", "crash_guard")`; dedicated test (AC4) |
