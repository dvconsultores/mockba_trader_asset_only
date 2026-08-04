"""
MockbaV4 — Dynamic asset universe scanner (Amendment 003).

Runs once per universe_scan_interval_hours (default 24), and on startup if the
stored scan is older than that. NEVER runs inside the trading cycle — bot.py
owns a dedicated background thread for it.

Pipeline (per venue):
  Stage 1  Candidate set — 2 whole-exchange market-data calls (bookTicker +
           24hr ticker) + exchange-info for status/min_notional.
  Stage 2  Hard filters — volume, spread vs TP, volume-rank band, fundability.
  Stage 3  Depth check (survivors only) — top-10 depth both sides; token-bucket
           rate limited; budget exhaustion aborts and preserves the previous
           universe (no partial write).
  Stage 4  Strategy replay — replay the live entry rule over recent 5m candles.
  Stage 5  Rank & store — reject low recovery/signal counts; rank by recovery,
           tiebreak by ATR; store top-N; blacklist carried forward.

The replay MUST reuse the live threshold functions, not reimplement them.
`compute_thresholds` is the shared function the live scalpers also call, so a
replay can never diverge from the strategy actually running.
"""

from __future__ import annotations
import time
from collections import deque
from statistics import median
from typing import Callable, Optional

import requests

from db.db_ops import (
    get_setting, get_setting_float, get_setting_int,
    get_universe_scan_age, replace_universe, get_venue_equity,
)
from trade.regime import _fetch_ohlcv

# ═════════════════════════════════════════════════════════════════════════════
# Shared threshold functions — the live scalpers and the replay both call these.
# ═════════════════════════════════════════════════════════════════════════════

WINDOW_SIZE = 40   # must match spot_scalper / futures_scalper
WARMUP = 10        # min candles before the dip rule fires (matches _is_dip)


def compute_thresholds(atr, dk, dm, pk, pm, tk, tm, sk, sm):
    """Adaptive threshold computation (Amendment 001). Shared by the live
    scalpers (spot_scalper / futures_scalper) and the universe replay, so the
    replay evaluates exactly the strategy being run.

    Returns (dip_needed, pump_needed, tp_effective, sl_effective).
    """
    if atr and atr > 0:
        dn = max(dk * atr, dm)
        pn = max(pk * atr, pm)
        te = max(tk * atr, tm)
        se = max(sk * atr, sm)
    else:
        dn, pn, te, se = dm, pm, tm, sm
    return dn, pn, te, se


# ═════════════════════════════════════════════════════════════════════════════
# Recovery-rate helpers
# ═════════════════════════════════════════════════════════════════════════════

def venue_fee_pct(venue: str) -> float:
    """Per-venue round-trip fee % (Amendment 003 — fees are first-class per venue)."""
    key = "dex_round_trip_fee_pct" if venue == "orderly" else "cex_round_trip_fee_pct"
    default = 0.06 if venue == "orderly" else 0.20
    return get_setting_float(key, default)


def breakeven_recovery_rate(venue: str) -> float:
    """Implied breakeven win rate from current settings, using the venue's own
    fee rate: (sl + fee) / (tp + sl)."""
    tp = get_setting_float("tp_min_pct", 0.8)
    sl = get_setting_float("sl_min_pct", 0.5)
    fee = venue_fee_pct(venue)
    denom = tp + sl
    if denom <= 0:
        return 0.0
    return (sl + fee) / denom


def min_recovery_rate(venue: str) -> float:
    """Resolve universe_min_recovery_rate.

    'auto' (default) → the breakeven win rate implied by current settings,
    which changes when tp_min_pct / sl_min_pct / the venue fee change.
    A literal value (0-1) overrides.
    """
    raw = get_setting("universe_min_recovery_rate") or "auto"
    try:
        return float(raw)
    except (ValueError, TypeError):
        return breakeven_recovery_rate(venue)


# ═════════════════════════════════════════════════════════════════════════════
# Stage 1 — candidate set (whole-exchange calls)
# ═════════════════════════════════════════════════════════════════════════════

