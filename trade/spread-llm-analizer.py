import os
import json
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

BINANCE_SYMBOLS_URL = "https://api.binance.com/api/v3/exchangeInfo"
BITGET_SYMBOLS_URL = "https://api.bitget.com/api/v2/spot/public/symbols"
BITGET_TICKERS_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_BOOK_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
BITGET_PRICE_URL = "https://api.bitget.com/api/v2/spot/market/tickers"

# I/O-bound HTTP requests benefit from thread concurrency.
DEFAULT_MAX_WORKERS = min(64, max(8, (os.cpu_count() or 4) * 4))
SPREAD_MIN_PCT_RAW = os.getenv("SPREAD_MIN_PCT")
SPREAD_MIN_PCT = float(SPREAD_MIN_PCT_RAW)
MIN_LIQUIDITY_24H_USDT = float(os.getenv("MIN_LIQUIDITY_24H_USDT", "1000000"))
MIN_TOP_BOOK_NOTIONAL_USDT = float(os.getenv("MIN_TOP_BOOK_NOTIONAL_USDT", "2000"))


def _safe_get_json(url: str, params: dict | None = None) -> Optional[dict]:
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None


def fetch_binance_symbols() -> set[str]:
    """Fetch all USDT pairs from Binance."""
    data = _safe_get_json(BINANCE_SYMBOLS_URL)
    if not data:
        return set()

    symbols = set()
    for symbol_info in data.get("symbols", []):
        symbol = symbol_info.get("symbol", "")
        if symbol.endswith("USDT") and symbol_info.get("status") == "TRADING":
            symbols.add(symbol)
    return symbols


def fetch_bitget_symbols() -> set[str]:
    """Fetch all USDT pairs from Bitget."""
    # Try primary endpoint
    data = _safe_get_json(BITGET_SYMBOLS_URL)
    if data:
        symbols = set()
        for product in data.get("data", []):
            symbol = product.get("symbol", "")
            if symbol.endswith("USDT") and product.get("status") == "live":
                symbols.add(symbol)
        if symbols:
            return symbols

    # Fallback: extract symbols from tickers endpoint
    # print("  Falling back to tickers endpoint for symbols...")
    data = _safe_get_json(BITGET_TICKERS_URL)
    if not data:
        return set()

    symbols = set()
    for ticker in data.get("data", []):
        symbol = ticker.get("symbol", "")
        if symbol.endswith("USDT"):
            symbols.add(symbol)
    return symbols


def get_binance_price(symbol: str) -> Optional[float]:
    """Get price from Binance."""
    data = _safe_get_json(BINANCE_PRICE_URL, {"symbol": symbol})
    if not data:
        return None
    try:
        return float(data.get("price"))
    except (ValueError, TypeError):
        return None


def get_bitget_price(symbol: str) -> Optional[float]:
    """Get price from Bitget."""
    data = _safe_get_json(BITGET_PRICE_URL, {"symbol": symbol})
    if not data:
        return None

    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return None

    try:
        return float(rows[0].get("lastPr"))
    except (ValueError, TypeError):
        return None


def get_binance_prices_bulk() -> dict[str, float]:
    """Get all Binance symbol prices in one request."""
    data = _safe_get_json(BINANCE_PRICE_URL)
    if not isinstance(data, list):
        return {}

    prices: dict[str, float] = {}
    for row in data:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            prices[symbol] = float(row.get("price"))
        except (ValueError, TypeError):
            continue
    return prices


def get_bitget_prices_bulk() -> dict[str, float]:
    """Get all Bitget symbol prices in one request."""
    data = _safe_get_json(BITGET_PRICE_URL)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}

    prices: dict[str, float] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            prices[symbol] = float(row.get("lastPr"))
        except (ValueError, TypeError):
            continue
    return prices


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
            quote_volumes[symbol] = float(row.get("quoteVolume"))
        except (ValueError, TypeError):
            continue
    return quote_volumes


def get_binance_top_book_notional_bulk() -> dict[str, float]:
    """Get conservative top-of-book notional on Binance (min(bid, ask) notional)."""
    data = _safe_get_json(BINANCE_BOOK_TICKER_URL)
    if not isinstance(data, list):
        return {}

    notionals: dict[str, float] = {}
    for row in data:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            bid_notional = float(row.get("bidPrice")) * float(row.get("bidQty"))
            ask_notional = float(row.get("askPrice")) * float(row.get("askQty"))
            notionals[symbol] = min(bid_notional, ask_notional)
        except (ValueError, TypeError):
            continue
    return notionals


