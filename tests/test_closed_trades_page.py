"""
Tests for Amendment 004 — Closed Trades Page (Mini App).

Acceptance criteria covered (unit-testable without exchange access):
  - card totals equal the sum of known fixture rows per venue (fails on a wrong aggregation)
  - filter All: visible rows sum to DEX + CEX card totals
  - filter DEX: only DEX rows listed; cards unchanged
  - month boundary in Caracas UTC-4 by close time (edge rows land in the correct month)
  - negative pnl_net renders and totals correctly
  - empty month returns an empty result, not an error
  - signal-mode rows (signal_id NULL) appear normally
  - precision: a small pnl_net survives the API unrounded
  - the endpoint performs no writes of any kind (read-only)
  - the 200-row cap truncates the list but not the totals
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dashboard.main as api
from fastapi.testclient import TestClient

CLOSED_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS closed_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset           TEXT NOT NULL,
    venue           TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    signal_price    REAL NOT NULL,
    qty             REAL NOT NULL,
    fee_entry       REAL NOT NULL DEFAULT 0,
    fee_exit        REAL NOT NULL DEFAULT 0,
    pnl_net         REAL NOT NULL,
    pnl_pct         REAL NOT NULL,
    opened_at       REAL NOT NULL,
    closed_at       REAL NOT NULL,
    exit_reason     TEXT NOT NULL,
    signal_id       INTEGER
);
"""

# Fixed "now": 2026-08-15 16:00 UTC == 2026-08-15 12:00 Caracas (UTC-4).
# Every test runs against the August 2026 window, regardless of when it is executed.
NOW_TS = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc).timestamp()


def _ts(y=2026, m=8, d=3, h=12, mi=0):
    """UTC epoch for a timestamp; default lands inside the Aug-2026 Caracas window."""
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: NOW_TS)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Point the dashboard API at a throwaway SQLite DB with a closed_trades table."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(CLOSED_TRADES_DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(api, "DB_PATH", db_path)
    with TestClient(api.app) as c:
        yield c, db_path


