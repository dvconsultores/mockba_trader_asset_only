"""
Unit tests for feature 009 — Entry Confirmation Candle.

Covers AC1–AC11 and AC14 from .specify/specs/009-entry-confirmation-candle/spec.md:
  - last_closed_return_up reads the last CLOSED bar, flat is not confirmation,
    indeterminate is None (AC1, AC2)
  - observe mode never blocks and always records (AC3, AC4)
  - enforce mode blocks unconfirmed / passes confirmed / fails closed on None
    (AC5, AC6, AC7)
  - futures direction symmetry (AC8) — EVIDENCE-FREE, see the test docstring
  - zero additional API calls on a warm cache (AC9)
  - setting registration (AC10), migration idempotency (AC11), A/B query (AC14)

Mirrors tests/test_spot_exit_hardening.py (tmp-DB fixture via db.db_ops.DB_PATH
monkeypatch + initialize_database_tables; autouse module-state reset; fake
BinanceSpot-shaped exchange) — no network, no real exchange.
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
    """Point db_ops at a throwaway SQLite DB and initialize the schema."""
    import db.db_ops as ops
    old = ops.DB_PATH
    ops.DB_PATH = str(tmp_path / "test.db")
    ops.initialize_database_tables()
    yield ops
    ops.DB_PATH = old


@pytest.fixture(autouse=True)
def _clear_module_state(db):
    """Clear the candle cache, price memory and cooldowns between tests."""
    import trade.regime as r
    import trading_bot.spot_scalper as sc
    import trading_bot.futures_scalper as fc
    import trade.pnl as pnl
    r._candle_cache.clear()
    r._cache.clear()
    for mod in (sc, fc):
        mod._price_memory.clear()
        mod._peak.clear()
        mod._trough.clear()
        mod._last_entry.clear()
        mod._last_sl.clear()
    pnl._day_cache.clear()
    pnl._day_cache_date.clear()
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _bar(o, c):
    """One OHLCV dict in the shape _fetch_ohlcv returns."""
    return {"open": o, "close": c, "high": max(o, c), "low": min(o, c), "volume": 1.0}


def _seed_candles(asset, venue, bars, age=0.0):
    """Write bars straight into the shared 5m ATR cache."""
    import trade.regime as r
    key = r._candle_cache_key(asset, venue, "5m")
    r._candle_cache[key] = (time.time() - age, bars)


def _seed_up(asset="AAA", venue="binance"):
    """Last CLOSED bar is up; the in-progress bar deliberately falls."""
    _seed_candles(asset, venue, [_bar(1.0, 1.0), _bar(1.0, 1.1), _bar(9.0, 1.0)])


def _seed_down(asset="AAA", venue="binance"):
    """Last CLOSED bar is down; the in-progress bar deliberately rises."""
    _seed_candles(asset, venue, [_bar(1.0, 1.0), _bar(1.1, 1.0), _bar(1.0, 9.0)])


class _FakeSpot:
    """BinanceSpot-shaped fake — records whether an order was ever placed."""
    class _Info:
        symbol = "AAAUSDT"; base_tick = 0.001; quote_tick = 0.01
        min_qty = 0.001; min_notional = 5.0

    class _Fill:
        fill_price = 100.0; fee_amount = 0.01
        sellable_qty = 1.0; filled_qty = 1.0
        tp_order_id = "tp1"; sl_order_id = "sl1"

    def __init__(self):
        self.entries = []

    def get_equity(self):
        return 1000.0

    def get_symbol_info(self, asset):
        return self._Info()

    def place_entry(self, *a, **k):
        self.entries.append((a, k))
        return self._Fill()


class _FakeFutures(_FakeSpot):
    """OrderlyFutures-shaped fake (same surface for these tests)."""


def _drive_dip(mod, asset="AAA", venue="binance"):
    """Fill the rolling window so _is_dip fires on the next low price."""
    for _ in range(12):
        mod._update_price_memory(asset, 100.0)


def _signals(db, **where):
    q = "SELECT * FROM signals"
    if where:
        q += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    with db.get_db_connection() as conn:
        return [dict(r) for r in conn.execute(q, tuple(where.values())).fetchall()]


# ── AC1 / AC2 — the helper ───────────────────────────────────────────────────

def test_helper_reads_last_closed_bar(db):
    """AC1: reads candles[-2], never the in-progress candles[-1]; flat is False."""
    from trade.regime import last_closed_return_up as up
    _seed_up()
    assert up("AAA", "binance") is True      # [-1] falls hard and must be ignored
    _seed_down()
    assert up("AAA", "binance") is False     # [-1] rises hard and must be ignored
    _seed_candles("AAA", "binance", [_bar(1.0, 1.0), _bar(2.0, 2.0), _bar(1.0, 9.0)])
    assert up("AAA", "binance") is False     # flat bar is NOT confirmation (Q1)


def test_helper_indeterminate(db):
    """AC2: unavailable or too-short series returns None (never confirmed)."""
    from trade.regime import last_closed_return_up as up
    with mock.patch("trade.regime.get_atr_pct", return_value=None):
        assert up("NOPE", "binance") is None            # empty cache, no fetch
        _seed_candles("ONE", "binance", [_bar(1.0, 2.0)])
        assert up("ONE", "binance") is None             # single bar


# ── AC9 — no extra market-data requests ──────────────────────────────────────

def test_no_additional_api_calls(db):
    """AC9: a warm cache resolves the helper with zero _fetch_ohlcv calls."""
    from trade.regime import last_closed_return_up as up
    _seed_up()
    with mock.patch("trade.regime._fetch_ohlcv") as fetch:
        assert up("AAA", "binance") is True
        assert fetch.call_count == 0


# ── AC3 / AC4 — observe mode ─────────────────────────────────────────────────

def test_observe_mode_never_blocks(db):
    """AC3: with the setting unset, an unconfirmed entry still fires."""
    import trading_bot.spot_scalper as sc
    _seed_down()                       # would be blocked if enforcing
    _drive_dip(sc)
    ex = _FakeSpot()
    res = sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0)
    assert res is not None and ex.entries, "observe mode must not block"
    assert not _signals(db, reason="entry_not_confirmed")


def test_observe_mode_records(db):
    """AC4: the entered row carries the evaluated verdict (0 here)."""
    import trading_bot.spot_scalper as sc
    _seed_down()
    _drive_dip(sc)
    sc.scalp_cycle("AAA", _FakeSpot(), "RANGE", 1.0, 90.0)
    entered = _signals(db, action="entered")
    assert entered and entered[0]["entry_confirmed"] == 0

    # and 1 when the last closed bar is up
    sc._last_entry.clear()
    _seed_up("BBB")
    _drive_dip(sc, "BBB")
    sc.scalp_cycle("BBB", _FakeSpot(), "RANGE", 1.0, 90.0)
    rows = _signals(db, action="entered", asset="BBB")
    assert rows and rows[0]["entry_confirmed"] == 1


# ── AC5 / AC6 / AC7 — enforce mode ───────────────────────────────────────────

def test_enforce_blocks_unconfirmed(db):
    """AC5: unconfirmed entry is skipped and no order reaches the exchange."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("entry_confirm_candle", "true")
    _seed_down()
    _drive_dip(sc)
    ex = _FakeSpot()
    assert sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0) is None
    assert ex.entries == [], "no order may be placed for a blocked entry"
    rows = _signals(db, reason="entry_not_confirmed")
    assert rows and rows[0]["action"] == "skipped" and rows[0]["entry_confirmed"] == 0


