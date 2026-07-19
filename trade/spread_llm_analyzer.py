"""
Cross-exchange spread analyzer — executable bid/ask pricing with statistical asset scoring.

Refactored from spread-llm-analizer.py (FR-01, FR-06, FR-07, NFR-01):
- Uses top-of-book bid/ask exclusively for spread detection (no last-trade prices).
- Statistical asset scoring over a rolling observation window.
- Shared break-even threshold used identically by analyzer and orchestrator.
- Standard Python module name (underscores) for direct import.
"""

import os
import json
import logging
import time
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── API endpoints ──────────────────────────────────────────────────────────
BINANCE_SYMBOLS_URL = "https://api.binance.com/api/v3/exchangeInfo"
BITGET_SYMBOLS_URL = "https://api.bitget.com/api/v2/spot/public/symbols"
BITGET_TICKERS_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
BINANCE_BOOK_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BINANCE_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BITGET_COINS_URL = "https://api.bitget.com/api/v2/spot/public/coins"

# ── Concurrency ─────────────────────────────────────────────────────────────
DEFAULT_MAX_WORKERS = min(64, max(8, (os.cpu_count() or 4) * 4))

# ── Configuration (from .env with defaults) ─────────────────────────────────
SPREAD_MIN_PCT = float(os.getenv("SPREAD_MIN_PCT", "0.5"))
MIN_LIQUIDITY_24H_USDT = float(os.getenv("MIN_LIQUIDITY_24H_USDT", "1000000"))
MIN_TOP_BOOK_NOTIONAL_USDT = float(os.getenv("MIN_TOP_BOOK_NOTIONAL_USDT", "2000"))

# Max-spread sanity filter: anything above this is a stale/broken book, not an opportunity.
# AIUSDT at +31% / -24% is the canonical example — dead token on one venue.
MAX_SPREAD_PCT = float(os.getenv("ARB_MAX_SPREAD_PCT", "3.0"))
MIN_PROFIT_USD = float(os.getenv("MIN_PROFIT_USD", "0.16"))
TRADING_FEE_PCT = float(os.getenv("TRADING_FEE_PCT", "0.1"))
SLIPPAGE_SAFETY_MARGIN_PCT = float(os.getenv("SLIPPAGE_SAFETY_MARGIN_PCT", "0.05"))
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT", "100"))

# New tunables (NFR-03)
SAMPLING_INTERVAL_SEC = float(os.getenv("ARB_SAMPLING_INTERVAL_SEC", "30"))
OBSERVATION_WINDOW_SEC = float(os.getenv("ARB_OBSERVATION_WINDOW_SEC", "3600"))
ROTATION_CADENCE_SEC = float(os.getenv("ARB_ROTATION_CADENCE_SEC", "300"))
DRY_SPELL_DURATION_SEC = float(os.getenv("ARB_DRY_SPELL_DURATION_SEC", "1800"))
ROTATION_SCORE_MARGIN = float(os.getenv("ARB_ROTATION_SCORE_MARGIN", "1.5"))

# ── Tier 1 / Tier 2: configurable quote-asset whitelist ──────────────────────
# ARB_QUOTE_ASSETS: comma-separated quote assets the bot is ALLOWED TO TRADE.
#   Default: "USDT,USDC" — stablecoin quotes only. USDC is economically identical
#   to USDT for inventory purposes and expands the candidate universe with zero
#   new complexity.
# ARB_OBSERVE_QUOTE_ASSETS: comma-separated quote assets to SCORE BUT NOT TRADE.
#   Default: "" (empty).  Example: "BRL" to collect multi-day data on BRL-pair
#   spreads without any inventory commitment.  After 2-3 weeks of data you can
#   decide whether to promote BRL to the tradable whitelist.
TRADABLE_QUOTE_ASSETS = [
    s.strip().upper()
    for s in os.getenv("ARB_QUOTE_ASSETS", "USDT,USDC").split(",")
    if s.strip()
]
OBSERVE_QUOTE_ASSETS = [
    s.strip().upper()
    for s in os.getenv("ARB_OBSERVE_QUOTE_ASSETS", "").split(",")
    if s.strip()
]
# All suffixes we fetch (tradable + observe-only)
ALL_QUOTE_ASSETS = list(set(TRADABLE_QUOTE_ASSETS + OBSERVE_QUOTE_ASSETS))

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "arbitrage.log"

