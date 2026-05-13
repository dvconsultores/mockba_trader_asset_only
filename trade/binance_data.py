"""
Binance Spot Data Fetcher - Public endpoints for klines, orderbook, price, trades.
Returns data in the same format as Orderly (historical_data.py) for compatibility.
"""
import os
import sys
import time
import threading
from typing import Dict, List
import requests
import pandas as pd
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logs.log_config import apolo_trader_logger as logger

BINANCE_BASE_URL = "https://api.binance.com"


class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            now = time.time()
            self.calls = [c for c in self.calls if c > now - self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                logger.info(f"⏳ Binance rate limit, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
            self.calls.append(time.time())


rate_limiter = RateLimiter(max_calls=10, period=1)


def get_binance_symbol(orderly_symbol: str) -> str:
    """Convert Orderly symbol format to Binance format.
    PERP_NEAR_USDC → NEARUSDT
    """
    s = orderly_symbol.replace("PERP_", "")
    parts = s.split("_")
    if len(parts) == 2:
        base, _quote = parts
        return f"{base}USDT"
    return s


def get_historical_data_binance(symbol: str, interval: str, limit: int = 100):
    """Fetch klines from Binance. Returns DataFrame matching Orderly format."""
    rate_limiter()

    binance_symbol = get_binance_symbol(symbol)
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": binance_symbol, "interval": interval, "limit": limit}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            logger.error(f"❌ Binance klines error: {response.status_code} {response.text[:200]}")
            return None

        data = response.json()
        if not data:
            logger.error(f"❌ No kline data from Binance for {binance_symbol}")
            return None

        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        df['start_time'] = df['open_time']
        df['start_timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)

        df = df[['start_time', 'start_timestamp', 'open', 'high', 'low', 'close', 'volume']]
        df = df.sort_values('start_timestamp').reset_index(drop=True)

        logger.info(f"✅ Binance: {len(df)} klines for {binance_symbol} {interval}")
        return df

    except Exception as e:
        logger.error(f"❌ Binance klines error: {e}")
        return None


def get_orderbook_binance(symbol: str, limit: int = 20) -> Dict[str, List]:
    """Fetch orderbook from Binance. Returns format matching Orderly."""
    rate_limiter()

    binance_symbol = get_binance_symbol(symbol)
    url = f"{BINANCE_BASE_URL}/api/v3/depth"
    params = {"symbol": binance_symbol, "limit": limit}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return {"bids": [], "asks": []}

        data = response.json()
        return {
            "bids": data.get("bids", []),
            "asks": data.get("asks", [])
        }
    except Exception:
        return {"bids": [], "asks": []}


def get_binance_price(symbol: str) -> float:
    """Get current price from Binance REST API."""
    rate_limiter()

    binance_symbol = get_binance_symbol(symbol)
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    params = {"symbol": binance_symbol}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return float(response.json()["price"])
    except Exception as e:
        logger.error(f"❌ Binance price error: {e}")
    return None


def get_binance_market_trades(symbol: str, limit: int = 50) -> List:
    """Fetch recent trades from Binance. Returns format compatible with Orderly."""
    rate_limiter()

    binance_symbol = get_binance_symbol(symbol)
    url = f"{BINANCE_BASE_URL}/api/v3/trades"
    params = {"symbol": binance_symbol, "limit": limit}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return []
        data = response.json()
        trades = []
        for t in data:
            trades.append({
                'executed_price': float(t['price']),
                'executed_quantity': float(t['qty']),
                'executed_timestamp': t['time'],
                'side': 'SELL' if t.get('isBuyerMaker') else 'BUY'
            })
        return trades
    except Exception:
        return []
