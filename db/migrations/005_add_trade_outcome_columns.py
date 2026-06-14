"""
Migration: Add 'realized_pnl' and 'trade_outcome' columns to signal_history.

Usage:
    python -m db.migrations.005_add_trade_outcome_columns

Adds columns for labeling signals with actual trade results:
- realized_pnl: Net PnL from Orderly/Binance trade (REAL)
- trade_outcome: 'win', 'loss', 'breakeven', or NULL if unmatched (TEXT)
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "trading.db"


def _migrate(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(signal_history)")
    existing = {row[1] for row in cur.fetchall()}

    for col, col_type, default in [
        ("realized_pnl", "REAL", None),
        ("trade_outcome", "TEXT", None),
    ]:
        if col not in existing:
            cur.execute(
                f"ALTER TABLE signal_history ADD COLUMN {col} {col_type}"
            )
            print(f"  ✅ Added {col} ({col_type})")
        else:
            print(f"  ⏭️  {col} already exists")

    conn.commit()


if __name__ == "__main__":
    conn = sqlite3.connect(str(DB_PATH))
    try:
        _migrate(conn)
        print("✅ Migration 005 complete")
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()
