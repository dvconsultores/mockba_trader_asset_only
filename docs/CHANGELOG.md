# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).

## 2026-08-09

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