logger = logging.getLogger("spread_analyzer")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)
    logger.propagate = False


# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED: break-even threshold  (FR-06)
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_break_even_threshold() -> float:
    """Return the minimum executable spread (as %) required for a profitable trade.

    Components: trading fees on both legs + slippage safety margin + minimum profit target.
    This is the *single shared definition* used by both the analyzer and the orchestrator.
    """
    trading_fees_pct = TRADING_FEE_PCT * 2  # buy + sell leg
    min_profit_pct = (MIN_PROFIT_USD / TRADE_AMOUNT_USDT) * 100 if TRADE_AMOUNT_USDT > 0 else 0.0
    return trading_fees_pct + SLIPPAGE_SAFETY_MARGIN_PCT + min_profit_pct


# ═══════════════════════════════════════════════════════════════════════════════
#  HTTP helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_get_json(url: str, params: dict | None = None) -> Optional[dict]:
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Symbol discovery (unchanged from original)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_binance_symbols(quote_assets: list[str] | None = None) -> set[str]:
    """Fetch all trading pairs matching the given quote-asset suffixes from Binance.

    Args:
        quote_assets: list of quote-asset suffixes to match (e.g. ['USDT','USDC']).
                      If None, defaults to ALL_QUOTE_ASSETS.
    """
    if quote_assets is None:
        quote_assets = ALL_QUOTE_ASSETS
    data = _safe_get_json(BINANCE_SYMBOLS_URL)
    if not data:
        return set()
    symbols = set()
    for symbol_info in data.get("symbols", []):
        symbol = symbol_info.get("symbol", "")
        if symbol_info.get("status") != "TRADING":
            continue
        for qa in quote_assets:
            if symbol.endswith(qa):
                symbols.add(symbol)
                break
    return symbols


def fetch_bitget_symbols(quote_assets: list[str] | None = None) -> set[str]:
    """Fetch all trading pairs matching the given quote-asset suffixes from Bitget."""
    if quote_assets is None:
        quote_assets = ALL_QUOTE_ASSETS
    # Try primary endpoint
    data = _safe_get_json(BITGET_SYMBOLS_URL)
    if data:
        symbols = set()
        for product in data.get("data", []):
            symbol = product.get("symbol", "")
            if product.get("status") != "live":
                continue
            for qa in quote_assets:
                if symbol.endswith(qa):
                    symbols.add(symbol)
                    break
        if symbols:
            return symbols
    # Fallback: extract symbols from tickers endpoint
    data = _safe_get_json(BITGET_TICKERS_URL)
    if not data:
        return set()
    symbols = set()
    for ticker in data.get("data", []):
        symbol = ticker.get("symbol", "")
        for qa in quote_assets:
            if symbol.endswith(qa):
                symbols.add(symbol)
                break
    return symbols


# ═══════════════════════════════════════════════════════════════════════════════
#  Top-of-book data fetching  (FR-01)
# ═══════════════════════════════════════════════════════════════════════════════

def get_binance_book_tickers_bulk() -> dict[str, dict]:
    """Fetch all Binance book tickers in one call. Returns {SYMBOL: {bid, bidQty, ask, askQty}}."""
    data = _safe_get_json(BINANCE_BOOK_TICKER_URL)
    if not isinstance(data, list):
        return {}
    result: dict[str, dict] = {}
    for row in data:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            result[symbol] = {
                "bid": float(row.get("bidPrice", 0)),
                "bid_qty": float(row.get("bidQty", 0)),
                "ask": float(row.get("askPrice", 0)),
                "ask_qty": float(row.get("askQty", 0)),
            }
        except (ValueError, TypeError):
            continue
    return result


