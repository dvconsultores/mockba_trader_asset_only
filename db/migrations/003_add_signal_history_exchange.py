"""
Migration: Add 'exchange' column to signal_history if it doesn't exist.

This allows separating DEX/CEX analytics in historical signal data.
Safe to run multiple times.

Usage:
    python -m db.migrations.003_add_signal_history_exchange
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(signal_history)")
        columns = [row[1] for row in cur.fetchall()]

        if "exchange" in columns:
            print("✅ signal_history.exchange already exists")
            conn.close()
            return True

        cur.execute("ALTER TABLE signal_history ADD COLUMN exchange TEXT DEFAULT 'unknown'")
        conn.commit()
        conn.close()

        print("✅ Added exchange column to signal_history")
        return True
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False


if __name__ == "__main__":
    migrate()
