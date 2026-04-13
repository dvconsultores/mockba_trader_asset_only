# db_ops.py

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from logs.log_config import apolo_trader_logger as logger
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "trading.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # enables dict-like access
    try:
        yield conn
    finally:
        conn.close()

def initialize_database_tables():
    with get_db_connection() as conn:
        cur = conn.cursor()

        # create table settings
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # create table trades_daily (track daily positive trades) - MIGRATION: Auto-created if missing
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                positive_trades_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # create table signal_history (track every signal: approved & rejected)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                asset TEXT NOT NULL,
                exchange TEXT DEFAULT 'unknown',
                regime TEXT,
                obi REAL,
                pattern_type TEXT,
                approved INTEGER DEFAULT 0,
                side TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                rejection_reasons TEXT,
                manipulation_warnings TEXT,
                atr REAL,
                live_price REAL,
                candle_count INTEGER
            );
        """)

        # Insert the default setting if it doesn't exist
        default_settings = [
            ('asset', 'PERP_NEAR_USDC'),  # List of all assets (comma-separated)
            ('current_asset', 'PERP_NEAR_USDC'),  # Current asset the bot trades on
            ('interval', '5m'),
            ('take_profit', '0.5'),
            ('stop_loss', '0.3'),
            ('auto_trade_dex', 'False'),
            ('auto_trade_cex', 'False'),
            ('cex_capital', '10'),  # USDT amount per Binance spot position
            ('leverage', '1000'),
            ('exchange', 'dex'),  # 'dex' (Orderly futures) or 'cex' (Binance spot)
        ]
        for key, value in default_settings:
            cur.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?);
            """, (key, value))

        _ensure_signal_history_schema(cur)
        
        conn.commit()
        
        logger.info("✅ SQLite tables initialized (includes trades_daily for daily positive trades counter).")


# Def to insert or update settings
def upsert_setting(key: str, value: str):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP;
        """, (key, value))
        conn.commit()

# Def to get setting by key
def get_setting(key: str) -> str | None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row['value'] if row else None 

# Def to get all settings
def get_all_settings() -> dict:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
        return {row['key']: row['value'] for row in rows}

# Helper functions for managing the asset list (stored as comma-separated string)

def get_asset_list() -> list:
    """Returns the asset setting as a list of strings."""
    val = get_setting('asset')
    if not val:
        return []
    return [x.strip() for x in val.split(',') if x.strip()]

def add_asset(asset: str):
    """Adds an asset to the list if not present."""
    assets = get_asset_list()
    if asset not in assets:
        assets.append(asset)
        upsert_setting('asset', ','.join(assets))

def remove_asset(asset: str):
    """Removes an asset from the list."""
    assets = get_asset_list()
    if asset in assets:
        assets.remove(asset)
        upsert_setting('asset', ','.join(assets))

# Helper functions for managing the automated_assets list

def get_automated_asset_list() -> list:
    """Returns the automated_assets setting as a list of strings."""
    val = get_setting('automated_assets')
    if not val:
        return []
    return [x.strip() for x in val.split(',') if x.strip()]

def add_automated_asset(asset: str):
    """Adds an asset to the automated_assets list if not present."""
    assets = get_automated_asset_list()
    if asset not in assets:
        assets.append(asset)
        upsert_setting('automated_assets', ','.join(assets))

def remove_automated_asset(asset: str):
    """Removes an asset from the automated_assets list."""
    assets = get_automated_asset_list()
    if asset in assets:
        assets.remove(asset)
        upsert_setting('automated_assets', ','.join(assets))

# === DAILY TRADES COUNTER (for managing MAX_TRADES_PER_DAY) ===

def get_today_date_utc4() -> str:
    """Get today's date in UTC-4 format (user's local time)."""
    from datetime import datetime, timezone, timedelta
    utc_now = datetime.now(timezone.utc)
    user_now = utc_now - timedelta(hours=4)
    return user_now.strftime('%Y-%m-%d')


def _ensure_trades_daily_schema(cur):
    """Ensure trades_daily has the expected positive counter column."""
    cur.execute("PRAGMA table_info(trades_daily)")
    columns = [row[1] for row in cur.fetchall()]

    if columns and 'positive_trades_count' not in columns:
        cur.execute(
            "ALTER TABLE trades_daily ADD COLUMN positive_trades_count INTEGER DEFAULT 0"
        )
        cur.connection.commit()


def _ensure_signal_history_schema(cur):
    """Ensure signal_history has expected columns for current analytics."""
    cur.execute("PRAGMA table_info(signal_history)")
    columns = [row[1] for row in cur.fetchall()]

    if columns and 'exchange' not in columns:
        cur.execute(
            "ALTER TABLE signal_history ADD COLUMN exchange TEXT DEFAULT 'unknown'"
        )
        cur.connection.commit()

def get_trades_today() -> int:
    """Get today's positive trade count."""
    today = get_today_date_utc4()
    with get_db_connection() as conn:
        cur = conn.cursor()
        _ensure_trades_daily_schema(cur)
        cur.execute("SELECT positive_trades_count FROM trades_daily WHERE date = ?", (today,))
        row = cur.fetchone()
        return row['positive_trades_count'] if row else 0

