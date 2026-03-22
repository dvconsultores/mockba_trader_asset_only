import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import threading
import time
from typing import Dict, Optional, List
import requests
import pandas as pd
import numpy as np
from base58 import b58decode
from base64 import urlsafe_b64encode
import urllib.parse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from logs.log_config import apolo_trader_logger as logger
from dotenv import load_dotenv

load_dotenv()

# ✅ Orderly API Config
BASE_URL = os.getenv("ORDERLY_BASE_URL")
ORDERLY_ACCOUNT_ID = os.getenv("ORDERLY_ACCOUNT_ID")
ORDERLY_SECRET = os.getenv("ORDERLY_SECRET")
ORDERLY_PUBLIC_KEY = os.getenv("ORDERLY_PUBLIC_KEY")

if not BASE_URL:
    raise ValueError("❌ ORDERLY_BASE_URL environment variable is not set!")
if not ORDERLY_ACCOUNT_ID:
    raise ValueError("❌ ORDERLY_ACCOUNT_ID environment variable is not set!")
if not ORDERLY_SECRET or not ORDERLY_PUBLIC_KEY:
    raise ValueError("❌ ORDERLY_SECRET or ORDERLY_PUBLIC_KEY environment variables are not set!")

logger.info(f"✅ Orderly API initialized: BASE_URL={BASE_URL}, ACCOUNT_ID={ORDERLY_ACCOUNT_ID[:10]}...")

# ✅ Remove "ed25519:" prefix if present in private key
if ORDERLY_SECRET.startswith("ed25519:"):
    ORDERLY_SECRET = ORDERLY_SECRET.replace("ed25519:", "")

# ✅ Decode Base58 Private Key
private_key = Ed25519PrivateKey.from_private_bytes(b58decode(ORDERLY_SECRET))

