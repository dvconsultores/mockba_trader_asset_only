# Research: Spot Exit Hardening (gap/crash protection)

**Feature**: 006-spot-exit-hardening | **Date**: 2026-08-12

Code-verified facts from the live repo (`trade/universe.py`,
`trading_bot/spot_scalper.py`, `trading_bot/executor.py`, `trade/pnl.py`,
`db/db_ops.py`, `trade/settings_schema.py`, `trade/settings_rules.py`,
`dashboard/main.py`, `bot.py`, `tests/test_amendment003.py`,
`tests/test_market_check.py`) plus the live DB (`data/trading.db`). Every
signature and line number below was read from source, not assumed.

## 1. Part 1 — the universe scan pipeline (`trade/universe.py`)

### 1.1 Stage-1 candidates — what is available before the depth stage

`_fetch_candidates(venue)` (line 221) builds each candidate dict with exactly
`asset, symbol, quote_volume_24h, spread_pct, bid, ask, min_notional` (line 246
for the binance branch). The 24h quote volume comes from
`_fetch_binance_24hr()` (line 162), which currently extracts **only**
`quoteVolume` from the whole-exchange `/ticker/24hr` response:

```python
def _fetch_binance_24hr() -> dict[str, float]:
    """Whole-exchange 24h quote volume. One call."""
    r = requests.get(f"{BINANCE_API}/ticker/24hr", timeout=15)
    r.raise_for_status()
    return {s["symbol"]: float(s.get("quoteVolume") or 0) for s in r.json()}
```

The `/ticker/24hr` payload does carry `highPrice`/`lowPrice`/`lastPrice`, but
they are **discarded** — a 24h high–low range is *not* currently available in
the candidate dicts without a return-type change to `_fetch_binance_24hr()`.

### 1.2 Stage-2 hard filters — `_hard_filters_pass` (line 299)

```python
def _hard_filters_pass(c: dict, tp_min: float, rank_min: int, rank_max: int,
                       min_volume: float, spread_ratio_max: float,
                       slot_size: float | None) -> bool:
```

Checks (in order): `quote_volume_24h >= min_volume` (line 303); spread not None
and `spread_pct <= tp_min * spread_ratio_max` (line 305); rank inside
`[rank_min, rank_max]` (line 308); `min_notional × 1.5 <= slot_size` (lines
311–314). It takes **no `venue` argument** — the venue-branch happens in the
caller, `scan_venue` (precedent: `hold_key = "max_hold_minutes_spot" if
venue == "binance" else "max_hold_minutes_futures"`, line 651).

`scan_venue(venue, equity=None, depth_budget=None)` (line 564) orchestrates:
- Stage 2 reads `tp_min/rank_min/rank_max/min_volume/spread_ratio_max/slot_size`
  fresh (lines 606–611), ranks candidates by 24h volume (line 615), then
  computes `survivors` with a list comprehension over `_hard_filters_pass`
  (lines 620–624) and records `summary["survivors_after_filters"]`.
- Stage 3 (depth, token-bucket) runs on survivors only (lines 630–645).
- Stage 4 replay computes per-asset metrics (lines 656–671), including
  `atr_pct_median` inside `replay_symbol` (def at line 415).
- Stage 5 `select_ranked(checked, metrics, ...)` (call at line 677, def at
  line 687) filters (`m is None` / `signals_count < min_signals` /
  `recovery_rate < min_rec`), then stores rows including `"atr_pct_median"`
  (line 711) and sorts by `recovery_rate` desc, tiebreak `atr_pct_median`
  desc (line 716).
- `replace_universe(venue, rows)` (line 679) writes wholesale; the summary is
  updated at lines 680–684. `_scan_summary_message(res)` (line 782) renders
  the summary for the Telegram notification.

### 1.3 The calibrated ATR measure — `replay_symbol` (def line 415)

`replay_symbol(...)` (def at line 415) returns `{signals_count,
recovery_rate, median_minutes_to_tp, atr_pct_median, minutes_list}` with
`"atr_pct_median": median(atr_values) if atr_values else None` (line 479) —
the median per-5m-candle ATR as a percentage of price, over the replay window.
This is the value stored in `asset_universe.atr_pct_median` and used as the
ranking tiebreak. It is only known **after** Stage 4.

### 1.4 Live-DB calibration evidence (2026-08-12)

Stored `asset_universe` for `binance`, ordered by `atr_pct_median` desc:

