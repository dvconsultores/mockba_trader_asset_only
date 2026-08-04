"""
Unit tests for Amendment 003 — Dynamic asset universe & Capital view.

Covers the acceptance criteria that are unit-testable without exchange access:
  - shared threshold function used by scalpers AND replay (patch test)
  - replay recovery_rate on a synthetic series
  - universe_min_recovery_rate='auto' breakeven resolution
  - hard filters (failing symbol never reaches depth stage)
  - min_signals exclusion / DEX short-store
  - blacklist survives a rescan
  - rate-limit exhaustion preserves the previous universe
  - per-venue slot sizing from live equity (never capital_*)
  - per-venue fee net-edge validation
  - universe cross-checks in the validator
"""

import os
import sys
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


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk(close, high=None, low=None, open_=None):
    return {
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": 1000.0,
    }


def _synthetic_recovery_candles():
    """40 flat candles at 100 → one candle dips to 95 → recover to 100.

    The dip (5% below the rolling peak) exceeds the adaptive dip threshold
    and TP is reached within the hold window. Every entry recovers (the price
    climbs straight back to 100), so recovery_rate == 1.0.
    """
    candles = [_mk(100.0) for _ in range(40)]
    candles.append(_mk(95.0, high=100.0, low=95.0, open_=100.0))   # dip candle
    candles.append(_mk(97.0, high=97.0, low=95.0, open_=95.0))
    candles.append(_mk(99.0, high=99.0, low=97.0, open_=97.0))
    candles.append(_mk(100.0, high=100.0, low=99.0, open_=99.0))
    return candles


# ── shared threshold function ────────────────────────────────────────────────

def test_compute_thresholds_parity(db):
    from trade.universe import compute_thresholds
    atr, dk, dm, pk, pm, tk, tm, sk, sm = 1.0, 0.5, 0.15, 0.5, 0.15, 1.0, 0.8, 0.6, 0.5
    dn, pn, te, se = compute_thresholds(atr, dk, dm, pk, pm, tk, tm, sk, sm)
    assert dn == max(dk * atr, dm)
    assert pn == max(pk * atr, pm)
    assert te == max(tk * atr, tm)
    assert se == max(sk * atr, sm)
    # no-ATR branch equals the floor values
    assert compute_thresholds(None, dk, dm, pk, pm, tk, tm, sk, sm) == (dm, pm, tm, sm)


def test_scalpers_share_threshold_function(db):
    """Both live scalpers import the SAME function object as the replay —
    a replay can never diverge from live entry logic."""
    import trade.universe as universe
    import trading_bot.spot_scalper as spot
    import trading_bot.futures_scalper as fut
    assert spot.compute_thresholds is universe.compute_thresholds
    assert fut.compute_thresholds is universe.compute_thresholds


def test_replay_uses_shared_threshold_function(db):
    """Patching the shared function must be observed by the replay call site."""
    import trade.universe as universe
    calls = []
    orig = universe.compute_thresholds

    def spy(*a, **k):
        calls.append(a)
        return orig(*a, **k)

    universe.compute_thresholds = spy
    try:
        m = universe.replay_symbol(_synthetic_recovery_candles(),
                                   atr_period=14, dip_k=0.5, dip_min_pct=0.15,
                                   tp_k=1.0, tp_min_pct=0.8, max_hold_minutes=60)
    finally:
        universe.compute_thresholds = orig
    assert calls, "replay did not go through the shared threshold function"


# ── replay ───────────────────────────────────────────────────────────────────

def test_replay_known_recovery_rate(db):
    from trade.universe import replay_symbol
    m = replay_symbol(_synthetic_recovery_candles(),
                      atr_period=14, dip_k=0.5, dip_min_pct=0.15,
                      tp_k=1.0, tp_min_pct=0.8, max_hold_minutes=60)
    assert m["signals_count"] >= 1
    assert m["recovery_rate"] == pytest.approx(1.0)
    assert m["median_minutes_to_tp"] is not None
    assert m["median_minutes_to_tp"] <= 60


def test_replay_no_recovery_when_tp_never_reached(db):
    """A monotonic decline (each candle high = previous close) never reaches
    TP above the entry — recovery_rate 0."""
    from trade.universe import replay_symbol
    candles = [_mk(100.0) for _ in range(40)]
    prev = 100.0
    for _ in range(30):  # each candle: high = prev close, close 0.5 lower
        candles.append(_mk(prev - 0.5, high=prev, low=prev - 0.5, open_=prev))
        prev -= 0.5
    m = replay_symbol(candles, atr_period=14, dip_k=0.5, dip_min_pct=0.15,
                      tp_k=1.0, tp_min_pct=0.8, max_hold_minutes=60)
    assert m["signals_count"] >= 1
    assert m["recovery_rate"] == pytest.approx(0.0)


# ── min recovery rate ('auto') ───────────────────────────────────────────────

