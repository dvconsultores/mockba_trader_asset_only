# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).

## 2026-08-16

- `ops:` Frequency recovery (feature 012, settings only — no code).
    `max_concurrent_positions` 2 → 4, `cex_slot_pct` 40 → 20,
    `capital_cex_usdt` 50 → 100 (operator capital plan: $100 funded, 4 × $20
    slots = $80 deployed, $20 standing loss buffer). Doubles concurrent shots
    (183 slot-bound skips in the prior 7 days) while spreading tail risk. Quality gates untouched — capacity, not looser filters (Constitution
    VIII; operator directive: the bot is meant to be high-frequency).
    **Finding recorded**: `BinanceSpot.get_equity` counts only USDT, not coin
    holdings, so equity-based sizing/limits shrink as positions open — audit
    item #12, slated for the kill-switch-integrity spec.

- `fix:` Spot exit parity (feature 011) — Constitution V on the live venue.
    Exchange-SL exits (main branch and the crash-guard pre-check) now record the
    **real** fill price and commission via `_real_fill` instead of the
    theoretical trigger price with `fee_exit=0`, so slippage past the stop is
    captured and the daily-loss / consecutive-loss kill switches see true
    losses. Every closed spot trade now carries the position's real `opened_at`
    (was hardcoded 0 — 80+ rows lost their hold duration). `_close`'s fee
    fallback reads `cex_round_trip_fee_pct / 2` instead of a hardcoded 0.001
    (identical numerics at the current 0.20% setting — zero behaviour change
    today). Exit decisions are untouched; only recorded values change.

## 2026-08-15

- `fix:` Futures exit integrity (feature 010) — Constitution III/V repair. The
    time stop cancelled both brackets and deleted the DB row **without ever
    closing the position**, leaving a live leveraged position with no stop and no
    local record; it now cancels, sends a reduce-only `market_close`, verifies
    via `get_open_positions`, and only then records the real fill. A failed or
    unverified close keeps the position and re-places its stop; if that also
    fails, a Telegram alert fires and `auto_trade_orderly` is disabled (entries
    only — exits keep running). The regime exit now cancels and re-places the
    breakeven TP on the exchange instead of writing to the DB alone. TP/SL exits
    record `average_executed_price`/`total_fee` instead of intended prices at a
    flat 0.0003, `opened_at` is real, and `cancel_order` no longer fires a junk
    `POST /v1/order` with `side: "CANCEL"` before its `DELETE`. New
    `OrderlyFutures` methods: `market_close`, `place_tp`, `place_stop`,
    `get_order_fills` — all honour `dry_run`. DEX was off throughout, so nothing
    was live; a manual `dry_run` checklist is in CURRENT_STATE.

- `feat:` Entry confirmation candle (feature 009). Entries now evaluate whether
    the last CLOSED 5m return is positive — a sign test on the completed bar, not
    a candlestick pattern — because the dip rule measures displacement only and
    fired while price was still falling. New setting `entry_confirm_candle`
    (default **false** = observe-only: the verdict is recorded in the new
    `signals.entry_confirmed` column but never blocks); when true an unconfirmed
    entry is skipped with reason `entry_not_confirmed`. Indeterminate fails
    closed. Futures is symmetric (long = up bar, short = down bar; the short arm
    is evidence-free). Zero additional API calls in the live configuration — the
    helper shares the ATR 5m cache. Migration 009 adds the column. Measured over
    113 real entries: confirmed +0.387%/trade vs unconfirmed −0.194%/trade,
    stop-out rate 20% → 12%.

- `docs:` Constitution **v1.1.0** — Principle II amended from "Reward Must
    Exceed Risk" (`tp_pct > sl_pct`) to "Reward Must Exceed Cost"
    (`tp_effective > round_trip_fee + slippage + min_net_edge`, enforced per
    entry; the stop is sized to asset volatility, and the payoff ratio may be
    below 1 when the hit rate is asymmetric). Ratifies commit `662837d`, which
    shipped the rule ahead of the amendment. Rationale, per-principle impact
    assessment and the evidence caveat are in the constitution's Amendment
    History.