def get_bitget_book_tickers_bulk() -> dict[str, dict]:
    """Fetch all Bitget tickers (includes bid/ask). Returns {SYMBOL: {bid, bidQty, ask, askQty}}."""
    data = _safe_get_json(BITGET_TICKERS_URL)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            result[symbol] = {
                "bid": float(row.get("bidPr", 0)),
                "bid_qty": float(row.get("bidSz", 0)),
                "ask": float(row.get("askPr", 0)),
                "ask_qty": float(row.get("askSz", 0)),
                "usdt_volume": float(row.get("usdtVolume", 0)),
            }
        except (ValueError, TypeError):
            continue
    return result


def get_binance_quote_volume_bulk() -> dict[str, float]:
    """Get Binance 24h quote volume (USDT) for all symbols."""
    data = _safe_get_json(BINANCE_24H_URL)
    if not isinstance(data, list):
        return {}
    quote_volumes: dict[str, float] = {}
    for row in data:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            quote_volumes[symbol] = float(row.get("quoteVolume", 0))
        except (ValueError, TypeError):
            continue
    return quote_volumes


# ═══════════════════════════════════════════════════════════════════════════════
#  Executable spread computation  (FR-01)
# ═══════════════════════════════════════════════════════════════════════════════

def executable_spread(
    binance_book: dict | None,
    bitget_book: dict | None,
) -> dict:
    """Compute executable spreads from top-of-book bid/ask.

    Executable spread for a direction = (sell_side_best_bid - buy_side_best_ask) / buy_side_best_ask * 100.

    Returns dict with keys:
        spread_b2b: buy Binance (at ask) → sell Bitget (at bid), as %
        spread_btog: buy Bitget (at ask) → sell Binance (at bid), as %
        binance_bid, binance_ask, bitget_bid, bitget_ask: raw prices
    """
    result = {
        "spread_b2b": None,
        "spread_btog": None,
        "binance_bid": None,
        "binance_ask": None,
        "bitget_bid": None,
        "bitget_ask": None,
    }
    if not binance_book or not bitget_book:
        return result

    bin_bid = binance_book.get("bid")
    bin_ask = binance_book.get("ask")
    bg_bid = bitget_book.get("bid")
    bg_ask = bitget_book.get("ask")

    result["binance_bid"] = bin_bid
    result["binance_ask"] = bin_ask
    result["bitget_bid"] = bg_bid
    result["bitget_ask"] = bg_ask

    if not all(v is not None and v > 0 for v in [bin_bid, bin_ask, bg_bid, bg_ask]):
        return result

    # Buy Binance → Sell Bitget: buy at bin_ask, sell at bg_bid
    result["spread_b2b"] = ((bg_bid - bin_ask) / bin_ask) * 100

    # Buy Bitget → Sell Binance: buy at bg_ask, sell at bin_bid
    result["spread_btog"] = ((bin_bid - bg_ask) / bg_ask) * 100

    return result


def executable_spread_for_direction(
    binance_book: dict | None,
    bitget_book: dict | None,
    direction: str,
) -> float | None:
    """Return executable spread % for a specific direction."""
    s = executable_spread(binance_book, bitget_book)
    if direction == "binance_to_bitget":
        return s["spread_b2b"]
    return s["spread_btog"]


# ═══════════════════════════════════════════════════════════════════════════════
#  Deposit/withdrawal status check (FR-07)
# ═══════════════════════════════════════════════════════════════════════════════

# Cached exchange info for deposit/withdrawal status
_exchange_info_cache: dict = {"binance": None, "bitget": None, "binance_ts": 0, "bitget_ts": 0}
_EXCHANGE_INFO_CACHE_TTL = 600  # 10 minutes


