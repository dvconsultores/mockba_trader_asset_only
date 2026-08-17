"""
MockbaV4 — deterministic market-structure engine (spec 001, the 3MS principle).

Everything objective lives here: swing pivots, trend classification, key
support/resistance zones, the 3MS reversal state machine, and retest
detection. The AI judge only *verifies* candidates this engine produces — it
never invents structure.

Candles are dicts {"ts", "open", "high", "low", "close", "volume"}; the last
element may be a forming candle and is ignored (closed candles only).

3MS (3 Market Structure), uptrend → downtrend (mirror for down → up):
  1. the market fails to make a higher high — must occur FIRST;
  2. price breaks the last low of the uptrend (the neckline / X line);
  3. two lower highs form, and the second must NOT be higher than the last
     low of the previous uptrend (the book's misleading variants, pp. 35/37,
     violate exactly this bound and are rejected).
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict


@dataclass
class Pivot:
    kind: str      # 'H' | 'L'
    index: int     # candle index
    price: float
    ts: float


@dataclass
class Zone:
    price: float       # zone center
    touches: int
    kind: str          # 'support' | 'resistance' | 'mixed'


@dataclass
class Structure:
    trend: str                     # 'up' | 'down' | 'range'
    pivots: list[Pivot] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    ms_state: str = "NONE"         # NONE|FAIL_HH|NECK_BREAK|CONFIRMED (and _UP variants)
    direction: str | None = None   # reversal direction: 'short' (up→down) | 'long' (down→up)
    neckline: float | None = None
    bound: float | None = None     # criterion-3 bound (old last low/high)
    retest: bool = False           # price currently retesting the broken neckline
    last_close: float = 0.0

    def packet(self) -> str:
        d = asdict(self)
        d["pivots"] = d["pivots"][-10:]
        return json.dumps(d, default=float)


def closed(candles: list[dict]) -> list[dict]:
    """Drop the last (possibly forming) candle."""
    return candles[:-1] if len(candles) > 1 else []


# ── Pivots ────────────────────────────────────────────────────────────────────

def find_pivots(candles: list[dict], k: int = 2) -> list[Pivot]:
    """Fractal swing points: a high (low) strictly above (below) its k
    neighbors on each side, reduced to a strictly alternating H/L sequence
    (keeping the more extreme of same-kind neighbors)."""
    raw: list[Pivot] = []
    n = len(candles)
    for i in range(k, n - k):
        hs = [candles[j]["high"] for j in range(i - k, i + k + 1)]
        ls = [candles[j]["low"] for j in range(i - k, i + k + 1)]
        if candles[i]["high"] == max(hs) and hs.count(candles[i]["high"]) == 1:
            raw.append(Pivot("H", i, candles[i]["high"], candles[i]["ts"]))
        elif candles[i]["low"] == min(ls) and ls.count(candles[i]["low"]) == 1:
            raw.append(Pivot("L", i, candles[i]["low"], candles[i]["ts"]))
    out: list[Pivot] = []
    for p in raw:
        if out and out[-1].kind == p.kind:
            better = (p.price > out[-1].price) if p.kind == "H" else (p.price < out[-1].price)
            if better:
                out[-1] = p
        else:
            out.append(p)
    return out


# ── Trend (book minimums: uptrend = 1 HH + 2 HL; downtrend mirror) ───────────

def classify_trend(pivots: list[Pivot]) -> str:
    highs = [p for p in pivots if p.kind == "H"][-3:]
    lows = [p for p in pivots if p.kind == "L"][-3:]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl2 = len(lows) >= 3 and lows[-1].price > lows[-2].price > lows[-3].price
        hl1 = lows[-1].price > lows[-2].price
        if hh and (hl2 or hl1):
            return "up"
        lh = highs[-1].price < highs[-2].price
        ll2 = len(highs) >= 3 and highs[-1].price < highs[-2].price < highs[-3].price
        if lh and lows[-1].price < lows[-2].price:
            return "down"
    return "range"


# ── Key zones (pivot clustering; the book insists on areas, not lines) ───────

def find_zones(pivots: list[Pivot], tolerance_pct: float = 0.75, min_touches: int = 2) -> list[Zone]:
    zones: list[list[Pivot]] = []
    for p in sorted(pivots, key=lambda p: p.price):
        if zones and abs(p.price - zones[-1][0].price) / zones[-1][0].price * 100 <= tolerance_pct:
            zones[-1].append(p)
        else:
            zones.append([p])
    out = []
    for group in zones:
        if len(group) >= min_touches:
            kinds = {p.kind for p in group}
            kind = "mixed" if len(kinds) == 2 else ("resistance" if "H" in kinds else "support")
            center = sum(p.price for p in group) / len(group)
            out.append(Zone(center, len(group), kind))
    return sorted(out, key=lambda z: z.price)


def near_zone(price: float, zones: list[Zone], tolerance_pct: float = 1.0) -> Zone | None:
    for z in zones:
        if abs(price - z.price) / z.price * 100 <= tolerance_pct:
            return z
    return None


def next_zone(price: float, zones: list[Zone], direction: str) -> Zone | None:
    """Nearest zone beyond price in the trade direction (the TP anchor)."""
    if direction == "long":
        cands = [z for z in zones if z.price > price * 1.005]
        return min(cands, key=lambda z: z.price) if cands else None
    cands = [z for z in zones if z.price < price * 0.995]
    return max(cands, key=lambda z: z.price) if cands else None


# ── 3MS state machine ────────────────────────────────────────────────────────

def _detect_3ms_short(pivots: list[Pivot], last_close: float, retest_tol: float):
    """Uptrend → downtrend. Walk pivots after the absolute top; enforce
    criterion order and the criterion-3 bound. Returns (state, neckline,
    bound, retest) or None if no uptrend context."""
    highs = [p for p in pivots if p.kind == "H"]
    if len(highs) < 2:
        return None
    # A = the absolute top of the prior uptrend
    a_idx = max(range(len(pivots)), key=lambda i: pivots[i].price if pivots[i].kind == "H" else -1e18)
    a = pivots[a_idx]
    before = pivots[:a_idx]
    up_lows = [p for p in before if p.kind == "L"]
    if len(up_lows) < 2 or not (a.price > max((p.price for p in before if p.kind == "H"), default=-1e18)):
        return None
    if not (up_lows[-1].price > up_lows[-2].price):     # needed HLs into the top
        return None
    neckline = up_lows[-1].price                        # last low of the uptrend (X line)
    bound = neckline                                    # criterion-3 bound
    after = pivots[a_idx + 1:]
    lower_highs = [p for p in after if p.kind == "H" and p.price < a.price]
    if not lower_highs:
        return ("TREND_UP", neckline, bound, False)
    # criterion 1 first: the first post-A high must be a lower high and must
    # occur before the neckline break
    first_lh = lower_highs[0]
    broke = any(p.kind == "L" and p.price < neckline for p in after) or last_close < neckline
    broke_before_fail = any(
        p.kind == "L" and p.price < neckline and p.index < first_lh.index for p in after
    )
    if broke_before_fail:
        return ("ORDER_VIOLATION", neckline, bound, False)
    if not broke:
        return ("FAIL_HH", neckline, bound, False)
    # criterion 3: two lower highs, second not higher than the bound
    if len(lower_highs) >= 2:
        second = lower_highs[1]
        if second.price <= bound:
            retest = abs(last_close - neckline) / neckline * 100 <= retest_tol
            return ("CONFIRMED", neckline, bound, retest)
        return ("BOUND_VIOLATION", neckline, bound, False)
    return ("NECK_BREAK", neckline, bound, False)


def _mirror(candles: list[dict]) -> list[dict]:
    """Price-invert candles so the long detector can reuse the short one."""
    out = []
    for c in candles:
        out.append({"ts": c["ts"], "open": -c["open"], "high": -c["low"],
                    "low": -c["high"], "close": -c["close"], "volume": c.get("volume", 0)})
    return out


def analyze(candles: list[dict], pivot_k: int = 2, zone_tol: float = 0.75,
            retest_tol: float = 1.0) -> Structure:
    """Full deterministic analysis of one timeframe's closed candles."""
    cs = closed(candles)
    if len(cs) < pivot_k * 2 + 5:
        return Structure(trend="range")
    pivots = find_pivots(cs, pivot_k)
    last_close = cs[-1]["close"]
    s = Structure(trend=classify_trend(pivots), pivots=pivots,
                  zones=find_zones(pivots, zone_tol), last_close=last_close)
    short = _detect_3ms_short(pivots, last_close, retest_tol)
    if short and short[0] == "CONFIRMED":
        s.ms_state, s.neckline, s.bound, s.retest = short[0], short[1], short[2], short[3]
        s.direction = "short"
        return s
    m_pivots = find_pivots(_mirror(cs), pivot_k)
    long_ = _detect_3ms_short(m_pivots, -last_close, retest_tol)
    if long_ and long_[0] == "CONFIRMED":
        s.ms_state = "CONFIRMED"
        s.neckline = -long_[1]
        s.bound = -long_[2]
        s.retest = long_[3]
        s.direction = "long"
        return s
    # report the more advanced partial state for observability
    states = {"NONE": 0, "TREND_UP": 1, "FAIL_HH": 2, "NECK_BREAK": 3,
              "ORDER_VIOLATION": 1, "BOUND_VIOLATION": 1}
    cand = []
    if short:
        cand.append((states.get(short[0], 0), short[0], short[1], "short"))
    if long_:
        cand.append((states.get(long_[0], 0), long_[0], -long_[1], "long"))
    if cand:
        rank, state, neck, direction = max(cand)
        if rank > 0:
            s.ms_state, s.neckline = state, neck
            s.direction = direction if rank >= 2 else None
    return s