## 2026-08-12

- `fix:` Market gate is now liquidity-only by default (feature 008). Regime
    WARNs (`regime_trending`/`regime_unknown`) are informational — they no longer
    count toward suspension nor fire the ⚠️ Telegram warning; the broad-market
    filter and per-asset regime filters already own macro trends (08-11's 3,709
    gate skips were regime-WARN over-blocking). New setting
    `market_gate_regime_escalates` (default false) re-enables regime suspension;
    liquidity FAIL / strong `liquidity_partial` still suspend. `_gate_apply`
    notifies ⚠️/✅ only for escalating WARNs.

- `ux:` Dashboard settings are now read-only (feature 007). The Settings page
    displays values but no longer edits them (operator manages settings via local
    DB + `push-db.sh`); `POST /api/miniapp` rejects any non-capital settings key
    with 403 "settings are read-only — manage via local DB push". The declared
    capital / slot keys (`capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`,
    `dex_slot_pct`) stay editable so the Capital page still works; `__ping__`
    probe, `GET /api/miniapp` and `telegram.py` (operator remote control) unchanged.
    New `tests/test_dashboard_settings_readonly.py`.

- `feat:` Spot exit hardening — universe max-ATR cap + crash-guard floor (feature 006).
  - New `universe_max_atr_pct` (default 1.5) — spot-only, strictly-additive cap in
    `trade/universe.py` `scan_venue` (post-replay, pre-rank, pinned to `atr_pct_median`);
    removes only BICO-class names (≈1.86) from the stored binance universe; the scan
    summary gains `dropped_by_max_atr`, surfaced in the scan notification.
  - New `max_loss_per_position_pct` (default 3.0) — catastrophic-move guard in
    `trading_bot/spot_scalper.py` `manage_open_positions` (guard-first, fill-aware): below
    `entry × (1 − pct/100)` it cancels TP/SL and market-sells, closing with
    `exit_reason='crash_guard'` and the real fill; `None` price → no action; stamps the SL
    re-entry cooldown.
  - Validator: hard error when `max_loss_per_position_pct < sl_min_pct_spot` (equality
    allowed); empty-universe warn for the ATR cap. Cross-checks now run before soft-range
    warnings so hard errors are never masked.
  - Dashboard `REASON_LABELS["crash_guard"] = "Crash guard"`.
  - New `tests/test_spot_exit_hardening.py` (15 tests, AC1–AC12).

## 2026-08-09

- `chore:` DB + code cleanup (unused schema and dead code removed).
  - New migration `db/migrations/008_cleanup_unused.sql` (idempotent): drops the
    legacy `asset_configs` table (unused since Amendment 003 — no code reads it),
    drops `settings_proposals` (never written/read, 0 rows), drops the
    `signals.position_id` column (never written by any INSERT path), and removes
    the unread `assets` setting. `db/schema_v2.sql` updated to match.
  - Removed dead code: `save_signal` in `db/db_ops.py` (wrote a non-existent
    `signals.timestamp` column — broken), its import in `trading_bot/spot_scalper.py`,
    unused imports in `bot.py` (`os`, `math`, `invalidate_cache`,
    `is_entry_blocked`, `can_trade_venue`, `compute_slot_size`,
    `max_effective_slots`) and `telegram.py` (`re`, `threading`,
    `importlib.util`, `io`, `redirect_stdout`, `html`, `json`, `timedelta`,
    `upsert_setting`), and the unreferenced standalone files
    `trade/discover_chains.py`, `trade/asset_chains_discovered.py`,
    `test_dex_signal.py`.
  - `fix:` dashboard `/api/stats/daily` queried the non-existent
    `signals.timestamp` column → now uses `signals.ts`.
- `feat:` `push-db.sh` — mirror of `fetch-db.sh` to upload the local DB to the
  server (same `.env` config, sshpass fallback, sidecar upload, 3s abort warning
  before overwriting a live server DB).
- `fix:` market gate cold-start — the gate skipped its first evaluation only
  when a venue is `"False"`; on startup/venue-activation it evaluated with empty
  observations and logged a misleading `regime_unknown=1.00` WARN. Now it waits
  until at least one round of observations exists (entries are still protected
  by the per-asset guards meanwhile).
