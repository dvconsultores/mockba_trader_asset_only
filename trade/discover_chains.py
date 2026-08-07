"""
Discover all available chains for trading assets across exchanges.
Stores mapping in SQLite: ASSET → [CHAIN1, CHAIN2, ...]
"""

import os
import sys
import json
import time
import requests
import hmac
import hashlib
from typing import Dict, List, Set
import logging

# Add parent directory to path to import db_ops
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db_ops import (
    initialize_database_tables, 
    add_asset_chains_batch, 
    get_all_asset_chains,
    clear_asset_chains
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load API keys
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

BINANCE_BASE = "https://api.binance.com/api/v3"
BITGET_BASE = "https://api.bitget.com/api/v2"

OUTPUT_FILE = "asset_chains_mapping.json"


def _binance_signature(query_string: str) -> str:
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def _bitget_signature(timestamp: str, method: str, path: str, body: str = "") -> str:
    message = timestamp + method + path + body
    return hmac.new(
        BITGET_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()


def get_binance_symbols() -> Set[str]:
    """Get all trading symbols from Binance."""
    try:
        response = requests.get(
            f"{BINANCE_BASE}/exchangeInfo",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        symbols = set()
        for symbol in data.get("symbols", []):
            if symbol.get("status") == "TRADING" and symbol.get("quoteAsset") == "USDT":
                base = symbol.get("baseAsset")
                if base:
                    symbols.add(base)
        
        logger.info(f"✓ Found {len(symbols)} USDT trading pairs on Binance")
        return symbols
    except Exception as e:
        logger.error(f"✗ Error fetching Binance symbols: {e}")
        return set()


def get_bitget_symbols() -> Set[str]:
    """Get all trading symbols from Bitget."""
    try:
        response = requests.get(
            f"{BITGET_BASE}/spot/public/products",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        symbols = set()
        for product in data.get("data", []):
            symbol = product.get("symbol", "")
            if symbol.endswith("USDT"):
                base = symbol.replace("USDT", "")
                if base:
                    symbols.add(base)
        
        logger.info(f"✓ Found {len(symbols)} USDT trading pairs on Bitget")
        return symbols
    except Exception as e:
        logger.error(f"✗ Error fetching Bitget symbols: {e}")
        return set()


def get_asset_chains_binance(asset: str) -> List[str]:
    """Get withdrawal chains for asset on Binance using config/getall endpoint."""
    try:
        # Use /sapi/v1/capital/config/getall which lists all coins and their networks
        response = requests.get(
            "https://api.binance.com/sapi/v1/capital/config/getall",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        # Find the asset in the response
        for coin_info in data:
            if coin_info.get("coin") == asset:
                networks = coin_info.get("networkList", [])
                chains = [n.get("network") for n in networks if n.get("network")]
                return sorted(chains)
        
        return []
    except Exception as e:
        logger.debug(f"  Binance {asset}: {type(e).__name__}")
        return []


def get_asset_chains_bitget(asset: str) -> List[str]:
    """Get withdrawal chains for asset on Bitget."""
    try:
        timestamp = str(int(time.time() * 1000))
        path = f"/spot/wallet/chains?coin={asset}"
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
        
        if response.status_code == 404:
            return []
        
        response.raise_for_status()
        result = response.json()
        
        chains = [item.get("chain") for item in result.get("data", []) if item.get("chain")]
        return sorted(chains)
    except Exception as e:
        logger.debug(f"  Bitget {asset}: {type(e).__name__}")
        return []


def discover_all_chains():
    """Discover and map all assets to their available chains."""
    logger.info("="*70)
    logger.info("DISCOVERING ASSET CHAINS MAPPING")
    logger.info("="*70)
    
    # Initialize database
    logger.info("\n[0] Initializing database...")
    initialize_database_tables()
    clear_asset_chains()  # Start fresh
    
    # Get all symbols
    logger.info("\n[1] Fetching all trading symbols...")
    binance_symbols = get_binance_symbols()
    bitget_symbols = get_bitget_symbols()
    
    all_symbols = binance_symbols | bitget_symbols
    logger.info(f"\n✓ Total unique assets: {len(all_symbols)}")
    
    # Discover chains
    mapping = {}
    logger.info(f"\n[2] Discovering chains for {len(all_symbols)} assets...")
    logger.info("(This may take 2-3 minutes, please wait...)\n")
    
    processed = 0
    for asset in sorted(all_symbols):
        processed += 1
        
        # Get chains
        binance_chains = get_asset_chains_binance(asset)
        bitget_chains = get_asset_chains_bitget(asset)
        
        # Merge chains
        all_chains = sorted(list(set(binance_chains + bitget_chains)))
        
        if all_chains:
            mapping[asset] = all_chains
            # Save to SQLite
            add_asset_chains_batch(asset, all_chains)
            status = "✓"
        else:
            status = "⊘"
        
        # Progress
        if processed % 10 == 0:
            logger.info(f"  [{processed}/{len(all_symbols)}] {asset}: {all_chains if all_chains else 'N/A'}")
    
    # Get mapping from SQLite
    mapping = get_all_asset_chains()
    
    # Save backup to JSON too
    logger.info(f"\n[3] Saving backup to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(mapping, f, indent=2)
    
    # Summary
    logger.info(f"\n{'='*70}")
    logger.info(f"DISCOVERY COMPLETE!")
    logger.info(f"{'='*70}")
    logger.info(f"Total assets with chains: {len(mapping)}")
    logger.info(f"Saved to: SQLite (trading.db) + backup JSON")
    logger.info(f"\nTop assets:")
    
    for asset in sorted(mapping.keys())[:20]:
        chains = mapping[asset]
        logger.info(f"  {asset}: {chains}")
    
    if len(mapping) > 20:
        logger.info(f"  ... and {len(mapping) - 20} more")
    
    return mapping
    
    for asset in sorted(mapping.keys())[:20]:
        chains = mapping[asset]
        logger.info(f"  {asset}: {chains}")
    
    if len(mapping) > 20:
        logger.info(f"  ... and {len(mapping) - 20} more")
    
    return mapping


def update_trading_executor_with_mapping(mapping: Dict[str, List[str]]):
    """Update trading_executor.py with discovered mapping."""
    # This will be used by trading_executor to get accurate chains
    mapping_code = f"""
# AUTO-GENERATED ASSET CHAINS MAPPING
# Generated by discover_chains.py
DISCOVERED_ASSET_CHAINS = {json.dumps(mapping, indent=4)}
"""
    
    with open("asset_chains_discovered.py", 'w') as f:
        f.write(mapping_code)
    
    logger.info(f"✓ Updated asset_chains_discovered.py")


if __name__ == "__main__":
    mapping = discover_all_chains()
    update_trading_executor_with_mapping(mapping)
    
    logger.info(f"\n💡 Usage:")
    logger.info(f"   - Mapping saved in: {OUTPUT_FILE}")
    logger.info(f"   - Python module: asset_chains_discovered.py")
    logger.info(f"   - All trading functions now have accurate chain data")
