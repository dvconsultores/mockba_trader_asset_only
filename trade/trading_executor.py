import os
import sys
import json
import time
import hmac
import hashlib
import base64
import requests
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple
import logging
from dotenv import load_dotenv

# Add parent directory to path to import db_ops
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db_ops import (
    initialize_database_tables,
    get_dex_asset_chains,
    get_dex_asset_wallet,
    get_latest_dex_asset_wallet,
    upsert_dex_asset_wallet,
)

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load API keys from .env
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip()
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "").strip()
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "").strip()

# API Endpoints
BINANCE_BASE = "https://api.binance.com/api/v3"
BITGET_BASE = "https://api.bitget.com/api/v2"

# Wallet storage
WALLETS_FILE = "wallet_addresses.json"
CHAINS_CACHE_FILE = "chains_cache.json"

# Default trading fees (USD per trade) — used as fallback when live values unavailable
BINANCE_BUY_FEE = 0.10   # ~0.1% of $100 trade
BITGET_SELL_FEE = 0.10   # ~0.1% of $100 trade
WITHDRAWAL_FEE = 0.30    # USDT BEP20 default; replaced dynamically per asset/chain at runtime

# Slippage / liquidity guards
SLIPPAGE_REJECT_PCT = 0.5   # reject if effective Binance buy price is >0.5% above bitget sell price closure
MIN_DEPTH_MULTIPLIER = 1.0  # require Bitget bid depth >= MIN_DEPTH_MULTIPLIER * qty_to_sell at sell price


_BINANCE_TIME_OFFSET_MS = 0  # local_ms + offset = server_ms
_BINANCE_TIMESTAMP_SAFETY_MS = 1500  # keep signed requests slightly behind server time


def _binance_signature(query_string: str) -> str:
    """Generate Binance signature."""
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def _binance_timestamp() -> int:
    """Return Binance server timestamp for signed requests."""
    global _BINANCE_TIME_OFFSET_MS
    try:
        r = requests.get(f"{BINANCE_BASE}/time", timeout=5)
        r.raise_for_status()
        server_ms = int(r.json().get("serverTime", 0))
        return server_ms - _BINANCE_TIMESTAMP_SAFETY_MS
    except Exception:
        return int(time.time() * 1000) + _BINANCE_TIME_OFFSET_MS - _BINANCE_TIMESTAMP_SAFETY_MS


def _refresh_binance_time_offset() -> None:
    """Sync local clock offset with Binance server time."""
    global _BINANCE_TIME_OFFSET_MS
    try:
        r = requests.get(f"{BINANCE_BASE}/time", timeout=5)
        r.raise_for_status()
        server_ms = int(r.json().get("serverTime", 0))
        local_ms = int(time.time() * 1000)
        _BINANCE_TIME_OFFSET_MS = server_ms - local_ms
        logger.info(f"Binance time offset synced: {_BINANCE_TIME_OFFSET_MS} ms")
    except Exception as e:
        logger.warning(f"Could not sync Binance time offset: {e}")


_BITGET_TIME_OFFSET_MS = 0


def _bitget_timestamp() -> str:
    """Return current timestamp adjusted to Bitget server time."""
    return str(int(time.time() * 1000) + _BITGET_TIME_OFFSET_MS)


def _refresh_bitget_time_offset() -> None:
    """Sync local clock offset with Bitget server time."""
    global _BITGET_TIME_OFFSET_MS
    try:
        r = requests.get("https://api.bitget.com/api/v2/public/time", timeout=5)
        r.raise_for_status()
        server_ms = int(r.json().get("data", {}).get("serverTime", 0))
        local_ms = int(time.time() * 1000)
        _BITGET_TIME_OFFSET_MS = server_ms - local_ms
        logger.info(f"Bitget time offset synced: {_BITGET_TIME_OFFSET_MS} ms")
    except Exception as e:
        logger.warning(f"Could not sync Bitget time offset: {e}")


# Sync on import
_refresh_binance_time_offset()
_refresh_bitget_time_offset()


