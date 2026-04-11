"""
Migration: Add 'exchange' setting if it doesn't exist.

Ensures existing databases get the new exchange setting (default: 'dex').
Safe to run multiple times — INSERT OR IGNORE.

Usage:
    python -m db.migrations.002_add_exchange_setting
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")


def migrate():
    """Add exchange setting if it doesn't exist."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('exchange', 'dex');
        """)

        conn.commit()

        # Verify
        cur.execute("SELECT value FROM settings WHERE key = 'exchange'")
        row = cur.fetchone()
        if row:
            print(f"✅ exchange setting exists: {row[0]}")
        else:
            print("❌ exchange setting not found after insert")
            return False

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Migration error: {e}")
        return False


if __name__ == "__main__":
    migrate()
