"""
Unit tests for feature 006 — Spot Exit Hardening (gap/crash protection).

Covers AC1–AC12 from .specify/specs/006-spot-exit-hardening/spec.md:
  - universe_max_atr_pct spot-only cap (AC1–AC3, venue branch, additive only)
  - max_loss_per_position_pct crash guard (AC4–AC9: fires below floor,
    fill-aware ordering, None-price no action, cooldown, no phantom double-close,
    real fills)
  - normal exits unchanged above the floor (AC7)
  - settings registration + validation hard-error-vs-equality (AC3, AC10)
  - dry-run unchanged (AC12)
  - dashboard label (AC11)

Mirrors tests/test_amendment003.py (tmp-DB fixture via db.db_ops.DB_PATH
monkeypatch + initialize_database_tables; network isolation via
mock.patch.object on trade.universe) and tests/test_market_check.py (autouse
fixture clearing module state). The scalper tests use a fake BinanceSpot-shaped
exchange and seed positions via db_ops.save_position.
"""

import os
import sys
import time
from contextlib import ExitStack

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
    """Clear in-memory scalper cooldowns and the PnL day cache between tests."""
    import trading_bot.spot_scalper as sc
    sc._last_sl.clear()
    sc._last_entry.clear()
    import trade.pnl as pnl
    pnl._day_cache.clear()
    pnl._day_cache_date.clear()
    import trade.universe as u
    u._force_rescan.clear()
    yield


# ── fake exchange ────────────────────────────────────────────────────────────

class _Fill:
    """Minimal stand-in for trading_bot.types.Fill (dry-run shape)."""
    def __init__(self, fill_price=0.0, fee_amount=0.0, filled_qty=0.0):
        self.fill_price = fill_price
        self.fee_amount = fee_amount
        self.filled_qty = filled_qty


class _FakeEx:
    """BinanceSpot-shaped fake for manage_open_positions."""
    def __init__(self, price=None, status=None, balance=1000.0,
                 sell_fill=None, order_fills=None, cancel_ok=True):
        self.price = price
        # status: dict order_id -> "NEW"/"FILLED", or a plain default string
        self.status_map = status if isinstance(status, dict) else {}
        self.default_status = status if isinstance(status, str) else "NEW"
        self.balance = balance
        self.sell_fill = sell_fill
        self.order_fills = order_fills
        self.cancel_ok = cancel_ok
        self.calls = {"market_sell": [], "cancel": [], "get_order_status": []}

    def get_price(self, asset):
        return self.price

    def get_order_status(self, sym, oid):
        self.calls["get_order_status"].append((sym, oid))
        return self.status_map.get(oid, self.default_status)

    def cancel_order(self, sym, oid):
        self.calls["cancel"].append((sym, oid))
        return self.cancel_ok

    def market_sell(self, asset, qty):
        self.calls["market_sell"].append((asset, qty))
        return self.sell_fill

    def get_asset_balance(self, asset):
        return self.balance

    def get_order_fills(self, sym, oid):
        return self.order_fills


def _seed_position(db, asset="AAA", entry=100.0, qty=5.0, tp=100.8, sl=99.4,
                   tp_oid="tp1", sl_oid="sl1", pid="p1", opened=None):
    """Insert one open spot position and return its dict (as loaded)."""
    from db.db_ops import save_position
    save_position({
        "id": pid, "asset": asset, "venue": "binance", "side": "long",
        "qty": qty, "entry_price": entry, "signal_price": entry,
        "tp_price": tp, "sl_price": sl,
        "tp_order_id": tp_oid, "sl_order_id": sl_oid,
        "opened_at": opened if opened is not None else time.time(),
        "signal_id": "sig1", "fee_entry": 0.01,
    })
    from db.db_ops import load_all_positions
    return load_all_positions(asset=asset, venue="binance")[0]


def _last_closed(db):
    import db.db_ops as ops
    with ops.get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM closed_trades ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ── settings registration + defaults (plan test 1, AC3/AC10) ────────────────