def test_auto_recovery_rate_resolution(db):
    from trade import universe
    db.upsert_setting("tp_min_pct", "0.8")
    db.upsert_setting("sl_min_pct", "0.5")
    db.upsert_setting("dex_round_trip_fee_pct", "0.06")
    expected = (0.5 + 0.06) / (0.8 + 0.5)
    assert universe.min_recovery_rate("orderly") == pytest.approx(expected)

    # changes when tp_min_pct changes
    db.upsert_setting("tp_min_pct", "1.2")
    assert universe.min_recovery_rate("orderly") == pytest.approx((0.5 + 0.06) / (1.2 + 0.5))
    assert universe.min_recovery_rate("orderly") != pytest.approx(expected)

    # literal value overrides 'auto'
    db.upsert_setting("universe_min_recovery_rate", "0.5")
    assert universe.min_recovery_rate("orderly") == pytest.approx(0.5)


def test_breakeven_uses_venue_fee(db):
    """DEX and CEX breakeven differ because their fee rates differ."""
    from trade import universe
    db.upsert_setting("tp_min_pct", "0.8")
    db.upsert_setting("sl_min_pct", "0.5")
    db.upsert_setting("dex_round_trip_fee_pct", "0.06")
    db.upsert_setting("cex_round_trip_fee_pct", "0.20")
    assert universe.breakeven_recovery_rate("orderly") != universe.breakeven_recovery_rate("binance")


# ── hard filters ─────────────────────────────────────────────────────────────

def test_hard_filters_pass(db):
    from trade.universe import _hard_filters_pass
    c = {"quote_volume_24h": 10_000_000, "spread_pct": 0.05, "rank": 30, "min_notional": 10}
    args = dict(tp_min=0.8, rank_min=15, rank_max=90, min_volume=5_000_000,
                spread_ratio_max=0.10, slot_size=1000)
    assert _hard_filters_pass(c, **args)
    # volume below minimum
    assert not _hard_filters_pass({**c, "quote_volume_24h": 1_000_000}, **args)
    # spread above tp × ratio (0.8 × 0.10 = 0.08%)
    assert not _hard_filters_pass({**c, "spread_pct": 0.5}, **args)
    # outside rank band
    assert not _hard_filters_pass({**c, "rank": 5}, **args)
    assert not _hard_filters_pass({**c, "rank": 200}, **args)
    # min_notional × 1.5 not fundable at slot size
    assert not _hard_filters_pass({**c, "min_notional": 1000}, **args)


# ── stage 5 selection ────────────────────────────────────────────────────────

def _checked(asset, rank):
    return {"asset": asset, "symbol": f"{asset}USDT", "rank": rank,
            "quote_volume_24h": 10_000_000, "spread_pct": 0.05,
            "depth_bid_top10": 100_000, "depth_ask_top10": 100_000}


def test_min_signals_excludes_regardless_of_recovery(db):
    from trade.universe import select_ranked
    checked = [_checked("AAA", 1), _checked("BBB", 2)]
    metrics = {
        "AAA": {"signals_count": 5, "recovery_rate": 0.9, "atr_pct_median": 1.0,
                "median_minutes_to_tp": 10},
        "BBB": {"signals_count": 40, "recovery_rate": 0.8, "atr_pct_median": 1.5,
                "median_minutes_to_tp": 8},
    }
    rows = select_ranked(checked, metrics, min_signals=20, min_rec=0.3, size=20, scanned_at=1.0)
    assert [r["asset"] for r in rows] == ["BBB"]


def test_dex_short_store_without_loosening_filters(db):
    """A venue with fewer qualifying symbols than universe_size stores what
    qualifies — filters are not loosened to fill a quota."""
    from trade.universe import select_ranked
    checked = [_checked("AAA", 1), _checked("BBB", 2), _checked("CCC", 3)]
    metrics = {
        a: {"signals_count": 30, "recovery_rate": 0.6, "atr_pct_median": 1.0,
            "median_minutes_to_tp": 10}
        for a in ("AAA", "BBB", "CCC")
    }
    rows = select_ranked(checked, metrics, min_signals=20, min_rec=0.5, size=20, scanned_at=1.0)
    assert len(rows) == 3  # fewer than size=20, all stored


def test_ranking_by_recovery_then_atr(db):
    from trade.universe import select_ranked
    checked = [_checked("AAA", 1), _checked("BBB", 2), _checked("CCC", 3)]
    metrics = {
        "AAA": {"signals_count": 30, "recovery_rate": 0.6, "atr_pct_median": 1.0,
                "median_minutes_to_tp": 10},
        "BBB": {"signals_count": 30, "recovery_rate": 0.8, "atr_pct_median": 0.5,
                "median_minutes_to_tp": 10},
        "CCC": {"signals_count": 30, "recovery_rate": 0.8, "atr_pct_median": 2.0,
                "median_minutes_to_tp": 10},
    }
    rows = select_ranked(checked, metrics, min_signals=20, min_rec=0.3, size=20, scanned_at=1.0)
    assert [r["asset"] for r in rows] == ["CCC", "BBB", "AAA"]  # rec then atr desc


# ── blacklist survives rescan ────────────────────────────────────────────────

