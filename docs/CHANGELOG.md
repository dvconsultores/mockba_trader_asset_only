# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).

## 2026-08-12

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
