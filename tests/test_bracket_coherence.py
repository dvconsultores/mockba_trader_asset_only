"""
Unit tests for feature 016 — Bracket Coherence Guard (spot).

The BICO incident (2026-08-16): live ATR outgrew the scan-time universe cap,
the adaptive stop (2xATR = 5.7%) exceeded the 3% crash floor, and the crash
guard fired at a loss the entry never priced. The guard skips such entries.
"""
import os, sys, time
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
def _clear(db):
    import trading_bot.spot_scalper as sc
    sc._last_sl.clear(); sc._last_entry.clear()
    sc._price_memory.clear(); sc._peak.clear(); sc._trough.clear()
    yield


class _FakeSpot:
    class _Info:
        symbol="AAAUSDT"; base_tick=0.001; quote_tick=0.01; min_qty=0.001; min_notional=5.0
    class _Fill:
        fill_price=90.0; fee_amount=0.01; sellable_qty=1.0; filled_qty=1.0
        tp_order_id="tp1"; sl_order_id="sl1"
    def __init__(self): self.entries=[]
    def get_equity(self): return 100.0
    def get_symbol_info(self, a): return self._Info()
    def place_entry(self,*a,**k): self.entries.append(a); return self._Fill()


def _drive(sc):
    for _ in range(12): sc._update_price_memory("AAA", 100.0)


def _run(db, atr):
    import trading_bot.spot_scalper as sc
    # live wide-stop configuration (the guard interacts with these)
    db.upsert_setting("sl_k_spot", "2.0")
    db.upsert_setting("sl_min_pct_spot", "1.5")
    db.upsert_setting("tp_k", "1.2")
    db.upsert_setting("max_loss_per_position_pct", "3.0")
    _drive(sc)
    ex=_FakeSpot()
    with mock.patch("trading_bot.spot_scalper.get_atr_pct", return_value=atr), \
         mock.patch("trading_bot.spot_scalper.last_closed_return_up", return_value=True):
        res=sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0)
    return res, ex


def test_incoherent_bracket_skipped(db):
    """BICO case: ATR 2.85 -> se 5.7% > 3% floor -> no order, reason recorded."""
    res, ex = _run(db, 2.85)
    assert res is None and ex.entries == []
    with db.get_db_connection() as c:
        rows=[dict(r) for r in c.execute("SELECT * FROM signals WHERE reason='sl_exceeds_crash_floor'")]
    assert rows and rows[0]["action"] == "skipped"


def test_equality_passes(db):
    """se == floor is coherent (ALICE-class): ATR 1.5 -> se 3.0 == 3.0 -> trades."""
    res, ex = _run(db, 1.5)
    assert res is not None and ex.entries


def test_normal_atr_unaffected(db):
    """ATR 0.7 -> se 1.5 (floor) well under 3 -> trades as before."""
    res, ex = _run(db, 0.7)
    assert res is not None and ex.entries