BINANCE_API = "https://api.binance.com/api/v3"
ORDERLY_API = "https://api-evm.orderly.org/v1/public"

# Base assets that are stablecoins / fiat / liquidity tokens — never tradeable
# for a mean-reversion scalper.
_NON_TRADEABLE_BASE = {
    "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "AEUR",
    "EUR", "GBP", "USTC", "PYUSD", "USDE", "USD1", "EURI", "EURT", "WBTC",
}
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "5L", "5S", "3L", "3S",
                       "TVOL", "BVOL", "2L", "2S")


def _is_spot_tradable(base: str, quote: str, status: str = "TRADING",
                      spot_allowed: bool = True) -> bool:
    """Basic symbol sanity: quote currency, no stablecoins/leveraged tokens."""
    if quote != "USDT":
        return False
    if base in _NON_TRADEABLE_BASE:
        return False
    upper = base.upper()
    for suf in _LEVERAGED_SUFFIXES:
        if upper.endswith(suf) and len(upper) > len(suf):
            return False
    if status != "TRADING" or not spot_allowed:
        return False
    return True


def _fetch_binance_book_ticker() -> list[dict]:
    """Whole-exchange best bid/ask. One call. Yields spread for every symbol."""
    r = requests.get(f"{BINANCE_API}/ticker/bookTicker", timeout=15)
    r.raise_for_status()
    out = []
    for s in r.json():
        bid = float(s.get("bidPrice") or 0)
        ask = float(s.get("askPrice") or 0)
        out.append({
            "symbol": s["symbol"], "bid": bid, "ask": ask,
            "bid_qty": float(s.get("bidQty") or 0),
            "ask_qty": float(s.get("askQty") or 0),
        })
    return out


def _fetch_binance_24hr() -> dict[str, float]:
    """Whole-exchange 24h quote volume. One call."""
    r = requests.get(f"{BINANCE_API}/ticker/24hr", timeout=15)
    r.raise_for_status()
    return {s["symbol"]: float(s.get("quoteVolume") or 0) for s in r.json()}


def _fetch_binance_exchange_info() -> dict[str, dict]:
    """Whole-exchange configuration: status, spot tradability, min_notional."""
    r = requests.get(f"{BINANCE_API}/exchangeInfo", timeout=15)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for s in r.json().get("symbols", []):
        min_notional = 0.0
        for f in s.get("filters", []):
            if f.get("filterType") == "MIN_NOTIONAL":
                min_notional = float(f.get("notional") or f.get("minNotional") or 0)
        out[s["symbol"]] = {
            "status": s.get("status"),
            "spot_allowed": bool(s.get("isSpotTradingAllowed")),
            "min_notional": min_notional,
        }
    return out


def _orderly_listing() -> set[str]:
    """Best-effort Orderly perp listing (PERP_*_USDC symbols).

    Orderly public market data is restricted; this uses the public symbols
    endpoint when reachable. Returns an empty set on failure — the caller
    treats that as 'no DEX candidates' and preserves the previous universe.
    """
    try:
        r = requests.get(f"{ORDERLY_API}/info", timeout=10)
        r.raise_for_status()
        data = r.json().get("data") or {}
        symbols = data.get("symbols") if isinstance(data, dict) else None
        listing = set()
        for s in symbols or []:
            sym = s.get("symbol") if isinstance(s, dict) else s
            if isinstance(sym, str) and sym.startswith("PERP_") and sym.endswith("_USDC"):
                listing.add(sym)
        return listing
    except Exception:
        return set()