def increment_trades_today() -> int:
    """Increment today's positive trade counter and return new count."""
    today = get_today_date_utc4()
    with get_db_connection() as conn:
        cur = conn.cursor()
        _ensure_trades_daily_schema(cur)
        cur.execute("""
            INSERT INTO trades_daily (date, positive_trades_count)
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET
                positive_trades_count = positive_trades_count + 1;
        """, (today,))
        conn.commit()
        
        # Return updated count
        cur.execute("SELECT positive_trades_count FROM trades_daily WHERE date = ?", (today,))
        row = cur.fetchone()
        return row['positive_trades_count'] if row else 1


# === SIGNAL HISTORY TRACKING (for ML/analysis later) ===

def save_signal_to_history(
    asset: str,
    exchange: str,
    regime: str,
    obi: float,
    pattern_type: str | None,
    approved: bool,
    side: str | None = None,
    entry_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    rejection_reasons: list | None = None,
    manipulation_warnings: list | None = None,
    atr: float | None = None,
    live_price: float | None = None,
    candle_count: int | None = None
) -> int:
    """
    Save signal analysis to database.
    
    Returns: signal_id (for later correlation with actual trades)
    """
    import json
    
    rejection_str = json.dumps(rejection_reasons or [])
    manipulation_str = json.dumps(manipulation_warnings or [])
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        _ensure_signal_history_schema(cur)
        cur.execute("""
            INSERT INTO signal_history (
                asset, exchange, regime, obi, pattern_type, approved, side,
                entry_price, stop_loss, take_profit,
                rejection_reasons, manipulation_warnings,
                atr, live_price, candle_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset, exchange, regime, obi, pattern_type, int(approved), side,
            entry_price, stop_loss, take_profit,
            rejection_str, manipulation_str,
            atr, live_price, candle_count
        ))
        conn.commit()
        return cur.lastrowid


def get_signal_history(limit: int = 100, approved_only: bool = False):
    """Retrieve signal history for analysis."""
    import json
    
    with get_db_connection() as conn:
        cur = conn.cursor()
        if approved_only:
            cur.execute("""
                SELECT * FROM signal_history 
                WHERE approved = 1 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        else:
            cur.execute("""
                SELECT * FROM signal_history 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        
        rows = cur.fetchall()
        result = []
        for row in rows:
            row_dict = dict(row)
            # Parse JSON fields
            row_dict['rejection_reasons'] = json.loads(row['rejection_reasons'])
            row_dict['manipulation_warnings'] = json.loads(row['manipulation_warnings'])
            result.append(row_dict)
        return result