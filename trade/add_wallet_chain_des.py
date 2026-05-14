"""
Manual CLI tool to add dex/asset/wallet/chain mappings into SQLite.
"""

import os
import sys

# Add parent directory to import db_ops
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.db_ops import initialize_database_tables, upsert_dex_asset_wallet


def main() -> None:
    initialize_database_tables()

    print("\n=== Add Wallet Chain (DEX) ===")

    dex = input("DEX/Exchange (e.g. bitget, binance): ").strip().lower()
    asset = input("Asset (e.g. JUV, USDT): ").strip().upper()
    wallet = input("Wallet address: ").strip()
    chain = input("Chain/Network (e.g. CAP20, BSC): ").strip().upper()

    if not dex or not asset or not wallet or not chain:
        print("\n✗ All fields are required.")
        return

    upsert_dex_asset_wallet(dex, asset, wallet, chain)

    print("\n✓ Saved successfully")
    print(f"  dex: {dex}")
    print(f"  asset: {asset}")
    print(f"  chain: {chain}")
    print(f"  wallet: {wallet}")


if __name__ == "__main__":
    main()
