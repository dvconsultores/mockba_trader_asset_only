"""
MockbaV4 — Regime detection from 1h and 4h OHLCV candles.

Pure price-and-volume: linear regression slope on close prices.
Volume grades trend strength; it never decides whether a trend exists.
Cached per asset for regime_cache_sec (default 300).

Also owns the 5m candle cache used for ATR, which `last_closed_return_up`
(feature 009 — entry confirmation) reads without issuing its own request.
"""

from __future__ import annotations
import time
import os
from typing import Optional
import requests

from db.db_ops import get_setting_float, get_setting_int


# ── Per-asset cache ───────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, str]] = {}  # key -> (timestamp, regime)


def _cache_key(asset: str, venue: str) -> str:
    return f"{venue}:{asset}"


# ── OHLCV fetching ────────────────────────────────────────────────────────────

def _fetch_binance_ohlcv(symbol: str, interval: str, limit: int = 30) -> list[dict]:
    """Fetch OHLCV from Binance. Returns list of {open, high, low, close, volume}."""
    url = "https://api.binance.com/api/v3/klines"
    resp = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    resp.raise_for_status()
    candles = []
    for k in resp.json():
        candles.append({
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


def _fetch_orderly_ohlcv(symbol: str, interval: str, limit: int = 30) -> list[dict]:
    """Fetch OHLCV from Orderly Network. Returns list of {open, high, low, close, volume}."""
    url = "https://api-evm.orderly.org/v1/public/kline"
    resp = requests.get(url, params={"symbol": symbol, "type": interval, "limit": limit}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", {}).get("rows", [])
    candles = []
    for r in rows:
        candles.append({
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        })
    return candles


def _fetch_ohlcv(venue: str, symbol: str, interval: str, limit: int = 30) -> list[dict]:
    if venue == "binance":
        return _fetch_binance_ohlcv(symbol, interval, limit)
    else:
        return _fetch_orderly_ohlcv(symbol, interval, limit)


# ── Slope computation ─────────────────────────────────────────────────────────

def _linreg_slope(closes: list[float]) -> float:
    """Linear regression slope as fractional change per candle.

    Returns slope = Δprice / price_mean / candle_count.
    Positive = uptrend, negative = downtrend.
    """
    n = len(closes)
    if n < 2:
        return 0.0
    mean_close = sum(closes) / n
    if mean_close == 0:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = mean_close
    num = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return (num / den) / mean_close  # fractional slope per candle


# ── Volume strength ───────────────────────────────────────────────────────────

def _volume_ratio(candles: list[dict], recent_n: int = 5) -> float:
    """Ratio of recent volume to all-period average. >1 = above average."""
    if len(candles) < recent_n:
        return 1.0
    avg_all = sum(c["volume"] for c in candles) / len(candles)
    avg_recent = sum(c["volume"] for c in candles[-recent_n:]) / recent_n
    if avg_all == 0:
        return 1.0
    return avg_recent / avg_all


# ── Classification ────────────────────────────────────────────────────────────

def _classify_regime(
    slope_1h: float, vol_ratio_1h: float,
    slope_4h: float, vol_ratio_4h: float,
    slope_threshold: float,
) -> str:
    """Classify regime from 1h and 4h slopes plus volume strength.

    Slope alone determines trend existence. Volume grades strength (logged, not used for classification).
    1h and 4h disagreeing → fall back to RANGE (conservative).
    """
    trend_up_1h = slope_1h > slope_threshold
    trend_down_1h = slope_1h < -slope_threshold
    trend_up_4h = slope_4h > slope_threshold
    trend_down_4h = slope_4h < -slope_threshold

    # Both agree on uptrend
    if trend_up_1h and trend_up_4h:
        return "TREND_UP"

    # Both agree on downtrend
    if trend_down_1h and trend_down_4h:
        return "TREND_DOWN"

    # 1h trending but 4h disagrees → RANGE (conservative)
    if trend_up_1h and not trend_up_4h:
        return "RANGE"
    if trend_down_1h and not trend_down_4h:
        return "RANGE"

    # Neither trending → RANGE
    return "RANGE"


# ── Public API ────────────────────────────────────────────────────────────────

def detect_regime(asset: str, venue: str) -> str:
    """
    Returns "RANGE" | "TREND_UP" | "TREND_DOWN" | "UNKNOWN".

    Fetches 1h and 4h OHLCV, computes slopes, classifies.
    Cached per asset for regime_cache_sec (default 300).
    """
    key = _cache_key(asset, venue)
    cache_sec = get_setting_int("regime_cache_sec", 300)
    now = time.time()

    if key in _cache:
        cached_at, cached_regime = _cache[key]
        if now - cached_at < cache_sec:
            return cached_regime

    slope_threshold = get_setting_float("slope_threshold", 0.0012)

    # Derive exchange symbol.
    # Orderly kline endpoint is unreliable — use Binance OHLCV as proxy
    # (consistent with _get_obi_orderly / _get_live_price_orderly in bot.py).
    fetch_venue = "binance" if venue == "orderly" else venue
    symbol = f"{asset}USDT" if fetch_venue == "binance" else f"PERP_{asset}_USDC"

    try:
        candles_1h = _fetch_ohlcv(fetch_venue, symbol, "1h", 30)
        candles_4h = _fetch_ohlcv(fetch_venue, symbol, "4h", 30)

        if len(candles_1h) < 5 or len(candles_4h) < 5:
            regime = "UNKNOWN"
        else:
            closes_1h = [c["close"] for c in candles_1h]
            closes_4h = [c["close"] for c in candles_4h]
            slope_1h = _linreg_slope(closes_1h)
            slope_4h = _linreg_slope(closes_4h)
            vol_1h = _volume_ratio(candles_1h)
            vol_4h = _volume_ratio(candles_4h)
            regime = _classify_regime(slope_1h, vol_1h, slope_4h, vol_4h, slope_threshold)
    except Exception:
        # API failure → keep cache if it exists, else UNKNOWN
        if key in _cache:
            return _cache[key][1]
        regime = "UNKNOWN"

    _cache[key] = (now, regime)
    return regime


def invalidate_cache(asset: str | None = None, venue: str | None = None):
    """Clear cached regimes, optionally scoped to asset/venue."""
    if asset is None and venue is None:
        _cache.clear()
        _candle_cache.clear()
        return
    to_delete = []
    for key in _cache:
        if asset and asset not in key:
            continue
        if venue and venue not in key:
            continue
        to_delete.append(key)
    for key in to_delete:
        del _cache[key]


# ── ATR from 5m candles (Amendment 001) ──────────────────────────────────────

_candle_cache: dict[str, tuple[float, list[dict]]] = {}  # key -> (ts, candles)


def _candle_cache_key(asset: str, venue: str, interval: str) -> str:
    return f"{venue}:{asset}:{interval}"


def get_atr_pct(asset: str, venue: str) -> float | None:
    """Compute ATR as % of current price from 5m candles. Cached per candle_cache_sec."""
    cache_sec = get_setting_int("candle_cache_sec", 60)
    period = get_setting_int("atr_period", 14)
    key = _candle_cache_key(asset, venue, "5m")
    now = time.time()

    if key in _candle_cache:
        cached_at, candles = _candle_cache[key]
        if now - cached_at < cache_sec and len(candles) >= period:
            return _compute_atr_pct(candles, period)

    symbol = f"{asset}USDT" if venue == "binance" else f"PERP_{asset}_USDC"
    # Orderly kline endpoint is unreliable — use Binance OHLCV as proxy
    fetch_venue = "binance" if venue == "orderly" else venue
    symbol_atr = f"{asset}USDT" if fetch_venue == "binance" else f"PERP_{asset}_USDC"
    try:
        candles = _fetch_ohlcv(fetch_venue, symbol_atr, "5m", period + 5)
        _candle_cache[key] = (now, candles)
        if len(candles) >= period:
            return _compute_atr_pct(candles, period)
    except Exception:
        if key in _candle_cache:
            _, old = _candle_cache[key]
            if len(old) >= period:
                return _compute_atr_pct(old, period)
    return None


def _compute_atr_pct(candles: list[dict], period: int = 14) -> float | None:
    """ATR as percentage of current price."""
    n = min(period, len(candles))
    if n < 2:
        return None
    tr_values = []
    for i in range(1, n):
        c = candles[-n + i]
        prev = candles[-n + i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        tr_values.append(tr)
    atr = sum(tr_values) / len(tr_values)
    price = candles[-1]["close"]
    if price <= 0:
        return None
    return (atr / price) * 100


# ── Entry confirmation (feature 009) ─────────────────────────────────────────

def last_closed_return_up(asset: str, venue: str) -> bool | None:
    """True when the last CLOSED 5m bar's return is positive (close > open).

    The dip rule measures displacement only, so it fires while price is still
    falling; this answers "has the fall paused?". Stated as a sign test on the
    last completed return — not candlestick pattern detection (Constitution I).

    Shares the ATR 5m cache: on a miss or a stale entry it calls get_atr_pct,
    which is the sole owner of the fetch / cache-write / stale-fallback path, so
    there is no second error branch to diverge. When adaptive_enabled is true
    (the live configuration) get_atr_pct already ran this cycle, making this a
    dict hit with zero requests.

    A flat bar is NOT confirmation. Returns None when the series is unavailable
    or too short — indeterminate never counts as confirmed (Constitution IV).
    """
    key = _candle_cache_key(asset, venue, "5m")
    cached = _candle_cache.get(key)
    if cached is None or (time.time() - cached[0]) >= get_setting_int("candle_cache_sec", 60):
        get_atr_pct(asset, venue)
        cached = _candle_cache.get(key)
    if cached is None or len(cached[1]) < 2:
        return None
    bar = cached[1][-2]  # [-1] is the in-progress bar
    return bar["close"] > bar["open"]
