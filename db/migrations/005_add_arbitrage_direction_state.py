"""
Migration: Add dedicated arbitrage direction state table.

Creates arbitrage_direction_state with a single persisted row (id=1)
that stores the current execution direction.
Default direction: 'binance_to_bitget'.

Usage:
    python -m db.migrations.005_add_arbitrage_direction_state
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")


def migrate():
    """Create arbitrage_direction_state table and insert default row."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_direction_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                direction TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (direction IN ('binance_to_bitget', 'bitget_to_binance'))
            );
        """)

        cur.execute("""
            INSERT OR IGNORE INTO arbitrage_direction_state (id, direction)
            VALUES (1, 'binance_to_bitget');
        """)

        conn.commit()

        cur.execute("SELECT direction FROM arbitrage_direction_state WHERE id = 1")
        row = cur.fetchone()
        if row:
            print(f"✅ arbitrage_direction_state exists: {row[0]}")
        else:
            print("❌ arbitrage_direction_state row missing after insert")
            conn.close()
            return False

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Migration error: {e}")
        return False


if __name__ == "__main__":
    migrate()