def _get_binance_exchange_info() -> dict:
    """Fetch and cache Binance exchange info for deposit/withdrawal status."""
    now = time.time()
    if _exchange_info_cache["binance"] and (now - _exchange_info_cache["binance_ts"]) < _EXCHANGE_INFO_CACHE_TTL:
        return _exchange_info_cache["binance"]
    data = _safe_get_json(BINANCE_EXCHANGE_INFO_URL)
    if data:
        _exchange_info_cache["binance"] = data
        _exchange_info_cache["binance_ts"] = now
    return data or {}


def _get_bitget_coins_info() -> dict:
    """Fetch and cache Bitget coins info for deposit/withdrawal status."""
    now = time.time()
    if _exchange_info_cache["bitget"] and (now - _exchange_info_cache["bitget_ts"]) < _EXCHANGE_INFO_CACHE_TTL:
        return _exchange_info_cache["bitget"]
    data = _safe_get_json(BITGET_COINS_URL)
    if data:
        _exchange_info_cache["bitget"] = data
        _exchange_info_cache["bitget_ts"] = now
    return data or {}


def check_deposit_withdrawal_status(symbol: str) -> dict:
    """Check if deposits and withdrawals are open for a symbol on both exchanges.

    Returns {deposits_open_binance, deposits_open_bitget, withdrawals_open_binance, withdrawals_open_bitget}.
    All default to True if the check cannot be performed (fail-open for trading).
    """
    result = {
        "deposits_open_binance": True,
        "deposits_open_bitget": True,
        "withdrawals_open_binance": True,
        "withdrawals_open_bitget": True,
    }

    base_asset = symbol.replace("USDT", "").replace("USDC", "")

    # Binance check
    bin_info = _get_binance_exchange_info()
    for s in bin_info.get("symbols", []):
        if s.get("symbol") == symbol:
            result["deposits_open_binance"] = s.get("isSpotTradingAllowed", True)
            # For withdrawals, we check permissions on the base asset via the symbols list
            # (Binance exchangeInfo does not directly report deposit/withdrawal status per asset;
            # the sapi/v1/capital/config/getall endpoint does but requires authentication.
            # We default to True and let the trade gate handle actual failures.)
            break

    # Bitget check
    bg_info = _get_bitget_coins_info()
    for coin in bg_info.get("data", []) or []:
        if coin.get("coin", "").upper() == base_asset.upper():
            for chain_info in coin.get("chains", []) or []:
                # If ANY chain allows deposits/withdrawals, consider it open
                if chain_info.get("rechargeable") in ("true", True, "yes"):
                    result["deposits_open_bitget"] = True
                if chain_info.get("withdrawable") in ("true", True, "yes"):
                    result["withdrawals_open_bitget"] = True
            break

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Statistical asset scoring  (FR-07)
# ═══════════════════════════════════════════════════════════════════════════════

