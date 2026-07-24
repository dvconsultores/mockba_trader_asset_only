"""
Migration: Add 'grid_position_capital' setting if it doesn't exist.

Ensures existing databases get the per-grid-position capital setting.
Safe to run multiple times - INSERT OR IGNORE.

Usage:
    python -m db.migrations.008_add_grid_position_capital_setting
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")


def migrate():
    """Add grid_position_capital setting if it doesn't exist."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('grid_position_capital', '15');
        """)

        conn.commit()

        cur.execute("SELECT value FROM settings WHERE key = 'grid_position_capital'")
        row = cur.fetchone()
        if row:
            print(f"✅ grid_position_capital setting exists: ${row[0]}")
        else:
            print("❌ grid_position_capital setting not found after insert")
            conn.close()
            return False

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Migration error: {e}")
        return False


if __name__ == "__main__":
    ok = migrate()
    exit(0 if ok else 1)