def test_enforce_passes_confirmed(db):
    """AC6: a confirmed entry proceeds through the unchanged entry path."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("entry_confirm_candle", "true")
    _seed_up()
    _drive_dip(sc)
    ex = _FakeSpot()
    assert sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0) is not None
    assert ex.entries, "a confirmed entry must reach the exchange"
    entered = _signals(db, action="entered")
    assert entered and entered[0]["entry_confirmed"] == 1


def test_enforce_none_fails_closed(db):
    """AC7: indeterminate confirmation skips the entry (Constitution IV)."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("entry_confirm_candle", "true")
    _drive_dip(sc)
    ex = _FakeSpot()
    with mock.patch("trading_bot.spot_scalper.last_closed_return_up", return_value=None):
        assert sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0) is None
    assert ex.entries == []
    rows = _signals(db, reason="entry_not_confirmed")
    assert rows and rows[0]["entry_confirmed"] is None


# ── AC8 — futures symmetry ───────────────────────────────────────────────────

def test_futures_direction_symmetry(db):
    """AC8: long needs an up bar, short needs a down bar.

    EVIDENCE-FREE (clarify Q4): the 009 study covers spot longs only, and DEX is
    off (auto_trade_orderly=False), so the short arm is asserted against
    synthetic series purely for the symmetry contract — not for edge.
    """
    import trading_bot.futures_scalper as fc
    db.upsert_setting("entry_confirm_candle", "true")

    # long + down bar -> blocked
    _seed_down("AAA", "orderly")
    _drive_dip(fc, "AAA", "orderly")
    ex = _FakeFutures()
    assert fc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0) is None
    assert ex.entries == []

    # short + down bar -> confirmed (the pump has paused)
    fc._price_memory.clear(); fc._peak.clear(); fc._trough.clear()
    fc._last_entry.clear()
    for _ in range(12):
        fc._update_price_memory("AAA", 100.0)
    ex2 = _FakeFutures()
    res = fc.scalp_cycle("AAA", ex2, "RANGE", 1.0, 110.0)   # pump -> short
    rows = _signals(db, asset="AAA", venue="orderly")
    assert any(r["direction"] == "short" and r["entry_confirmed"] == 1 for r in rows), \
        "a short must be confirmed by a DOWN bar"