def _bitget_signature(timestamp: str, method: str, path: str, body: str = "") -> str:
    """Generate Bitget v2 signature (base64-encoded HMAC-SHA256).

    Bitget signs the FULL request path including the /api/v2 prefix.
    Callers pass the relative path (e.g. /spot/wallet/...); we normalize here.
    """
    if not path.startswith("/api/"):
        path = "/api/v2" + path
    message = timestamp + method.upper() + path + body
    digest = hmac.new(
        BITGET_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def load_wallets() -> Dict:
    """Load wallet addresses from file."""
    if os.path.exists(WALLETS_FILE):
        with open(WALLETS_FILE, 'r') as f:
            return json.load(f)
    return {"binance": {}, "bitget": {}}


def save_wallets(wallets: Dict):
    """Save wallet addresses to file."""
    with open(WALLETS_FILE, 'w') as f:
        json.dump(wallets, f, indent=2)


def add_wallet(exchange: str, asset: str, chain: str, address: str):
    """Store wallet address in SQLite manual table."""
    initialize_database_tables()
    upsert_dex_asset_wallet(exchange, asset, address, chain)
    logger.info(f"✓ Saved {exchange} wallet: {asset} on {chain} = {address}")


def get_wallet(exchange: str, asset: str, chain: str) -> Optional[str]:
    """Get stored wallet address from SQLite manual table."""
    initialize_database_tables()
    return get_dex_asset_wallet(exchange, asset, chain)


def _prompt_manual_wallet_mapping(asset: str, exchange: str) -> Tuple[Optional[str], Optional[str]]:
    """Prompt user to manually register dex/asset/chain/wallet when mapping is missing."""
    print(f"\nNo configured chain found for asset {asset}.")
    dex = input(f"Enter dex/exchange (default {exchange}): ").strip().lower() or exchange
    chain = input("Enter chain/network (e.g. CHZ2, TRX, ETH): ").strip().upper()
    wallet = input("Enter wallet address: ").strip()

    if not dex or not chain or not wallet:
        print("Invalid input. Mapping not saved.")
        return None, None

    add_wallet(dex, asset, chain, wallet)
    print(f"✓ Saved mapping: dex={dex}, asset={asset}, chain={chain}")
    if dex == exchange:
        return chain, wallet
    return None, None


def load_chains_cache() -> Dict:
    """Load chains cache from file."""
    if os.path.exists(CHAINS_CACHE_FILE):
        try:
            with open(CHAINS_CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load chains cache: {e}")
    return {"binance": {}, "bitget": {}}


def save_chains_cache(cache: Dict):
    """Save chains cache to file."""
    try:
        with open(CHAINS_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save chains cache: {e}")


def get_binance_chains(asset: str) -> List[str]:
    """Get available chains for asset from Binance using config/getall endpoint (SIGNED)."""
    try:
        timestamp = _binance_timestamp()
        params = {"timestamp": timestamp, "recvWindow": 10000}
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        params["signature"] = _binance_signature(query_string)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.get(
            "https://api.binance.com/sapi/v1/capital/config/getall",
            params=params,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        # Find the asset in the response
        for coin_info in data:
            if coin_info.get("coin") == asset:
                networks = coin_info.get("networkList", [])
                chains = [n.get("network") for n in networks if n.get("network")]
                logger.info(f"✓ Binance chains for {asset}: {chains}")

                # Cache it
                cache = load_chains_cache()
                cache["binance"][asset] = chains
                save_chains_cache(cache)

                return sorted(chains)

        logger.debug(f"Asset {asset} not found in Binance config")
        return []
    except Exception as e:
        logger.debug(f"Binance chain fetch failed: {e}. Falling back to Bitget chains...")
        # Fallback to Bitget chains (they're usually more complete)
        bitget_chains = get_bitget_chains(asset)
        if bitget_chains:
            logger.info(f"✓ Using Bitget chains for {asset}: {bitget_chains}")
            # Cache as Binance chains too
            cache = load_chains_cache()
            cache["binance"][asset] = bitget_chains
            save_chains_cache(cache)
            return bitget_chains
        return []


def get_bitget_chains(asset: str) -> List[str]:
    """Get available chains for asset from Bitget public coins endpoint."""
    try:
        response = requests.get(
            f"{BITGET_BASE}/spot/public/coins",
            params={"coin": asset},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()

        chains: List[str] = []
        for coin in result.get("data", []):
            if coin.get("coin", "").upper() != asset.upper():
                continue
            for c in coin.get("chains", []):
                name = c.get("chain")
                if name and c.get("rechargeable") in ("true", True, "yes"):
                    chains.append(name)
        logger.info(f"✓ Bitget chains for {asset}: {chains}")

        # Cache it
        cache = load_chains_cache()
        cache["bitget"][asset] = chains
        save_chains_cache(cache)

        return sorted(set(chains))
    except Exception as e:
        logger.warning(f"Could not fetch Bitget chains for {asset}: {e}")
        return []


def get_available_chains(exchange: str, asset: str, use_cache: bool = True) -> List[str]:
    """Get available chains for asset on exchange. Query priority: SQLite → API → Hardcoded."""
    # Hardcoded common chains as final fallback
    COMMON_CHAINS = {
        "USDT": ["TRX", "ETH", "BSC"],
        "USDC": ["ETH", "SOL"],
        "BTC": ["Bitcoin"],
        "ETH": ["Ethereum"],
        "JUV": ["CHZ2"],  # Chiliz Chain (verified from Binance)
        "FLUX": ["ETH", "BSC"],
        "POWR": ["ETH", "BSC"],
    }

    # Layer 1: Try SQLite manual mapping table first
    try:
        initialize_database_tables()
        chains = get_dex_asset_chains(exchange, asset)
        if chains:
            logger.info(f"[SQLite] Found chains for {asset}: {chains}")
            return chains
    except Exception as e:
        logger.debug(f"SQLite lookup failed for {asset}: {e}")

    # Layer 2: Try cache if requested
    if use_cache:
        cache = load_chains_cache()
        if asset in cache.get(exchange, {}):
            chains = cache[exchange][asset]
            if chains:
                logger.info(f"[Cache] Using cached chains for {exchange}/{asset}: {chains}")
                return chains

    # Layer 3: Try API
    if exchange == "binance":
        chains = get_binance_chains(asset)
    elif exchange == "bitget":
        chains = get_bitget_chains(asset)
    else:
        return []

    if chains:
        logger.info(f"[API] Got chains from {exchange} for {asset}: {chains}")
        return chains

    # Layer 4: Final fallback to hardcoded
    if asset in COMMON_CHAINS:
        logger.info(f"[Hardcoded] Using fallback chains for {asset}: {COMMON_CHAINS[asset]}")
        return COMMON_CHAINS[asset]

    logger.warning(f"⚠ No chains found for {asset} (all layers exhausted)")

    # Manual fallback requested by user
    chain, _wallet = _prompt_manual_wallet_mapping(asset, exchange)
    if chain:
        return [chain]

    return []


# ============================================================================
# BINANCE FUNCTIONS
# ============================================================================

def _normalize_binance_quantity(symbol: str, quantity: float) -> float:
    """Normalize quantity to Binance LOT_SIZE step for a symbol."""
    try:
        info_resp = requests.get(
            f"{BINANCE_BASE}/exchangeInfo",
            params={"symbol": symbol},
            timeout=10,
        )
        info_resp.raise_for_status()
        symbol_info = info_resp.json().get("symbols", [])[0]

        lot_size = None
        for f in symbol_info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                lot_size = f
                break

        if not lot_size:
            return quantity

        step = Decimal(lot_size.get("stepSize", "0.00000001"))
        min_qty = Decimal(lot_size.get("minQty", "0"))
        qty = Decimal(str(quantity))

        # Floor to step size to satisfy LOT_SIZE filter.
        if step > 0:
            qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

        if qty < min_qty:
            qty = min_qty

        return float(qty)
    except Exception as e:
        logger.warning(f"Could not normalize quantity for {symbol}, using raw value: {e}")
        return quantity

def binance_market_buy(symbol: str, quantity: float) -> Optional[Dict]:
    """Market buy on Binance using quoteOrderQty (USDT amount) to avoid LOT_SIZE issues."""
    response = None
    try:
        quantity = _normalize_binance_quantity(symbol, quantity)

        # Convert qty to USDT notional via current price
        price_resp = requests.get(
            f"{BINANCE_BASE}/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        price_resp.raise_for_status()
        last_price = float(price_resp.json().get("price", 0))
        if last_price <= 0:
            logger.error(f"✗ Invalid price for {symbol}")
            return None

        quote_order_qty = round(quantity * last_price, 2)

        timestamp = _binance_timestamp()
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": quote_order_qty,
            "timestamp": timestamp,
            "recvWindow": 10000
        }
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        params["signature"] = _binance_signature(query_string)

        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.post(
            f"{BINANCE_BASE}/order",
            params=params,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"✓ Market buy {symbol} (~${quote_order_qty}) - Order ID: {result.get('orderId')}")
        return result
    except Exception as e:
        body = response.text if response is not None else ""
        logger.error(f"✗ Binance market buy failed: {e} | response={body}")
        return None


def binance_limit_sell(symbol: str, quantity: float, price: float) -> Optional[Dict]:
    """Limit sell on Binance."""
    response = None
    try:
        quantity = _normalize_binance_quantity(symbol, quantity)
        timestamp = _binance_timestamp()
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
            "timestamp": timestamp,
            "recvWindow": 10000
        }
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        params["signature"] = _binance_signature(query_string)

        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.post(
            f"{BINANCE_BASE}/order",
            params=params,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"✓ Limit sell {quantity} {symbol} @ ${price} - Order ID: {result.get('orderId')}")
        return result
    except Exception as e:
        body = response.text if response is not None else ""
        logger.error(f"✗ Binance limit sell failed: {e} | response={body}")
        return None


def binance_withdraw(asset: str, address: str, amount: float, chain: str) -> Optional[Dict]:
    """Withdraw from Binance."""
    response = None
    try:
        timestamp = _binance_timestamp()
        params = {
            "coin": asset,
            "withdrawOrderId": f"withdraw_{timestamp}",
            "network": chain,
            "address": address,
            "amount": amount,
            "timestamp": timestamp,
            "recvWindow": 10000
        }
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        params["signature"] = _binance_signature(query_string)

        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.post(
            f"{BINANCE_BASE}/capital/withdraw/apply",
            params=params,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"✓ Withdraw {amount} {asset} from Binance via {chain} - ID: {result.get('id')}")
        return result
    except Exception as e:
        body = response.text if response is not None else ""
        logger.error(f"✗ Binance withdraw failed: {e} | response={body}")
        return None


# ============================================================================
# BITGET FUNCTIONS
# ============================================================================

def bitget_market_buy(symbol: str, quantity: float) -> Optional[Dict]:
    """Market buy on Bitget."""
    try:
        timestamp = _bitget_timestamp()

        body = {
            "symbol": symbol,
            "side": "buy",
            "orderType": "market",
            "quantity": str(quantity),
            "force": "GTC"
        }
        body_json = json.dumps(body)

        path = "/spot/trade/orders"
        signature = _bitget_signature(timestamp, "POST", path, body_json)

        headers = {
            "ACCESS-KEY": BITGET_API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type": "application/json"
        }

        response = requests.post(
            f"{BITGET_BASE}{path}",
            json=body,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        order_id = result.get("data", {}).get("orderId", "unknown")
        logger.info(f"✓ Market buy {quantity} {symbol} on Bitget - Order ID: {order_id}")
        return result
    except Exception as e:
        logger.error(f"✗ Bitget market buy failed: {e}")
        return None


def bitget_limit_sell(symbol: str, quantity: float, price: float) -> Optional[Dict]:
    """Limit sell on Bitget."""
    try:
        timestamp = _bitget_timestamp()

        body = {
            "symbol": symbol,
            "side": "sell",
            "orderType": "limit",
            "quantity": str(quantity),
            "price": str(price),
            "force": "GTC"
        }
        body_json = json.dumps(body)

        path = "/spot/trade/orders"
        signature = _bitget_signature(timestamp, "POST", path, body_json)

        headers = {
            "ACCESS-KEY": BITGET_API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type": "application/json"
        }

        response = requests.post(
            f"{BITGET_BASE}{path}",
            json=body,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        order_id = result.get("data", {}).get("orderId", "unknown")
        logger.info(f"✓ Limit sell {quantity} {symbol} @ ${price} on Bitget - Order ID: {order_id}")
        return result
    except Exception as e:
        logger.error(f"✗ Bitget limit sell failed: {e}")
        return None


def bitget_withdraw(asset: str, address: str, amount: float, chain: str) -> Optional[Dict]:
    """Withdraw from Bitget."""
    try:
        timestamp = _bitget_timestamp()

        body = {
            "coin": asset,
            "transferType": 1,  # On-chain withdrawal
            "address": address,
            "chain": chain,
            "amount": str(amount),
            "fee": "0.1"
        }
        body_json = json.dumps(body)

        path = "/spot/wallet/withdrawal"
        signature = _bitget_signature(timestamp, "POST", path, body_json)

        headers = {
            "ACCESS-KEY": BITGET_API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type": "application/json"
        }

        response = requests.post(
            f"{BITGET_BASE}{path}",
            json=body,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
        logger.info(f"✓ Withdraw {amount} {asset} from Bitget via {chain}")
        return result
    except Exception as e:
        logger.error(f"✗ Bitget withdraw failed: {e}")
        return None


def bitget_get_deposit_address(asset: str, chain: str) -> Optional[str]:
    """Get Bitget deposit address."""
    # Try primary chain plus lightweight aliases and Bitget-reported chains.
    candidates: List[str] = []
    seen: set[str] = set()

    def _add_candidate(value: str):
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    _add_candidate(chain)
    _add_candidate(chain.upper())
    _add_candidate(chain.lower())

    # Example: CHZ2 -> CHZ (some exchanges append a suffix).
    chain_no_digits = "".join(ch for ch in chain if not ch.isdigit())
    _add_candidate(chain_no_digits)
    _add_candidate(chain_no_digits.upper())

    # Chiliz alias normalization across exchanges (e.g. CHZ2 <-> CAP20).
    chain_upper = chain.upper()
    if chain_upper.startswith("CHZ") or "CHILI" in chain_upper:
        _add_candidate("CAP20")
        _add_candidate("cap20")

    bitget_chains = get_bitget_chains(asset)
    for bitget_chain in bitget_chains:
        _add_candidate(bitget_chain)

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": "",
        "ACCESS-TIMESTAMP": "",
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json"
    }

    for chain_candidate in candidates:
        response = None
        try:
            timestamp = _bitget_timestamp()
            path = f"/spot/wallet/deposit-address?coin={asset}&chain={chain_candidate}"
            signature = _bitget_signature(timestamp, "GET", path)

            headers["ACCESS-SIGN"] = signature
            headers["ACCESS-TIMESTAMP"] = timestamp

            response = requests.get(
                f"{BITGET_BASE}{path}",
                headers=headers,
                timeout=10
            )
            response.raise_for_status()

            result = response.json()
            address = result.get("data", {}).get("address")
            if address:
                logger.info(
                    f"✓ Got Bitget deposit address for {asset} using chain {chain_candidate}: {address}"
                )
                return address
        except Exception as e:
            body = response.text if response is not None else ""
            logger.warning(
                f"Deposit address lookup failed for {asset} on chain {chain_candidate}: {e} | response={body}"
            )

    logger.error(
        f"✗ Failed to get Bitget deposit address for {asset}. Tried chains: {candidates}"
    )
    return None


def binance_get_deposit_address(asset: str, chain: str) -> Optional[str]:
    """Get Binance deposit address for asset on chain (SIGNED)."""
    try:
        timestamp = _binance_timestamp()
        params = {
            "coin": asset,
            "network": chain,
            "timestamp": timestamp,
            "recvWindow": 10000,
        }
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        params["signature"] = _binance_signature(query_string)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.get(
            "https://api.binance.com/sapi/v1/capital/deposit/address",
            params=params,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        address = data.get("address")
        if address:
            logger.info(f"✓ Got Binance deposit address for {asset} via {chain}: {address}")
        return address
    except Exception as e:
        logger.warning(f"Binance deposit address lookup failed for {asset}/{chain}: {e}")
        return None


# Chain naming differs between exchanges. Map Binance network code -> Bitget chain code (and vice versa).
_BINANCE_TO_BITGET_CHAIN = {
    "BSC": "BEP20",
    "ETH": "ERC20",
    "TRX": "TRC20",
    "MATIC": "Polygon",
    "POLYGON": "Polygon",
    "AVAXC": "C-Chain",
    "ARBITRUM": "ArbitrumOne",
    "OPTIMISM": "Optimism",
    "SOL": "SOL",
    "BTC": "BTC",
    "CHZ2": "CAP20",
}
_BITGET_TO_BINANCE_CHAIN = {v: k for k, v in _BINANCE_TO_BITGET_CHAIN.items()}

# Preference order (cheap first)
_CHAIN_PREFERENCE = ["BSC", "TRX", "MATIC", "ARBITRUM", "OPTIMISM", "SOL", "AVAXC", "ETH"]


def _normalize_chain_pair(binance_chain: str, bitget_chain: str) -> bool:
    """Return True if the two chain codes refer to the same network."""
    b = binance_chain.upper()
    g = bitget_chain.upper()
    if b == g:
        return True
    if _BINANCE_TO_BITGET_CHAIN.get(b, "").upper() == g:
        return True
    if _BITGET_TO_BINANCE_CHAIN.get(g, "").upper() == b:
        return True
    return False


def auto_resolve_deposit(deposit_exchange: str, source_exchange: str, asset: str) -> Optional[Tuple[str, str]]:
    """
    Find a chain supported by both exchanges and fetch the deposit address from `deposit_exchange`.
    Returns (chain_on_deposit_exchange, address). Persists to dex_asset_wallets.
    """
    initialize_database_tables()

    # Reuse cached row if present
    cached = get_latest_dex_asset_wallet(deposit_exchange, asset)
    if cached:
        return cached

    binance_chains = get_binance_chains(asset) if "binance" in (deposit_exchange, source_exchange) else []
    bitget_chains = get_bitget_chains(asset) if "bitget" in (deposit_exchange, source_exchange) else []

    if deposit_exchange == "bitget":
        src_chains, dst_chains = binance_chains, bitget_chains
    else:
        src_chains, dst_chains = bitget_chains, binance_chains

    # Build common-chain pairs (src_chain_name, dst_chain_name)
    pairs: List[Tuple[str, str]] = []
    for sc in src_chains:
        for dc in dst_chains:
            if _normalize_chain_pair(sc, dc) if deposit_exchange == "bitget" else _normalize_chain_pair(dc, sc):
                pairs.append((sc, dc))

    if not pairs:
        logger.error(f"✗ No common chain between exchanges for {asset}. binance={binance_chains} bitget={bitget_chains}")
        return None

    # Order pairs by preference (using the binance-side name for ranking)
    def rank(pair):
        bin_name = pair[0] if deposit_exchange == "bitget" else pair[1]
        try:
            return _CHAIN_PREFERENCE.index(bin_name.upper())
        except ValueError:
            return len(_CHAIN_PREFERENCE)
    pairs.sort(key=rank)

    for src_chain, dst_chain in pairs:
        if deposit_exchange == "bitget":
            addr = bitget_get_deposit_address(asset, dst_chain)
        else:
            addr = binance_get_deposit_address(asset, dst_chain)
        if addr:
            upsert_dex_asset_wallet(deposit_exchange, asset, addr, dst_chain)
            logger.info(f"✓ Auto-resolved {asset} on {deposit_exchange}: chain={dst_chain}, addr={addr}")
            return dst_chain, addr

    logger.error(f"✗ Tried all common chains for {asset}, none returned a deposit address")
    return None


# ============================================================================
# INTERACTIVE CLI
# ============================================================================

def menu_add_wallet():
    """Interactive wallet addition."""
    print("\n=== Add Wallet Address ===")
    exchange = input("Exchange (binance/bitget): ").lower()
    if exchange not in ["binance", "bitget"]:
        print("Invalid exchange")
        return

    asset = input("Asset (e.g., USDT, JUV, BTC): ").upper()
    print(f"\nFetching available chains for {asset} on {exchange}...")

    chains = get_available_chains(exchange, asset, use_cache=False)

    if not chains:
        print(f"✗ No chains found for {asset} on {exchange}")
        print("This might be an invalid asset. Verify the asset symbol.")
        return

    print(f"\n✓ Available chains for {asset}:")
    for i, chain in enumerate(chains, 1):
        print(f"  {i}. {chain}")

    try:
        selection = int(input(f"\nSelect chain (1-{len(chains)}): ")) - 1
        if selection < 0 or selection >= len(chains):
            print("Invalid selection")
            return
        chain = chains[selection]
    except ValueError:
        print("Invalid input")
        return

    address = input(f"\nEnter {exchange} {asset} address on {chain}: ").strip()

    if not address:
        print("Invalid address")
        return

    add_wallet(exchange, asset, chain, address)


def menu_withdraw():
    """Interactive withdrawal."""
    print("\n=== Withdraw ===")
    exchange = input("Exchange (binance/bitget): ").lower()

    if exchange not in ["binance", "bitget"]:
        print("Invalid exchange")
        return

    asset = input("Asset to withdraw: ").upper()
    print(f"\nFetching available chains for {asset} on {exchange}...")

    chains = get_available_chains(exchange, asset, use_cache=True)

    if not chains:
        print(f"✗ No chains available for {asset}")
        return

    print(f"✓ Available chains:")
    for i, chain in enumerate(chains, 1):
        print(f"  {i}. {chain}")

    try:
        selection = int(input(f"\nSelect chain (1-{len(chains)}): ")) - 1
        if selection < 0 or selection >= len(chains):
            print("Invalid selection")
            return
        chain = chains[selection]
    except ValueError:
        print("Invalid input")
        return

    amount = float(input(f"Amount to withdraw: "))

    address = get_wallet(exchange, asset, chain)
    if not address:
        print(f"\nNo stored address found for {exchange}/{asset}/{chain}")
        address = input("Enter receiving address: ").strip()
        if address:
            save = input("Save this address for future use? (y/n): ").lower()
            if save == 'y':
                add_wallet(exchange, asset, chain, address)

    if not address:
        print("No address provided")
        return

    print(f"\n✓ Withdrawing {amount} {asset} via {chain}...")

    if exchange == "binance":
        result = binance_withdraw(asset, address, amount, chain)
    else:
        result = bitget_withdraw(asset, address, amount, chain)

    if result:
        print(f"✓ Success!")
    else:
        print(f"✗ Failed")


def menu_market_buy():
    """Interactive market buy."""
    print("\n=== Market Buy ===")
    exchange = input("Exchange (binance/bitget): ").lower()
    symbol = input("Symbol (e.g., JUVUSDT): ").upper()
    quantity = float(input(f"Quantity to buy: "))

    if exchange == "binance":
        result = binance_market_buy(symbol, quantity)
    elif exchange == "bitget":
        result = bitget_market_buy(symbol, quantity)
    else:
        print("Invalid exchange")
        return

    if result:
        print(f"✓ Order placed successfully")


def menu_limit_sell():
    """Interactive limit sell."""
    print("\n=== Limit Sell ===")
    exchange = input("Exchange (binance/bitget): ").lower()
    symbol = input("Symbol (e.g., JUVUSDT): ").upper()
    quantity = float(input(f"Quantity to sell: "))
    price = float(input(f"Price per unit: "))

    if exchange == "binance":
        result = binance_limit_sell(symbol, quantity, price)
    elif exchange == "bitget":
        result = bitget_limit_sell(symbol, quantity, price)
    else:
        print("Invalid exchange")
        return

    if result:
        print(f"✓ Order placed successfully")


# ============================================================================
# PRE-FLIGHT HELPERS (balances, withdraw fees, withdrawability, order books)
# ============================================================================

def binance_get_balance(asset: str) -> Optional[float]:
    """Return free balance for `asset` on Binance Spot. None on failure."""
    try:
        timestamp = _binance_timestamp()
        params = {"timestamp": timestamp, "recvWindow": 10000}
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = _binance_signature(query_string)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        r = requests.get(f"{BINANCE_BASE}/account", params=params, headers=headers, timeout=10)
        r.raise_for_status()
        for b in r.json().get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free", 0))
        return 0.0
    except Exception as e:
        logger.warning(f"Binance balance fetch failed for {asset}: {e}")
        return None


def bitget_get_balance(asset: str) -> Optional[float]:
    """Return available balance for `asset` on Bitget Spot. None on failure."""
    try:
        timestamp = _bitget_timestamp()
        path = f"/spot/account/assets?coin={asset}"
        signature = _bitget_signature(timestamp, "GET", path)
        headers = {
            "ACCESS-KEY": BITGET_API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
            "Content-Type": "application/json",
        }
        r = requests.get(f"{BITGET_BASE}{path}", headers=headers, timeout=10)
        r.raise_for_status()
        for item in r.json().get("data", []) or []:
            if item.get("coin") == asset:
                return float(item.get("available", 0))
        return 0.0
    except Exception as e:
        logger.warning(f"Bitget balance fetch failed for {asset}: {e}")
        return None


def _binance_get_capital_config() -> Optional[List[Dict]]:
    """Fetch Binance capital config with one clock-resync retry on -1021."""
    endpoint = "https://api.binance.com/sapi/v1/capital/config/getall"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    for attempt in range(2):
        timestamp = _binance_timestamp()
        params = {"timestamp": timestamp, "recvWindow": 10000}
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = _binance_signature(query_string)

        r = requests.get(endpoint, params=params, headers=headers, timeout=10)

        # Binance can format JSON with/without spaces; parse error code robustly.
        err_code = None
        if r.status_code >= 400:
            try:
                err_code = r.json().get("code")
            except Exception:
                err_code = None

        if err_code == -1021 and attempt == 0:
            logger.warning("Binance timestamp ahead (-1021) on capital config; resyncing clock and retrying once")
            _refresh_binance_time_offset()
            continue

        if r.status_code >= 400:
            raise RuntimeError(f"Binance capital config failed: status={r.status_code}, body={r.text[:300]}")

        data = r.json()
        return data if isinstance(data, list) else None

    return None


def binance_get_withdraw_fee(asset: str, chain: str) -> Optional[float]:
    """Return per-withdrawal fee for `asset` on `chain` from Binance config/getall."""
    try:
        rows = _binance_get_capital_config()
        if not rows:
            return None
        for coin in rows:
            if coin.get("coin") == asset:
                for n in coin.get("networkList", []):
                    if n.get("network", "").upper() == chain.upper():
                        return float(n.get("withdrawFee", 0))
        return None
    except Exception as e:
        logger.warning(f"Binance withdraw-fee lookup failed for {asset}/{chain}: {e}")
        return None


def binance_get_min_withdraw_fee_usdt(asset: str = "USDT") -> Optional[float]:
    """Return cheapest withdrawal fee across all chains for `asset` (Binance side).

    For arbitrage signal calc: we don't yet know which chain will be used,
    so use the cheapest available as the optimistic estimate.
    """
    try:
        rows = _binance_get_capital_config()
        if not rows:
            return None
        for coin in rows:
            if coin.get("coin") == asset:
                fees = [
                    float(n.get("withdrawFee", 0))
                    for n in coin.get("networkList", [])
                    if n.get("withdrawEnable")
                ]
                if fees:
                    return min(fees)
        return None
    except Exception as e:
        logger.warning(f"Binance min withdraw-fee lookup failed for {asset}: {e}")
        return None


def binance_is_withdrawable(asset: str, chain: str) -> bool:
    """True if Binance allows withdrawal of `asset` on `chain` right now."""
    try:
        rows = _binance_get_capital_config()
        if not rows:
            return False
        for coin in rows:
            if coin.get("coin") == asset:
                for n in coin.get("networkList", []):
                    if n.get("network", "").upper() == chain.upper():
                        return bool(n.get("withdrawEnable"))
        return False
    except Exception as e:
        logger.warning(f"Binance withdrawable lookup failed for {asset}/{chain}: {e}")
        return False


def bitget_get_chain_info(asset: str, chain: str) -> Optional[Dict]:
    """Return Bitget per-chain coin info dict (rechargeable, withdrawable, fees)."""
    try:
        r = requests.get(
            f"{BITGET_BASE}/spot/public/coins",
            params={"coin": asset}, timeout=10,
        )
        r.raise_for_status()
        for coin in r.json().get("data", []) or []:
            if coin.get("coin", "").upper() != asset.upper():
                continue
            for c in coin.get("chains", []) or []:
                if (c.get("chain") or "").upper() == chain.upper():
                    return c
        return None
    except Exception as e:
        logger.warning(f"Bitget chain info fetch failed for {asset}/{chain}: {e}")
        return None


def bitget_is_withdrawable(asset: str, chain: str) -> bool:
    """True if Bitget allows withdrawal of `asset` on `chain` right now."""
    info = bitget_get_chain_info(asset, chain)
    if not info:
        return False
    val = info.get("withdrawable")
    return val in (True, "true", "yes")


def bitget_get_withdraw_fee(asset: str, chain: str) -> Optional[float]:
    info = bitget_get_chain_info(asset, chain)
    if not info:
        return None
    fee = info.get("withdrawFee") or info.get("withdraw_fee") or info.get("fee")
    try:
        return float(fee) if fee is not None else None
    except (TypeError, ValueError):
        return None


def binance_get_orderbook(symbol: str, limit: int = 20) -> Optional[Dict]:
    try:
        r = requests.get(f"{BINANCE_BASE}/depth", params={"symbol": symbol, "limit": limit}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Binance orderbook fetch failed for {symbol}: {e}")
        return None


def bitget_get_orderbook(symbol: str, limit: int = 20) -> Optional[Dict]:
    try:
        r = requests.get(
            f"{BITGET_BASE}/spot/market/orderbook",
            params={"symbol": symbol, "limit": str(limit), "type": "step0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        return data
    except Exception as e:
        logger.warning(f"Bitget orderbook fetch failed for {symbol}: {e}")
        return None


def estimate_buy_vwap(asks: List, quote_amount: float) -> Optional[Tuple[float, float]]:
    """Walk asks [[price, qty], ...] consuming `quote_amount` USDT.
    Returns (vwap_price, base_qty_obtained) or None if depth insufficient."""
    remaining = quote_amount
    base_qty = 0.0
    cost = 0.0
    for level in asks:
        try:
            price = float(level[0])
            qty = float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        level_cost = price * qty
        if level_cost >= remaining:
            take_qty = remaining / price
            base_qty += take_qty
            cost += remaining
            remaining = 0
            break
        else:
            base_qty += qty
            cost += level_cost
            remaining -= level_cost
    if remaining > 0 or base_qty <= 0:
        return None
    return cost / base_qty, base_qty


def cumulative_bid_qty_at_or_above(bids: List, target_price: float) -> float:
    """Sum bid quantity at price >= target_price."""
    total = 0.0
    for level in bids:
        try:
            price = float(level[0])
            qty = float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        if price >= target_price:
            total += qty
    return total


# ============================================================================
# AUTOMATED ARBITRAGE EXECUTION
# ============================================================================

def buy_binance_sell_bitget(base_asset: str, binance_price: float, bitget_price: float, trade_amount_usdt: float = 100) -> Optional[Dict]:
    """
    Execute full arbitrage cycle:
    1. Buy on Binance
    2. Withdraw from Binance to Bitget
    3. Limit sell on Bitget
    4. Withdraw USDT back to Binance
    """
    symbol = f"{base_asset}USDT"

    logger.info(f"\n{'='*70}")
    logger.info(f"STARTING ARBITRAGE: BUY {base_asset} ON BINANCE, SELL ON BITGET")
    logger.info(f"{'='*70}")

    chain = None
    bitget_deposit_addr = None
    # Prefer manual DB mapping for destination deposit (bitget/base_asset)
    manual_deposit = get_latest_dex_asset_wallet("bitget", base_asset)
    if manual_deposit:
        chain, bitget_deposit_addr = manual_deposit
        logger.info(
            f"\n[1] Using table mapping for {base_asset} deposit: chain={chain}, wallet={bitget_deposit_addr}"
        )
    else:
        # Auto-resolve: intersect Binance withdraw networks ∩ Bitget deposit chains, fetch address, cache to DB
        logger.info(f"\n[1] Auto-resolving deposit chain/address for {base_asset} on Bitget...")
        resolved = auto_resolve_deposit("bitget", "binance", base_asset)
        if resolved:
            chain, bitget_deposit_addr = resolved

    if not bitget_deposit_addr or not chain:
        logger.error(f"✗ Could not get Bitget deposit address for {base_asset}")
        return None

    logger.info(f"✓ Using chain: {chain}")
    logger.info(f"✓ Bitget deposit address: {bitget_deposit_addr}")

    # ---- PRE-FLIGHT: resolve USDT return path NOW (so we don't get stuck on Bitget) ----
    logger.info(f"\n[2] Pre-flight checks...")
    usdt_chain = None
    binance_usdt_addr = None
    manual_usdt_return = get_latest_dex_asset_wallet("binance", "USDT")
    if manual_usdt_return:
        usdt_chain, binance_usdt_addr = manual_usdt_return
    else:
        resolved_usdt = auto_resolve_deposit("binance", "bitget", "USDT")
        if resolved_usdt:
            usdt_chain, binance_usdt_addr = resolved_usdt
    if not (usdt_chain and binance_usdt_addr):
        logger.error("✗ Pre-flight: cannot resolve Binance USDT return wallet/chain. Aborting.")
        return None
    logger.info(f"  USDT return: chain={usdt_chain}, wallet={binance_usdt_addr}")

    # Bitget must allow USDT withdrawal on the return chain
    if not bitget_is_withdrawable("USDT", usdt_chain):
        logger.error(f"✗ Pre-flight: Bitget USDT withdrawal disabled on {usdt_chain}. Aborting.")
        return None
    logger.info(f"  ✓ Bitget USDT withdrawable on {usdt_chain}")

    # Binance must hold enough USDT for the buy
    binance_usdt_bal = binance_get_balance("USDT")
    if binance_usdt_bal is None:
        logger.error("✗ Pre-flight: cannot read Binance USDT balance. Aborting.")
        return None
    if binance_usdt_bal < trade_amount_usdt:
        logger.error(
            f"✗ Pre-flight: Binance USDT balance ${binance_usdt_bal:.2f} < trade amount ${trade_amount_usdt:.2f}. Aborting."
        )
        return None
    logger.info(f"  ✓ Binance USDT balance: ${binance_usdt_bal:.2f}")

    # Real withdraw fees (fall back to defaults if API fails)
    bin_wd_fee_units = binance_get_withdraw_fee(base_asset, chain)
    if bin_wd_fee_units is None:
        bin_wd_fee_units = WITHDRAWAL_FEE / binance_price  # fallback to USD-based default
        logger.warning(f"  ⚠ Using fallback Binance withdraw fee for {base_asset}/{chain}")
    bin_wd_fee_usd = bin_wd_fee_units * binance_price
    logger.info(f"  Binance withdraw fee {base_asset}/{chain}: {bin_wd_fee_units} ({bin_wd_fee_usd:.2f} USD)")

    bg_wd_fee_usdt = bitget_get_withdraw_fee("USDT", usdt_chain)
    if bg_wd_fee_usdt is None:
        bg_wd_fee_usdt = WITHDRAWAL_FEE
        logger.warning(f"  ⚠ Using fallback Bitget USDT withdraw fee on {usdt_chain}")
    logger.info(f"  Bitget USDT withdraw fee on {usdt_chain}: {bg_wd_fee_usdt}")

    # Slippage guard: VWAP buy on Binance vs limit sell price on Bitget
    sell_price = bitget_price * 0.99  # limit price (1% below ticker for fast fill)
    bin_book = binance_get_orderbook(symbol, limit=50)
    if not bin_book or not bin_book.get("asks"):
        logger.error("✗ Pre-flight: cannot read Binance order book. Aborting.")
        return None
    vwap_pair = estimate_buy_vwap(bin_book["asks"], trade_amount_usdt)
    if not vwap_pair:
        logger.error("✗ Pre-flight: insufficient Binance ask depth for trade size. Aborting.")
        return None
    vwap_price, vwap_qty = vwap_pair
    effective_spread_pct = (sell_price - vwap_price) / vwap_price * 100
    logger.info(
        f"  Binance VWAP buy: ${vwap_price:.8f} ({vwap_qty:.4f} {base_asset}); "
        f"effective spread vs Bitget limit ${sell_price:.8f}: {effective_spread_pct:.3f}%"
    )
    if effective_spread_pct < SLIPPAGE_REJECT_PCT:
        logger.error(
            f"✗ Slippage guard: effective spread {effective_spread_pct:.3f}% < threshold {SLIPPAGE_REJECT_PCT}%. Aborting."
        )
        return None

    # Liquidity guard: Bitget bid depth at >= sell_price must cover qty_to_sell
    qty_to_sell_est = vwap_qty - bin_wd_fee_units
    bg_book = bitget_get_orderbook(symbol, limit=50)
    if not bg_book or not bg_book.get("bids"):
        logger.error("✗ Pre-flight: cannot read Bitget order book. Aborting.")
        return None
    bid_depth = cumulative_bid_qty_at_or_above(bg_book["bids"], sell_price)
    logger.info(f"  Bitget bid depth >= ${sell_price:.8f}: {bid_depth:.4f} {base_asset} (need {qty_to_sell_est:.4f})")
    if bid_depth < qty_to_sell_est * MIN_DEPTH_MULTIPLIER:
        logger.error(
            f"✗ Liquidity guard: Bitget bid depth {bid_depth:.4f} < required {qty_to_sell_est * MIN_DEPTH_MULTIPLIER:.4f}. Aborting."
        )
        return None
    logger.info("  ✓ All pre-flight checks passed\n")

    # STEP 1: Market buy on Binance
    logger.info(f"\n[3] Market buying {base_asset} on Binance...")
    qty_bought = trade_amount_usdt / binance_price
    buy_result = binance_market_buy(symbol, qty_bought)
    if not buy_result:
        logger.error("✗ Failed to buy on Binance")
        return None
    binance_order_id = buy_result.get("orderId")
    logger.info(f"✓ Binance order: {binance_order_id}")

    # Wait for balance to update
    time.sleep(2)

    # STEP 2: Withdraw from Binance to Bitget
    logger.info(f"\n[4] Withdrawing {qty_bought:.4f} {base_asset} from Binance to Bitget...")
    withdraw_result = binance_withdraw(base_asset, bitget_deposit_addr, qty_bought, chain)
    if not withdraw_result:
        logger.error("✗ Failed to withdraw from Binance")
        return None
    withdrawal_id = withdraw_result.get("id")
    logger.info(f"✓ Withdrawal ID: {withdrawal_id}")
    logger.info(f"⏳ Waiting for deposit on Bitget (this takes ~3-5 minutes on {chain})...")

    # STEP 3: Wait for deposit
    start_wait = time.time()
    max_wait = 600  # 10 minutes max
    poll_interval = 15  # Check every 15 seconds

    while time.time() - start_wait < max_wait:
        try:
            timestamp = _bitget_timestamp()
            path = f"/spot/account/assets?coin={base_asset}"
            signature = _bitget_signature(timestamp, "GET", path)

            headers = {
                "ACCESS-KEY": BITGET_API_KEY,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
                "Content-Type": "application/json"
            }

            response = requests.get(
                f"{BITGET_BASE}{path}",
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("data"):
                for item in data["data"]:
                    if item.get("coin") == base_asset:
                        available = float(item.get("available", 0))
                        if available >= qty_bought * 0.95:  # Allow 5% tolerance
                            logger.info(f"✓ Received {available:.4f} {base_asset} on Bitget!")
                            break
        except Exception as e:
            logger.debug(f"Checking balance... (attempt {int((time.time() - start_wait) / poll_interval)})")

        time.sleep(poll_interval)
    else:
        logger.error(f"✗ Timeout waiting for deposit after {max_wait}s")
        return None

    # STEP 4: Limit sell on Bitget
    logger.info(f"\n[5] Placing limit sell order on Bitget...")
    qty_to_sell = qty_bought - bin_wd_fee_units  # Account for actual Binance withdrawal fee
    # sell_price already computed in pre-flight

    # Capture USDT baseline for poll-based fill detection
    usdt_baseline = bitget_get_balance("USDT") or 0.0

    sell_result = bitget_limit_sell(symbol, qty_to_sell, sell_price)
    if not sell_result:
        logger.error("✗ Failed to place sell order on Bitget")
        return None
    bitget_order_id = sell_result.get("data", {}).get("orderId", "unknown")
    logger.info(f"✓ Bitget sell order: {bitget_order_id}")
    logger.info(f"⏳ Waiting for sell order to fill (poll USDT balance)...")

    # STEP 5: Poll Bitget USDT balance until fill (or timeout)
    sell_max_wait = 180  # 3 minutes
    sell_poll = 5
    sell_start = time.time()
    expected_proceeds = qty_to_sell * sell_price * 0.99  # 1% slack for fees/partial
    usdt_after_sell = usdt_baseline
    while time.time() - sell_start < sell_max_wait:
        cur = bitget_get_balance("USDT")
        if cur is not None and cur - usdt_baseline >= expected_proceeds:
            usdt_after_sell = cur
            logger.info(f"✓ Sell filled — USDT on Bitget: ${cur:.2f}")
            break
        time.sleep(sell_poll)
    else:
        usdt_after_sell = bitget_get_balance("USDT") or usdt_baseline
        logger.warning(
            f"⚠ Sell fill not confirmed within {sell_max_wait}s. Proceeding with current USDT balance: ${usdt_after_sell:.2f}"
        )

    # STEP 6: Withdraw all available USDT back to Binance immediately
    logger.info(f"\n[6] Withdrawing USDT back to Binance...")
    usdt_balance = bitget_get_balance("USDT") or 0.0
    if usdt_balance > max(1.0, bg_wd_fee_usdt + 0.5):
        amount_to_send = round(usdt_balance - bg_wd_fee_usdt, 4)
        logger.info(
            f"Withdrawing ${amount_to_send:.4f} USDT (balance ${usdt_balance:.2f} - fee {bg_wd_fee_usdt}) to Binance via {usdt_chain}..."
        )
        withdraw_usdt_result = bitget_withdraw("USDT", binance_usdt_addr, amount_to_send, usdt_chain)
        if withdraw_usdt_result:
            logger.info(f"✓ USDT withdrawal initiated")
        else:
            logger.error("✗ USDT withdrawal failed — funds remain on Bitget")
    else:
        logger.warning(f"⚠ Bitget USDT balance ${usdt_balance:.2f} too low to cover withdraw fee {bg_wd_fee_usdt}; skipping return")

    # Calculate actual profit (real fees)
    bin_buy_fee_usd = trade_amount_usdt * 0.001  # 0.1% taker on Binance
    bg_sell_fee_usd = qty_to_sell * sell_price * 0.001  # 0.1% taker on Bitget
    total_fees_usd = bin_wd_fee_usd + bg_wd_fee_usdt + bin_buy_fee_usd + bg_sell_fee_usd

    gross_revenue = qty_to_sell * sell_price
    net_profit = gross_revenue - trade_amount_usdt - total_fees_usd

    logger.info(f"\n{'='*70}")
    logger.info(f"TRADE COMPLETED!")
    logger.info(f"{'='*70}")
    logger.info(f"Expected profit: ${net_profit:.2f}")
    logger.info(f"{'='*70}\n")

    return {
        "symbol": symbol,
        "binance_order": binance_order_id,
        "bitget_order": bitget_order_id,
        "quantity": qty_to_sell,
        "expected_profit": net_profit
    }


def buy_bitget_sell_binance(base_asset: str, bitget_price: float, binance_price: float, trade_amount_usdt: float = 100) -> Optional[Dict]:
    """
    Reverse arbitrage cycle:
    1. Market buy on Bitget
    2. Withdraw base_asset from Bitget to Binance
    3. Limit sell on Binance
    4. Withdraw USDT back to Bitget
    """
    symbol = f"{base_asset}USDT"

    logger.info(f"\n{'='*70}")
    logger.info(f"STARTING ARBITRAGE: BUY {base_asset} ON BITGET, SELL ON BINANCE")
    logger.info(f"{'='*70}")

    # Resolve Binance deposit chain/address for base_asset
    chain = None
    binance_deposit_addr = None
    manual_deposit = get_latest_dex_asset_wallet("binance", base_asset)
    if manual_deposit:
        chain, binance_deposit_addr = manual_deposit
        logger.info(
            f"\n[1] Using table mapping for {base_asset} deposit: chain={chain}, wallet={binance_deposit_addr}"
        )
    else:
        logger.info(f"\n[1] Auto-resolving deposit chain/address for {base_asset} on Binance...")
        resolved = auto_resolve_deposit("binance", "bitget", base_asset)
        if resolved:
            chain, binance_deposit_addr = resolved

    if not binance_deposit_addr or not chain:
        logger.error(f"✗ Could not get Binance deposit address for {base_asset}")
        return None

    logger.info(f"✓ Using chain: {chain}")
    logger.info(f"✓ Binance deposit address: {binance_deposit_addr}")

    # ---- PRE-FLIGHT: resolve USDT return path (Bitget USDT deposit) ----
    logger.info(f"\n[2] Pre-flight checks...")
    usdt_chain = None
    bitget_usdt_addr = None
    manual_usdt_return = get_latest_dex_asset_wallet("bitget", "USDT")
    if manual_usdt_return:
        usdt_chain, bitget_usdt_addr = manual_usdt_return
    else:
        resolved_usdt = auto_resolve_deposit("bitget", "binance", "USDT")
        if resolved_usdt:
            usdt_chain, bitget_usdt_addr = resolved_usdt
    if not (usdt_chain and bitget_usdt_addr):
        logger.error("✗ Pre-flight: cannot resolve Bitget USDT return wallet/chain. Aborting.")
        return None
    logger.info(f"  USDT return: chain={usdt_chain}, wallet={bitget_usdt_addr}")

    # Binance must allow USDT withdrawal on the return chain
    if not binance_is_withdrawable("USDT", usdt_chain):
        logger.error(f"✗ Pre-flight: Binance USDT withdrawal disabled on {usdt_chain}. Aborting.")
        return None
    logger.info(f"  ✓ Binance USDT withdrawable on {usdt_chain}")

    # Bitget must hold enough USDT for the buy
    bitget_usdt_bal = bitget_get_balance("USDT")
    if bitget_usdt_bal is None:
        logger.error("✗ Pre-flight: cannot read Bitget USDT balance. Aborting.")
        return None
    if bitget_usdt_bal < trade_amount_usdt:
        logger.error(
            f"✗ Pre-flight: Bitget USDT balance ${bitget_usdt_bal:.2f} < trade amount ${trade_amount_usdt:.2f}. Aborting."
        )
        return None
    logger.info(f"  ✓ Bitget USDT balance: ${bitget_usdt_bal:.2f}")

    # Real withdraw fees
    bg_wd_fee_units = bitget_get_withdraw_fee(base_asset, chain)
    if bg_wd_fee_units is None:
        bg_wd_fee_units = WITHDRAWAL_FEE / bitget_price
        logger.warning(f"  ⚠ Using fallback Bitget withdraw fee for {base_asset}/{chain}")
    bg_wd_fee_usd = bg_wd_fee_units * bitget_price
    logger.info(f"  Bitget withdraw fee {base_asset}/{chain}: {bg_wd_fee_units} ({bg_wd_fee_usd:.2f} USD)")

    bin_wd_fee_usdt = binance_get_withdraw_fee("USDT", usdt_chain)
    if bin_wd_fee_usdt is None:
        bin_wd_fee_usdt = WITHDRAWAL_FEE
        logger.warning(f"  ⚠ Using fallback Binance USDT withdraw fee on {usdt_chain}")
    logger.info(f"  Binance USDT withdraw fee on {usdt_chain}: {bin_wd_fee_usdt}")

    # Slippage guard: VWAP buy on Bitget vs limit sell price on Binance
    sell_price = binance_price * 0.99
    bg_book = bitget_get_orderbook(symbol, limit=50)
    if not bg_book or not bg_book.get("asks"):
        logger.error("✗ Pre-flight: cannot read Bitget order book. Aborting.")
        return None
    vwap_pair = estimate_buy_vwap(bg_book["asks"], trade_amount_usdt)
    if not vwap_pair:
        logger.error("✗ Pre-flight: insufficient Bitget ask depth for trade size. Aborting.")
        return None
    vwap_price, vwap_qty = vwap_pair
    effective_spread_pct = (sell_price - vwap_price) / vwap_price * 100
    logger.info(
        f"  Bitget VWAP buy: ${vwap_price:.8f} ({vwap_qty:.4f} {base_asset}); "
        f"effective spread vs Binance limit ${sell_price:.8f}: {effective_spread_pct:.3f}%"
    )
    if effective_spread_pct < SLIPPAGE_REJECT_PCT:
        logger.error(
            f"✗ Slippage guard: effective spread {effective_spread_pct:.3f}% < threshold {SLIPPAGE_REJECT_PCT}%. Aborting."
        )
        return None

    # Liquidity guard: Binance bid depth at >= sell_price must cover qty_to_sell
    qty_to_sell_est = vwap_qty - bg_wd_fee_units
    bin_book = binance_get_orderbook(symbol, limit=50)
    if not bin_book or not bin_book.get("bids"):
        logger.error("✗ Pre-flight: cannot read Binance order book. Aborting.")
        return None
    bid_depth = cumulative_bid_qty_at_or_above(bin_book["bids"], sell_price)
    logger.info(f"  Binance bid depth >= ${sell_price:.8f}: {bid_depth:.4f} {base_asset} (need {qty_to_sell_est:.4f})")
    if bid_depth < qty_to_sell_est * MIN_DEPTH_MULTIPLIER:
        logger.error(
            f"✗ Liquidity guard: Binance bid depth {bid_depth:.4f} < required {qty_to_sell_est * MIN_DEPTH_MULTIPLIER:.4f}. Aborting."
        )
        return None
    logger.info("  ✓ All pre-flight checks passed\n")

    # STEP 1: Market buy on Bitget
    logger.info(f"\n[3] Market buying {base_asset} on Bitget...")
    qty_bought = trade_amount_usdt / bitget_price
    buy_result = bitget_market_buy(symbol, qty_bought)
    if not buy_result:
        logger.error("✗ Failed to buy on Bitget")
        return None
    bitget_order_id = buy_result.get("data", {}).get("orderId", "unknown")
    logger.info(f"✓ Bitget order: {bitget_order_id}")

    # Wait for balance to update
    time.sleep(2)

    # STEP 2: Withdraw from Bitget to Binance
    logger.info(f"\n[4] Withdrawing {qty_bought:.4f} {base_asset} from Bitget to Binance...")
    withdraw_result = bitget_withdraw(base_asset, binance_deposit_addr, qty_bought, chain)
    if not withdraw_result:
        logger.error("✗ Failed to withdraw from Bitget")
        return None
    logger.info(f"✓ Withdrawal initiated")
    logger.info(f"⏳ Waiting for deposit on Binance (this takes ~3-5 minutes on {chain})...")

    # STEP 3: Wait for deposit on Binance — poll balance
    start_wait = time.time()
    max_wait = 600
    poll_interval = 15
    received = False

    while time.time() - start_wait < max_wait:
        try:
            bal = binance_get_balance(base_asset)
            if bal is not None and bal >= qty_bought * 0.95:
                logger.info(f"✓ Received {bal:.4f} {base_asset} on Binance!")
                received = True
                break
        except Exception:
            pass
        time.sleep(poll_interval)

    if not received:
        logger.error(f"✗ Timeout waiting for deposit after {max_wait}s")
        return None

    # STEP 4: Limit sell on Binance
    logger.info(f"\n[5] Placing limit sell order on Binance...")
    qty_to_sell = qty_bought - bg_wd_fee_units

    # Capture USDT baseline for poll-based fill detection
    usdt_baseline = binance_get_balance("USDT") or 0.0

    sell_result = binance_limit_sell(symbol, qty_to_sell, sell_price)
    if not sell_result:
        logger.error("✗ Failed to place sell order on Binance")
        return None
    binance_order_id = sell_result.get("orderId")
    logger.info(f"✓ Binance sell order: {binance_order_id}")
    logger.info(f"⏳ Waiting for sell order to fill (poll USDT balance)...")

    # STEP 5: Poll Binance USDT balance until fill
    sell_max_wait = 180
    sell_poll = 5
    sell_start = time.time()
    expected_proceeds = qty_to_sell * sell_price * 0.99
    usdt_after_sell = usdt_baseline
    while time.time() - sell_start < sell_max_wait:
        cur = binance_get_balance("USDT")
        if cur is not None and cur - usdt_baseline >= expected_proceeds:
            usdt_after_sell = cur
            logger.info(f"✓ Sell filled — USDT on Binance: ${cur:.2f}")
            break
        time.sleep(sell_poll)
    else:
        usdt_after_sell = binance_get_balance("USDT") or usdt_baseline
        logger.warning(
            f"⚠ Sell fill not confirmed within {sell_max_wait}s. Proceeding with current USDT balance: ${usdt_after_sell:.2f}"
        )

    # STEP 6: Withdraw available USDT back to Bitget
    logger.info(f"\n[6] Withdrawing USDT back to Bitget...")
    usdt_balance = binance_get_balance("USDT") or 0.0
    if usdt_balance > max(1.0, bin_wd_fee_usdt + 0.5):
        amount_to_send = round(usdt_balance - bin_wd_fee_usdt, 4)
        logger.info(
            f"Withdrawing ${amount_to_send:.4f} USDT (balance ${usdt_balance:.2f} - fee {bin_wd_fee_usdt}) to Bitget via {usdt_chain}..."
        )
        withdraw_usdt_result = binance_withdraw("USDT", bitget_usdt_addr, amount_to_send, usdt_chain)
        if withdraw_usdt_result:
            logger.info(f"✓ USDT withdrawal initiated")
        else:
            logger.error("✗ USDT withdrawal failed — funds remain on Binance")
    else:
        logger.warning(f"⚠ Binance USDT balance ${usdt_balance:.2f} too low to cover withdraw fee {bin_wd_fee_usdt}; skipping return")

    # Profit calc
    bg_buy_fee_usd = trade_amount_usdt * 0.001   # 0.1% taker on Bitget
    bin_sell_fee_usd = qty_to_sell * sell_price * 0.001  # 0.1% taker on Binance
    total_fees_usd = bg_wd_fee_usd + bin_wd_fee_usdt + bg_buy_fee_usd + bin_sell_fee_usd

    gross_revenue = qty_to_sell * sell_price
    net_profit = gross_revenue - trade_amount_usdt - total_fees_usd

    logger.info(f"\n{'='*70}")
    logger.info(f"TRADE COMPLETED!")
    logger.info(f"{'='*70}")
    logger.info(f"Expected profit: ${net_profit:.2f}")
    logger.info(f"{'='*70}\n")

    return {
        "symbol": symbol,
        "bitget_order": bitget_order_id,
        "binance_order": binance_order_id,
        "quantity": qty_to_sell,
        "expected_profit": net_profit
    }


def main_menu():
    """Main interactive menu."""
    while True:
        print("\n" + "="*50)
        print("TRADING EXECUTOR")
        print("="*50)
        print("1. Add Wallet Address")
        print("2. Withdraw")
        print("3. Market Buy")
        print("4. Limit Sell")
        print("5. Execute Arbitrage (Buy Binance → Sell Bitget)")
        print("6. Exit")
        print("="*50)

        choice = input("Select option (1-6): ").strip()

        if choice == "1":
            menu_add_wallet()
        elif choice == "2":
            menu_withdraw()
        elif choice == "3":
            menu_market_buy()
        elif choice == "4":
            menu_limit_sell()
        elif choice == "5":
            menu_arbitrage()
        elif choice == "6":
            print("Exiting...")
            break
        else:
            print("Invalid option")


def menu_arbitrage():
    """Execute arbitrage trade from user input."""
    print("\n=== Execute Arbitrage ===")
    base_asset = input("Asset (e.g., JUV, FLUX): ").upper()
    binance_price = float(input("Binance price: $"))
    bitget_price = float(input("Bitget price: $"))

    trade_amount = input("Trade amount USDT (default 100): ").strip()
    trade_amount = float(trade_amount) if trade_amount else 100.0

    print(f"\n{'='*60}")
    print(f"Arbitrage Trade Summary:")
    print(f"{'='*60}")
    print(f"Asset: {base_asset}USDT")
    print(f"Binance: ${binance_price:.8f}")
    print(f"Bitget:  ${bitget_price:.8f}")

    spread = ((bitget_price - binance_price) / binance_price) * 100
    print(f"Spread: {spread:.2f}%")

    qty = trade_amount / binance_price
    qty_sell = qty - (WITHDRAWAL_FEE / binance_price)
    revenue = qty_sell * bitget_price
    fees = BINANCE_BUY_FEE + BITGET_SELL_FEE + WITHDRAWAL_FEE
    profit = revenue - trade_amount - fees

    print(f"\nBuy qty: {qty:.4f}")
    print(f"Sell qty: {qty_sell:.4f}")
    print(f"Revenue: ${revenue:.2f}")
    print(f"Fees: ${fees:.2f}")
    print(f"Net Profit: ${profit:.2f}")
    print(f"{'='*60}")

    if profit <= 0:
        print(f"✗ NOT PROFITABLE (Loss: ${abs(profit):.2f})")
        return

    print(f"✓ PROFITABLE!")

    confirm = input("\n⚠ Ready to execute? THIS WILL TRANSFER REAL FUNDS! (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    result = buy_binance_sell_bitget(base_asset, binance_price, bitget_price, trade_amount)
    if result:
        print(f"\n✓ Trade executed successfully!")
        print(f"Expected profit: ${result['expected_profit']:.2f}")
    else:
        print(f"\n✗ Trade failed!")


if __name__ == "__main__":
    main_menu()
