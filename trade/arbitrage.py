import requests
from typing import Optional, Tuple


BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
BITGET_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
KUCOIN_URL = "https://api.kucoin.com/api/v1/market/orderbook/level1"
BYBIT_URL = "https://api.bybit.com/v5/market/tickers"


def _parse_asset(asset: str, default_quote: str = "USDT") -> Tuple[str, str]:
    symbol = asset.strip().upper().replace("/", "")

    known_quotes = ["USDT", "USDC", "BUSD", "BTC", "ETH"]
    for quote in known_quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote

    return symbol, default_quote


def _build_symbol_for_exchange(base: str, quote: str, exchange: str) -> str:
    if exchange == "kucoin":
        return f"{base}-{quote}"
    return f"{base}{quote}"


def _safe_get_json(url: str, params: dict) -> Optional[dict]:
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _get_binance_price(symbol: str) -> Optional[float]:
    data = _safe_get_json(BINANCE_URL, {"symbol": symbol})
    if not data:
        return None
    price = data.get("price")
    return float(price) if price is not None else None


def _get_bitget_price(symbol: str) -> Optional[float]:
    data = _safe_get_json(BITGET_URL, {"symbol": symbol})
    if not data:
        return None

    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return None

    price = rows[0].get("lastPr")
    return float(price) if price is not None else None


def _get_kucoin_price(symbol: str) -> Optional[float]:
    data = _safe_get_json(KUCOIN_URL, {"symbol": symbol})
    if not data:
        return None

    row = data.get("data") or {}
    price = row.get("price")
    return float(price) if price is not None else None


def _get_bybit_price(symbol: str) -> Optional[float]:
    data = _safe_get_json(BYBIT_URL, {"category": "spot", "symbol": symbol})
    if not data:
        return None

    result = data.get("result") or {}
    rows = result.get("list")
    if not isinstance(rows, list) or not rows:
        return None

    price = rows[0].get("lastPrice")
    return float(price) if price is not None else None


def _collect_prices(base: str, quote: str) -> dict[str, Optional[float]]:
    return {
        "binance": _get_binance_price(_build_symbol_for_exchange(base, quote, "binance")),
        "bitget": _get_bitget_price(_build_symbol_for_exchange(base, quote, "bitget")),
        "kucoin": _get_kucoin_price(_build_symbol_for_exchange(base, quote, "kucoin")),
        "bybit": _get_bybit_price(_build_symbol_for_exchange(base, quote, "bybit")),
    }


def _print_arbitrage_signal(asset_label: str, prices: dict[str, Optional[float]], threshold_pct: float = 0.2) -> None:
    valid = {exchange: price for exchange, price in prices.items() if price is not None}

    print(f"\nAsset: {asset_label}")
    for exchange, price in prices.items():
        if price is None:
            print(f"- {exchange}: unavailable")
        else:
            print(f"- {exchange}: {price:.8f}")

    if len(valid) < 2:
        print("Arbitrage check: not enough exchange prices available.")
        return

    min_exchange, min_price = min(valid.items(), key=lambda x: x[1])
    max_exchange, max_price = max(valid.items(), key=lambda x: x[1])
    spread_pct = ((max_price - min_price) / min_price) * 100

    print(
        f"Spread: buy on {min_exchange} at {min_price:.8f}, "
        f"sell on {max_exchange} at {max_price:.8f} -> {spread_pct:.4f}%"
    )

    if spread_pct >= threshold_pct:
        print(f"Arbitrage possible: YES (>= {threshold_pct:.2f}%)")
    else:
        print(f"Arbitrage possible: NO (< {threshold_pct:.2f}%)")


def main() -> None:
    # You can add one or multiple assets here: BTCUSDT, NEARUSDT, ETHUSDT, etc.
    assets = ["BTCUSDT"]

    # Example token-only input supported as well: "BTC" -> BTCUSDT by default.
    token = "BTC"
    assets.append(token)

    seen = set()
    normalized_assets = []
    for asset in assets:
        base, quote = _parse_asset(asset)
        pair = f"{base}{quote}"
        if pair not in seen:
            seen.add(pair)
            normalized_assets.append((base, quote, pair))

    for base, quote, pair in normalized_assets:
        prices = _collect_prices(base, quote)
        _print_arbitrage_signal(pair, prices)


if __name__ == "__main__":
    main()