# ✅ Rate limiter (Ensures max 8 API requests per second globally)
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            now = time.time()
            self.calls = [call for call in self.calls if call > now - self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                print(f"⏳ Rate limit reached! Sleeping for {sleep_time:.2f} seconds...")
                time.sleep(sleep_time)
            self.calls.append(time.time())

# ✅ Initialize Global Rate Limiter
rate_limiter = RateLimiter(max_calls=10, period=1)

# ✅ Fetch historical Orderly data with global rate limiting
def get_historical_data_limit_apolo(symbol, interval, limit):
    logger.info(f"📥 Fetching historical data: {symbol} {interval} (limit={limit})")
    rate_limiter()  # ✅ Apply global rate limit

    timestamp = str(int(time.time() * 1000))
    params = {"symbol": symbol, "type": interval, "limit": limit}
    path = "/v1/kline"
    query = f"?{urllib.parse.urlencode(params)}"
    message = f"{timestamp}GET{path}{query}"
    signature = urlsafe_b64encode(private_key.sign(message.encode())).decode()

    headers = {
        "orderly-timestamp": timestamp,
        "orderly-account-id": ORDERLY_ACCOUNT_ID,
        "orderly-key": ORDERLY_PUBLIC_KEY,
        "orderly-signature": signature,
    }

    url = f"{BASE_URL}{path}{query}"
    logger.debug(f"Request URL: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Network error fetching {symbol} {interval}: {e}")
        return None

    if response.status_code != 200:
        logger.error(f"❌ API error for {symbol} {interval}: Status {response.status_code}, Response: {response.text[:400]}")
        return None

    data = response.json().get("data", {})
    if not data or "rows" not in data:
        logger.error(f"❌ No data/rows for {symbol} {interval}. Full response: {response.json()}")
        return None

    rows_count = len(data["rows"])
    logger.info(f"✅ Received {rows_count} rows from API for {symbol} {interval}")
    
    df = pd.DataFrame(data["rows"])
    required_columns = ["start_timestamp", "open", "high", "low", "close", "volume"]
    
    if not set(required_columns).issubset(df.columns):
        logger.error(f"❌ Missing required columns for {symbol} {interval}. Found: {df.columns.tolist()}, Required: {required_columns}")
        return None
        
    # Convert start_timestamp from ms to datetime
    df["start_time"] = df["start_timestamp"]  # keep raw ms
    df["start_timestamp"] = pd.to_datetime(df["start_timestamp"], unit="ms", utc=True)
    
    # Optional: set index but still keep the columns
    df = df.set_index("start_timestamp", drop=False)

    # Remove duplicates
    df = df[~df.index.duplicated(keep="first")]

    # Keep column order nice
    df = df[["start_time", "start_timestamp", "open", "high", "low", "close", "volume"]]

    # ✅ SORT CHRONOLOGICALLY (OLDEST → NEWEST)
    df = df.reset_index(drop=True).sort_values('start_timestamp').reset_index(drop=True)
    
    logger.info(f"✅ Final DataFrame: {len(df)} rows with indicators for {symbol} {interval}")
    return df



def get_orderbook(symbol: str, limit: int = 5) -> Dict[str, List[List[str]]]:
    """
    Fetch authenticated order book from Orderly (required for PERP_*_USDC).
    Returns: {"bids": [["price","qty"], ...], "asks": [["price","qty"], ...]}
    """
    rate_limiter()  # ✅ Apply global rate limit

    max_level = min(limit, 500)
    path = f"/v1/orderbook/{symbol}"
    query = f"?max_level={max_level}"

    # Sign the request
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{path}{query}"
    signature = urlsafe_b64encode(private_key.sign(message.encode())).decode()

    headers = {
        "orderly-timestamp": timestamp,
        "orderly-account-id": ORDERLY_ACCOUNT_ID,
        "orderly-key": ORDERLY_PUBLIC_KEY,
        "orderly-signature": signature,
    }

    url = f"{BASE_URL}{path}{query}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return {"bids": [], "asks": []}

        payload = response.json()
        if not payload.get("success") or "data" not in payload:
            return {"bids": [], "asks": []}

        data = payload["data"]
        bids = [[str(b["price"]), str(b["quantity"])] for b in data.get("bids", [])]
        asks = [[str(a["price"]), str(a["quantity"])] for a in data.get("asks", [])]
        return {"bids": bids, "asks": asks}

    except Exception:
        return {"bids": [], "asks": []}

def get_funding_rate_history(symbol: str, limit: int = 1000):
    rate_limiter()
    url = f"{BASE_URL}/v1/public/funding_rate_history"
    r = requests.get(url, params={"symbol": symbol, "limit": limit}, timeout=10)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data", [])
    # Some endpoints use {'data': {'rows': [...]}}
    if isinstance(data, dict) and "rows" in data:
        return data["rows"]
    return data if isinstance(data, list) else []    

def get_market_trades(symbol: str, limit: int = 50) -> List:
    """
    Fetch recent public market trades from Orderly (no auth required).
    Returns list of dicts with: symbol, side, executed_price, executed_quantity, executed_timestamp
    """
    rate_limiter()
    url = f"{BASE_URL}/v1/public/market_trades"
    params = {"symbol": symbol, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            logger.error(f"❌ Market trades API error: {r.status_code}")
            return []
        data = r.json().get("data", {})
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"❌ Error fetching market trades: {e}")
        return []


def get_public_liquidations(symbol: str = None, lookback_hours: int = 24):
    """
    Liquidations in a time window. Many APIs require start_t/end_t in ms.
    """
    rate_limiter()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(lookback_hours * 3600 * 1000)
    params = {"start_t": start_ms, "end_t": end_ms}
    if symbol:
        params["symbol"] = symbol
    url = f"{BASE_URL}/v1/public/liquidated_positions"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json().get("data")
    if isinstance(data, dict):
        # expected shape: {'rows': [...], 'meta': {...}}
        return data.get("rows", [])
    return data or []


# if __name__ == "__main__":
    # data = get_market_trades("PERP_NEAR_USDC", limit=80)
#   # current_price = float(data["close"].iloc[-1])
    # print(data)
#   orderbook = get_orderbook("PERP_BTC_USDC", limit=5)
#   print(orderbook)
    # data = get_funding_rate_history("PERP_BTC_USDC", limit=50)
    # # print(data)
    # data =  get_public_liquidations("PERP_BTC_USDC", lookback_hours=24)
    # print(data)