def test_blacklist_survives_rescan(db):
    from db.db_ops import replace_universe, get_universe, set_blacklist
    rows = [{"asset": "AAA", "symbol": "AAAUSDT", "rank": 1, "scanned_at": 1.0,
             "quote_volume_24h": 1e7, "spread_pct": 0.05, "recovery_rate": 0.8,
             "signals_count": 30}]
    replace_universe("binance", rows)
    assert set_blacklist("binance", "AAA", True)
    assert get_universe("binance", include_blacklisted=False) == []
    # rescan replaces wholesale, blacklist carried forward
    replace_universe("binance", [dict(r, scanned_at=2.0) for r in rows])
    stored = get_universe("binance", include_blacklisted=True)
    assert stored[0]["blacklisted"] == 1


# ── rate-limit exhaustion preserves previous universe ────────────────────────

def test_budget_exhaustion_aborts_scan(db):
    import trade.universe as u
    for k, v in {
        "tp_min_pct": "0.8", "sl_min_pct": "0.5", "universe_rank_min": "1",
        "universe_rank_max": "90", "universe_min_volume_usd": "5000000",
        "universe_spread_ratio_max": "0.10", "universe_depth_slot_multiple": "3",
        "cex_slot_pct": "10", "universe_min_signals": "1", "universe_size": "20",
    }.items():
        db.upsert_setting(k, v)

    # ranks are recomputed inside scan_venue (1, 2 for two candidates)
    candidates = [
        {"asset": "AAA", "symbol": "AAAUSDT", "quote_volume_24h": 10_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
        {"asset": "BBB", "symbol": "BBBUSDT", "quote_volume_24h": 10_000_000,
         "spread_pct": 0.05, "min_notional": 10, "bid": 100, "ask": 100.05},
    ]
    with mock.patch.object(u, "_fetch_candidates", return_value=candidates), \
         mock.patch.object(u, "_fetch_depth", return_value={"bid": 1_000_000, "ask": 1_000_000}), \
         mock.patch.object(u, "replace_universe") as rep:
        res = u.scan_venue("binance", equity=100_000, depth_budget=1)  # only 1 depth call
        assert not res["ok"]
        assert "budget" in res["reason"]
        rep.assert_not_called()  # previous universe preserved — no partial write


# ── slot sizing (capital model) ──────────────────────────────────────────────

def test_slot_sizing_uses_live_equity_not_capital(db):
    from trade.pnl import compute_slot_size, _day_cache, _day_cache_date
    _day_cache.clear()
    _day_cache_date.clear()
    db.upsert_setting("cex_slot_pct", "10")
    slot = compute_slot_size("binance", equity=50_000, min_notional=5, capital=1_000_000)
    assert slot == pytest.approx(5_000)  # 10% × live equity, NOT the capital arg

    _day_cache.clear()
    _day_cache_date.clear()
    db.upsert_setting("dex_slot_pct", "20")
    slot2 = compute_slot_size("orderly", equity=10_000, min_notional=5)
    assert slot2 == pytest.approx(2_000)

    # min-notional floor
    _day_cache.clear()
    _day_cache_date.clear()
    db.upsert_setting("cex_slot_pct", "1")
    slot3 = compute_slot_size("binance", equity=100, min_notional=10)
    assert slot3 == pytest.approx(15.0)  # max(1% × 100, 10 × 1.5)


# ── per-venue fee net-edge validation ────────────────────────────────────────

def test_per_venue_net_edge_validation(db):
    from trade.settings_rules import validate, SettingsContext
    db.upsert_setting("tp_min_pct", "0.8")
    db.upsert_setting("sl_min_pct", "0.5")
    db.upsert_setting("assumed_slippage_pct", "0.03")
    db.upsert_setting("min_net_edge_pct", "0.30")
    db.upsert_setting("dex_round_trip_fee_pct", "0.06")   # net = 0.71 → passes
    db.upsert_setting("cex_round_trip_fee_pct", "0.55")   # net = 0.22 → fails
    v = validate("dex_round_trip_fee_pct", 0.06, SettingsContext(venue="orderly"))
    assert v.level == "ok"
    v2 = validate("cex_round_trip_fee_pct", 0.55, SettingsContext(venue="binance"))
    assert v2.level == "error"


# ── validator universe cross-checks ──────────────────────────────────────────

def test_universe_cross_checks(db):
    from trade.settings_rules import validate
    db.upsert_setting("universe_rank_max", "90")
    assert validate("universe_rank_min", 95).level == "error"
    db.upsert_setting("universe_scan_interval_hours", "24")
    assert validate("universe_max_age_hours", 12).level == "error"  # guarantees staleness
    db.upsert_setting("universe_spread_ratio_max", "0.30")
    assert validate("universe_spread_ratio_max", 0.30).level == "warn"


def test_max_slots_times_slot_pct_validation(db):
    from trade.settings_rules import validate
    db.upsert_setting("cex_slot_pct", "20")
    assert validate("max_slots_cex", 10).level == "error"  # 10 × 20 = 200 > 100
    assert validate("max_slots_cex", 4).level == "ok"      # 4 × 20 = 80 <= 100