def score_candidate_from_observations(
    observations: list[dict],
    break_even_threshold: float,
) -> dict:
    """Compute an opportunity score for a symbol from its observation history.

    Scoring factors:
      - count of samples where executable spread in either direction > threshold
      - average magnitude of those exceedances
      - conservative top-of-book notional relative to configured trade size
    """
    if not observations:
        return {"score": 0.0, "exceedance_count": 0, "avg_exceedance": 0.0, "min_notional": 0.0}

    exceedances = []
    min_notionals = []

    for obs in observations:
        spread_b2b = obs.get("spread_b2b") or 0.0
        spread_btog = obs.get("spread_btog") or 0.0
        best_spread = max(spread_b2b, spread_btog)

        if best_spread > break_even_threshold:
            exceedances.append(best_spread - break_even_threshold)

        # Conservative notional: min(bid*ask on each side for executable direction)
        bin_ask = obs.get("binance_ask") or 0
        bin_ask_qty = obs.get("binance_ask_qty") or 0
        bg_bid = obs.get("bitget_bid") or 0
        bg_bid_qty = obs.get("bitget_bid_qty") or 0
        bin_bid = obs.get("binance_bid") or 0
        bin_bid_qty = obs.get("binance_bid_qty") or 0
        bg_ask = obs.get("bitget_ask") or 0
        bg_ask_qty = obs.get("bitget_ask_qty") or 0

        notional_b2b = min(bin_ask * bin_ask_qty, bg_bid * bg_bid_qty) if bin_ask > 0 and bg_bid > 0 else 0
        notional_btog = min(bg_ask * bg_ask_qty, bin_bid * bin_bid_qty) if bg_ask > 0 and bin_bid > 0 else 0
        min_notionals.append(min(notional_b2b, notional_btog))

    exceedance_count = len(exceedances)
    avg_exceedance = sum(exceedances) / exceedance_count if exceedance_count > 0 else 0.0
    avg_min_notional = sum(min_notionals) / len(min_notionals) if min_notionals else 0.0

    # Score: weighted combination
    # Base score from exceedance frequency and magnitude
    exceedance_rate = exceedance_count / len(observations) if observations else 0.0
    score = (exceedance_rate * 50.0) + (avg_exceedance * 10.0)

    # Bonus for sufficient notional relative to trade size
    if avg_min_notional >= TRADE_AMOUNT_USDT * 2:
        score += 10.0
    elif avg_min_notional >= TRADE_AMOUNT_USDT:
        score += 5.0

    return {
        "score": round(score, 4),
        "exceedance_count": exceedance_count,
        "avg_exceedance": round(avg_exceedance, 6),
        "min_notional": round(avg_min_notional, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Main analyzer entry points
# ═══════════════════════════════════════════════════════════════════════════════

def is_tradable(symbol: str) -> bool:
    """Return True if the symbol's quote asset is in the tradable whitelist."""
    for qa in TRADABLE_QUOTE_ASSETS:
        if symbol.endswith(qa):
            return True
    return False


def is_observed(symbol: str) -> bool:
    """Return True if the symbol matches any quote asset (tradable or observe-only)."""
    for qa in ALL_QUOTE_ASSETS:
        if symbol.endswith(qa):
            return True
    return False


def is_spread_sane(spread_data: dict) -> bool:
    """Reject pairs where either directional spread exceeds MAX_SPREAD_PCT.

    A spread > ~3% in one direction combined with a large negative spread in
    the other is the signature of a dead or stale book on one venue (e.g.
    AIUSDT at +31% / −24%).  These are not opportunities — they're data
    artifacts that would lose money if traded.
    """
    b2b = spread_data.get("spread_b2b") or 0
    btg = spread_data.get("spread_btog") or 0
    return abs(b2b) <= MAX_SPREAD_PCT and abs(btg) <= MAX_SPREAD_PCT


def observe_and_score(
    session_asset: Optional[str] = None,
    trade_direction: str = "binance_to_bitget",
) -> dict:
    """Run one observation cycle: fetch book tickers, compute executable spreads,
    persist observations, and return scored candidates.

    Tier 1 (tradable): quote assets in ARB_QUOTE_ASSETS — eligible for trading.
    Tier 2 (observe-only): quote assets in ARB_OBSERVE_QUOTE_ASSETS — scored for
      multi-day data collection, but never selected as best_asset for trading.

    Returns dict with:
        best_asset: highest-scored *tradable* asset (or None)
        best_score: score of best tradable asset
        best_observe_asset: highest-scored observe-only asset (or None)
        candidates: {symbol: score_dict} — all valid candidates (both tiers)
        tradable_candidates: {symbol: score_dict} — Tier 1 only
        observe_candidates: {symbol: score_dict} — Tier 2 only
        session_asset_score: score of current session asset (if any)
        rotation_recommended: bool
        estimated_rotation_cost: float
    """
    from db.db_ops import (
        insert_observation, get_all_observations_in_window, get_observations_since,
        insert_rotation_decision,
    )

    break_even = calculate_break_even_threshold()
    window_start = (datetime.utcnow() - timedelta(seconds=OBSERVATION_WINDOW_SEC)).strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        f"Observation cycle: window={OBSERVATION_WINDOW_SEC}s, be={break_even:.4f}%, "
        f"tradable={TRADABLE_QUOTE_ASSETS}, observe={OBSERVE_QUOTE_ASSETS or 'none'}"
    )

    # Fetch top-of-book data (all symbols, unfiltered)
    binance_books = get_binance_book_tickers_bulk()
    bitget_books = get_bitget_book_tickers_bulk()
    binance_volumes = get_binance_quote_volume_bulk()

    if not binance_books or not bitget_books:
        logger.warning("Failed to fetch book tickers")
        return {"best_asset": None, "best_score": 0.0, "candidates": {},
                "tradable_candidates": {}, "observe_candidates": {},
                "best_observe_asset": None}

    common_symbols = set(binance_books.keys()) & set(bitget_books.keys())
    # Filter to only our quote assets (both tiers)
    common_symbols = {s for s in common_symbols if is_observed(s)}
    logger.info(
        f"Common book-ticker symbols: {len(common_symbols)} "
        f"(tradable={len([s for s in common_symbols if is_tradable(s)])}, "
        f"observe={len([s for s in common_symbols if not is_tradable(s)])})"
    )

    # Sample each common symbol
    observations_persisted = 0
    candidates: dict[str, dict] = {}
    tradable_candidates: dict[str, dict] = {}
    observe_candidates: dict[str, dict] = {}

    for symbol in common_symbols:
        bn_book = binance_books.get(symbol)
        bg_book = bitget_books.get(symbol)

        # Liquidity filter
        bn_volume = binance_volumes.get(symbol, 0.0)
        bg_volume = bg_book.get("usdt_volume", 0.0) if bg_book else 0.0
        bn_notional = min(
            (bn_book.get("bid", 0) or 0) * (bn_book.get("bid_qty", 0) or 0),
            (bn_book.get("ask", 0) or 0) * (bn_book.get("ask_qty", 0) or 0),
        ) if bn_book else 0
        bg_notional = min(
            (bg_book.get("bid", 0) or 0) * (bg_book.get("bid_qty", 0) or 0),
            (bg_book.get("ask", 0) or 0) * (bg_book.get("ask_qty", 0) or 0),
        ) if bg_book else 0

        if (
            bn_volume < MIN_LIQUIDITY_24H_USDT
            or bg_volume < MIN_LIQUIDITY_24H_USDT
            or bn_notional < MIN_TOP_BOOK_NOTIONAL_USDT
            or bg_notional < MIN_TOP_BOOK_NOTIONAL_USDT
        ):
            continue

        # Compute executable spread
        spread_data = executable_spread(bn_book, bg_book)

        # ── Max-spread sanity filter: reject stale/broken books ──────────
        if not is_spread_sane(spread_data):
            continue

        # Deposit/withdrawal check
        dw_status = check_deposit_withdrawal_status(symbol)

        # Persist observation (both tiers)
        insert_observation(
            symbol=symbol,
            binance_bid=spread_data["binance_bid"],
            binance_ask=spread_data["binance_ask"],
            binance_bid_qty=bn_book.get("bid_qty") if bn_book else None,
            binance_ask_qty=bn_book.get("ask_qty") if bn_book else None,
            bitget_bid=spread_data["bitget_bid"],
            bitget_ask=spread_data["bitget_ask"],
            bitget_bid_qty=bg_book.get("bid_qty") if bg_book else None,
            bitget_ask_qty=bg_book.get("ask_qty") if bg_book else None,
            spread_b2b=spread_data["spread_b2b"],
            spread_btog=spread_data["spread_btog"],
            deposits_open_binance=dw_status["deposits_open_binance"],
            deposits_open_bitget=dw_status["deposits_open_bitget"],
            withdrawals_open_binance=dw_status["withdrawals_open_binance"],
            withdrawals_open_bitget=dw_status["withdrawals_open_bitget"],
        )
        observations_persisted += 1

        # Exclude candidates failing deposit/withdrawal check
        if not dw_status["deposits_open_binance"] or not dw_status["deposits_open_bitget"]:
            continue
        if not dw_status["withdrawals_open_binance"] or not dw_status["withdrawals_open_bitget"]:
            continue

        # Score from observation window
        symbol_observations = get_observations_since(symbol, window_start)
        score_data = score_candidate_from_observations(symbol_observations, break_even)

        candidate_data = {
            **score_data,
            "binance_ask": spread_data["binance_ask"],
            "bitget_bid": spread_data["bitget_bid"],
            "binance_bid": spread_data["binance_bid"],
            "bitget_ask": spread_data["bitget_ask"],
            "spread_b2b": spread_data["spread_b2b"],
            "spread_btog": spread_data["spread_btog"],
            "tradable": is_tradable(symbol),
        }

        candidates[symbol] = candidate_data
        if is_tradable(symbol):
            tradable_candidates[symbol] = candidate_data
        else:
            observe_candidates[symbol] = candidate_data

    logger.info(
        f"Observations persisted: {observations_persisted}, "
        f"tradable candidates: {len(tradable_candidates)}, "
        f"observe candidates: {len(observe_candidates)}"
    )

    # Find best TRADABLE candidate (for the bot to act on)
    best_asset = None
    best_score = 0.0
    for sym, data in tradable_candidates.items():
        if data["score"] > best_score:
            best_score = data["score"]
            best_asset = sym

    # Find best OBSERVE-ONLY candidate (for reporting)
    best_observe_asset = None
    best_observe_score = 0.0
    for sym, data in observe_candidates.items():
        if data["score"] > best_observe_score:
            best_observe_score = data["score"]
            best_observe_asset = sym

    # Log observe-only top picks for manual review
    if observe_candidates:
        sorted_obs = sorted(observe_candidates.items(), key=lambda x: x[1]["score"], reverse=True)
        logger.info(f"Top observe-only candidates (Tier 2 — not traded):")
        for sym, data in sorted_obs[:3]:
            logger.info(f"  {sym}: score={data['score']:.2f} spread_b2b={data.get('spread_b2b'):.4f}%")

    result = {
        "best_asset": best_asset,
        "best_score": best_score,
        "best_observe_asset": best_observe_asset,
        "candidates": candidates,
        "tradable_candidates": tradable_candidates,
        "observe_candidates": observe_candidates,
        "break_even_threshold": break_even,
        "session_asset_score": None,
        "rotation_recommended": False,
        "estimated_rotation_cost": 0.0,
    }

    if session_asset and session_asset in candidates:
        result["session_asset_score"] = candidates[session_asset]["score"]

    return result

    logger.info(f"Observations persisted: {observations_persisted}, valid candidates: {len(candidates)}")

    # Find best candidate
    best_asset = None
    best_score = 0.0
    for sym, data in candidates.items():
        if data["score"] > best_score:
            best_score = data["score"]
            best_asset = sym

    result = {
        "best_asset": best_asset,
        "best_score": best_score,
        "candidates": candidates,
        "break_even_threshold": break_even,
        "session_asset_score": None,
        "rotation_recommended": False,
        "estimated_rotation_cost": 0.0,
    }

    if session_asset and session_asset in candidates:
        result["session_asset_score"] = candidates[session_asset]["score"]

    return result


def get_best_spread_asset(
    sample_size: int = 100,
    min_spread_pct: float | None = None,
    max_workers: int | None = None,
    trade_direction: str = "binance_to_bitget",
) -> Optional[str]:
    """Return the single best *tradable* symbol by executable spread.

    Only considers symbols whose quote asset is in TRADABLE_QUOTE_ASSETS.
    Retains backward compatibility with the original API but uses
    executable bid/ask pricing internally.
    """
    logger.info(f"Spread analyzer: scanning for best tradable asset (direction={trade_direction})")

    binance_books = get_binance_book_tickers_bulk()
    bitget_books = get_bitget_book_tickers_bulk()
    binance_volumes = get_binance_quote_volume_bulk()

    if not binance_books or not bitget_books:
        logger.warning("Spread analyzer: no book-ticker data available")
        return None

    common_symbols = set(binance_books.keys()) & set(bitget_books.keys())
    logger.info(f"Spread analyzer: {len(common_symbols)} common symbols")

    threshold = SPREAD_MIN_PCT if min_spread_pct is None else min_spread_pct
    best_symbol = None
    best_spread = -999.0

    # Filter to tradable only and apply sample limit
    tradable_symbols = [s for s in common_symbols if is_tradable(s)]
    for symbol in tradable_symbols[:sample_size]:
        bn_book = binance_books.get(symbol)
        bg_book = bitget_books.get(symbol)
        if not bn_book or not bg_book:
            continue

        # Liquidity filter
        bn_volume = binance_volumes.get(symbol, 0.0)
        bg_volume = bg_book.get("usdt_volume", 0.0)
        if bn_volume < MIN_LIQUIDITY_24H_USDT or bg_volume < MIN_LIQUIDITY_24H_USDT:
            continue

        bn_notional = min(
            (bn_book.get("bid", 0) or 0) * (bn_book.get("bid_qty", 0) or 0),
            (bn_book.get("ask", 0) or 0) * (bn_book.get("ask_qty", 0) or 0),
        )
        bg_notional = min(
            (bg_book.get("bid", 0) or 0) * (bg_book.get("bid_qty", 0) or 0),
            (bg_book.get("ask", 0) or 0) * (bg_book.get("ask_qty", 0) or 0),
        )
        if bn_notional < MIN_TOP_BOOK_NOTIONAL_USDT or bg_notional < MIN_TOP_BOOK_NOTIONAL_USDT:
            continue

        # Max-spread sanity check
        s = executable_spread(bn_book, bg_book)
        if not is_spread_sane(s):
            continue

        direction_spread = executable_spread_for_direction(bn_book, bg_book, trade_direction)
        if direction_spread is not None and direction_spread > best_spread:
            best_spread = direction_spread
            best_symbol = symbol

    if best_symbol and best_spread >= threshold:
        logger.info(f"Spread analyzer: selected {best_symbol} (spread={best_spread:.4f}%)")
        return best_symbol

    logger.info(f"Spread analyzer: no symbol met threshold {threshold}%")
    return None


def main() -> None:
    """Standalone run: show top opportunities (unchanged output format)."""
    logger.info("Fetching trading symbols from exchanges...")
    binance_symbols = fetch_binance_symbols()
    bitget_symbols = fetch_bitget_symbols()
    logger.info(f"Binance USDT pairs: {len(binance_symbols)}")
    logger.info(f"Bitget USDT pairs: {len(bitget_symbols)}")
    common_symbols = binance_symbols & bitget_symbols
    logger.info(f"Common pairs: {len(common_symbols)}")

    if not common_symbols:
        logger.warning("No common symbols found!")
        return

    # Run one observation cycle
    result = observe_and_score()
    candidates = result.get("candidates", {})

    sorted_candidates = sorted(candidates.items(), key=lambda x: x[1]["score"], reverse=True)
    logger.info(f"Top scored candidates:")
    for idx, (symbol, data) in enumerate(sorted_candidates[:5], 1):
        logger.info(
            f"  {idx}. {symbol}: score={data['score']:.2f} "
            f"exceedances={data['exceedance_count']} avg_exc={data['avg_exceedance']:.4f}%"
        )

    # Save results
    results = {
        "timestamp": datetime.utcnow().isoformat(),
        "total_common_pairs": len(common_symbols),
        "candidates_scored": len(candidates),
        "break_even_threshold": result["break_even_threshold"],
        "top_candidates": {
            sym: {"score": data["score"], "spread_b2b": data.get("spread_b2b"), "spread_btog": data.get("spread_btog")}
            for sym, data in sorted_candidates[:5]
        },
    }
    with open("spread_analysis_results.json", "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Results saved to spread_analysis_results.json")


if __name__ == "__main__":
    main()
