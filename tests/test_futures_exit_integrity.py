"""
Unit tests for feature 010 — Futures Exit Integrity.

Covers AC1–AC14 from .specify/specs/010-futures-exit-integrity/spec.md:
  - time stop actually closes the position, verifies, and records the real fill
    (AC1, AC5, AC6, AC11)
  - a failed / unverified close keeps the position and restores its stop
    (AC2, AC3), escalating when the stop cannot be restored either (AC4)
  - regime exit reaches the exchange, or the DB is left alone (AC7, AC8)
  - TP/SL exits record real fills, not intended prices (AC9, AC10)
  - fee fallback uses dex_round_trip_fee_pct, never a hardcoded rate (AC12)
  - cancel_order issues one request (AC13); dry_run places nothing (AC14)

Orderly cannot be reached from CI, so correctness is established with a
scriptable fake (the tests/test_spot_exit_hardening.py pattern). The plan
carries a manual dry_run checklist to run before DEX is ever armed.
"""

import os
import sys
import time

import pytest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def db(tmp_path):
    import db.db_ops as ops
    old = ops.DB_PATH
    ops.DB_PATH = str(tmp_path / "test.db")
    ops.initialize_database_tables()
    ops.upsert_setting("auto_trade_orderly", "Automatic")
    ops.upsert_setting("dex_round_trip_fee_pct", "0.06")
    yield ops
    ops.DB_PATH = old


@pytest.fixture(autouse=True)
def _clear_state(db):
    import trading_bot.futures_scalper as fc
    fc._last_sl.clear()
    fc._last_entry.clear()
    yield


class _Fill:
    def __init__(self, price=0.0, fee=0.0, qty=1.0):
        self.fill_price = price; self.fee_amount = fee
        self.filled_qty = qty; self.sellable_qty = qty


class _FakeOrderly:
    """OrderlyFutures-shaped fake; every call recorded, every path forceable."""

    def __init__(self, status=None, close_fill=_Fill(95.0, 0.02),
                 still_open=False, order_fills=None, cancel_ok=True,
                 stop_id="sl2", tp_id="tp2"):
        self.status_map = status or {}
        self.close_fill = close_fill
        self.still_open = still_open
        self.order_fills = order_fills
        self.cancel_ok = cancel_ok
        self.stop_id = stop_id
        self.tp_id = tp_id
        self.calls = {"close": [], "cancel": [], "stop": [], "tp": [], "positions": 0}

    def get_order_status(self, oid):
        return self.status_map.get(oid, "NEW")

    def get_order_fills(self, oid):
        return self.order_fills

    def cancel_order(self, sym, oid):
        self.calls["cancel"].append(oid)
        return self.cancel_ok

    def market_close(self, asset, side, qty):
        self.calls["close"].append((asset, side, qty))
        return self.close_fill

    def get_open_positions(self, asset=None):
        self.calls["positions"] += 1
        return [{"symbol": f"PERP_{asset}_USDC", "position_qty": 1}] if self.still_open else []

    def place_stop(self, asset, side, qty, price, pid):
        self.calls["stop"].append((asset, side, qty, price))
        return self.stop_id

    def place_tp(self, asset, side, qty, price, pid):
        self.calls["tp"].append((asset, side, qty, price))
        return self.tp_id


def _seed(db, asset="AAA", entry=100.0, qty=1.0, tp=101.0, sl=98.0,
          opened=None, side="long", tp_oid="tp1", sl_oid="sl1"):
    db.save_position({
        "id": "p1", "asset": asset, "venue": "orderly", "side": side,
        "qty": qty, "entry_price": entry, "signal_price": entry,
        "tp_price": tp, "sl_price": sl, "tp_order_id": tp_oid, "sl_order_id": sl_oid,
        "opened_at": opened if opened is not None else time.time(),
        "signal_id": None, "fee_entry": 0.01,
    })


def _closed(db):
    with db.get_db_connection() as c:
        return [dict(r) for r in c.execute("SELECT * FROM closed_trades")]


def _open(db):
    return db.load_all_positions(venue="orderly")


OLD = 1.0  # opened_at far in the past -> time stop fires


# ── Time stop, happy path (AC1, AC5, AC6, AC11) ──────────────────────────────

def test_time_stop_closes_position(db):
    """AC1: the position is actually closed on the exchange, then recorded."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    ex = _FakeOrderly()
    fc.manage_open_positions("AAA", ex, "RANGE")
    assert ex.calls["close"] == [("AAA", "long", 1.0)], "a closing order must be sent"
    assert ex.calls["positions"] == 1, "closure must be verified"
    assert _open(db) == [] and len(_closed(db)) == 1


def test_time_stop_records_real_fill(db):
    """AC6: exit price is the close fill, never the entry price."""
    import trading_bot.futures_scalper as fc
    _seed(db, entry=100.0, opened=OLD)
    fc.manage_open_positions("AAA", _FakeOrderly(close_fill=_Fill(95.0, 0.02)), "RANGE")
    t = _closed(db)[0]
    assert t["exit_price"] == 95.0 and t["exit_reason"] == "time_stop"
    assert t["pnl_net"] < 0, "a real loss must be booked, not -fees at entry price"


def test_opened_at_is_real(db):
    """AC11: hold duration is preserved."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    fc.manage_open_positions("AAA", _FakeOrderly(), "RANGE")
    assert _closed(db)[0]["opened_at"] == OLD


# ── Time stop, failure paths (AC2, AC3, AC4, AC5) ────────────────────────────