| asset | atr_pct_median | recovery_rate | rank |
|---|---|---|---|
| BICO | 1.859 | 0.725 | 4 |
| MMT  | 0.873 | 0.638 | 7 |
| GIGGLE | 0.607 | 0.739 | 3 |
| PUMP | 0.598 | 0.830 | 1 |
| RE   | 0.587 | 0.757 | 2 |
| CRV  | 0.404 | 0.723 | 5 |
| ZAMA | 0.329 | 0.687 | 6 |

`orderly` (DEX): PUMP 0.590, CRV 0.461, WLD 0.365, XPL 0.284, PENGU 0.271,
INJ 0.246, ONDO 0.207, VIRTUAL 0.167, AAVE 0.161.

**Calibration check for `universe_max_atr_pct = 1.5` on `atr_pct_median`**:
BICO (1.86) is rejected; MMT (0.87), PUMP (0.60) and every other current member
remain. Exactly the spec's stated impact (BICO-class removed, PUMP/MMT kept,
Constitution VIII satisfied). A 1.5 cap on the **24h high–low range** would
instead reject every current member (a ~0.6% median 5m ATR still corresponds to
a multi-percent daily range), i.e. it would empty the universe.

### 1.5 Pinned decision — ATR source and filter placement

**Decision**: the cap is pinned to the Stage-4 replay **`atr_pct_median`** —
the only measure that reproduces the spec's calibration (1.1 above) and is
already computed/stored with no new API call.

**Placement consequence** (deviation from spec Layout §1, documented):
because `atr_pct_median` is only known after the replay, the cap **cannot** be
applied inside `_hard_filters_pass` (Stage 2) "before the depth stage". It is
applied as a hard pass/fail filter in `scan_venue` **immediately after the
Stage-4 replay loop and before `select_ranked`**, under a `venue == "binance"`
branch (the `hold_key` venue-branch precedent, line 651). The filter is
strictly additive (it only removes names; it never loosens the Stage-2 volume /
spread / rank / fundability checks), and rejected names never reach
`replace_universe` (line 679), so they are never stored. The scan summary
(line 680) gains a `dropped_by_max_atr` count for observability.

The alternative (extending `_fetch_binance_24hr` to also return high/low and
filtering in Stage 2) was evaluated and rejected: an uncalibrated 24h-range
cap would empty the universe (Constitution VIII) — see 1.4.

## 2. Part 2 — `manage_open_positions` in `trading_bot/spot_scalper.py`

`manage_open_positions(asset, exchange)` (line 75). Current structure (per
position `pd`):

1. **Live price** (line 80): fetched only when some position has an `sl_price`:
   ```python
   live = exchange.get_price(asset) if any(pd.get("sl_price") for pd in positions) else None
   ```
   A position stored with `sl_price = None` (`_save_open` line 243 writes
   `"sl_price": slp if sl > 0 else None`) therefore never has a price fetched —
   the crash floor must fix this.
2. **Exchange-fill checks first** (lines 91–95): `tp_order_id` FILLED → close as
   `"tp"` with `_real_fill`; `sl_order_id` FILLED → close as `"sl"` at the stored
   `sl_price`. These guarantee a position already closed by the exchange is
   never market-sold (no phantom double-close).
3. **Price-based SL check** (line 94–123): `slp > 0 and live is not None and
   live <= slp` → cancel TP (+SL), `market_sell`, and `_close(..., "sl", ...)`.
   Includes the **no-balance / orphan recovery** pattern (lines 105–121): if
   `market_sell` returns `None`, check the asset balance; if below `qty`,
   recover the real TP fill if filled, else close as `"orphan"`; otherwise log
   and keep the position for the next cycle.
4. **Time stop** (lines 125–153): `(now - opened_at) > max_hold_minutes_spot`
   → same cancel + `market_sell` + `_close(..., "time_stop", ...)` flow with the
   same recovery pattern.

`_close(a,v,s,ep,xp,sp,q,pid,si,rsn,fee_ep=0.0,fee_xp=0.0)` (line 164):
- **Cooldown stamping** (line 166): `if rsn == "sl": _last_sl[f"{v}:{a}:{s}"]
  = time.time()`. Only `"sl"` stamps today.
- Fee fallbacks: `fee_ep = ep*q*0.001` if ≤ 0, `fee_xp = xp*q*0.001` if ≤ 0.
- `record_closed_trade(...)` (line 169, `trade/pnl.py` line 44 — PnL from
  actual fills, Constitution V) + `delete_position(a,v,pid)`.

