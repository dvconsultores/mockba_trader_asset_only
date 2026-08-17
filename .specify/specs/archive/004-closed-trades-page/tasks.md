# Tasks — 004 Closed Trades Page

Order: T01 → T02 → T03/T04/T05/T06 (frontend) → T07 → T08 (tests) → T09 (docs) → T10 (review).

All decisions resolved (Q1–Q10) — see `spec.md` "Resolved decisions". **Implement only after explicit authorization.**

---

## T01 — Confirm the data contract ✅ DONE (2026-08-05)

Reported against the live DB (`data/trading.db`) and the write paths:

- **Populated columns**: all `closed_trades` columns populated in the single existing row; `signal_id` is NULL; `opened_at` is 0 in every close record (`_close` writes `opened_at=0` in both scalpers).
- **`reason` set**: `tp`, `sl`, `time_stop` — only these three are ever written.
- **`pnl_net` semantics**: `gross − fee_entry − fee_exit`; fees are **estimated** (per-side `0.001` spot / `0.0003` futures); **funding not included**.
- **Signal-mode**: `Signal` mode returns before `place_entry` → no position → no `closed_trades` row. Excluded by construction.
- **Venue values**: `binance` / `orderly` (DB) → CEX / DEX (UI). **Side values**: `long` / `short` only.
- **Volume**: 1 row today (fresh rebuild). Cap at 200.

*Acceptance*: Q3, Q4, Q5 answerable from real data — met.

---

## T02 — Read endpoint

**Files**: `dashboard/main.py`

Add `GET /api/trades/closed?venue=all|dex|cex` (inline SQL, `_get_db()`, single-file pattern):

- Server-side Caracas UTC-4 month window from `closed_at` (Q1/Q2); month label from the Caracas clock.
- Per-venue totals (`GROUP BY venue`) + rows (`ORDER BY closed_at DESC LIMIT 200`) over the same window; `truncated` flag.
- Venue filter (`all|dex|cex`, map via `(dex, orderly)/(cex, binance)`); 400 on unknown.
- `reason` → label mapping; raw `pnl_net` floats.
- Read-only (SELECT only).

*Acceptance*: aggregates and rows cannot disagree by construction; window computed in one place; client cannot shift it.

---

## T03 — Summary cards

**Files**: `dashboard-ui/src/ClosedTrades.tsx` (new)

Two side-by-side cards (DEX / CEX): month total `pnl_net` + trade count. **Unaffected by the venue filter** (values come from `totals`, never re-derived from the filtered list). P&L sign colour-coded, readable in both Telegram themes.

*Acceptance*: switching the filter does not change the card values.

---

## T04 — Trade list

**Files**: `dashboard-ui/src/ClosedTrades.tsx`

Asset, Venue (DEX/CEX), Side (BUY/SELL), PnL Net, Reason, close time (Caracas). Most recent first (server order, fixed). Two-line mobile rows; no horizontal scrolling.

*Acceptance*: readable on a phone without horizontal scrolling.

---

## T05 — Venue filter

**Files**: `dashboard-ui/src/ClosedTrades.tsx`

Three-state control (All / DEX / CEX), refetches `?venue=...`. Filter narrows the list only.

*Acceptance*: only matching venue rows listed; cards unchanged.

---

## T06 — States

**Files**: `dashboard-ui/src/ClosedTrades.tsx`

Loading (spinner), error (message + retry), empty-month ("No closed trades this month"), empty-for-venue ("No DEX/CEX trades this month") — visually distinct from each other and from a zero total. 30s polling; GET only.

*Acceptance*: empty month renders the empty state, not a spinner or blank grid.

---

## T07 — More options entry

**Files**: `dashboard-ui/src/App.tsx`

Add `'closed'` to `Tab`; add `Closed Trades` to `moreTabs` (lucide `History`, fallback `ReceiptText`); render branch; BackButton "back to live" includes `'closed'`. No change to existing entries.

*Acceptance*: page reachable from More options; existing entries unchanged.

---

## T08 — Tests (REQUIRED)

**Files**: `tests/test_closed_trades_page.py` (new)

Fixture: temp SQLite + `closed_trades` DDL from `db/schema_v2.sql`; monkeypatch `dashboard.main.DB_PATH`.

1. **Aggregation fails on wrong sum** — card totals == sum/count of known fixture rows per venue.
2. **Filter All** — visible rows sum to DEX + CEX card totals.
3. **Filter DEX** — only `orderly` rows; cards identical to All.
4. **Month boundary (Caracas UTC-4)** — row at `2026-08-01 03:30Z` (= Jul 31 23:30 Caracas) excluded; row at `2026-08-01 04:30Z` (= Aug 1 00:30 Caracas) included. Fixed `now` injection.
5. **Negative `pnl_net`** — renders and totals correctly.
6. **Empty month** — empty state (empty list + zero totals), not an error.
7. **Signal-mode** — `signal_id NULL` row appears normally (documented: nothing to filter).
8. **Precision** — endpoint returns raw small `pnl_net` (e.g. `0.004123`), not `0.0`.
9. **Read-only** — temp DB contents byte-identical before/after the call.
10. **Cap** — 205 rows → 200 returned, `truncated: true`, totals cover all 205.

*Acceptance*: each test written, executed, and reported. Run: `./venv/bin/python -m pytest tests/test_closed_trades_page.py --basetemp=.pytest_tmp`

---

## T09 — Docs

**Files**: `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`

Page purpose, endpoint contract (`GET /api/trades/closed`), month/timezone rule (calendar month, Caracas UTC-4, by close time), `reason` mapping, `pnl_net` semantics footnote (estimated fees, no funding), 200-row cap. Add a CHANGELOG entry per the spec-workflow convention.

*Acceptance*: a reader can reproduce the page's numbers from the doc alone.

---

## T10 — Review

- Read-only confirmed (test 9).
- Totals reconcile (tests 1–2); filter semantics consistent (test 3).
- Month boundary edge verified (test 4).
- Mobile layout verified in Telegram (manual).
- No new dependency (nothing added to `package.json` / `requirements.txt`).

*Acceptance*: all checks pass; `git diff` shows only in-scope files.
