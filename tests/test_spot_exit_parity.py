"""
Unit tests for feature 011 — Spot Exit Parity.

Covers AC1–AC4 from .specify/specs/011-spot-exit-parity/spec.md:
  - exchange-SL exits record the REAL fill (price and fee), not the trigger
    price with fee_exit=0 — both the main branch and the crash-guard pre-check
  - every closed spot trade carries the position's true opened_at
  - _close's fee fallback comes from cex_round_trip_fee_pct, not 0.001
  - exit DECISIONS are unchanged (test_spot_exit_hardening keeps guarding that)

Mirrors the tests/test_spot_exit_hardening.py fixture pattern.
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
    yield ops
    ops.DB_PATH = old


@pytest.fixture(autouse=True)
def _clear_state(db):
    import trading_bot.spot_scalper as sc
    sc._last_sl.clear()
    sc._last_entry.clear()
    yield


class _FakeEx:
    """BinanceSpot-shaped fake for manage_open_positions."""

    def __init__(self, price=100.0, status=None, order_fills=None):
        self.price = price
        self.status_map = status or {}
        self.order_fills = order_fills
        self.calls = {"fills": []}

    def get_price(self, asset):
        return self.price

    def get_order_status(self, sym, oid):
        return self.status_map.get(oid, "NEW")

    def cancel_order(self, sym, oid):
        return True

    def market_sell(self, asset, qty):
        return None

    def get_asset_balance(self, asset):
        return 1000.0

    def get_order_fills(self, sym, oid):
        self.calls["fills"].append(oid)
        return self.order_fills


OPENED = 1234567890.0


def _seed(db, entry=100.0, qty=5.0, tp=100.8, sl=99.4):
    db.save_position({
        "id": "p1", "asset": "AAA", "venue": "binance", "side": "long",
        "qty": qty, "entry_price": entry, "signal_price": entry,
        "tp_price": tp, "sl_price": sl, "tp_order_id": "tp1", "sl_order_id": "sl1",
        "opened_at": OPENED, "signal_id": None, "fee_entry": 0.01,
    })


def _trade(db):
    with db.get_db_connection() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM closed_trades")]
    assert len(rows) == 1
    return rows[0]


def test_sl_exit_uses_real_fill(db):
    """AC1: the slipped fill is booked, not the trigger price with fee 0."""
    import trading_bot.spot_scalper as sc
    _seed(db, sl=99.4)
    ex = _FakeEx(status={"sl1": "FILLED"}, order_fills=(98.7, 0.049))
    sc.manage_open_positions("AAA", ex)
    t = _trade(db)
    assert t["exit_price"] == 98.7, "the real (slipped) fill must be recorded"
    assert t["fee_exit"] == 0.049 and t["exit_reason"] == "sl"
    assert "sl1" in ex.calls["fills"], "the SL branch must query real fills"


def test_crash_guard_sl_branch_uses_real_fill(db):
    """AC1: the crash-guard pre-check's SL branch records real fills too."""
    import trading_bot.spot_scalper as sc
    _seed(db, entry=100.0, sl=99.4)
    # live far below the crash floor -> crash-guard block runs first;
    # SL already FILLED -> its fill-aware pre-check must use the real fill
    ex = _FakeEx(price=90.0, status={"sl1": "FILLED"}, order_fills=(98.6, 0.05))
    sc.manage_open_positions("AAA", ex)
    t = _trade(db)
    assert t["exit_price"] == 98.6 and t["exit_reason"] == "sl"


def test_sl_fill_query_failure_falls_back(db):
    """AC1: no fill data -> stored sl_price, with the settings-based fee."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("cex_round_trip_fee_pct", "0.30")   # distinctive value
    _seed(db, sl=99.4, qty=5.0)
    ex = _FakeEx(status={"sl1": "FILLED"}, order_fills=None)
    sc.manage_open_positions("AAA", ex)
    t = _trade(db)
    assert t["exit_price"] == 99.4, "fallback is the stored trigger price"
    assert t["fee_exit"] == pytest.approx(99.4 * 5.0 * 0.0030 / 2), \
        "fee fallback must come from cex_round_trip_fee_pct, not 0.001"


def test_opened_at_recorded(db):
    """AC2: the closed trade carries the position's true opened_at."""
    import trading_bot.spot_scalper as sc
    _seed(db)
    ex = _FakeEx(status={"tp1": "FILLED"}, order_fills=(100.85, 0.05))
    sc.manage_open_positions("AAA", ex)
    assert _trade(db)["opened_at"] == OPENED


def test_default_setting_matches_old_rate(db):
    """AC3/AC4: at the default 0.20% round trip the fallback equals the old
    hardcoded 0.001 per leg — zero numeric behaviour change today."""
    import trading_bot.spot_scalper as sc
    _seed(db, sl=99.4, qty=5.0)
    ex = _FakeEx(status={"sl1": "FILLED"}, order_fills=None)
    sc.manage_open_positions("AAA", ex)
    t = _trade(db)
    assert t["fee_exit"] == pytest.approx(99.4 * 5.0 * 0.001)
