"""
Migration: Add 'capital_usage' setting if it doesn't exist.

Ensures existing databases get the buying-power-deployment-percentage setting.
Safe to run multiple times - INSERT OR IGNORE.

Usage:
    python -m db.migrations.007_add_capital_usage_setting
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")


def migrate():
    """Add capital_usage setting if it doesn't exist."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('capital_usage', '50');
        """)

        conn.commit()

        cur.execute("SELECT value FROM settings WHERE key = 'capital_usage'")
        row = cur.fetchone()
        if row:
            print(f"✅ capital_usage setting exists: {row[0]}%")
        else:
            print("❌ capital_usage setting not found after insert")
            conn.close()
            return False

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Migration error: {e}")
        return False


if __name__ == "__main__":
    migrate()