def get_bitget_liquidity_bulk() -> dict[str, dict[str, float]]:
    """Get Bitget 24h quote volume and conservative top-of-book notional."""
    data = _safe_get_json(BITGET_PRICE_URL)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}

    liquidity: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        try:
            quote_volume = float(row.get("usdtVolume"))
            bid_notional = float(row.get("bidPr")) * float(row.get("bidSz"))
            ask_notional = float(row.get("askPr")) * float(row.get("askSz"))
            liquidity[symbol] = {
                "quote_volume": quote_volume,
                "top_book_notional": min(bid_notional, ask_notional),
            }
        except (ValueError, TypeError):
            continue
    return liquidity


def _calculate_symbol_spread(symbol: str) -> tuple[str, Optional[dict]]:
    binance_price = get_binance_price(symbol)
    bitget_price = get_bitget_price(symbol)

    if not (binance_price and bitget_price):
        return symbol, None

    # Direction 1: Sell Bitget -> Buy Binance
    spread_bitget_to_binance = ((bitget_price - binance_price) / binance_price) * 100

    # Direction 2: Sell Binance -> Buy Bitget
    spread_binance_to_bitget = ((binance_price - bitget_price) / bitget_price) * 100

    data = {
        "binance_price": binance_price,
        "bitget_price": bitget_price,
        "spread_bitget_to_binance": spread_bitget_to_binance,
        "spread_binance_to_bitget": spread_binance_to_bitget,
        "best_direction": "Sell Bitget → Buy Binance" if spread_bitget_to_binance > spread_binance_to_bitget else "Sell Binance → Buy Bitget",
        "best_spread": max(spread_bitget_to_binance, spread_binance_to_bitget),
    }
    return symbol, data


def calculate_spreads(common_symbols: set[str], sample_size: int = 50, max_workers: int | None = None) -> dict:
    """Calculate spreads for both directions (Bitget→Binance and Binance→Bitget).
    No minimum filter - returns all pairs."""
    spreads = {}
    symbols_list = list(common_symbols)[:sample_size]
    workers = max_workers or DEFAULT_MAX_WORKERS

    # print(f"Sampling spreads for {len(symbols_list)} symbols with {workers} workers...")

    # Fast path: one bulk request per exchange, then local compute.
    binance_prices = get_binance_prices_bulk()
    bitget_prices = get_bitget_prices_bulk()
    if binance_prices and bitget_prices:
        binance_quote_volumes = get_binance_quote_volume_bulk()
        binance_top_book_notionals = get_binance_top_book_notional_bulk()
        bitget_liquidity = get_bitget_liquidity_bulk()
        liquidity_filter_enabled = bool(
            binance_quote_volumes and binance_top_book_notionals and bitget_liquidity
        )
        if liquidity_filter_enabled:
            print(
                f"Liquidity filter: 24h volume >= ${MIN_LIQUIDITY_24H_USDT:,.0f}, "
                f"top-book notional >= ${MIN_TOP_BOOK_NOTIONAL_USDT:,.0f}"
            )

        skipped_liquidity = 0
        # print("  Using bulk ticker snapshots (fast path)...")
        for i, symbol in enumerate(symbols_list, 1):
            # if i % 10 == 0 or i == len(symbols_list):
            #     print(f"  Processed {i}/{len(symbols_list)}")

            binance_price = binance_prices.get(symbol)
            bitget_price = bitget_prices.get(symbol)
            if not (binance_price and bitget_price):
                continue

            if liquidity_filter_enabled:
                bg_liquidity = bitget_liquidity.get(symbol, {})
                binance_quote_volume = float(binance_quote_volumes.get(symbol, 0.0))
                bitget_quote_volume = float(bg_liquidity.get("quote_volume", 0.0))
                binance_top_book = float(binance_top_book_notionals.get(symbol, 0.0))
                bitget_top_book = float(bg_liquidity.get("top_book_notional", 0.0))
                if (
                    binance_quote_volume < MIN_LIQUIDITY_24H_USDT
                    or bitget_quote_volume < MIN_LIQUIDITY_24H_USDT
                    or binance_top_book < MIN_TOP_BOOK_NOTIONAL_USDT
                    or bitget_top_book < MIN_TOP_BOOK_NOTIONAL_USDT
                ):
                    skipped_liquidity += 1
                    continue

            spread_bitget_to_binance = ((bitget_price - binance_price) / binance_price) * 100
            spread_binance_to_bitget = ((binance_price - bitget_price) / bitget_price) * 100

            spreads[symbol] = {
                "binance_price": binance_price,
                "bitget_price": bitget_price,
                "spread_bitget_to_binance": spread_bitget_to_binance,
                "spread_binance_to_bitget": spread_binance_to_bitget,
                "best_direction": "Sell Bitget → Buy Binance" if spread_bitget_to_binance > spread_binance_to_bitget else "Sell Binance → Buy Bitget",
                "best_spread": max(spread_bitget_to_binance, spread_binance_to_bitget),
            }
        if liquidity_filter_enabled:
            print(f"Liquidity filter excluded {skipped_liquidity} symbols")
        return spreads

    print("  Bulk price fetch unavailable, falling back to threaded per-symbol requests...")

    processed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_calculate_symbol_spread, symbol) for symbol in symbols_list]

        for future in as_completed(futures):
            processed += 1
            if processed % 10 == 0 or processed == len(symbols_list):
                print(f"  Processed {processed}/{len(symbols_list)}")

            symbol, data = future.result()
            if data:
                spreads[symbol] = data

    return spreads