# ── AC10 / AC11 / AC14 — setting, migration, measurability ───────────────────

def test_setting_registered(db):
    """AC10: registered as a bool in the entry group and accepted by the validator."""
    from trade.settings_schema import BY_KEY
    from trade.settings_rules import validate
    spec = BY_KEY["entry_confirm_candle"]
    assert spec.type is bool and spec.group == "entry"
    assert validate("entry_confirm_candle", True).level == "ok"
    assert validate("entry_confirm_candle", False).level == "ok"
    assert db.get_setting_bool("entry_confirm_candle", False) is False


def test_migration_idempotent(db):
    """AC11: re-running init leaves one column and never rewrites existing rows."""
    with db.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO signals (ts,asset,venue,regime,price,action,reason) "
            "VALUES (?,?,?,?,?,?,?)", (time.time(), "OLD", "binance", "RANGE", 1.0, "skipped", "x"))
        conn.commit()
    db.initialize_database_tables()
    with db.get_db_connection() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)")]
        row = conn.execute("SELECT entry_confirmed FROM signals WHERE asset='OLD'").fetchone()
    assert cols.count("entry_confirmed") == 1
    assert row["entry_confirmed"] is None, "pre-existing rows must stay NULL"


def test_ab_query(db):
    """AC14: the observe-mode A/B is answerable with one grouped query."""
    import trading_bot.spot_scalper as sc
    _seed_up()
    _drive_dip(sc)
    sc.scalp_cycle("AAA", _FakeSpot(), "RANGE", 1.0, 90.0)
    with db.get_db_connection() as conn:
        rows = conn.execute(
            "SELECT entry_confirmed, COUNT(*) n FROM signals "
            "WHERE action IN ('entered','signaled') GROUP BY entry_confirmed").fetchall()
    assert rows and any(r["n"] > 0 for r in rows)