def _seed(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO closed_trades (asset, venue, side, entry_price, exit_price, "
        "signal_price, qty, fee_entry, fee_exit, pnl_net, pnl_pct, opened_at, "
        "closed_at, exit_reason, signal_id) "
        "VALUES (:asset, :venue, :side, :entry_price, :exit_price, :signal_price, "
        ":qty, :fee_entry, :fee_exit, :pnl_net, :pnl_pct, :opened_at, :closed_at, "
        ":exit_reason, :signal_id)",
        rows,
    )
    conn.commit()
    conn.close()


def _row(asset="NEAR", venue="orderly", pnl=0.1, closed_at=None, side="long",
         reason="tp", signal_id=None):
    return {
        "asset": asset, "venue": venue, "side": side,
        "entry_price": 1.0, "exit_price": 1.0, "signal_price": 1.0,
        "qty": 1.0, "fee_entry": 0.0, "fee_exit": 0.0,
        "pnl_net": pnl, "pnl_pct": 0.0,
        "opened_at": 0.0, "closed_at": closed_at if closed_at is not None else _ts(),
        "exit_reason": reason, "signal_id": signal_id,
    }


def _totals(data):
    return {t["venue"]: t for t in data["totals"]}


# ── aggregation: card totals == sum of known fixture rows per venue ─────────

def test_totals_equal_sum_of_rows_per_venue(client):
    """Card totals must equal the exact sum/count of the fixture rows per venue.
    This test fails if the aggregation groups by the wrong venue or window."""
    c, db = client
    _seed(db, [
        _row(asset="A", venue="orderly", pnl=0.5),
        _row(asset="B", venue="orderly", pnl=-0.25),
        _row(asset="C", venue="binance", pnl=0.12),
    ])
    r = c.get("/api/trades/closed")
    assert r.status_code == 200
    data = r.json()
    t = _totals(data)
    assert t["dex"]["pnl_net"] == 0.25
    assert t["dex"]["count"] == 2
    assert t["cex"]["pnl_net"] == 0.12
    assert t["cex"]["count"] == 1


def test_filter_all_rows_sum_to_cards(client):
    """With filter All, the visible rows sum exactly to DEX + CEX card totals."""
    c, db = client
    _seed(db, [
        _row(asset="A", venue="orderly", pnl=0.1234),
        _row(asset="B", venue="orderly", pnl=-0.0501),
        _row(asset="C", venue="binance", pnl=0.0077),
    ])
    data = c.get("/api/trades/closed?venue=all").json()
    t = _totals(data)
    row_sum = sum(tr["pnl_net"] for tr in data["trades"])
    assert row_sum == pytest.approx(t["dex"]["pnl_net"] + t["cex"]["pnl_net"])


def test_filter_dex_narrows_list_keeps_cards(client):
    """Filter DEX: only orderly rows listed; card values identical to filter All."""
    c, db = client
    _seed(db, [
        _row(asset="A", venue="orderly", pnl=0.5),
        _row(asset="C", venue="binance", pnl=0.12),
    ])
    all_data = c.get("/api/trades/closed?venue=all").json()
    dex_data = c.get("/api/trades/closed?venue=dex").json()
    assert dex_data["totals"] == all_data["totals"]
    assert [tr["venue"] for tr in dex_data["trades"]] == ["dex"]
    assert {tr["asset"] for tr in dex_data["trades"]} == {"A"}


# ── month boundary: calendar month in Caracas UTC-4, by close time ──────────

def test_month_boundary_caracas_utc4(client):
    """A close at 2026-08-01 03:30Z (= Jul 31 23:30 Caracas) belongs to July and is
    excluded; a close at 2026-08-01 04:30Z (= Aug 1 00:30 Caracas) belongs to August."""
    c, db = client
    before = _ts(y=2026, m=8, d=1, h=3, mi=30)   # Jul 31 23:30 Caracas
    after = _ts(y=2026, m=8, d=1, h=4, mi=30)    # Aug 1 00:30 Caracas
    _seed(db, [
        _row(asset="JUL", venue="orderly", pnl=1.0, closed_at=before),
        _row(asset="AUG", venue="orderly", pnl=2.0, closed_at=after),
    ])
    data = c.get("/api/trades/closed").json()
    assets = {tr["asset"] for tr in data["trades"]}
    assert assets == {"AUG"}
    assert _totals(data)["dex"]["pnl_net"] == 2.0
    assert _totals(data)["dex"]["count"] == 1


def test_prev_month_rows_excluded(client):
    """A trade closed in the previous month never appears in the current month view."""
    c, db = client
    _seed(db, [_row(asset="OLD", venue="binance", pnl=9.0, closed_at=_ts(y=2026, m=7, d=28))])
    data = c.get("/api/trades/closed").json()
    assert data["trades"] == []
    assert _totals(data)["cex"]["count"] == 0
    assert _totals(data)["cex"]["pnl_net"] == 0.0


# ── sign, states, signal-mode, precision, read-only, cap ───────────────────

def test_negative_pnl_renders_and_totals(client):
    c, db = client
    _seed(db, [_row(asset="LOSER", venue="binance", pnl=-0.31, reason="sl")])
    data = c.get("/api/trades/closed").json()
    assert _totals(data)["cex"]["pnl_net"] == -0.31
    tr = data["trades"][0]
    assert tr["pnl_net"] == -0.31
    assert tr["reason_label"] == "SL"


def test_empty_month_is_empty_not_error(client):
    c, db = client
    _seed(db, [_row(asset="OLD", venue="orderly", pnl=1.0, closed_at=_ts(y=2026, m=6, d=1))])
    r = c.get("/api/trades/closed")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["trades"] == []
    assert all(t["count"] == 0 for t in data["totals"])


def test_signal_null_row_appears(client):
    """Signal-mode trades never reach closed_trades; a row with signal_id NULL
    (the normal case today) must still be listed — nothing to filter."""
    c, db = client
    _seed(db, [_row(asset="SIG", venue="orderly", pnl=0.05, signal_id=None)])
    data = c.get("/api/trades/closed").json()
    assert [tr["asset"] for tr in data["trades"]] == ["SIG"]


def test_small_pnl_precision_preserved(client):
    """A real small scalp P&L must not be rounded away by the API."""
    c, db = client
    _seed(db, [_row(asset="TINY", venue="binance", pnl=0.004123)])
    data = c.get("/api/trades/closed").json()
    assert data["trades"][0]["pnl_net"] == 0.004123
    assert _totals(data)["cex"]["pnl_net"] == 0.004123


def test_reason_label_mapping(client):
    c, db = client
    _seed(db, [
        _row(asset="A", venue="orderly", reason="tp"),
        _row(asset="B", venue="orderly", reason="sl"),
        _row(asset="C", venue="orderly", reason="time_stop"),
    ])
    data = c.get("/api/trades/closed").json()
    labels = {tr["reason"]: tr["reason_label"] for tr in data["trades"]}
    assert labels == {"tp": "TP", "sl": "SL", "time_stop": "Time stop"}


def test_read_only_no_writes(client):
    """Calling the endpoint must leave the database byte-identical (no write path)."""
    c, db = client
    _seed(db, [
        _row(asset="A", venue="orderly", pnl=0.5),
        _row(asset="C", venue="binance", pnl=-0.2),
    ])
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT * FROM closed_trades").fetchall()
    conn.close()

    for q in ("", "?venue=all", "?venue=dex", "?venue=cex"):
        assert c.get(f"/api/trades/closed{q}").status_code == 200

    conn = sqlite3.connect(db)
    after = conn.execute("SELECT * FROM closed_trades").fetchall()
    conn.close()
    assert after == before


def test_cap_200_truncates_list_not_totals(client):
    c, db = client
    rows = [_row(asset=f"A{i:03d}", venue="orderly", pnl=0.01, closed_at=_ts(h=12, mi=i % 60))
            for i in range(205)]
    _seed(db, rows)
    data = c.get("/api/trades/closed").json()
    assert len(data["trades"]) == 200
    assert data["truncated"] is True
    assert _totals(data)["dex"]["count"] == 205
    assert _totals(data)["dex"]["pnl_net"] == pytest.approx(205 * 0.01)


def test_unknown_venue_is_400(client):
    c, db = client
    assert c.get("/api/trades/closed?venue=bogus").status_code == 400
