# Plan — 004 Closed Trades Page

Applies after 003. Read-only page; one endpoint; no schema change; no new dependency.

---

## Backend — `dashboard/main.py` (single-file pattern)

The dashboard container ships **only** `main.py`; every existing endpoint uses inline SQL
through `_get_db()` (read-only, `DB_PATH` env). The new endpoint follows the same pattern —
no cross-module import, no Dockerfile change.

### `GET /api/trades/closed?venue=all|dex|cex`

- `venue` ∈ `all` (default) | `dex` | `cex`; anything else → `400`. Filter applies to the
  **list only**; totals always cover the full month across both venues.
- Canonical venue mapping drives everything (venue symmetry):
  `(("dex", "orderly"), ("cex", "binance"))`. Totals iterate this list; the filter maps the
  requested label to its DB venue. Venues with **no rows in the month are zero-filled**
  (`pnl_net: 0.0, count: 0`) so both cards always render.
- **Month window, computed server-side, one place (Q1/Q2):**

  ```python
  # Caracas = UTC-4, no DST since 2007 (matches timezone.ts fixed offset)
  CARACAS = timezone(timedelta(hours=-4))
  now_c = datetime.now(tz=CARACAS)
  start = now_c.replace(day=1, hour=0, minute=0, second=0, microsecond=0)   # 1st 00:00 Caracas
  end   = first instant of next month in Caracas
  # rows: closed_at >= start.timestamp() AND closed_at < end.timestamp()
  ```

  Month label (`YYYY-MM`) is the Caracas month of "now", so the page never disagrees with the
  window that produced the numbers.
- **Totals and rows in one query pass over the same window** (two SELECTs, identical WHERE on
  `closed_at`, `GROUP BY venue` for totals). No client-side summing; no paginated drift.
- **Reason mapping (Q3):** `{"tp": "TP", "sl": "SL", "time_stop": "Time stop"}`; unknown →
  `reason.upper()`.
- **List cap (Q7):** `LIMIT 200` ordered by `closed_at DESC`; `truncated: bool` when the month
  has more rows than the cap. Totals are uncapped.
- `pnl_net` returned as **raw float** (Q10) — no rounding server-side.
- Response shape:

  ```json
  {
    "ok": true,
    "month": "2026-08",
    "window": {"start": 1782950400.0, "end": 1785628800.0, "tz": "UTC-4 (Caracas)", "by": "close_time"},
    "totals": [
      {"venue": "dex", "label": "DEX", "pnl_net": 0.1201, "count": 1},
      {"venue": "cex", "label": "CEX", "pnl_net": 0.0, "count": 0}
    ],
    "trades": [
      {"id": 7, "asset": "ACX", "venue": "cex", "side": "long",
       "pnl_net": 0.1201, "reason": "tp", "reason_label": "TP", "closed_at": 1785973647.7}
    ],
    "truncated": false
  }
  ```

- `closed_at` handled as unix epoch (matches all existing queries — no `datetime(closed_at,'unixepoch')`
  string comparison needed for the window; the epoch range is exact).

## Frontend — `dashboard-ui/`

### `App.tsx` (minimal edits)

- Add `'closed'` to the `Tab` union.
- Add `{ id: 'closed', label: 'Closed Trades', icon: History }` to `moreTabs` (import `History`
  from `lucide-react`; verify it exists in the installed version — fallback `ReceiptText`).
- Add render branch `{tab === 'closed' && <ClosedTrades />}`.
- Telegram BackButton handler: include `'closed'` in the "back to live" branch
  (`tab === 'status' || tab === 'settings' || tab === 'closed'`).

### `ClosedTrades.tsx` (new)

- **Header**: month label (e.g. `Aug 2026`) + `UTC-4` note.
- **Two cards**, `grid grid-cols-2`, each: venue label, `pnl_net` total (green ≥ 0 / red < 0,
  readable on the dark theme), trade count. **Not affected by the filter.**
- **Three-state filter** (All / DEX / CEX) — segmented control; refetches with `?venue=...`.
- **List**: capped 200 rows, two-line mobile rows (no horizontal scroll):
  line 1 `ASSET · BUY/SELL · DEX/CEX`, line 2 `+0.1201 · TP · 08-05 19:47`.
- **Precision (Q10):** `formatPnl(v)` → `toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 4})`
  with a `+`/`−` sign; exported for clarity.
- **Timestamps (Q2):** reuse `toCaracasTime` from `timezone.ts` for `closed_at`.
- **States (Q9), visually distinct:**
  - loading → `Loader2` spinner;
  - error → message + retry button (distinct from empty);
  - empty month (no trades at all) → "No closed trades this month";
  - empty for selected venue (trades exist, filter yields none) → "No DEX/CEX trades this month";
  - zero totals are numbers, never confused with empty.
- **Read-only:** only `fetch('/api/trades/closed?...')` GET calls. No other fetch, no POST/PUT.
- Poll every 30s (monthly totals are low-frequency; 30s matches the "live-ish" feel without load).

## Tests — `tests/test_closed_trades_page.py` (new, pytest)

Fixture mirrors `test_amendment003.py`: monkeypatch `dashboard.main.DB_PATH` to a temp SQLite,
create `closed_trades` (copy of the table DDL from `db/schema_v2.sql`), seed known rows.

Coverage (T08):

1. **Aggregation fails on wrong sum:** seed known per-venue rows; `totals[x].pnl_net` equals the
   sum of those exact rows, and `totals[x].count` equals the row count (would fail if the query
   aggregated wrong).
2. **Filter All reconciliation:** sum of returned `trades[].pnl_net` == Σ both card totals.
3. **Filter DEX:** returned rows are all `orderly`; card values identical to filter `all`.
4. **Month boundary (Caracas):** a row closing at `2026-08-01 03:30 UTC` (= `2026-07-31 23:30`
   Caracas) is excluded; a row at `2026-08-01 04:30 UTC` (= `2026-08-01 00:30` Caracas) is
   included — under the fixed `-4h` rule. Use fixed `now` injection.
5. **Negative P&L:** a losing row renders in the list and is summed correctly (negative total).
6. **Empty month:** no rows in window → empty trades list, zero totals — not an error.
7. **Signal-mode:** a row with `signal_id NULL` still appears (there is no separate signal-only
   population; nothing to filter). Documents Q5.
8. **Precision:** endpoint returns `pnl_net` raw (e.g. `0.004123`, not `0.0`); small P&L survives.
9. **Read-only:** snapshot of the temp DB (all tables' contents) before/after calling the endpoint
   is unchanged — no write of any kind.
10. **Cap:** 205 seeded rows → `trades` length 200, `truncated: true`; totals still cover all 205.

Run: `./venv/bin/python -m pytest tests/test_closed_trades_page.py --basetemp=.pytest_tmp`

## Docs

- `docs/CURRENT_STATE.md`: add page + endpoint, month/timezone rule, `reason` mapping, `pnl_net`
  semantics footnote.

## Risks

- **Client-side totals over a capped list** — eliminated by construction (server aggregates).
- **Month-boundary ambiguity** — single server-side Caracas window; client cannot shift it;
  edge tested.
- **`pnl_net` semantics** — documented on page (estimated fees, no funding); no change to recording.
- **Dashboard container scope** — endpoint kept in `main.py`; no Dockerfile change.
