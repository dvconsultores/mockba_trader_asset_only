# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).

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
