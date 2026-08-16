# MockbaV4 — Current State Analysis

> Generated: 2026-07-26 | Phase 1, Section 1.1
> Updated: 2026-08-16 | Features 009–012 & Constitution v1.1.0

---

## 0. Frequency Recovery (feature 012, 2026-08-16 — settings only)

`max_concurrent_positions` 2 → **4**, `cex_slot_pct` 40 → **20**,
`capital_cex_usdt` 50 → **100** (operator capital plan, clarify Q1: $100
funded, 4 × $20 slots = $80 deployed ceiling, $20 standing loss buffer —
covers >8 consecutive max-loss rounds at the 3% crash floor). 183 slot-bound
skips in the prior 7 days showed the 2-slot cap choking exactly the burst days
that earn (08-06: 49 trades, +$3.90). No quality gate was touched — frequency is
recovered through capacity (Constitution VIII; the bot is meant to be HF).
`compute_slot_size` caches per UTC day, so the new slot size applies from the
next restart/midnight.

**Finding (audit #12)**: `BinanceSpot.get_equity` returns USDT (free+locked)
only — coin holdings are invisible, so with positions open every
percent-of-equity number (slot sizing, daily-loss limits) reads ~$10 on a ~$50
account. Direction-safe but wrong in magnitude; slated for the
kill-switch-integrity spec alongside the other `get_equity` defect.

---

## 0. Spot Exit Parity (feature 011, 2026-08-16)

Brings the **live** venue to the recording standard feature 010 set for futures
(Constitution V). Exit *decisions* are untouched — only recorded values change:

- **Exchange-SL exits** (main branch + crash-guard pre-check) record the real
  fill price and commission via `_real_fill`, not the theoretical trigger price
  with `fee_exit=0`. Slippage past the stop is now captured, so
  `closed_trades.pnl_net` — the input to the daily-loss and consecutive-loss
  kill switches — reflects true losses.
- **`opened_at` is real** on every closed spot trade (was hardcoded 0; hold-time
  analytics were impossible and the 009 study had to rebuild entry times from
  `signals`).
- **Fee fallback from settings**: `cex_round_trip_fee_pct / 2` per leg replaces
  the hardcoded `0.001` — numerically identical at the current 0.20% setting.

No migration, no schema change, no new setting.

---

## 0. Futures Exit Integrity (feature 010, 2026-08-15)

Three of the five paths in `futures_scalper.manage_open_positions` violated the
constitution. DEX was off (`auto_trade_orderly=False`), so none of it was live —
which is exactly why it was fixed before anything arms it.

| Path | Was | Now |
|---|---|---|
| **Time stop** | Cancelled TP **and SL**, deleted the DB row, **never sent a closing order** — a live leveraged position with no stop and no local record. Booked the exit at the entry price. | Cancel brackets → reduce-only `market_close` → **verify** via `get_open_positions` → record the real fill and delete. |
| **Regime exit** | Wrote the breakeven TP to the DB only; the exchange order was never amended, then the fabricated price was booked as the exit. | Cancel + re-place on the exchange; `update_position` runs **only** on success. |
| **TP / SL exits** | Recorded the *intended* price at a flat `0.0003` fee. | Real `average_executed_price` / `total_fee`; fallbacks logged as estimates. |
| `opened_at` | Hardcoded `0` — hold duration lost. | The position's real `opened_at`. |
| `cancel_order` | Fired a junk `POST /v1/order` with `side: "CANCEL"` before the real `DELETE`. | One `DELETE`. |

### The exit ladder (Constitution III has no exception)

```
time stop → cancel TP+SL → market_close(reduce_only)
   ├ verified closed         → record real fill, delete row
   ├ failed / still open     → KEEP row, re-place stop, ERROR
   └ stop re-placement fails → Telegram alert + auto_trade_orderly=false
```

No branch ends a cycle with an unprotected position, and no branch deletes a row
whose closure was not verified. Disabling on double failure blocks **entries
only** — exits keep running, so the stranded position is still managed.

### Verified Orderly contract

The order fields were confirmed against `ccxt/woofipro.py` in the repo venv,
which targets **`https://api-evm.orderly.org`** — the same host `OrderlyFutures`
uses: `reduce_only: true` (bool) in the order body, `order_type: "MARKET"` with
`order_quantity`, and `average_executed_price` / `total_fee` on a filled order
(the two fields `place_entry` already read). **ccxt is documentation only — it is
not imported by the bot.**

New `OrderlyFutures` methods: `market_close`, `place_tp`, `place_stop`,
`get_order_fills`. All honour `dry_run`.

### Before arming DEX — manual checklist

1. `dry_run=true`, `auto_trade_orderly=Automatic`, seed a futures position with a
   past `opened_at`.
2. Confirm the log shows `market_close` → verification → a `closed_trades` row
   with a non-entry exit price and a real `opened_at`.
3. Force an adverse regime; confirm cancel + re-place appear and `tp_price` moves
   only after success.
4. Only then consider `dry_run=false`.

**Still open on the futures side** (deliberately out of scope): `_save_open` does
not store `fee_entry`, so entry fees are settings estimates; `place_entry` leaves
a dangling TP when its emergency close fires.

---

## 0. Entry Confirmation Candle (feature 009, 2026-08-15)

The dip rule measures **displacement only** — `_is_dip` compares price to the
rolling peak and never checks whether price is still falling — so the bot
routinely entered mid-fall. Feature 009 adds a confirmation test: **the last
CLOSED 5m return must be positive** (`close > open`; a flat bar does not
confirm). Stated as a sign test on the last completed return, not a candlestick
pattern — no multi-bar shapes, no pattern taxonomy (Constitution I).

Futures is symmetric: a `long` is confirmed by an up bar, a `short` by a down
bar. **The short arm is evidence-free** — the study covers spot longs only and
DEX is currently off.

### Two modes

| `entry_confirm_candle` | Behaviour |
|---|---|
| `false` (**default**) | **Observe-only.** The verdict is recorded on every `signals` row via the new `entry_confirmed` column, but nothing is blocked. Zero behaviour change. |
| `true` | **Enforce.** An unconfirmed entry is skipped and recorded with reason `entry_not_confirmed`; no order reaches the exchange. |

`entry_confirmed`: `1` confirmed, `0` not confirmed, `NULL` indeterminate or
never evaluated (market-gate and global-loss skips, pre-migration rows).
Indeterminate never counts as confirmed — in enforce mode it fails closed
(Constitution IV).

### Cost

`trade/regime.py::last_closed_return_up` reads the **same 5m cache** that
`get_atr_pct` fills, delegating to it on a miss or stale entry so there is only
ever one fetch/error path. With `adaptive_enabled=true` (the live setting)
`get_atr_pct` has already run for that asset in the same cycle, so the helper is
a dict hit — **zero additional requests**. With `adaptive_enabled=false` the
delegation costs at most one 5m fetch per asset per `candle_cache_sec` (60s).
The cached bar may be up to `candle_cache_sec` stale; observe-mode measures live
behaviour including that staleness.

### Evidence

113 real `action='entered'` signals (binance, 2026-08-05 → 08-15), each replayed
over its actual 5m path from its true entry timestamp at TP 1.2 / SL 2.0 /
120-min hold, 0.2% round-trip fee:

| Entry timing | n | net/trade | TP hit | stopped |
|---|---|---|---|---|
| No filter | 113 | +0.017% | 59% | **20%** |
| Last 5m return **up** | 41 (36%) | **+0.387%** | 73% | **12%** |
| Last 5m return **down** | 72 (64%) | −0.194% | 51% | 25% |

64% of entries fired while price was still falling, and those were the losing
half. "Up" beat "down" in all seven TP/SL configurations tested, so the effect
is not an artefact of the current pair. Caveat: n=41 in the confirmed arm, one
venue, one 10-day window, one regime — observe-mode exists to upgrade that.

### Constitution VIII trade-off

Enforcement keeps 36% of entries (~11/day → ~4/day). That is why the default is
observe-only. Throughput is **not** slot-limited: 42-min average hold implies
~34 trades/day/slot against 6.5 actual (19% of capacity), with zero
`max_slots_cex` / `max_concurrent_positions` skips in the three days before the
feature. Daily return on slot capital rises +0.19%/day → +1.59%/day on the study
sample. Frequency recovery (`max_concurrent_positions`, `max_active_pairs`,
`universe_size`) is deliberately a follow-up spec.

Decide enforcement from the observe data:

```sql
SELECT entry_confirmed, COUNT(*) n FROM signals
WHERE action IN ('entered','signaled') GROUP BY entry_confirmed;
```

### Constitution v1.1.0 (2026-08-15)

Principle II changed from **"Reward Must Exceed Risk"** (`tp_pct > sl_pct`) to
**"Reward Must Exceed Cost"** — `tp_effective > round_trip_fee(venue) +
slippage + min_net_edge`, enforced per entry, with the stop sized to asset
volatility rather than to the target. The payoff ratio may be below 1 when the
hit rate is asymmetric. Rationale, per-principle impact and the evidence caveat
are in `.specify/memory/constitution.md` → Amendment History.

---

## 0.1 Market Gate: Liquidity-Only Suspension (feature 008, 2026-08-12)

The automatic market gate (feature 005) now suspends on **liquidity only** by
default. Regime-based WARNs (`regime_trending`, `regime_unknown`) remain
visible as informational PASS→WARN downgrades in the `[GATE]` log but no
longer count toward suspension and no longer fire the ⚠️ Telegram warning —
the broad-market filter (BTC/ETH/SOL/BNB) and the per-asset `regime` filters
already own macro-trend protection, and the regime WARN over-blocked on small
universes (08-11: 3,709 gate skips).

- `trade/market_check.py` `_warn_is_strong` — regime reasons escalate only
  when `market_gate_regime_escalates` is `true`; `liquidity_partial ≥
  market_gate_warn_liquidity_share` and `liquidity_fail_share` (FAIL) still
  escalate (aggregate liquidity collapse is the gate's unique value).
- `bot.py` `_gate_apply` — the ⚠️/✅ WARN notification lifecycle fires only for
  strong/escalating WARNs (reuses `_warn_is_strong` on the report reasons).
- New setting `market_gate_regime_escalates` (bool, group `gate`, default
  `false`) lets the operator re-enable regime suspension from the DB.

---

## 0. Dashboard Settings Read-Only (feature 007, 2026-08-12)

The dashboard Settings page is **read-only**: it still displays every setting
(via the unchanged `GET /api/miniapp`) but can no longer edit them. The
operator manages settings locally (AI assistant + `push-db.sh`), so the
editing path was locked to prevent config drift (e.g. `cex_slot_pct` had
drifted to 90 via UI/Telegram edits).

- `dashboard-ui/src/MiniSettings.tsx` — settings always read-only (`editable` is
  permanently `false`; the Telegram/browser edit-enablement was removed).
- `dashboard/main.py` `POST /api/miniapp` — any key not in
  `CAPITAL_SETTING_KEYS` (`capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`,
  `dex_slot_pct`) returns 403 "settings are read-only — manage via local DB push"
  with no DB write. The `__ping__` auth probe and the capital-key write path are
  kept (the Capital page still manages declared capital/slots).
- `telegram.py` setting commands remain the operator's auth-gated remote
  control (including toggling `auto_trade_*` on/off).

---

## 0. Spot Exit Hardening — gap/crash protection (feature 006, 2026-08-12)

Two setting-driven protections hard-cap the worst-case loss of a spot position
when price gaps or crashes through the stop (motivated by a real −44.35% spot
slippage loss on PUMP, 2026-08-07). Normal TP/SL/time-stop behavior is
untouched; these additions only keep crash-prone names out of the universe and
add an emergency exit floor.

### Volatility cap — `universe_max_atr_pct` (spot only)

- New setting `universe_max_atr_pct` (float, default **1.5**, hard 0.1–20,
  soft 0.5–5, group `universe`; defaults in `get_setting_float` fallbacks — no
  migration).
- Applied in `trade/universe.py` `scan_venue` **after the Stage-4 replay and
  before `select_ranked`**, under a `venue == "binance"` branch (the Orderly/
  futures universe is untouched — DEX uses exchange-side bracket stops).
- The ATR measure is the replay **`atr_pct_median`** (the Stage-1 24hr ticker
  exposes only `quoteVolume`, so no usable high–low range exists pre-replay).
- **Strictly additive**: only genuinely high-ATR names are dropped (a candidate
  with a missing/None ATR is left to the existing `select_ranked` exclusion);
  the Stage-2 volume/spread/rank/fundability filters are untouched.
- Calibration (live DB 2026-08-12): with cap 1.5 only BICO-class names
  (atr_pct_median ≈ 1.86) are removed; MMT (0.87), PUMP (0.60), GIGGLE, RE, CRV,
  ZAMA remain (Constitution VIII).
- Observability: `scan_venue` summary gains `dropped_by_max_atr` (binance only),
  surfaced in the `_scan_summary_message` Telegram notification.

### Catastrophic-move guard — `max_loss_per_position_pct`

- New setting `max_loss_per_position_pct` (float, default **3.0**, hard 0.1–20,
  soft 0.5–5, group `exit`, `depends_on=("sl_min_pct_spot",)`).
- In `trading_bot/spot_scalper.py` `manage_open_positions`, the crash guard is
  the **first, fill-aware** per-position check: when
  `live < entry × (1 − max_loss_per_position_pct/100)` the bot verifies the
  TP/SL fill status first — an already-filled order records its real fill with
  its real reason (`tp`/`sl`) and is never market-sold — otherwise it cancels
  the open TP/SL orders and `market_sell`s, closing via `_close` with
  `exit_reason='crash_guard'` and the real fill price/fee (Constitution V).
- The live price is now fetched for every managed asset (the floor applies to
  all positions, including `sl_price=None` ones). `live is None` → no action,
  position kept (Constitution IV).
- Re-entry cooldown: a `crash_guard` exit stamps `_last_sl` exactly like an
  `sl` exit, blocking the `(asset, side)` for `cooldown_sec × SL_COOLDOWN_MULT`
  (~10 min); longer-horizon exclusion comes from `universe_max_atr_pct` on the
  next scan.
- Validation: `max_loss_per_position_pct` strictly inside `sl_min_pct_spot`
  (`<`) is a **hard error** (the guard would pre-empt and cancel the spot stop,
  Constitution III); equality (`==`) is allowed. An `universe_max_atr_pct`
  below the lowest stored binance ATR warns "universe will be empty".
- Dashboard: `REASON_LABELS["crash_guard"] = "Crash guard"` (`dashboard/main.py`).

---

## 0. Market Conditions Check & Auto-Gate (feature 005, 2026-08-09)

One shared, structured per-venue market-health check powers two consumers:
the automatic gate in `bot.py` (observed mode) and the manual `/market`
Telegram report (live mode).

### `trade/market_check.py` — the shared check

- **One verdict contract, two modes** (`check_venue_live` / `check_venue_observed`):
  both return the identical structured dict — `venue, mode, timestamp,
  scan_fresh, scan_age_hours, verdict (PASS|WARN|FAIL), reasons, regime_mix,
  assets{passes_liquidity, volume_ok, depth_ok, spread_ok,
  live_spread_degraded, regime}, thresholds`. Only `mode` and the freshness of
  the per-asset facts differ (AC2).
- **Freshness first (AC3)**: `_ensure_fresh_scan` triggers
  `run_scans_if_due(venues=(venue,))` before evaluating if the stored scan is
  stale/due; if still stale after refresh (e.g. kill-switch paused) the verdict
  is **FAIL `scan_stale`** — the check never judges on stale data (Constitution IV).
- **Non-divergence (hard rule, AC1)**: every threshold/depth/regime/sizing call
  goes through the live functions — `trade.universe.compute_thresholds`,
  `trade.universe._fetch_binance_book_ticker`/`_24hr`/`_exchange_info`,
  `trade.universe._fetch_depth`, `trade.universe._TokenBucket`,
  `trade.universe.is_universe_stale`/`run_scans_if_due`,
  `trade.regime.detect_regime`, `trade.pnl.compute_slot_size`, `db.db_ops`
  settings. Nothing is reimplemented. `compute_thresholds` runs as a
  **non-gating diagnostic** `thresholds` field (from the stored median ATR).
- **Verdict rules** (data-model.md §2, first hit wins): stale ⇒ FAIL; venue
  share of assets failing liquidity ≥ `market_gate_fail_share` ⇒ FAIL
  `liquidity_fail_share=…`; any failing asset ⇒ WARN `liquidity_partial=…`;
  TREND_UP+TREND_DOWN share ≥ `market_gate_trend_share` or UNKNOWN share ≥
  `market_gate_unknown_share` downgrade PASS→WARN; else PASS. UNKNOWN never
  counts toward a good verdict.
- **Observed mode = zero API load (AC9)**: consumes only the per-cycle rolling
  observations bot.py records at its two existing call sites (after
  `detect_regime` and after `_get_obi_and_spread`); no `_fetch_*` calls.
- **Live mode**: whole-exchange bookTicker + 24hr + exchangeInfo, then
  per-survivor depth through a token bucket (`capacity=max(1, len(universe))`,
  refill 60/s — the scanner's own class/rate). Whole-exchange failure ⇒ FAIL
  `data_unavailable` (never partial).
- **`format_report(report)`**: compact per-venue text, no emoji, verdict
  tokens rendered verbatim; static labels localized by `telegram.py` via
  `translate()`.

### The automatic gate (`bot.py`, opt-in)

- **Opt-in**: `market_gate_enabled` defaults to `false` — when off the gate
  block is skipped entirely (a single startup `[GATE] disabled …` INFO log),
  with zero behavior change: no entry blocking, no notifications, no state
  writes (AC5).
- **Cadence**: evaluated in the existing main loop every
  `market_gate_interval_min` (default 5), mirroring the periodic mode-log
  block — no thread/process. Settings are read fresh each cycle, so
  Telegram/UI changes take effect without restart.
- **Cold start**: on bot restart / venue-activation the gate skips its first
  evaluation until at least one round of per-cycle observations exists
  (otherwise every asset counts as UNKNOWN and the verdict is a misleading
  `regime_unknown=1.00` WARN). Entries are still protected meanwhile by the
  per-asset guards.
- **Debounce state machine (per venue)**: pure `update_gate_state(state,
  verdict, settings)` — suspend new entries only after `market_gate_bad_streak`
  (default 2) consecutive FAIL; resume only after `market_gate_good_streak`
  (default 2) consecutive PASS; **WARN = neutral hold** (resets both streaks,
  never suspends/resumes → no flapping). State is in-memory only
  (`_gate_state`, `_last_gate_eval`, `_gate_observations`), per-venue
  independent; on restart it starts unsuspended and re-establishes within
  `bad_streak × interval`.
- **Entries only (Constitution III, AC7)**: the gate guard sits after exit
  management (`spot_manage`/`futures_manage`) and after observation recording,
  just before the scalper entry call. Open positions always run to
  TP/SL/time-stop/regime exit. Observations keep flowing during a suspension,
  so resume is never deadlocked.
- **Notifications (AC8)**: exactly ONE debounced `send_message` on suspend
  (`[GATE] DEX (orderly) suspended — poor market conditions`) and ONE on
  resume, via `trading_bot.send_bot_message.send_message` (the same mechanism
  `_notify_entry` uses). Structured single-line INFO logs, no emoji:
  `[GATE] venue=… verdict=… reason=… action=suspend|resume|hold`.
- The gate is an **additional venue-level layer** — it does NOT replace the
  stale-universe guard, per-asset regime gating, spread-degradation guard, or
  kill switches.

### Manual Telegram report (`telegram.py`)

- `/market` command + a **Market check** button in `/list`
  (`callback_data="market"`, routed through `_dispatch_callback`) — the
  operator's escape hatch / override view.
- Runs `check_venue_live("binance")` and `check_venue_live("orderly")`
  (live-snapshot mode; `_ensure_fresh_scan` first), renders each via
  `format_report` with `translate()` applied to static labels (verdict tokens
  verbatim), concatenates and chunks at `TELEGRAM_MAX_MESSAGE_LEN = 4096`.
- Immediate feedback: sends an "Analyzing market conditions…" message first
  and replaces it with the report when the live-snapshot API calls finish,
  so the button never appears to hang.
- Same private-chat + `TELEGRAM_CHAT_ID` authorization as `/list`
  (unauthorized → `🔍 Not authorized`).

### Settings (`market_gate_*`, new `"gate"` group in `trade/settings_schema.py`)

| Key | Type | Default | Hard range | Soft range |
|---|---|---|---|---|
| `market_gate_enabled` | bool | false | — | — |
| `market_gate_interval_min` | int | 5 | 1–1440 | 2–60 |
| `market_gate_bad_streak` | int | 2 | 1–100 | 1–20 |
| `market_gate_good_streak` | int | 2 | 1–100 | 1–20 |
| `market_gate_fail_share` | float | 0.5 | 0.0–1.0 | 0.25–0.75 |
| `market_gate_trend_share` | float | 0.6 | 0.0–1.0 | 0.3–0.8 |
| `market_gate_unknown_share` | float | 0.5 | 0.0–1.0 | 0.2–0.7 |

Defaults are deliberately lenient (Constitution VIII): a healthy RANGE venue
evaluates PASS. Hard ranges enforce `interval/streak >= 1` in the schema — no
`trade/settings_rules.py` change was needed (Amendment 002 validator passes
unchanged; AC12). Defaults live in the `get_setting_*` fallbacks — no DB
migration.

---

## 1. Module Inventory

### Core Trading Path (order placement capability)

| File | Lines | Purpose |
|---|---|---|
| `trade/main.py` | 2,171 | **Orchestrator.** `ReversalScalper` class (pattern detection, ML gate, LLM gate, regime detection, manipulation detection, CEX smart-entry gates). `process_signal()` for manual triggers. `autotrade()` loop: DEX→grid scalper, CEX→grid scalper (RANGE) or reversal scalper (non-RANGE). **Can place orders.** |
| `trading_bot/spot_grid_scalper.py` | 330 | **Spot mean-reversion grid.** Dip detection + OBI → limit buy → limit sell TP. Tracks positions in module-level `_open_positions: list[dict]`. NO position persistence (restart = lost). **Can place orders** via `_limit_buy_with_fallback` + `_place_tp_sell`. |
| `trading_bot/futures_grid_scalper.py` | 326 | **DEX futures mean-reversion grid.** Dip/pump detection + OBI → bracket order (entry + TP + SL). **Has P0 bugs:** `_grid_setting` returns float but used for string `"long"` → `ValueError` at import; `qty` computed but never sent in payload; `place_futures_order()` return value discarded; `get_user_statistics()` return type mismatch (returns dict, compared to int). Positions tracked via `_open_position_count: int` (just count, no side/size). **Can place orders.** |
| `trading_bot/spot_executor_binance.py` | 566 | **Binance spot order execution.** `place_spot_order()` — market buy + GTC limit sell. `_limit_buy_with_fallback()` — tries LIMIT, falls back to MARKET after timeout. Fee-adjusted sell quantity. Exchange info fetching. |
| `trading_bot/futures_executor_apolo.py` | 611 | **Orderly DEX futures execution.** `place_futures_order()` — bracket: MARKET entry + POSITIONAL_TP_SL. Rate limiter (8 req/s). WebSocket live price. `get_user_statistics()` returns **dict** with `"positions"` list, not an int. `get_available_balance()` returns float. |
| `trading_bot/send_bot_message.py` | 66 | Telegram message sender. MarkdownV2 escaping, chunking, retry. |

### Data and DB

| File | Lines | Purpose |
|---|---|---|
| `db/db_ops.py` | 453 | **SQLite operations.** CRUD for `settings`, `signal_history`, `trades_daily`, `dex_asset_wallets`. Migrations via `_ensure_*_schema()` functions. `save_signal_to_history()`, `get_signal_history()`. **No grid position persistence functions exist.** `save_grid_position`/`load_grid_positions` referenced in ARCHITECTURE.md do NOT exist. |
| `db/migrations/` | 8 files, ~417 total | Incremental schema migrations (001–008). Add columns to existing tables. |
| `logs/log_config.py` | 52 | Custom `DateRotatingFileHandler`. Rotates at 5MB, 5 backups. Dual output: file (DEBUG) + stdout (INFO). |

### Exchange Data & Utilities

| File | Lines | Purpose |
|---|---|---|
| `trade/binance_data.py` | 162 | Binance orderbook + price fetching. `get_orderbook_binance()`, `get_binance_price()`, symbol mapping. |
| `trade/historical_data.py` | 233 | Orderly DEX OHLCV + orderbook + market trades. Rate-limited (10 req/s). |
| `trade/get_binance_trades.py` | 237 | Fetches Binance trade history via API. |
| `trade/get_trades.py` | 93 | Fetches Orderly DEX trade history. Exports to `data/all_trades.json`. |
| `trade/trading_executor.py` | 1,012 | **Standalone. Not imported by any module.** Chain/wallet management for cross-chain deposits/withdrawals on Binance + Bitget. **Does NOT place trading orders.** Dead code for the bot's purposes. |
| `trade/add_wallet_chain_des.py` | 38 | Wallet/chain mapping utility (standalone). |
| `trade/seed_chains.py` | 87 | Chain cache seeding (standalone). |
| `trade/test_data.py` | 179 | Test data utilities (standalone). |

### ML & Analysis

| File | Lines | Purpose |
|---|---|---|
| `trade/signal_agent/__init__.py` | 27 | Package init. |
| `trade/signal_agent/features.py` | 121 | Feature extraction (11 features) from signal_history for XGBoost training + live inference. |
| `trade/signal_agent/model.py` | 149 | XGBoost wrapper: train, load, save, predict, decide. Threshold-based binary classification. |
| `trade/signal_agent/labeler.py` | 667 | Background process: matches trades to signals by timestamp proximity, labels `win`/`loss`/`breakeven`. |
| `trade/signal_agent/train.py` | 242 | CLI trainer: loads labeled signals, trains XGBoost, saves model to `data/signal_model.json`. |
| `trade/signal_analyzer.py` | 421 | Post-hoc signal history analysis. Approval rates, rejection reasons, pattern distribution. CLI tool. |
| `trade/performance-llm.py` | 983 | **LLM-based trade analysis.** Regex-extracts parameters from `main.py` source code, builds prompt for DeepSeek/OpenAI, generates JSON recommendations. Offline advisory only. Has bugs: average-win calculation divides sum of all PnLs by win count (should filter positives); hardcoded timezone offset of 4; hardcoded `-$10` large-loss threshold. |

### Orchestration & UI

| File | Lines | Purpose |
|---|---|---|
| `forever.py` | 64 | **Process supervisor.** Launches `telegram.py` as subprocess. Restarts on exit. Monitors ONE process — not designed for two. |
| `telegram.py` | 584 | **Telegram bot + entry point.** Starts `autotrade()` in daemon thread. Commands: /start, /list, /trades, settings via inline keyboards. Imports `process_signal` and `autotrade` from `trade.main`. |
| `dashboard/main.py` | 716 | **FastAPI backend.** SSE log stream, signal history API, ML stats API, Mini App settings CRUD. Has its own inline DB helpers (duplicating `db_ops.py`). |
| `dashboard-ui/src/` | ~6 TSX files | React frontend. Tabs: Live, Signals, ML Monitor, Status, Settings, Assets. |

### Tests

| File | Lines | Purpose |
|---|---|---|
| `tests/test_spot_grid_scalper.py` | 1 (placeholder) | Not implemented. |
| `trade/tests/__init__.py` | 0 | Empty. |

---

## 2. The Trading Path

### Entry point
```
forever.py → telegram.py → autotrade() [daemon thread]
                          → bot.polling() [main thread, Telegram listener]
```

### DEX (Orderly) trading path
```
autotrade() in trade/main.py
  → ReversalScalper.quick_scan()          # get regime, OBI, live_price
  → if regime == "RANGE":
      → futures_grid_scalp_cycle()        # in trading_bot/futures_grid_scalper.py
        → _is_price_dip() OR _is_price_pump()  # rolling 40-sample deque
        → place_futures_order(payload)    # in trading_bot/futures_executor_apolo.py
          → Orderly POST /v1/order        # bracket: MARKET + TP + SL
        → return value DISCARDED          # BUG: no fill verification
```

### CEX (Binance) trading path
```
autotrade() in trade/main.py
  → ReversalScalper.quick_scan()          # get regime, OBI, live_price
  → if regime == "RANGE":
      → grid_scalp_cycle()                # in trading_bot/spot_grid_scalper.py
        → _is_price_dip()                 # rolling peak from 40-sample deque
        → _limit_buy_with_fallback()      # in trading_bot/spot_executor_binance.py
          → POST /api/v3/order (LIMIT, then MARKET fallback)
        → _place_tp_sell()                # GTC limit sell at entry * (1 + tp_pct)
  → if regime != "RANGE":
      → scalper.analyze_signal()          # full ReversalScalper analysis
        → ML gate (XGBoost score >= 0.80)
        → LLM gate (DeepSeek second opinion, 8s timeout)
        → CEX smart-entry gates ×6
        → place_spot_order(payload)
```

### Where position state lives
| Venue | State location | Persisted? | Restart-safe? |
|---|---|---|---|
| DEX futures | `_open_position_count: int` (module global in futures_grid_scalper.py) | No | No — counts as 0 on restart |
| CEX spot | `_open_positions: list[dict]` (module global in spot_grid_scalper.py) | No | No — empty list on restart |
| DEX actual | Orderly API `GET /v1/positions` | Exchange | Yes — but bot doesn't query it on startup |
| CEX actual | Binance open orders | Exchange | Yes — but bot doesn't reconcile on startup |

---

## 3. Database Reality

### Tables (4 actual)

| Table | Rows | Read by | Write by | Assessment |
|---|---|---|---|---|
| `settings` | 20 | All modules via `get_setting()` | `upsert_setting()`, dashboard, Telegram | **Keep.** Core config. |
| `signal_history` | 39,047 | Dashboard (API), signal_agent (train/label), signal_analyzer, trade/main.py (CEX smart-entry gates) | `save_signal_to_history()` in trade/main.py | **Remove from hot path.** 39K rows, mostly rejected signals (33,854 rejected vs 5,193 approved). Dashboard uses it for UI. Keep for `research/` analysis only. |
| `trades_daily` | 9 | `get_trades_today()` in db_ops.py, `increment_trades_today()` in spot_executor | `increment_trades_today()` | **Remove.** Only counts "positive trades" per day — no PnL, no detail. Replaced by `closed_trades`. |
| `dex_asset_wallets` | 0 | `get_dex_asset_wallet()`, `get_latest_dex_asset_wallet()` | `upsert_dex_asset_wallet()` | **Remove.** Empty table. Used by standalone `trading_executor.py` (dead code). |

### Tables ARCHITECTURE.md claimed exist but don't

ARCHITECTURE.md listed these as existing tables to drop: `arbitrage_compounding`, `arbitrage_cycle_steps`, `arbitrage_inventory`, `arbitrage_capital_allocation`, `arbitrage_observations`, `arbitrage_rotation_decisions`, `ai_recommendations`, `performance_metrics`, `strategy_parameters`, `execution_errors`, `market_regimes`.

**None of these tables exist in the current database.** They appear in `DROP TABLE IF EXISTS` statements in `db/db_ops.py:initialize_database_tables()` but have already been dropped. ARCHITECTURE.md was wrong — the DB is already clean of these. Only 4 tables actually exist.

### Settings inventory (20 keys)

```
asset                    = PERP_NEAR_USDC
auto_trade_cex           = False
auto_trade_dex           = False
capital_usage            = 50
cex_capital              = 45
current_asset            = PERP_NEAR_USDC
exchange                 = dex
grid_cooldown_sec        = 300
grid_max_positions       = 1
grid_obi_buy             = 0.96
grid_obi_sell            = 1.18
grid_position_capital    = 15
grid_price_dip_pct       = 0.2
grid_tp_pct              = 0.3
interval                 = 5m
leverage                 = 3
ml_threshold             = 0.80
risk_level               = 2.5
stop_loss                = 1
take_profit              = 0.5
```

Missing from this list vs what the rebuilder will need:
- `grid_sl_pct` — exists as code constant but NOT as DB setting (futures_grid_scalper.py reads `_grid_setting("grid_sl_pct", "0.8")` but setting was never inserted)
- `grid_direction` — exists as code constant but NOT as DB setting (same issue)
- `grid_pump_pct` — doesn't exist anywhere
- No `dex_slot_pct` / `cex_slot_pct` (new design uses equity-based sizing)
- No `dry_run` setting
- No `daily_loss_limit` setting
- No `max_consecutive_losses` setting
- No `max_hold_minutes` setting
- No `max_leverage` setting
- No `round_trip_fee_pct` setting
- No `assumed_slippage_pct` setting
- No `min_net_edge_pct` setting

---

## 4. What Trading Data Actually Exists

### Orderly DEX (`data/all_trades.json`)
- **25 trades** from 2026-06-18 to 2026-06-20 (3 days)
- 12 BUY, 13 SELL
- **Has real fill prices, quantities, and fees** — suitable for slippage calibration
- Fields: `executed_price`, `executed_quantity`, `fee`, `fee_asset`, `realized_pnl`, `side`, `symbol`

### Binance (`data/binance_trades.json`)
- **989 entries** — raw Binance trade history (likely includes ALL account trades, not just bot)
- Structure needs inspection to determine bot vs manual trades

### Accumulated (`data/accumulated_trades.json`)
- **1,450 entries** — combined trade history
- Structure needs inspection

### signal_history (SQLite)
- **39,047 rows** from 2026-04-12 to 2026-07-21 (~100 days)
- **1,370 rows have trade outcomes** (1,132 win, 234 loss, 4 breakeven)
- **2,900 rows have ML scores**
- These are SIGNALS, not trades. Outcomes were labeled post-hoc by `labeler.py` matching signals to trades by timestamp proximity.
- For calibration: the outcome-labeled rows can provide win rate per regime, but fill prices are mostly NULL.

### Calibration feasibility
- **Slippage:** `all_trades.json` has real fill prices. 25 trades over 3 days is small but better than nothing. Can measure entry slippage vs signal price by cross-referencing with signal_history timestamps.
- **Fees:** `all_trades.json` has real fees. Measurable directly: `fee / (executed_price * executed_quantity)`.
- **Win rate:** 1,370 labeled signals give statistical significance. Win rate = 1,132/1,370 = 82.6% — but this is likely biased (only signals that became trades got labeled).
- **Regime distribution:** Need historical OHLCV data (not in DB). Must fetch from exchange APIs.

---

## 5. ARCHITECTURE.md Errors and Corrections

| # | ARCHITECTURE.md claim | Reality | Impact |
|---|---|---|---|
| 1 | "15+ tables" in DB | 4 tables exist. The 11 "legacy" tables were already dropped. | Overstated cleanup scope. Migration is simpler. |
| 2 | `spot_grid_scalper.py` uses `save_grid_position`/`load_grid_positions` from `db_ops.py` | These functions **do not exist**. Positions are tracked in module-level `_open_positions: list[dict]`. | DB ops for positions must be built from scratch. |
| 3 | `_grid_setting` returns `float` — P0.1 in hardening doc | Confirmed. `GRID_DIRECTION = _grid_setting("grid_direction", "long")` → float("long") raises. | ARCHITECTURE.md is correct here. This is a real bug. |
| 4 | "No real PnL tracking" | Partially wrong. `all_trades.json` has `realized_pnl` per trade from Orderly. `signal_history` has `realized_pnl` column (populated by labeler). But the bot itself doesn't compute or use PnL in decision-making. | The data exists; the bot just doesn't use it. |
| 5 | Omitted `trading_executor.py` (1,012 lines) | Standalone module for chain/wallet management. Not imported by any trading module. Dead code. | Not part of trading path. Can be safely removed or moved to `research/`. |
| 6 | Omitted `trade/test_data.py` (179 lines) | Standalone test utility. | Low impact. |
| 7 | Omitted `trade/get_binance_trades.py` (237 lines) and `trade/get_trades.py` (93 lines) | Trade history fetchers. Not in hot path. | Data collection tools. Can stay or move to `research/`. |
| 8 | Omitted `trade/performance-llm.py` (983 lines) | LLM analysis tool. Has known bugs (average-win calculation, hardcoded timezone). | Phase 2.8 addresses this. |
| 9 | "2,171-line monster" for `trade/main.py` | Line count is correct (2,171). Assessment is accurate. | No correction needed. |
| 10 | Regime blocks all trading outside RANGE | Correct about current design. The rebuild prompt supersedes this — regime gates direction, not trading. | This is an intentional design change, not an error. |
| 11 | `pnl.close_position()` "exists but nothing calls it" | `pnl.py` **doesn't exist yet**. It's proposed in the new architecture. ARCHITECTURE.md described it as if it exists but is unused — incorrect framing. | The exit management gap is real; the fix is new code, not wiring up existing code. |
| 12 | Missing `manage_open_positions()` | This is the largest gap. Neither scalper has exit management beyond the TP order placement. Spot has `_check_open_positions()` which only detects TP fills. Futures has NO exit detection at all — fire and forget. | Confirmed. Must be built from scratch. |

---

## 6. Summary Statistics

| Metric | Value |
|---|---|
| Total Python lines (all files) | ~10,600 |
| Python lines in trading path | ~4,000 (main.py + scalpers + executors) |
| Lines that can place orders | ~5,200 (main.py + scalpers + executors + telegram.py) |
| DB tables | 4 |
| DB rows (signal_history) | 39,047 |
| Labeled trade outcomes | 1,370 |
| Actual DEX trades (all_trades.json) | 25 over 3 days |
| Trading days recorded (trades_daily) | 9 days over ~1 month |
| Settings in DB | 20 |
| Settings needed for rebuild | ~19 (see spec) |
| Modules with import-time bugs | 1 (`futures_grid_scalper.py` — `float("long")`) |
| Modules with no position persistence | 2 (both scalpers) |
| Modules with no exit management | 2 (both scalpers — spot has partial, futures has none) |

---

# Amendment 003 — Current State (Dynamic Universe & Capital View)

> Added: 2026-08-04. This section reflects the CURRENT codebase; sections above
> describe the historical Phase-1 code and are preserved for reference.

## New modules & tables

| Item | Purpose |
|---|---|
| `trade/universe.py` | Daily universe scanner (5 stages) + shared `compute_thresholds` used by both live scalpers and the replay. |
| `db/migrations/006_amendment_003.sql` | Creates `asset_universe` + `venue_state`, seeds universe/capital settings + baselines. |
| `asset_universe` table | One row per venue per asset; replaced wholesale per scan; `blacklisted` carried forward. |
| `venue_state` table | Live equity cache written by `bot.py` each cycle; read by the Capital view. |

## Where the trading universe comes from

`bot.py` iterates `get_tradeable_universe(venue)` (non-blacklisted `asset_universe`
rows) instead of the legacy `asset_configs` pairs. Exits run for universe members
AND dropped-out assets (churn never forces an exit). A stale scan
(`universe_max_age_hours`) blocks new entries but not exit management. A live
spread exceeding the scan-time spread by `universe_spread_degradation_multiple`
skips entries for that asset (no extra API call — spread comes from the OBI
snapshot).

The scanner runs in a dedicated background thread in `bot.py` — never inside the
trading cycle. It runs on startup if the stored scan is stale, then every
`universe_scan_interval_hours`. Rate-limit exhaustion aborts the scan and
preserves the previous universe (no partial write).

## Capital model

- Slot size = `{venue}_slot_pct` × **live exchange equity**, floored at
  `min_notional × 1.5`, recomputed daily (`trade/pnl.compute_slot_size`).
- Declared pools (`capital_cex_usdt` / `capital_dex_usdc`) are for display and
  validation only — sizing never reads them. Divergence beyond 25% surfaces a
  warning; the exchange wins.
- Per-venue slot limits: `max_slots_cex` / `max_slots_dex`.
- Fees are per-venue (`dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`) and
  drive all net-edge calculations, including the universe replay's minimum
  recovery rate (`universe_min_recovery_rate='auto'` → breakeven `(sl+fee)/(tp+sl)`).

## Interfaces

- **Telegram:** `/capital`, `/universe [cex|dex]`, `/blacklist add|remove <ASSET>`.
  Per-asset add/toggle/remove handlers removed; the manual signal asset picker
  now sources from the universe.
- **Dashboard API:** `GET /api/capital`, `GET /api/universe/{venue}`,
  `PUT /api/universe/{venue}/{asset}/blacklist`, and `GET /api/trades/closed`
  (read-only month view). The `/api/assets*` per-asset endpoints were replaced.
- **Mini App:** Assets tab → Capital view (`CapitalManager.tsx`) with per-venue
  panels and read-only Universe panels (blacklist toggles only). Plus the Closed
  Trades page (`ClosedTrades.tsx`) under More options.

### Closed Trades page (`GET /api/trades/closed?venue=all|dex|cex`)

Read-only month view of `closed_trades` (Amendment 004).

- **Window:** current calendar month **by close time** (`closed_at`), boundary in
  **Caracas UTC-4** (fixed −4h, matches `dashboard-ui/src/timezone.ts`), computed
  server-side in one place. The client never defines the window.
- **Totals:** per-venue `pnl_net` total + trade count for the full month, computed
  server-side in the same query pass as the rows; both cards are always shown
  (zero-filled when a venue has no trades) and are **unaffected by the venue filter**.
- **Trades:** most recent close first, capped at 200 (`truncated` flag; totals are
  uncapped). Venue filter (`all|dex|cex`) narrows the list only.
- **Reason mapping:** `tp → TP`, `sl → SL`, `time_stop → Time stop`; unknown values
  render uppercased.
- **`pnl_net` semantics (Q4):** `gross − fee_entry − fee_exit`, where fees are
  **estimated** at fixed per-side rates (spot `0.001`, futures `0.0003`); **funding
  is not included**. The page discloses this ("Net of estimated fees · funding not
  included"). Values are returned raw (no rounding); the UI formats with up to 4
  decimals.
- The legacy `asset_configs` table and the never-used `settings_proposals`
  table, the never-written `signals.position_id` column, and the unread
  `assets` setting were **dropped** by migration
  `db/migrations/008_cleanup_unused.sql` (2026-08-09). `sqlite_sequence`
  remains (internal AUTOINCREMENT counter).

## Status

- Backend, scanner, guards, Telegram, dashboard API, Mini App Capital view, and
  unit tests implemented (`tests/test_amendment003.py`, 17 tests).
- **Not yet done:** dry-run validation under the new universe (48h), the
  predicted-vs-realized recovery-rate gap, rank-band decile evidence, and
  measured CEX fee rate — see `docs/CALIBRATION.md`.