def _fetch_candidates(venue: str) -> list[dict]:
    """Stage 1. Returns candidate dicts with symbol, asset, quote_volume_24h,
    spread_pct, bid, ask, min_notional. Empty list on failure."""
    if venue == "binance":
        book = _fetch_binance_book_ticker()
        vol = _fetch_binance_24hr()
        info = _fetch_binance_exchange_info()
        by_asset: dict[str, dict] = {}
        for b in book:
            symbol = b["symbol"]
            if symbol not in vol:
                continue
            ex = info.get(symbol, {})
            if not _is_spot_tradable(symbol[:-4], symbol[-4:],
                                     ex.get("status", "TRADING"),
                                     ex.get("spot_allowed", True)):
                continue
            asset = symbol[:-4]
            bid, ask = b["bid"], b["ask"]
            spread = ((ask - bid) / bid * 100) if bid and bid > 0 else None
            by_asset[asset] = {
                "asset": asset, "symbol": symbol,
                "quote_volume_24h": vol[symbol],
                "spread_pct": spread, "bid": bid, "ask": ask,
                "min_notional": ex.get("min_notional", 0.0),
            }
        return list(by_asset.values())

    # DEX (Orderly): same pipeline over the (small) perp listing, using the
    # Binance whole-exchange snapshot as the data proxy for spread/volume —
    # consistent with the existing Binance-proxy pattern for Orderly data.
    listing = _orderly_listing()
    if not listing:
        return []
    try:
        book = _fetch_binance_book_ticker()
        vol = _fetch_binance_24hr()
        info = _fetch_binance_exchange_info()
    except Exception:
        return []
    by_asset: dict[str, dict] = {}
    for sym in sorted(listing):
        asset = sym[len("PERP_"):-len("_USDC")]
        bsymbol = f"{asset}USDT"
        b = next((x for x in book if x["symbol"] == bsymbol), None)
        if b is None:
            continue
        ex = info.get(bsymbol, {})
        bid, ask = b["bid"], b["ask"]
        spread = ((ask - bid) / bid * 100) if bid and bid > 0 else None
        by_asset[asset] = {
            "asset": asset, "symbol": sym,
            "quote_volume_24h": vol.get(bsymbol, 0.0),
            "spread_pct": spread, "bid": bid, "ask": ask,
            "min_notional": ex.get("min_notional", 0.0),
        }
    return list(by_asset.values())


# ═════════════════════════════════════════════════════════════════════════════
# Stage 2 — hard filters (pass/fail, no ranking)
# ═════════════════════════════════════════════════════════════════════════════

def _hard_filters_pass(c: dict, tp_min: float, rank_min: int, rank_max: int,
                       min_volume: float, spread_ratio_max: float,
                       slot_size: float | None) -> bool:
    """A symbol failing any hard filter never reaches the depth stage."""
    if c["quote_volume_24h"] < min_volume:
        return False
    if c.get("spread_pct") is None or c.get("spread_pct", 0) > tp_min * spread_ratio_max:
        return False
    rank = c.get("rank", 0)
    if rank < rank_min or rank > rank_max:
        return False
    # min_notional × 1.5 fundable at the venue's current slot size
    if slot_size is not None and slot_size > 0 and c.get("min_notional", 0) > 0:
        if c["min_notional"] * 1.5 > slot_size:
            return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Stage 3 — depth check (per-symbol, survivors only)
# ═════════════════════════════════════════════════════════════════════════════

class ScanBudgetExhausted(Exception):
    """Raised when the per-scan depth-call budget is used up. The caller must
    preserve the previous universe — no partial write."""


class _TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_per_sec = refill_per_sec
        self.updated = time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now
        if self.tokens < n:
            return False
        self.tokens -= n
        return True


def _fetch_depth(venue: str, symbol: str, limit: int = 10) -> dict | None:
    """Top-of-book depth. Returns {bid: quote_depth, ask: quote_depth}."""
    try:
        if venue == "binance":
            r = requests.get(f"{BINANCE_API}/depth",
                             params={"symbol": symbol, "limit": limit}, timeout=8)
            r.raise_for_status()
            data = r.json()
        else:
            r = requests.get(f"{ORDERLY_API}/orderbook",
                             params={"symbol": symbol, "limit": limit}, timeout=8)
            r.raise_for_status()
            data = r.json().get("data") or {}
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid_q = sum(float(b[0]) * float(b[1]) for b in bids[:limit])
        ask_q = sum(float(a[0]) * float(a[1]) for a in asks[:limit])
        return {"bid": bid_q, "ask": ask_q}
    except Exception:
        return None