def _direction_spread(spread_data: dict, trade_direction: str) -> float:
    """Return spread percentage for the requested arbitrage direction."""
    if trade_direction == "binance_to_bitget":
        # buy binance -> sell bitget
        return float(spread_data.get("spread_bitget_to_binance", 0.0))
    # buy bitget -> sell binance
    return float(spread_data.get("spread_binance_to_bitget", 0.0))


def get_best_spread_asset(
    sample_size: int = 100,
    min_spread_pct: float | None = None,
    max_workers: int | None = None,
    trade_direction: str = "binance_to_bitget",
) -> Optional[str]:
    """Return the single best symbol by spread, or None if no symbol meets threshold."""
    binance_symbols = fetch_binance_symbols()
    bitget_symbols = fetch_bitget_symbols()
    common_symbols = binance_symbols & bitget_symbols
    if not common_symbols:
        return None

    workers = max_workers or int(os.getenv("SPREAD_MAX_WORKERS", str(DEFAULT_MAX_WORKERS)))
    spreads = calculate_spreads(common_symbols, sample_size=sample_size, max_workers=workers)
    if not spreads:
        return None

    threshold = SPREAD_MIN_PCT if min_spread_pct is None else min_spread_pct
    print(f"Spread threshold: {threshold:g}%")
    best_symbol, best_data = max(
        spreads.items(),
        key=lambda x: _direction_spread(x[1], trade_direction),
    )
    if _direction_spread(best_data, trade_direction) >= threshold:
        return best_symbol
    return None


def main() -> None:
    print("Fetching trading symbols from exchanges...")
    binance_symbols = fetch_binance_symbols()
    bitget_symbols = fetch_bitget_symbols()

    print(f"Binance USDT pairs: {len(binance_symbols)}")
    print(f"Bitget USDT pairs: {len(bitget_symbols)}")

    common_symbols = binance_symbols & bitget_symbols
    print(f"Common pairs: {len(common_symbols)}")

    if not common_symbols:
        print("No common symbols found!")
        return

    max_workers = int(os.getenv("SPREAD_MAX_WORKERS", str(DEFAULT_MAX_WORKERS)))
    spreads = calculate_spreads(common_symbols, sample_size=100, max_workers=max_workers)
    print(f"\nSuccessfully sampled {len(spreads)} pairs")

    sorted_spreads = sorted(spreads.items(), key=lambda x: x[1]["best_spread"], reverse=True)

    threshold = SPREAD_MIN_PCT
    top_profitable = [(s, d) for s, d in sorted_spreads if d["best_spread"] > threshold][:3]
    print("\nTop 3 opportunities:")
    if not top_profitable:
        print(f"\n  No opportunities above {threshold}%")
    for idx, (symbol, data) in enumerate(top_profitable, 1):
        print(f"\n  {idx}. {symbol}: {data['best_spread']:.4f}% - {data['best_direction']}")
        print(f"     Binance: ${data['binance_price']:.8f}  |  Bitget: ${data['bitget_price']:.8f}")

    # Save results
    top_3_data = {symbol: data for symbol, data in top_profitable}
    results = {
        "timestamp": str(os.popen("date").read().strip()),
        "total_common_pairs": len(common_symbols),
        "total_sampled": len(spreads),
        "profitable_over_spread_percent": len([d for d in spreads.values() if d["best_spread"] > threshold]),
        "average_spread": sum([d["best_spread"] for d in spreads.values()]) / len(spreads) if spreads else 0,
        "top_3_opportunities": top_3_data,
    }

    with open("spread_analysis_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to spread_analysis_results.json")


if __name__ == "__main__":
    main()