def test_time_stop_failed_close_keeps_position(db):
    """AC2: a failed close must NOT delete the row or book a trade."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    fc.manage_open_positions("AAA", _FakeOrderly(close_fill=None), "RANGE")
    assert len(_open(db)) == 1, "position must survive a failed close"
    assert _closed(db) == [], "no trade may be booked for a position still open"


def test_time_stop_failed_close_replaces_sl(db):
    """AC3: Constitution III — the stop is restored before the cycle ends."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    ex = _FakeOrderly(close_fill=None)
    fc.manage_open_positions("AAA", ex, "RANGE")
    assert ex.calls["stop"] == [("AAA", "long", 1.0, 98.0)]
    assert _open(db)[0]["sl_order_id"] == "sl2", "new stop id must be persisted"


def test_time_stop_unverified_close_keeps_position(db):
    """AC5: a fill that leaves the position open is not a close."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    ex = _FakeOrderly(still_open=True)
    fc.manage_open_positions("AAA", ex, "RANGE")
    assert len(_open(db)) == 1 and _closed(db) == []
    assert ex.calls["stop"], "an unverified close must re-protect too"


def test_double_failure_escalates(db):
    """AC4: close AND stop both fail -> alert + DEX entries disabled."""
    import trading_bot.futures_scalper as fc
    _seed(db, opened=OLD)
    ex = _FakeOrderly(close_fill=None, stop_id=None)
    with mock.patch("trading_bot.send_bot_message.send_message") as msg:
        fc.manage_open_positions("AAA", ex, "RANGE")
    assert msg.called, "an unprotected position must alert the operator"
    assert db.get_setting("auto_trade_orderly") == "false"
    assert len(_open(db)) == 1, "the position is still tracked for manual handling"


# ── Regime exit (AC7, AC8) ───────────────────────────────────────────────────

def test_regime_exit_amends_exchange(db):
    """AC7: the breakeven TP is placed on the exchange, then persisted."""
    import trading_bot.futures_scalper as fc
    _seed(db)
    ex = _FakeOrderly()
    fc.manage_open_positions("AAA", ex, "TREND_DOWN")
    assert ex.calls["cancel"] == ["tp1"], "the live TP must be cancelled"
    assert len(ex.calls["tp"]) == 1, "a replacement TP must be placed"
    pos = _open(db)[0]
    assert pos["tp_price"] == pytest.approx(100.0 * 1.0006)
    assert pos["tp_order_id"] == "tp2"


def test_regime_exit_failure_leaves_db(db):
    """AC8: a failed replacement must not record a price the exchange lacks."""
    import trading_bot.futures_scalper as fc
    _seed(db)
    ex = _FakeOrderly(tp_id=None)
    fc.manage_open_positions("AAA", ex, "TREND_DOWN")
    assert _open(db)[0]["tp_price"] == 101.0, "tp_price must stay untouched"


# ── Real fills (AC9, AC10, AC12) ─────────────────────────────────────────────

def test_tp_exit_uses_real_fill(db):
    """AC9: a filled TP books the exchange price, not the intended one."""
    import trading_bot.futures_scalper as fc
    _seed(db, tp=101.0)
    ex = _FakeOrderly(status={"tp1": "FILLED"}, order_fills=(100.55, 0.03))
    fc.manage_open_positions("AAA", ex, "RANGE")
    t = _closed(db)[0]
    assert t["exit_price"] == 100.55 and t["fee_exit"] == 0.03 and t["exit_reason"] == "tp"


def test_sl_exit_uses_real_fill(db):
    """AC10: slippage past the stop trigger is captured, not hidden."""
    import trading_bot.futures_scalper as fc
    _seed(db, sl=98.0)
    ex = _FakeOrderly(status={"sl1": "FILLED"}, order_fills=(96.4, 0.03))
    fc.manage_open_positions("AAA", ex, "RANGE")
    t = _closed(db)[0]
    assert t["exit_price"] == 96.4, "the real (slipped) fill must be recorded"
    assert t["exit_reason"] == "sl"


def test_fee_fallback_uses_setting(db):
    """AC12: no exchange fee -> dex_round_trip_fee_pct, never 0.0003."""
    import trading_bot.futures_scalper as fc
    _seed(db, tp=101.0, qty=10.0)
    ex = _FakeOrderly(status={"tp1": "FILLED"}, order_fills=None)  # no fill data
    fc.manage_open_positions("AAA", ex, "RANGE")
    t = _closed(db)[0]
    assert t["exit_price"] == 101.0, "falls back to the stored price"
    assert t["fee_exit"] == pytest.approx(101.0 * 10.0 * 0.0006 / 2)


# ── Executor (AC13, AC14) ────────────────────────────────────────────────────

def test_cancel_order_single_request(db):
    """AC13: exactly one HTTP call — the junk POST is gone."""
    import trading_bot.executor as ex
    o = ex.OrderlyFutures()
    with mock.patch.object(o, "_post") as post, \
         mock.patch("trading_bot.executor.requests.delete") as delete, \
         mock.patch.object(o, "_sign", return_value="sig"):
        delete.return_value = mock.Mock(status_code=200)
        assert o.cancel_order("PERP_AAA_USDC", "o1") is True
    assert post.call_count == 0, "no order request may be sent to cancel an order"
    assert delete.call_count == 1


def test_dry_run_places_nothing(db):
    """AC14: dry_run short-circuits every new order path."""
    import trading_bot.executor as ex
    db.upsert_setting("dry_run", "true")
    o = ex.OrderlyFutures()
    with mock.patch.object(o, "_post") as post:
        fill = o.market_close("AAA", "long", 1.0)
        assert o.place_stop("AAA", "long", 1.0, 98.0, "p1") == "dry-sl"
        assert o.place_tp("AAA", "long", 1.0, 101.0, "p1") == "dry-tp"
    assert fill is not None and post.call_count == 0