Re-entry cooldown: `_last_sl` (line 59) + `SL_COOLDOWN_MULT = 10` (line 58);
`_cooldown_ok` (line 61) blocks the same `(asset, side)` for
`cooldown_sec × SL_COOLDOWN_MULT` (~10 min at default `cooldown_sec=60`).
`_last_sl` is keyed `"binance:{asset}:{side}"`.

### 2.1 Exchange interface the guard reuses (`trading_bot/executor.py`)

| Method | Line | Contract |
|---|---|---|
| `get_price(asset)` | 164 | `float \| None` (ticker; None on failure — Constitution IV no-action) |
| `get_asset_balance(asset)` | 153 | `float \| None` (None = account query failed) |
| `market_sell(asset, qty)` | 300 | `Fill \| None`; **dry-run returns `Fill(fill_price=0.0, fee_amount=0.0, ...)`** (line 304) |
| `get_order_status(symbol, order_id)` | 346 | `"FILLED"` / other / `"UNKNOWN"` |
| `get_order_fills(symbol, order_id)` | 353 | `(avg_price, total_commission) \| None` — real fill (Constitution V) |
| `cancel_order(symbol, order_id)` | 376 | `bool` |

`Fill` has `fill_price`, `fee_amount`, `sellable_qty`. In the existing SL/time-stop
paths the exit price is `sell.fill_price if sell.fill_price > 0 else (slp|ep)`
— dry-run fills (price 0.0) fall back to the stored/entry price, preserving
dry-run behavior (breakeven-ish close, no real order).

## 3. Part 3 — settings schema & validator

- `SettingSpec` (frozen dataclass, `trade/settings_schema.py` line 10): `key,
  type, group, unit, hard_min, hard_max, soft_min, soft_max, short,
  depends_on=()`. Groups already present: `"exit"` (e.g. `max_hold_minutes_spot`,
  line ~80) and `"universe"` (e.g. `universe_rank_min`, line ~190). `BY_KEY`
  and `GROUPS` derive automatically (lines 228–230).
- `trade/settings_rules.py` `validate(key, value, ctx=None)` (line 52):
  unknown-key error → type coercion → hard range error → soft range warn →
  cross-setting checks. Existing patterns to mirror:
  - Hard error for `tp_min_pct <= sl_min_pct` (lines 85–90) — the "startup
    gate, not a warning" precedent (Constitution II).
  - Spot SL override check `sl_min_pct_spot` must stay below `tp_min_pct`
    (lines 105–109).
  - Empty-universe warn for depth requirement (lines 228–257): reads the
    stored `asset_universe` median depth and warns "universe will be empty".
- `db.db_ops.get_setting_float(key, default)` (line 102): returns `default`
  when unset/invalid — new defaults live in the fallbacks, **no DB migration**
  (established Amendment 003 / 005 pattern). Live DB currently has
  `sl_min_pct_spot = 0.6`, `sl_k_spot = 0.8`.

## 4. Dashboard label & call sites

- `dashboard/main.py` line 823:
  `REASON_LABELS = {"tp": "TP", "sl": "SL", "time_stop": "Time stop", "orphan": "Orphan"}`
  used at line 935 to render the Closed Trades reason. A `"crash_guard"` entry
  is needed so the new reason renders as "Crash guard" instead of `CRASH_GUARD`.
- `bot.py` line 406: `spot_manage(asset, binance)` runs in the per-asset loop
  **before** any entry logic ("Manage exits FIRST") — the guard lives entirely
  inside `manage_open_positions`; no bot.py change needed.

## 5. Test conventions (established patterns)

- `tests/test_amendment003.py`: `db(tmp_path)` fixture monkeypatches
  `db.db_ops.DB_PATH` + `initialize_database_tables()`; network isolation via
  `mock.patch.object` on `trade.universe` module functions; direct calls to
  `_hard_filters_pass`, `select_ranked`, `replay_symbol`; `scan_venue` tested
  end-to-end with patched `_fetch_candidates`/`_fetch_depth`/`replace_universe`
  (e.g. `test_budget_exhaustion_aborts_scan`).
- `tests/test_market_check.py`: same fixture plus an autouse fixture clearing
  module-level caches between tests; `record_closed_trade` used with synthetic
  prices. No existing test targets `spot_scalper.manage_open_positions` — the
  new file establishes the pattern (fake `BinanceSpot`-shaped exchange object,
  position seeded via `save_position`, assertions on `closed_trades` rows,
  `delete_position`, module `_last_sl`).
- Run command: `./venv/bin/python -m pytest tests/test_spot_exit_hardening.py --basetemp=.pytest_tmp -q`.
