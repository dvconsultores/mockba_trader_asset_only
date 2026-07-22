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
            try:
                err_code = response.json().get("code") if response is not None else None
            except Exception:
                err_code = None
            if str(err_code) == "40018" or "Invalid IP" in body:
                logger.error(
                    "✗ Bitget deposit address lookup denied by API key IP whitelist. "
                    f"Run from a whitelisted IP or add a manual wallet mapping for bitget/{asset}."
                )
                return None
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


