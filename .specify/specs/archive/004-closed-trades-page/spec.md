# Feature Specification: 004 — Closed Trades Page (Mini App)

**Feature Branch**: `004-closed-trades-page`

**Created**: 2026-08-05

**Status**: Draft — awaiting implementation authorization

**Flow**: constitution → specify → clarify → plan → checklist → tasks → analyze → **implement (AUTHORIZATION REQUIRED)** → converge

---

## What

A new read-only page in the Telegram Mini App (React SPA in `dashboard-ui/`) showing closed trades for the **active month**, reachable from the **More options** menu. Backed by one new read endpoint in the dashboard API.

## Why

The bot's stated goal is many small continuous trades. Without a history view the only way to see what actually closed and why is to read the database directly. Two venue totals plus a reason column answer the two questions that matter day to day: *am I up this month, and what is closing my trades?*

---

## Resolved decisions (T01 + clarify)

| # | Question | Decision |
|---|---|---|
| Q1 | "Active month" definition | **Calendar month to date, by close time** (`closed_at`). A trade opened on the 31st and closed on the 1st belongs to the month of its close. |
| Q2 | Timezone for the month boundary | **Caracas UTC-4**, computed **server-side**. Matches the Mini App's display convention (`timezone.ts` hardcodes UTC-4). The client never defines the window. |
| Q3 | `reason` values | Real values are exactly `tp`, `sl`, `time_stop` (verified in `trading_bot/spot_scalper.py` and `trading_bot/futures_scalper.py`). Display mapping: `tp → TP`, `sl → SL`, `time_stop → Time stop`. Unknown future values render uppercased as-is. |
| Q4 | Is `pnl_net` fully net? | `pnl_net = gross − fee_entry − fee_exit` where fees are **estimated** at fixed per-side rates (spot `0.001`, futures `0.0003`); **funding is NOT included** (nothing in the write path records funding). **Accepted and documented on the page**: "Net of estimated fees; funding not included." |
| Q5 | Signal-mode trades | **Never reach `closed_trades` by construction** — in `Signal` mode the scalpers log the signal and return before `place_entry`, so no position and no close record is created. No filtering needed. |
| Q6 | Card contents | **Total P&L + trade count** per venue. |
| Q7 | Volume / pagination | **Server cap of the 200 most-recent rows**, no pagination UI. Totals always cover the full month (capped list ≠ capped totals). `truncated` flag when more rows exist. Current real volume: 1 row (fresh rebuild). |
| Q8 | Sort order | **Most recent close first, fixed** (no user re-sort). |
| Q9 | States | Loading, error, empty-month, and empty-for-venue are visually distinct from each other and from a zero total. |
| Q10 | Precision | **Up to 4 decimals** (matches `/api/stats/daily`'s `round(pnl, 4)`). Frontend formatter: `minimumFractionDigits: 2`, `maximumFractionDigits: 4` so a real small P&L never shows `0.00`. API returns raw floats (no rounding). |

---

## Layout

1. **Two summary cards at the top** — one per venue (DEX / CEX), side by side on mobile.
   - Each card: venue label, month-to-date `pnl_net` total, trade count.
   - **Cards always show the full month per venue and do not change with the filter.**
   - A small header note states the disclosure: *Net of estimated fees; funding not included.* (Q4 / constitution V transparency).
2. **Filter** — three-state control: All / DEX / CEX. Narrow the list only.
3. **Trade list** — one row per closed trade, most recent first:

   | Column | Source |
   |---|---|
   | Asset | `closed_trades.asset` |
   | Venue | mapped `orderly → DEX`, `binance → CEX` |
   | Side | `long` / `short` (rendered BUY / SELL) |
   | PnL Net | `closed_trades.pnl_net` (raw float; formatted client-side) |
   | Reason | mapped label (TP / SL / Time stop) |
   | Closed at | `closed_at`, displayed in Caracas UTC-4 |

   Mobile-first: no horizontal scrolling; each row is a two-line card.

**Data window:** active calendar month only (Caracas UTC-4). No date picker, no history beyond the current month.

**Refresh:** the page polls `GET /api/trades/closed` every 30 seconds (low-frequency data; keeps cards and list live without load).

---

## Scope

- **Mini App**: new `ClosedTrades` page, new entry in the More options menu (single addition to the `moreTabs` array in `App.tsx`).
- **Backend**: one read endpoint `GET /api/trades/closed?venue=all|dex|cex` in `dashboard/main.py` (single-file pattern — the dashboard container ships only `main.py`).
- **Database**: none — `closed_trades` already exists and is retained in the rebuild.
- **Tests**: required (`tests/test_closed_trades_page.py`).
- **Docs**: page + endpoint documented in `docs/CURRENT_STATE.md`.

---

## Constraints

- **Read-only.** No control on this page may reach a write path or the executor. Test asserts the endpoint performs no writes.
- **Active month only**, computed server-side in Caracas UTC-4, in one place.
- **Both venues supported identically** — totals and filtering driven by one canonical `(label, db_venue)` mapping; no DEX- or CEX-specific branch.
- **Totals reconcile exactly with the rows shown.** Aggregates computed server-side over the same window as the rows; with filter `all`, the visible rows sum to DEX total + CEX total (asserted in tests).
- **Mobile viewport is the primary target.**
- **No new dependency.** No new UI framework, charting, or table library. P&L sign colour-coded and readable in both Telegram themes.
- **Minimum modification.** One page, one endpoint, one menu entry. No refactor of navigation or existing views.

---

## Acceptance criteria

1. T01 reported: real column values, `reason` set, `pnl_net` semantics, signal-mode presence.
2. Page reachable from More options; read-only.
3. Cards show DEX and CEX month-to-date totals plus counts; unaffected by the filter.
4. With filter All and a month of ≤200 rows, visible rows sum exactly to the two card totals. (Beyond the cap the list is truncated — `truncated: true` — while the totals remain full-month; guaranteed by the cap test.)
5. Filter narrows the list and leaves the cards unchanged.
6. Month boundary behaves per the documented rule (Caracas UTC-4, by close time), verified at the edge.
7. Empty, loading, and error states distinguishable.
8. Usable on a phone inside Telegram.
9. All T08 tests pass, including the aggregation test that fails on a wrong sum.