def _depth_check(venue: str, survivors: list[dict], slot_size: float,
                 multiple: float, bucket: _TokenBucket) -> list[dict]:
    """Require top-10 depth on BOTH sides >= multiple × slot size.

    Rate-limited by the token bucket. Raises ScanBudgetExhausted if the budget
    runs out — the caller aborts and keeps the previous universe.
    """
    checked = []
    for c in survivors:
        if not bucket.take(1):
            raise ScanBudgetExhausted(venue)
        d = _fetch_depth(venue, c["symbol"])
        if d is None:
            continue  # depth unavailable → reject (fail closed)
        need = multiple * slot_size
        if d["bid"] >= need and d["ask"] >= need:
            c["depth_bid_top10"] = d["bid"]
            c["depth_ask_top10"] = d["ask"]
            checked.append(c)
    return checked


# ═════════════════════════════════════════════════════════════════════════════
# Stage 4 — strategy replay (the ranking key)
# ═════════════════════════════════════════════════════════════════════════════

def _atr_pct_at(candles: list[dict], i: int, period: int) -> float | None:
    """ATR% at candle i over the trailing `period` candles — same TR formula
    and ATR% definition as trade/regime._compute_atr_pct (the live bot)."""
    if i < 1:
        return None
    start = max(0, i - period + 1)
    trs = []
    for j in range(start + 1, i + 1):
        cur = candles[j]
        prev = candles[j - 1]
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        trs.append(tr)
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    price = candles[i]["close"]
    return (atr / price * 100) if price and price > 0 else None