def test_settings_registered_with_defaults(db):
    from trade.settings_schema import BY_KEY
    from db.db_ops import get_setting_float
    for key, default, grp, hmin, hmax, smin, smax in (
        ("universe_max_atr_pct", 1.5, "universe", 0.1, 20.0, 0.5, 5.0),
        ("max_loss_per_position_pct", 3.0, "exit", 0.1, 20.0, 0.5, 5.0),
    ):
        s = BY_KEY[key]
        assert s.type is float
        assert s.group == grp
        assert s.hard_min == hmin and s.hard_max == hmax
        assert s.soft_min == smin and s.soft_max == smax
        assert get_setting_float(key, default) == default  # fallback when unset


# ── universe cap (plan tests 2/3/4, AC1–AC3) ────────────────────────────────

def _scan_settings(db):
    for k, v in {
        "tp_min_pct": "0.8", "sl_min_pct": "0.5", "universe_rank_min": "1",
        "universe_rank_max": "90", "universe_min_volume_usd": "5000000",
        "universe_spread_ratio_max": "0.10", "universe_depth_slot_multiple": "3",
        "cex_slot_pct": "10", "dex_slot_pct": "10",
        "universe_min_signals": "1", "universe_size": "20",
        "universe_max_atr_pct": "1.5",
    }.items():
        db.upsert_setting(k, v)


