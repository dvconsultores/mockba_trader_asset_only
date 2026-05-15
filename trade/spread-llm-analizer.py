import os
import json
import requests
from typing import Optional
from collections import defaultdict
import httpx
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

BINANCE_SYMBOLS_URL = "https://api.binance.com/api/v3/exchangeInfo"
BITGET_SYMBOLS_URL = "https://api.bitget.com/api/v2/spot/public/symbols"
BITGET_TICKERS_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BITGET_PRICE_URL = "https://api.bitget.com/api/v2/spot/market/tickers"

DEEPSEEK_API_KEY = os.getenv("DEEP_SEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


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
    print("  Falling back to tickers endpoint for symbols...")
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


def calculate_spreads(common_symbols: set[str], sample_size: int = 50) -> dict:
    """Calculate spreads for both directions (Bitget→Binance and Binance→Bitget).
    No minimum filter - returns all pairs."""
    spreads = {}
    symbols_list = list(common_symbols)[:sample_size]

    print(f"Sampling spreads for {len(symbols_list)} symbols...")
    for i, symbol in enumerate(symbols_list):
        if (i + 1) % 10 == 0:
            print(f"  Processed {i + 1}/{len(symbols_list)}")

        binance_price = get_binance_price(symbol)
        bitget_price = get_bitget_price(symbol)

        if binance_price and bitget_price:
            # Direction 1: Sell Bitget → Buy Binance
            spread_bitget_to_binance = ((bitget_price - binance_price) / binance_price) * 100
            
            # Direction 2: Sell Binance → Buy Bitget
            spread_binance_to_bitget = ((binance_price - bitget_price) / bitget_price) * 100

            spreads[symbol] = {
                "binance_price": binance_price,
                "bitget_price": bitget_price,
                "spread_bitget_to_binance": spread_bitget_to_binance,
                "spread_binance_to_bitget": spread_binance_to_bitget,
                "best_direction": "Sell Bitget → Buy Binance" if spread_bitget_to_binance > spread_binance_to_bitget else "Sell Binance → Buy Bitget",
                "best_spread": max(spread_bitget_to_binance, spread_binance_to_bitget)
            }

    return spreads


def analyze_with_deepseek(spreads_data: dict) -> str:
    """Use DeepSeek to analyze spread data and recommend trading pairs."""
    if not DEEPSEEK_API_KEY:
        return "Error: DEEP_SEEK_API_KEY not set"

    # Sort by best spread
    sorted_spreads = sorted(spreads_data.items(), key=lambda x: x[1]["best_spread"], reverse=True)
    top_spreads = sorted_spreads[:20]

    data_text = "Top 20 assets by best spread (Bitget vs Binance USDT):\n"
    for symbol, data in top_spreads:
        direction = data["best_direction"]
        spread = data["best_spread"]
        data_text += f"- {symbol}: {spread:.4f}% ({direction})\n"

    data_text += f"\nTotal opportunities found: {len(spreads_data)} assets\n"
    avg_spread = sum([d["best_spread"] for d in spreads_data.values()]) / len(spreads_data) if spreads_data else 0
    data_text += f"Average spread: {avg_spread:.4f}%\n"

    prompt = f"""Analyze this arbitrage data between Bitget and Binance. Filter for 1.5%+ spreads only. Be VERY brief.

{data_text}

Provide ONLY:
1. Top 3 best pairs with 2%+ spread
2. One-line reason why
3. Risk level (Low/Medium/High)

Keep it under 200 words."""

    try:
        client = httpx.Client()
        response = client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.5,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error calling DeepSeek API: {e}"


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

    spreads = calculate_spreads(common_symbols, sample_size=100)
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

    spreads = calculate_spreads(common_symbols, sample_size=100)
    print(f"\nSuccessfully sampled {len(spreads)} pairs")

    sorted_spreads = sorted(spreads.items(), key=lambda x: x[1]["best_spread"], reverse=True)
    
    print("\nTop 3 opportunities:")
    for idx, (symbol, data) in enumerate(sorted_spreads[:3], 1):
        print(f"\n  {idx}. {symbol}: {data['best_spread']:.4f}% - {data['best_direction']}")
        print(f"     Binance: ${data['binance_price']:.8f}  |  Bitget: ${data['bitget_price']:.8f}")

    print("\n" + "=" * 60)
    print("Split Capital Strategy Recommendation (2%+ spreads)")
    print("=" * 60)
    
    # Show how split capital works with 1.5%+ spreads
    profitable_pairs = [(s, d) for s, d in sorted_spreads if d["best_spread"] >= 1.5]
    if profitable_pairs:
        print(f"\nPairs with 1.5%+ spread: {len(profitable_pairs)}")
        print("\nExample with $200 capital ($100 each exchange):")
        example_symbol, example_data = profitable_pairs[0]
        spread = example_data["best_spread"]
        profit_gross = 100 * (spread / 100)
        profit_net = profit_gross - 0.20
        print(f"  Pair: {example_symbol} ({spread:.2f}% spread)")
        print(f"  Sell $100 on one exchange -> Get ${100 + profit_gross:.2f}")
        print(f"  Minus fees (~$0.20) -> Net profit: ${profit_net:.2f}")
        print(f"  Trades/day (3-4 spreads): ${profit_net * 3.5:.2f}")
    else:
        print("\nNo pairs with 1.5%+ spread currently.")
        print("Waiting for better opportunities...")
    
    print("\n" + "=" * 60)
    print("Analysis:")
    print("=" * 60)
    analysis = analyze_with_deepseek(spreads)
    print(analysis)

    # Save results
    top_3_data = {symbol: data for symbol, data in sorted_spreads[:3]}
    results = {
        "timestamp": str(os.popen("date").read().strip()),
        "total_common_pairs": len(common_symbols),
        "total_sampled": len(spreads),
        "profitable_1_5plus_percent": len([d for d in spreads.values() if d["best_spread"] >= 1.5]),
        "average_spread": sum([d["best_spread"] for d in spreads.values()]) / len(spreads) if spreads else 0,
        "top_3_opportunities": top_3_data,
        "analysis": analysis,
    }

    with open("spread_analysis_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to spread_analysis_results.json")


if __name__ == "__main__":
    main()
