"""
Unit tests for the deterministic structure engine (spec 001).

Fixtures encode the book's diagrams directly: the valid 3MS reversal
(uptrend -> downtrend, p.34), the misleading variant where the second lower
high violates the criterion-3 bound (p.35), its mirror (p.37), and the
criterion-order rule (failure to make a higher high must precede the
neckline break).
"""
import os, sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from trade.structure import (
    analyze, find_pivots, classify_trend, find_zones, near_zone, next_zone, closed,
)


def candles_from_path(waypoints, steps=4):
    """Interpolate a waypoint path into candles (one final forming candle
    appended, which the engine must drop)."""
    prices = []
    for a, b in zip(waypoints, waypoints[1:]):
        for s in range(steps):
            prices.append(a + (b - a) * s / steps)
    prices.append(waypoints[-1])
    out = []
    for i, p in enumerate(prices):
        out.append({"ts": float(i), "open": p, "high": p + 0.1,
                    "low": p - 0.1, "close": p, "volume": 1.0})
    out.append({"ts": float(len(prices)), "open": prices[-1],
                "high": prices[-1] + 0.1, "low": prices[-1] - 0.1,
                "close": prices[-1], "volume": 1.0})  # forming candle
    return out


# The book's valid up->down reversal: uptrend (lows 105, 108 rising into the
# top A=120), first lower high C=117, neckline break at D=105, second lower
# high E=107 <= bound(108), close retesting the neckline.
VALID_SHORT = [100, 110, 105, 115, 108, 120, 112, 117, 105, 107, 106.4, 107.8]
# p.35 misleading variant: E=112 > bound(108) -> rejected.
MISLEADING_SHORT = [100, 110, 105, 115, 108, 120, 112, 117, 105, 112, 111, 111.5]
# order violation: neckline breaks BEFORE any failure to make a higher high.
ORDER_VIOLATION = [100, 110, 105, 115, 108, 120, 105, 117, 112, 113]


def test_forming_candle_dropped():
    cs = candles_from_path(VALID_SHORT)
    assert len(closed(cs)) == len(cs) - 1


def test_pivots_alternate():
    piv = find_pivots(closed(candles_from_path(VALID_SHORT)))
    for a, b in zip(piv, piv[1:]):
        assert a.kind != b.kind


def test_uptrend_classified():
    piv = find_pivots(closed(candles_from_path([100, 110, 104, 116, 109, 122, 114, 115])))
    assert classify_trend(piv) == "up"


def test_downtrend_classified():
    piv = find_pivots(closed(candles_from_path([122, 110, 118, 104, 112, 98, 104, 103])))
    assert classify_trend(piv) == "down"


def test_valid_3ms_short_confirmed_with_retest():
    s = analyze(candles_from_path(VALID_SHORT))
    assert s.ms_state == "CONFIRMED"
    assert s.direction == "short"
    assert s.neckline == pytest.approx(108, abs=0.3)
    assert s.retest is True


def test_misleading_second_high_rejected():
    """Book p.35: two lower highs form but the second is above the last low
    of the old uptrend -> NOT a confirmed reversal."""
    s = analyze(candles_from_path(MISLEADING_SHORT))
    assert s.ms_state != "CONFIRMED"


def test_criterion_order_enforced():
    """Failure to make a higher high must come before the neckline break."""
    s = analyze(candles_from_path(ORDER_VIOLATION))
    assert s.ms_state != "CONFIRMED"


def test_valid_3ms_long_mirror():
    """Book p.36: mirror image confirms a long reversal."""
    path = [220 - p for p in VALID_SHORT]
    s = analyze(candles_from_path(path))
    assert s.ms_state == "CONFIRMED"
    assert s.direction == "long"
    assert s.neckline == pytest.approx(112, abs=0.3)
    assert s.retest is True


def test_misleading_long_mirror_rejected():
    """Book p.37 mirror-misleading variant rejected."""
    path = [220 - p for p in MISLEADING_SHORT]
    s = analyze(candles_from_path(path))
    assert s.ms_state != "CONFIRMED"


def test_no_retest_when_far_from_neckline():
    path = VALID_SHORT[:-2] + [103, 102]   # confirmed but price fell away
    s = analyze(candles_from_path(path))
    if s.ms_state == "CONFIRMED":
        assert s.retest is False


def test_zones_cluster_and_lookup():
    piv = find_pivots(closed(candles_from_path(
        [100, 110, 100.5, 110.4, 99.8, 110.2, 100.2, 105])))
    zones = find_zones(piv, tolerance_pct=0.75)
    assert any(z.touches >= 2 for z in zones)
    res = [z for z in zones if z.price > 105]
    assert res, "resistance cluster around 110 expected"
    assert near_zone(110.1, zones, 1.0) is not None
    nz = next_zone(105.0, zones, "long")
    assert nz is not None and nz.price > 105


def test_insufficient_data_is_range():
    s = analyze(candles_from_path([100, 101]))
    assert s.trend == "range" and s.ms_state == "NONE"
