"""
Migration: Add trades_daily table for daily trade counter

This script safely adds the trades_daily table if it doesn't already exist.
Safe to run multiple times - will only create table once.

Usage:
    python -m db.migrations.001_add_trades_daily_table
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "trading.db")

def migrate():
    """Add trades_daily table if it doesn't exist."""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Check if table already exists
        cur.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='trades_daily'
        """)
        
        if cur.fetchone():
            print("✅ trades_daily table already exists - no migration needed")
            conn.close()
            return True
        
        # Create table (tracks positive trades only - max 2 per day)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                positive_trades_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        conn.commit()
        conn.close()
        
        print("✅ Successfully created trades_daily table")
        print("📊 Daily positive trades counter is now active (max 2 per day)")
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        return False

if __name__ == "__main__":
    migrate()