- `ux:` `/market` (and the Market check button) now sends an immediate
  "Analyzing market conditions…" message and replaces it with the report,
  instead of appearing to do nothing during the live-snapshot API calls.
- `feat:` Market conditions check & auto-gate (feature 005).
  - New shared core `trade/market_check.py`: one per-venue market-health verdict
    (PASS/WARN/FAIL + reasons + per-asset liquidity + regime mix + scan
    freshness) in two modes — **live** snapshot (manual `/market` report,
    token-bucket bounded, fresh whole-exchange calls) and **observed**
    (automatic gate, consumes per-cycle rolling observations with zero extra
    API load). Non-divergence hard rule: reuses `trade.universe`
    (`compute_thresholds`, `_fetch_binance_book_ticker`/`_24hr`/`_exchange_info`,
    `_fetch_depth`, `_TokenBucket`, `is_universe_stale`, `run_scans_if_due`),
    `trade.regime.detect_regime`, `trade.pnl.compute_slot_size` and `db.db_ops`
    settings — nothing reimplemented. Freshness-first: a stale/due stored scan
    is refreshed before judging and still-stale ⇒ FAIL (Constitution IV).
  - Automatic gate in `bot.py` (opt-in via `market_gate_enabled`, default
    false — zero behavior change when off): periodic per-venue evaluation in
    the main loop (no thread), per-venue debounce state machine
    (suspend after `market_gate_bad_streak` FAIL, resume after
    `market_gate_good_streak` PASS, WARN = neutral hold), blocks NEW entries
    only (exits always run — Constitution III), exactly one debounced Telegram
    notification per transition via `trading_bot.send_bot_message`, structured
    `[GATE] venue=… verdict=… reason=… action=…` INFO logs (no emoji).
    In-memory state only; observations recorded at the two existing bot.py
    call sites.
  - Manual Telegram report: `/market` command + **Market check** button in
    `/list` (`callback_data="market"` through `_dispatch_callback`),
    live-snapshot mode, `translate()`-localized static labels with verdict
    tokens verbatim, chunked at `TELEGRAM_MAX_MESSAGE_LEN = 4096`, same
    `TELEGRAM_CHAT_ID` auth as `/list`.
  - Seven new settings (new `"gate"` group in `trade/settings_schema.py`):
    `market_gate_enabled`, `market_gate_interval_min`, `market_gate_bad_streak`,
    `market_gate_good_streak`, `market_gate_fail_share`, `market_gate_trend_share`,
    `market_gate_unknown_share` — hard ranges enforce `interval/streak >= 1`,
    so `trade/settings_rules.py` is unchanged (Amendment 002 validator passes).
  - Tests: `tests/test_market_check.py` (15 tests) covering AC1–AC12
    (non-divergence patch test, same-contract parity, freshness fail-closed,
    verdict matrix, disabled default, debounce transitions, entries-only,
    exactly-one transition notifications, zero observed-mode API load, compact
    report, not-near-zero-trade, settings validation).

## 2026-08-05

- `feat:` Closed Trades page in the Mini App (Amendment 004).
  - New read-only page under **More options** showing the current calendar month's
    closed trades: per-venue (DEX/CEX) month-to-date P&L cards + trade count, a
    three-state venue filter (All/DEX/CEX), and a trade list (asset, venue, side,
    PnL net, reason, close time).
  - New endpoint `GET /api/trades/closed?venue=all|dex|cex` in `dashboard/main.py`
    (single-file pattern; no DB change; no new dependency).
  - Month window: calendar month **by close time**, boundary in **Caracas UTC-4**,
    computed server-side in one place.
  - Totals computed server-side in the same pass as the rows; cards stay month-wide
    while the filter narrows the list; list capped at 200 with `truncated` flag.
  - `pnl_net` disclosure on the page: net of estimated fees, funding not included.
  - Tests: `tests/test_closed_trades_page.py` (13 tests), including the aggregation
    test that fails on a wrong sum.