def replay_symbol(candles: list[dict], atr_period: int, dip_k: float,
                  dip_min_pct: float, tp_k: float, tp_min_pct: float,
                  max_hold_minutes: int) -> dict:
    """Replay the live entry rule over a candle series (oldest → newest).

    Uses the same rolling peak/trough window (WINDOW_SIZE, WARMUP) and the
    same adaptive thresholds as the live scalpers (via compute_thresholds).

    Returns {signals_count, recovery_rate, median_minutes_to_tp,
             atr_pct_median, minutes_list}.
    """
    closes = [c["close"] for c in candles]
    n = len(candles)
    signals = 0
    recovered = 0
    minutes_to_tp: list[float] = []
    atr_values: list[float] = []

    window = deque(maxlen=WINDOW_SIZE)
    hold_candles = max(1, int(max_hold_minutes / 5))

    for i in range(n):
        price = closes[i]
        if price and price > 0:
            window.append(price)

        atr = _atr_pct_at(candles, i, atr_period)
        if atr is not None:
            atr_values.append(atr)

        if len(window) < WARMUP or atr is None:
            continue

        # Same adaptive thresholds as the live scalper for this candle.
        dn, _pn, te, _se = compute_thresholds(
            atr, dip_k, dip_min_pct, dip_k, dip_min_pct, tp_k, tp_min_pct,
            0.6, 0.5,
        )
        peak = max(window)
        if peak <= 0:
            continue
        dip = (peak - price) / peak * 100
        if dip < dn:
            continue

        # Entry fired at candle i. Look forward for TP within the hold window.
        signals += 1
        tp_price = price * (1 + te / 100)
        reached_at: Optional[int] = None
        for f in range(1, hold_candles + 1):
            j = i + f
            if j >= n:
                break
            if candles[j]["high"] >= tp_price:
                reached_at = f
                break
        if reached_at is not None:
            recovered += 1
            minutes_to_tp.append(reached_at * 5)

    return {
        "signals_count": signals,
        "recovery_rate": (recovered / signals) if signals > 0 else 0.0,
        "median_minutes_to_tp": median(minutes_to_tp) if minutes_to_tp else None,
        "atr_pct_median": median(atr_values) if atr_values else None,
        "minutes_list": minutes_to_tp,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Scan orchestration
# ═════════════════════════════════════════════════════════════════════════════

def is_universe_stale(venue: str) -> bool:
    """True when the stored scan is older than universe_max_age_hours
    (or there is no scan at all)."""
    age = get_universe_scan_age(venue)
    if age is None:
        return True
    max_age = get_setting_float("universe_max_age_hours", 36) * 3600
    return (time.time() - age) > max_age


def _slot_size_for(venue: str, equity: float | None) -> float | None:
    """Venue slot size = {venue}_slot_pct × live equity. Returns None when no
    equity is known (fundability/depth filters are then skipped, never
    blocking the whole scan on a missing number)."""
    if equity is None:
        st = get_venue_equity(venue)
        equity = float(st["equity"]) if st else 0.0
    if equity <= 0:
        return None
    pct_key = "cex_slot_pct" if venue == "binance" else "dex_slot_pct"
    pct = get_setting_float(pct_key, 10.0)
    return equity * pct / 100


def scan_venue(venue: str, equity: float | None = None,
               depth_budget: int | None = None) -> dict:
    """Run the full scan pipeline for one venue and store the result.

    On any failure the previous stored universe is preserved (the table is
    only written at the end, wholesale). Returns a summary dict.
    """
    started = time.time()
    summary = {"venue": venue, "ok": False, "stored": False, "reason": ""}

    # ── Stage 1 — candidates ───────────────────────────────────────────
    try:
        candidates = _fetch_candidates(venue)
    except Exception as e:
        summary["reason"] = f"stage1 candidates failed: {e}"
        return summary
    if not candidates:
        summary["reason"] = "no candidates (exchange data unavailable or empty)"
        return summary

    # ── Stage 2 — hard filters (no ranking yet) ─────────────────────────
    tp_min = get_setting_float("tp_min_pct", 0.8)
    rank_min = get_setting_int("universe_rank_min", 15)
    rank_max = get_setting_int("universe_rank_max", 90)
    min_volume = get_setting_float("universe_min_volume_usd", 5_000_000)
    spread_ratio_max = get_setting_float("universe_spread_ratio_max", 0.10)
    slot_size = _slot_size_for(venue, equity)

    # Rank by 24h volume descending (1 = most volume).
    ranked = sorted(candidates, key=lambda c: c["quote_volume_24h"], reverse=True)
    for i, c in enumerate(ranked, start=1):
        c["rank"] = i

    survivors = [
        c for c in ranked
        if _hard_filters_pass(c, tp_min, rank_min, rank_max, min_volume,
                              spread_ratio_max, slot_size)
    ]
    summary["candidates"] = len(candidates)
    summary["survivors_after_filters"] = len(survivors)
    if not survivors:
        summary["reason"] = "no symbols passed hard filters"
        return summary

    # ── Stage 3 — depth check (survivors only, token-bucket rate limit) ─
    multiple = get_setting_float("universe_depth_slot_multiple", 3.0)
    if depth_budget is None:
        depth_budget = 1200
    bucket = _TokenBucket(capacity=float(depth_budget), refill_per_sec=60.0)
    try:
        checked = _depth_check(venue, survivors, slot_size or 0.0, multiple, bucket)
    except ScanBudgetExhausted:
        # Abort cleanly — keep the previous universe, never store a partial one.
        summary["reason"] = "depth-call budget exhausted — previous universe preserved"
        return summary
    summary["survivors_after_depth"] = len(checked)
    if not checked:
        summary["reason"] = "no symbols passed the depth check"
        return summary

    # ── Stage 4 — replay (the ranking key) ──────────────────────────────
    atr_period = get_setting_int("atr_period", 14)
    dip_k = get_setting_float("dip_k", 0.5)
    dip_min = get_setting_float("dip_min_pct", 0.15)
    tp_k = get_setting_float("tp_k", 1.0)
    hold_key = "max_hold_minutes_spot" if venue == "binance" else "max_hold_minutes_futures"
    max_hold = get_setting_int(hold_key, 120 if venue == "binance" else 240)
    replay_days = get_setting_int("universe_replay_days", 7)
    limit = replay_days * 288  # 288 × 5m candles per day

    metrics = {}
    for c in checked:
        try:
            # DEX OHLCV comes from the Binance proxy ({ASSET}USDT) — the Orderly
            # kline endpoint is documented unreliable and every other module
            # (regime.py, bot.py) proxies Orderly data through Binance.
            if venue == "orderly":
                candles = _fetch_ohlcv("binance", f"{c['asset']}USDT", "5m", limit)
            else:
                candles = _fetch_ohlcv("binance", c["symbol"], "5m", limit)
            m = replay_symbol(candles, atr_period, dip_k, dip_min, tp_k, tp_min,
                              max_hold)
            metrics[c["asset"]] = m
        except Exception:
            metrics[c["asset"]] = None

    # ── Stage 5 — rank & store ──────────────────────────────────────────
    min_signals = get_setting_int("universe_min_signals", 20)
    min_rec = min_recovery_rate(venue)
    size = get_setting_int("universe_size", 20)

    rows = select_ranked(checked, metrics, min_signals, min_rec, size, started)

    replace_universe(venue, rows)
    summary.update({
        "ok": True, "stored": True, "stored_count": len(rows),
        "min_recovery_rate": min_rec, "reason": "",
    })
    return summary


def select_ranked(checked: list[dict], metrics: dict, min_signals: int,
                  min_rec: float, size: int, scanned_at: float) -> list[dict]:
    """Stage 5 — filter, rank and truncate survivors.

    Rejects symbols with signals_count < min_signals (regardless of recovery
    rate) or recovery_rate < min_rec. Ranks by recovery_rate desc, tiebreak
    by atr_pct_median desc. Truncates to `size` — a venue with fewer
    qualifying symbols stores what qualifies (never loosens filters).
    """
    rows = []
    for c in checked:
        m = metrics.get(c["asset"])
        if m is None:
            continue
        if m["signals_count"] < min_signals:
            continue
        if m["recovery_rate"] < min_rec:
            continue
        rows.append({
            "asset": c["asset"], "symbol": c["symbol"], "rank": c["rank"],
            "scanned_at": scanned_at,
            "quote_volume_24h": c["quote_volume_24h"], "spread_pct": c["spread_pct"],
            "depth_bid_top10": c.get("depth_bid_top10"),
            "depth_ask_top10": c.get("depth_ask_top10"),
            "atr_pct_median": m["atr_pct_median"],
            "signals_count": m["signals_count"], "recovery_rate": m["recovery_rate"],
            "median_minutes_to_tp": m["median_minutes_to_tp"],
        })

    rows.sort(key=lambda r: (r["recovery_rate"], r.get("atr_pct_median") or 0.0),
              reverse=True)
    rows = rows[:size]
    # Re-number ranks 1..N in stored order.
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def run_scans_if_due(venues=("binance", "orderly"),
                     equity_fn: Callable[[str], float | None] | None = None,
                     notify: Callable[[str], None] | None = None) -> list[dict]:
    """Scan each venue if the stored scan is absent or older than
    universe_scan_interval_hours. Called by the bot's scanner thread.

    Returns the list of scan summaries. Never raises.
    """
    results = []
    for venue in venues:
        interval = get_setting_float("universe_scan_interval_hours", 24) * 3600
        age = get_universe_scan_age(venue)
        if age is not None and (time.time() - age) < interval:
            continue
        equity = equity_fn(venue) if equity_fn else None
        try:
            res = scan_venue(venue, equity=equity)
            results.append(res)
            if notify:
                notify(_scan_summary_message(res))
        except Exception as e:
            results.append({"venue": venue, "ok": False, "reason": str(e)})
    return results


def _scan_summary_message(res: dict) -> str:
    if res.get("ok"):
        return (f"🛰️ Universe scan {res['venue']}: {res.get('stored_count', 0)} assets "
                f"stored (candidates={res.get('candidates', 0)}, "
                f"after_depth={res.get('survivors_after_depth', 0)})")
    return f"⚠️ Universe scan {res['venue']} failed: {res.get('reason', 'unknown')}"