def _mk_candidates():
    return [
        {"asset": "BICOX", "symbol": "BICOXUSDT", "quote_volume_24h": 10_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
        {"asset": "PUMPX", "symbol": "PUMPXUSDT", "quote_volume_24h": 10_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
    ]


def _patch_scan(u, candidates, atr_map):
    """Patch the network layers of scan_venue. atr_map: symbol -> atr_pct_median.

    Returns an ExitStack context manager; inside the block, u.replace_universe is
    the mock (use u.replace_universe.call_args to inspect stored rows)."""
    def fake_ohlcv(venue, symbol, interval, limit):
        return {"symbol": symbol}  # sentinel carrying the symbol through

    def fake_replay(candles, atr_period, dip_k, dip_min, tp_k, tp_min, max_hold):
        atr = atr_map.get(candles["symbol"], 0.5)
        return {"signals_count": 30, "recovery_rate": 0.8,
                "median_minutes_to_tp": 5, "atr_pct_median": atr, "minutes_list": []}

    stack = ExitStack()
    stack.enter_context(mock.patch.object(u, "_fetch_candidates", return_value=candidates))
    stack.enter_context(mock.patch.object(u, "_fetch_depth", return_value={"bid": 1_000_000, "ask": 1_000_000}))
    stack.enter_context(mock.patch.object(u, "_fetch_ohlcv", side_effect=fake_ohlcv))
    stack.enter_context(mock.patch.object(u, "replay_symbol", side_effect=fake_replay))
    stack.enter_context(mock.patch.object(u, "replace_universe"))
    return stack


def test_universe_cap_rejects_high_atr_spot_only(db):
    import trade.universe as u
    _scan_settings(db)
    candidates = _mk_candidates()
    with _patch_scan(u, candidates, {"BICOXUSDT": 2.1, "PUMPXUSDT": 0.6}):
        res = u.scan_venue("binance", equity=100_000)
        assert res["ok"]
        assert res["dropped_by_max_atr"] == 1
        stored = u.replace_universe.call_args[0][1]
        assert [r["asset"] for r in stored] == ["PUMPX"]  # BICOX (2.1 > 1.5) dropped


def test_universe_cap_venue_branch_orderly_untouched(db):
    import trade.universe as u
    _scan_settings(db)
    candidates = _mk_candidates()
    with _patch_scan(u, candidates, {"BICOXUSDT": 2.1, "PUMPXUSDT": 0.6}):
        res = u.scan_venue("orderly", equity=100_000)
        assert res["ok"]
        assert "dropped_by_max_atr" not in res  # venue-branch: orderly untouched
        stored = u.replace_universe.call_args[0][1]
        assert {r["asset"] for r in stored} == {"BICOX", "PUMPX"}


def test_universe_cap_additive_only(db):
    """The cap never loosens pre-existing filters: a low-ATR candidate failing
    min_volume is still rejected, while a high-ATR one is dropped by the cap."""
    import trade.universe as u
    _scan_settings(db)
    db.upsert_setting("universe_min_volume_usd", "100000000")  # 100M
    candidates = [
        {"asset": "AAABIG", "symbol": "AAABIGUSDT", "quote_volume_24h": 200_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
        {"asset": "BBBSML", "symbol": "BBBSMLUSDT", "quote_volume_24h": 10_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
        {"asset": "CCCHI", "symbol": "CCCHIUSDT", "quote_volume_24h": 200_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
    ]
    with _patch_scan(u, candidates, {"AAABIGUSDT": 0.6, "BBBSMLUSDT": 0.6, "CCCHIUSDT": 2.1}):
        res = u.scan_venue("binance", equity=100_000)
        assert res["ok"]
        assert res["dropped_by_max_atr"] == 1  # only CCCHI (2.1 > 1.5)
        stored = u.replace_universe.call_args[0][1]
        # AAABIG passes all pre-existing filters AND the cap → stored;
        # BBBSML fails min_volume (additive — still rejected);
        # CCCHI is dropped by the cap.
        assert [r["asset"] for r in stored] == ["AAABIG"]


# ── crash guard (plan tests 5–11, AC4–AC9) ─────────────────────────────────

def test_crash_guard_fires_below_floor(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=96.0, sell_fill=_Fill(fill_price=95.5, fee_amount=0.05))
    manage_open_positions("AAA", ex)
    assert ex.calls["market_sell"] == [("AAA", 5.0)]       # market-sold
    assert ("AAAUSDT", "tp1") in ex.calls["cancel"]        # TP cancelled
    assert ("AAAUSDT", "sl1") in ex.calls["cancel"]        # SL cancelled
    t = _last_closed(db)
    assert t["exit_reason"] == "crash_guard"
    assert t["exit_price"] == 95.5                          # real fill (Constitution V)
    from db.db_ops import load_all_positions
    assert load_all_positions(asset="AAA", venue="binance") == []  # position closed


def test_crash_guard_fill_aware_sl_filled(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=96.0, status={"tp1": "NEW", "sl1": "FILLED"})
    manage_open_positions("AAA", ex)
    assert ex.calls["market_sell"] == []                    # never market-sold
    t = _last_closed(db)
    assert t["exit_reason"] == "sl"                         # real reason, real stop
    assert t["exit_price"] == 99.4


def test_crash_guard_fill_aware_tp_filled(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=96.0, status={"tp1": "FILLED"}, order_fills=(100.5, 0.01))
    manage_open_positions("AAA", ex)
    assert ex.calls["market_sell"] == []                    # never market-sold
    t = _last_closed(db)
    assert t["exit_reason"] == "tp"                         # real reason via real fill
    assert t["exit_price"] == 100.5


def test_crash_guard_none_price_no_action(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=None)                                # API failure (Constitution IV)
    manage_open_positions("AAA", ex)
    assert ex.calls["market_sell"] == []
    assert ex.calls["cancel"] == []
    from db.db_ops import load_all_positions
    assert len(load_all_positions(asset="AAA", venue="binance")) == 1  # kept


def test_crash_guard_stamps_cooldown(db):
    from trading_bot.spot_scalper import manage_open_positions, _cooldown_ok, _last_sl
    db.upsert_setting("max_loss_per_position_pct", "3")
    db.upsert_setting("cooldown_sec", "60")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=96.0, sell_fill=_Fill(fill_price=95.5, fee_amount=0.05))
    manage_open_positions("AAA", ex)
    assert "binance:AAA:long" in _last_sl                   # stamped like an sl exit
    assert not _cooldown_ok("AAA", "long", 60)              # re-entry blocked ~10 min


def test_crash_guard_applies_to_no_sl_price_positions(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    # tp_price is NOT NULL in the schema; sl_price and the SL order id are nullable
    # (a spot position with no stop-loss). The crash floor still applies.
    _seed_position(db, entry=100.0, tp=100.8, sl=None, tp_oid="tp1", sl_oid=None)
    ex = _FakeEx(price=96.0, sell_fill=_Fill(fill_price=95.5, fee_amount=0.05))
    manage_open_positions("AAA", ex)
    assert ex.calls["market_sell"] == [("AAA", 5.0)]        # floor applies even w/o SL
    assert _last_closed(db)["exit_reason"] == "crash_guard"


def test_above_floor_normal_exits_unchanged(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    # Above floor (97.0), below sl_price (99.0) → existing SL path, reason "sl".
    _seed_position(db, entry=100.0, tp=100.8, sl=99.0, tp_oid="tp1", sl_oid="sl1")
    ex = _FakeEx(price=98.5, sell_fill=_Fill(fill_price=98.6, fee_amount=0.05))
    manage_open_positions("AAA", ex)
    assert _last_closed(db)["exit_reason"] == "sl"
    # Above floor, TP already filled → existing TP path, reason "tp".
    _seed_position(db, entry=100.0, tp=100.8, sl=99.0, tp_oid="tp2", sl_oid="sl2", pid="p2")
    ex2 = _FakeEx(price=99.5, status={"tp2": "FILLED"}, order_fills=(100.5, 0.01))
    manage_open_positions("AAA", ex2)
    assert _last_closed(db)["exit_reason"] == "tp"


# ── validation (plan test 12, AC10) ─────────────────────────────────────────

def test_validation_hard_error_vs_equality(db):
    from trade.settings_rules import validate, validate_all
    db.upsert_setting("sl_min_pct_spot", "0.6")
    assert validate("max_loss_per_position_pct", 0.5).level == "error"  # strictly inside
    assert validate("max_loss_per_position_pct", 0.6).level == "ok"     # equality allowed
    assert validate("max_loss_per_position_pct", 3.0).level == "ok"
    assert validate("max_loss_per_position_pct", 0.05).level == "error"  # hard min
    assert validate("max_loss_per_position_pct", 25).level == "error"    # hard max
    res = validate_all()
    assert all(v.level != "error" for v in res.values())  # defaults pass


def test_universe_cap_empty_universe_warn(db):
    from trade.settings_rules import validate
    from db.db_ops import replace_universe
    replace_universe("binance", [{
        "asset": "AAA", "symbol": "AAAUSDT", "rank": 1, "scanned_at": time.time(),
        "quote_volume_24h": 10_000_000, "spread_pct": 0.05,
        "depth_bid_top10": 1_000_000, "depth_ask_top10": 1_000_000,
        "atr_pct_median": 0.5, "signals_count": 30, "recovery_rate": 0.8,
        "median_minutes_to_tp": 5,
    }])
    v = validate("universe_max_atr_pct", 0.3)
    assert v.level == "warn"                                # cap would empty the universe
    assert validate("universe_max_atr_pct", 0.5).level in ("ok", "warn")


# ── dry-run (plan test 14, AC12) ────────────────────────────────────────────

def test_dry_run_unchanged(db):
    from trading_bot.spot_scalper import manage_open_positions
    db.upsert_setting("max_loss_per_position_pct", "3")
    _seed_position(db, entry=100.0, tp=100.8, sl=99.4, tp_oid="tp1", sl_oid="sl1")
    # Dry-run market_sell returns Fill(fill_price=0.0) → xp falls back to entry
    # (same convention as the sl / time_stop dry-run paths).
    ex = _FakeEx(price=96.0, sell_fill=_Fill(fill_price=0.0, fee_amount=0.0))
    manage_open_positions("AAA", ex)
    t = _last_closed(db)
    assert t["exit_reason"] == "crash_guard"
    assert t["exit_price"] == 100.0                          # ep fallback, no new path


# ── dashboard label (plan AC11) ─────────────────────────────────────────────

def test_dashboard_reason_label(db):
    from dashboard.main import REASON_LABELS
    assert REASON_LABELS["crash_guard"] == "Crash guard"
