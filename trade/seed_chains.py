"""
Seed the asset_chains table with known, working chains.
This provides immediate functionality without waiting for full API discovery.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db_ops import (
    initialize_database_tables,
    add_asset_chains_batch,
    get_all_asset_chains,
    clear_asset_chains
)
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Known working chains for popular assets
KNOWN_CHAINS = {
    # Stablecoins
    "USDT": ["TRX", "ETH", "BSC", "POLYGON", "SOLANA"],
    "USDC": ["ETH", "SOLANA", "POLYGON", "ARBITRUM"],
    "BUSD": ["ETH", "BSC"],
    "TUSD": ["ETH", "SOLANA"],
    
    # Major coins
    "BTC": ["Bitcoin"],
    "ETH": ["Ethereum"],
    "BNB": ["BSC"],
    "SOL": ["Solana"],
    "XRP": ["XRP"],
    "ADA": ["Cardano"],
    "DOGE": ["DOGE"],
    "MATIC": ["POLYGON"],
    
    # Popular altcoins
    "JUV": ["CHZ2"],  # Chiliz Chain (not TRX)
    "FLUX": ["ETH", "BSC"],
    "POWR": ["ETH", "BSC"],
    "ARB": ["ARBITRUM", "ETH"],
    "OP": ["OPTIMISM", "ETH"],
    "AVAX": ["AVAX"],
    "FTM": ["FANTOM"],
    
    # Tokens commonly on multiple chains
    "UNI": ["ETH", "POLYGON", "ARBITRUM"],
    "LINK": ["ETH", "POLYGON", "ARBITRUM"],
    "AAVE": ["ETH", "POLYGON", "ARBITRUM"],
    "CRV": ["ETH", "POLYGON", "ARBITRUM"],
}

def seed_database():
    """Initialize database and seed with known chains."""
    logger.info("=" * 70)
    logger.info("SEEDING ASSET CHAINS DATABASE")
    logger.info("=" * 70)
    
    # Initialize database
    logger.info("\n[1] Initializing database...")
    initialize_database_tables()
    clear_asset_chains()
    logger.info("✓ Database cleared and ready")
    
    # Add chains
    logger.info("\n[2] Seeding known chains...")
    for asset, chains in KNOWN_CHAINS.items():
        add_asset_chains_batch(asset, chains)
        logger.info(f"  ✓ {asset}: {chains}")
    
    # Verify
    logger.info("\n[3] Verifying seed...")
    mapping = get_all_asset_chains()
    logger.info(f"✓ Seeded {len(mapping)} assets into database")
    
    # Summary
    logger.info(f"\n{'=' * 70}")
    logger.info("SEEDING COMPLETE!")
    logger.info(f"{'=' * 70}")
    logger.info(f"Total assets: {len(mapping)}")
    logger.info(f"\nDatabase is ready for trading!")
    logger.info(f"\n💡 Later, run discover_chains.py to find more assets.\n")

if __name__ == "__main__":
    seed_database()
