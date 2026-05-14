import requests
from typing import Optional, Tuple


BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
BITGET_URL = "https://api.bitget.com/api/v2/spot/market/tickers"


def _parse_asset(asset: str, default_quote: str = "USDT") -> Tuple[str, str]:
    symbol = asset.strip().upper().replace("/", "")

    known_quotes = ["USDT", "USDC", "BUSD", "BTC", "ETH"]
    for quote in known_quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote

    return symbol, default_quote


def _build_symbol_for_exchange(base: str, quote: str, exchange: str) -> str:
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





def _collect_prices(base: str, quote: str) -> dict[str, Optional[float]]:
    return {
        "binance": _get_binance_price(_build_symbol_for_exchange(base, quote, "binance")),
        "bitget": _get_bitget_price(_build_symbol_for_exchange(base, quote, "bitget")),
    }


def _print_arbitrage_signal(asset_label: str, prices: dict[str, Optional[float]], threshold_pct: float = 2, trade_amount: float = 100, ask_execute: bool = False) -> Optional[dict]:
    valid = {exchange: price for exchange, price in prices.items() if price is not None}

    print(f"\nAsset: {asset_label}")
    for exchange, price in prices.items():
        if price is None:
            print(f"  {exchange}: unavailable")
        else:
            print(f"  {exchange}: {price:.8f}")

    if len(valid) < 2:
        print("  ✗ Not enough prices")
        return None

    min_exchange, min_price = min(valid.items(), key=lambda x: x[1])
    max_exchange, max_price = max(valid.items(), key=lambda x: x[1])
    spread_pct = ((max_price - min_price) / min_price) * 100

    print(f"\nSpread: {spread_pct:.2f}% (buy {min_exchange}, limit sell {max_exchange})")

    # Profit calculation — use real Binance withdraw fee for the base asset
    base_asset_label = asset_label.replace("USDT", "").replace("USDC", "")
    try:
        from trading_executor import binance_get_min_withdraw_fee_usdt as _bin_min_fee
        wd_fee_units = _bin_min_fee(base_asset_label)
    except Exception:
        wd_fee_units = None
    if wd_fee_units is None:
        # fallback: rough $0.15 worth of base asset (legacy default)
        wd_fee_units = 0.15 / min_price
    withdrawal_fee = wd_fee_units * min_price  # in USDT

    trading_fee_buy = trade_amount * 0.001   # 0.1% Binance taker
    trading_fee_sell = trade_amount * 0.001  # 0.1% Bitget taker

    qty_bought = trade_amount / min_price
    qty_after_withdrawal = qty_bought - wd_fee_units
    gross_revenue = qty_after_withdrawal * max_price

    total_fees = trading_fee_buy + trading_fee_sell + withdrawal_fee
    net_profit = gross_revenue - trade_amount - total_fees
    
    print(f"\nProfit Calculation (${trade_amount:.0f} trade):")
    print(f"  Buy {qty_bought:.4f} @ ${min_price:.8f} = ${trade_amount:.2f}")
    print(f"  Fees: ${total_fees:.2f} (withdraw ${withdrawal_fee:.2f} + trading ${trading_fee_buy + trading_fee_sell:.2f})")
    print(f"  Sell {qty_after_withdrawal:.4f} @ ${max_price:.8f} = ${gross_revenue:.2f}")
    print(f"\n  Net Profit: ${net_profit:.2f}")
    
    # Assessment
    is_profitable = False
    if net_profit > 1.0:
        trades_per_day = int(24*60/3)  # 3 min per trade
        daily = net_profit * trades_per_day
        print(f"  ✓ PROFITABLE! (~{trades_per_day} trades/day = ${daily:.0f}/day)")
        is_profitable = True
    elif net_profit > 0:
        print(f"  ⚠ Marginal (execute if repeats)")
    else:
        print(f"  ✗ Not viable")
    
    # Ask to execute for any positive setup (profitable or marginal)
    if ask_execute and net_profit > 0:
        if net_profit < 1.0:
            print(f"  ✗ Rejected: net profit ${net_profit:.2f} below $1.00 threshold")
            return None
        confirm = input(f"\n💡 Execute this trade? (yes/no): ").strip().lower()
        if confirm == "yes":
            # Return trade details for execution
            return {
                "asset": asset_label.replace("USDT", ""),
                "min_exchange": min_exchange,
                "max_exchange": max_exchange,
                "min_price": min_price,
                "max_price": max_price,
                "trade_amount": trade_amount,
                "profit": net_profit
            }
    
    return None


def main() -> None:
    # You can add one or multiple assets here: BTCUSDT, NEARUSDT, ETHUSDT, etc.
    assets = ["SYNUSDT"]

    # Trade amount for slippage analysis (in USDT)
    trade_amount = 45
    
    # Ask to execute if profitable
    ask_execute = True

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
        trade_info = _print_arbitrage_signal(pair, prices, trade_amount=trade_amount, ask_execute=ask_execute)
        
        # If user confirmed execution
        if trade_info:
            try:
                from trading_executor import buy_binance_sell_bitget, buy_bitget_sell_binance
                print("\n⏳ Initiating trade execution...")
                if trade_info["min_exchange"] == "binance":
                    result = buy_binance_sell_bitget(
                        trade_info["asset"],
                        trade_info["min_price"],
                        trade_info["max_price"],
                        trade_info["trade_amount"]
                    )
                elif trade_info["min_exchange"] == "bitget":
                    result = buy_bitget_sell_binance(
                        trade_info["asset"],
                        trade_info["min_price"],
                        trade_info["max_price"],
                        trade_info["trade_amount"]
                    )
                else:
                    print(f"\n✗ Unsupported buy exchange: {trade_info['min_exchange']}")
                    result = None
                if result:
                    print(f"\n✓ Trade executed! Expected profit: ${result['expected_profit']:.2f}")
                else:
                    print(f"\n✗ Trade execution failed!")
            except ImportError:
                print("\n✗ Could not import trading_executor. Make sure it's in the same directory.")
            except Exception as e:
                print(f"\n✗ Trade execution error: {e}")


if __name__ == "__main__":
    main()
