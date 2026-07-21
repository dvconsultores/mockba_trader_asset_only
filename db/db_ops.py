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
    conn = sqlite3.connect(DB_PATH, timeout=30)
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

        # remove old discovered chains table and use manual wallet mapping table
        cur.execute("DROP TABLE IF EXISTS asset_chains;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS dex_asset_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dex TEXT NOT NULL,
                asset TEXT NOT NULL,
                wallet TEXT NOT NULL,
                chain TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dex, asset, chain)
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
            ('risk_level', '1.0'),  # % of balance risked per trade
            ('capital_usage', '50'),  # % of buying power to deploy per trade
            ('exchange', 'dex'),  # 'dex' (Orderly futures) or 'cex' (Binance spot)
            ('ml_threshold', '0.80'),  # ML gate decision threshold
        ]
        for key, value in default_settings:
            cur.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?);
            """, (key, value))

        # create table arbitrage_compounding (track arbitrage cycles)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_compounding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_num INTEGER NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'binance_to_bitget',
                capital_start REAL NOT NULL,
                capital_end REAL NOT NULL,
                gain REAL NOT NULL,
                gain_pct REAL NOT NULL,
                spread_pct REAL NOT NULL,
                buy_price REAL NOT NULL,
                sell_price REAL NOT NULL,
                qty REAL NOT NULL,
                buy_fee REAL NOT NULL,
                sell_fee REAL NOT NULL,
                status TEXT DEFAULT 'completed',
                CHECK (direction IN ('binance_to_bitget', 'bitget_to_binance'))
            );
        """)

        # create table arbitrage_cycle_steps (detailed step logging)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_cycle_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_num INTEGER NOT NULL,
                step_order INTEGER NOT NULL,
                step_name TEXT NOT NULL,
                step_details TEXT,
                direction TEXT DEFAULT 'binance_to_bitget',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'completed',
                FOREIGN KEY (cycle_num) REFERENCES arbitrage_compounding(cycle_num)
            );
        """)

        # create table ai_recommendations (store AI suggestions)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                strategy TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                asset TEXT,
                parameter_name TEXT,
                old_value REAL,
                new_value REAL,
                confidence_score REAL,
                rationale TEXT,
                implemented INTEGER DEFAULT 0,
                result_gain REAL
            );
        """)

        # create table performance_metrics (pre-computed stats)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                period TEXT NOT NULL,
                strategy TEXT NOT NULL,
                asset TEXT,
                total_trades INTEGER,
                win_count INTEGER,
                loss_count INTEGER,
                win_rate REAL,
                avg_gain REAL,
                avg_loss REAL,
                total_gain REAL,
                roi_pct REAL,
                sharpe_ratio REAL,
                max_drawdown_pct REAL,
                directional_bias TEXT
            );
        """)

        # create table strategy_parameters (current settings for each strategy)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_parameters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                strategy TEXT NOT NULL UNIQUE,
                active INTEGER DEFAULT 1,
                ai_version TEXT,
                min_spread_pct REAL,
                max_position_usdt REAL,
                risk_limit_pct REAL,
                time_window_start TEXT,
                time_window_end TEXT,
                custom_params TEXT
            );
        """)

        # create table execution_errors (failed trades & recovery)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS execution_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                strategy TEXT NOT NULL,
                cycle_num INTEGER,
                error_type TEXT NOT NULL,
                error_message TEXT,
                severity TEXT,
                recovery_action TEXT,
                resolved INTEGER DEFAULT 0
            );
        """)

        # create table market_regimes (detected market conditions)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_regimes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                asset TEXT NOT NULL,
                regime TEXT NOT NULL,
                confidence REAL,
                slope REAL,
                volatility REAL,
                duration_minutes INTEGER
            );
        """)

        _ensure_signal_history_schema(cur)

        # === Arbitrage refactor: new tables (additive, following existing pattern) ===

        # Inventory ledger: free balance snapshots per exchange/asset
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                exchange TEXT NOT NULL,
                asset TEXT NOT NULL,
                free_balance REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'api',
                UNIQUE(exchange, asset, timestamp)
            );
        """)

        # Capital allocation: USDT-denominated capital allocated to arbitrage per exchange
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_capital_allocation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                exchange TEXT NOT NULL,
                allocated_usdt REAL NOT NULL,
                change_amount REAL NOT NULL DEFAULT 0,
                reason TEXT
            );
        """)

        # Per-sample observations for statistical asset scoring
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL,
                binance_bid REAL,
                binance_ask REAL,
                binance_bid_qty REAL,
                binance_ask_qty REAL,
                bitget_bid REAL,
                bitget_ask REAL,
                bitget_bid_qty REAL,
                bitget_ask_qty REAL,
                spread_b2b REAL,
                spread_btog REAL,
                deposits_open_binance INTEGER DEFAULT 1,
                deposits_open_bitget INTEGER DEFAULT 1,
                withdrawals_open_binance INTEGER DEFAULT 1,
                withdrawals_open_bitget INTEGER DEFAULT 1
            );
        """)

        # Rotation decision log
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_rotation_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                current_asset TEXT NOT NULL,
                candidate_asset TEXT NOT NULL,
                current_score REAL,
                candidate_score REAL,
                estimated_rotation_cost REAL,
                score_margin REAL,
                config_margin REAL,
                decision TEXT NOT NULL DEFAULT 'declined',
                reason TEXT
            );
        """)

        # Additive columns for arbitrage_compounding (new execution model)
        _ensure_arbitrage_compounding_schema(cur)
        # Additive columns for arbitrage_cycle_steps (remove transfer steps, new step names)
        _ensure_arbitrage_cycle_steps_schema(cur)

        # Default settings for arbitrage refactor
        arbitrage_defaults = [
            ('arbitrage_run_state', 'running'),
            ('arbitrage_initial_capital_binance', '100'),
            ('arbitrage_initial_capital_bitget', '100'),
        ]
        for key, value in arbitrage_defaults:
            cur.execute("""
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?);
            """, (key, value))
        
        conn.commit()
        
        logger.info("✅ SQLite tables initialized (includes autonomy tables for AI agents).")


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

    # ML feature columns (added by signal agent)
    ml_columns = {
        'slope_15m': 'REAL',
        'slope_4h': 'REAL',
        'slope_1d': 'REAL',
        'dist_to_4h_high_pct': 'REAL',
        'dist_to_4h_low_pct': 'REAL',
        'dist_to_1d_high_pct': 'REAL',
        'dist_to_1d_low_pct': 'REAL',
        'tf_alignment_score': 'INTEGER',
        'ml_score': 'REAL',
        'ml_decision': 'TEXT',
        'structural_sl': 'REAL',
        # Outcome labeling columns
        'realized_pnl': 'REAL',
        'trade_outcome': 'TEXT',
    }
    for col_name, col_type in ml_columns.items():
        if columns and col_name not in columns:
            try:
                cur.execute(
                    f"ALTER TABLE signal_history ADD COLUMN {col_name} {col_type}"
                )
            except Exception:
                pass  # column may already exist from concurrent migration

    if any(c not in (columns or []) for c in ml_columns):
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
    candle_count: int | None = None,
    ml_score: float | None = None,
    ml_decision: str | None = None,
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
                atr, live_price, candle_count,
                ml_score, ml_decision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asset, exchange, regime, obi, pattern_type, int(approved), side,
            entry_price, stop_loss, take_profit,
            rejection_str, manipulation_str,
            atr, live_price, candle_count,
            ml_score, ml_decision
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


# === MANUAL DEX/ASSET/WALLET/CHAIN MAPPING ===

def upsert_dex_asset_wallet(dex: str, asset: str, wallet: str, chain: str):
    """Insert or update manual mapping for dex/asset/chain -> wallet."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO dex_asset_wallets (dex, asset, wallet, chain, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dex, asset, chain) DO UPDATE SET
                wallet = excluded.wallet,
                updated_at = CURRENT_TIMESTAMP;
        """, (dex.lower(), asset.upper(), wallet.strip(), chain.upper()))
        conn.commit()


def get_dex_asset_wallet(dex: str, asset: str, chain: str) -> str | None:
    """Get wallet by dex/asset/chain."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT wallet
            FROM dex_asset_wallets
            WHERE dex = ? AND asset = ? AND chain = ?
        """, (dex.lower(), asset.upper(), chain.upper()))
        row = cur.fetchone()
        return row['wallet'] if row else None


def get_latest_dex_asset_wallet(dex: str, asset: str) -> tuple[str, str] | None:
    """Get latest configured (chain, wallet) by dex and asset, regardless of chain."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT chain, wallet
            FROM dex_asset_wallets
            WHERE dex = ? AND asset = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
        """, (dex.lower(), asset.upper()))
        row = cur.fetchone()
        if not row:
            return None
        return row['chain'], row['wallet']


def get_dex_asset_chains(dex: str, asset: str) -> list:
    """Get all configured chains by dex and asset."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT chain
            FROM dex_asset_wallets
            WHERE dex = ? AND asset = ?
            ORDER BY chain
        """, (dex.lower(), asset.upper()))
        rows = cur.fetchall()
        return [row['chain'] for row in rows]


# === ARBITRAGE REFACTOR: schema migration helpers ===

def _ensure_arbitrage_compounding_schema(cur):
    """Add new columns to arbitrage_compounding for the two-leg execution model."""
    cur.execute("PRAGMA table_info(arbitrage_compounding)")
    columns = [row[1] for row in cur.fetchall()]
    if not columns:
        return

    new_cols = {
        'buy_exchange': 'TEXT',
        'sell_exchange': 'TEXT',
        'buy_leg_order_id': 'TEXT',
        'sell_leg_order_id': 'TEXT',
        'buy_leg_fill_price': 'REAL',
        'sell_leg_fill_price': 'REAL',
        'buy_leg_fill_qty': 'REAL',
        'sell_leg_fill_qty': 'REAL',
        'buy_leg_fee': 'REAL',
        'sell_leg_fee': 'REAL',
        'spread_at_detect': 'REAL',
        'spread_at_fill': 'REAL',
        'is_simulation': 'INTEGER DEFAULT 0',
        'inventory_snapshot': 'TEXT',
    }
    for col_name, col_type in new_cols.items():
        if col_name not in columns:
            try:
                cur.execute(
                    f"ALTER TABLE arbitrage_compounding ADD COLUMN {col_name} {col_type}"
                )
            except Exception:
                pass


def _ensure_arbitrage_cycle_steps_schema(cur):
    """No structural changes needed; step names are updated in application code.
    Kept as a hook for future additive migrations on this table."""
    pass


# === ARBITRAGE REFACTOR: inventory ledger helpers ===

def insert_inventory_snapshot(exchange: str, asset: str, free_balance: float, source: str = "api"):
    """Record an inventory snapshot."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO arbitrage_inventory (exchange, asset, free_balance, source, timestamp)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (exchange, asset, free_balance, source))
        conn.commit()


def get_latest_inventory(exchange: str, asset: str) -> float | None:
    """Get most recent inventory balance for exchange/asset."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT free_balance FROM arbitrage_inventory
            WHERE exchange = ? AND asset = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (exchange, asset))
        row = cur.fetchone()
        return row['free_balance'] if row else None


def get_inventory_at_time(exchange: str, asset: str, before_timestamp: str) -> float | None:
    """Get inventory balance closest to a given timestamp."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT free_balance FROM arbitrage_inventory
            WHERE exchange = ? AND asset = ? AND timestamp <= ?
            ORDER BY timestamp DESC LIMIT 1
        """, (exchange, asset, before_timestamp))
        row = cur.fetchone()
        return row['free_balance'] if row else None


# === ARBITRAGE REFACTOR: capital allocation helpers ===

def get_current_capital_allocation(exchange: str) -> float:
    """Get latest capital allocation for an exchange."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT allocated_usdt FROM arbitrage_capital_allocation
            WHERE exchange = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (exchange,))
        row = cur.fetchone()
        return row['allocated_usdt'] if row else 0.0


def record_capital_change(exchange: str, allocated_usdt: float, change_amount: float, reason: str):
    """Record a capital allocation change."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO arbitrage_capital_allocation (exchange, allocated_usdt, change_amount, reason)
            VALUES (?, ?, ?, ?)
        """, (exchange, allocated_usdt, change_amount, reason))
        conn.commit()


def initialize_capital_allocation(exchange: str, amount_usdt: float):
    """Initialize capital allocation if none exists for this exchange."""
    current = get_current_capital_allocation(exchange)
    if current == 0.0:
        record_capital_change(exchange, amount_usdt, amount_usdt, "initial_allocation")


def get_capital_allocation_history(exchange: str, limit: int = 50) -> list:
    """Get capital allocation change history for an exchange."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM arbitrage_capital_allocation
            WHERE exchange = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (exchange, limit))
        return [dict(row) for row in cur.fetchall()]


# === ARBITRAGE REFACTOR: observation persistence helpers ===

def insert_observation(
    symbol: str,
    binance_bid: float | None = None,
    binance_ask: float | None = None,
    binance_bid_qty: float | None = None,
    binance_ask_qty: float | None = None,
    bitget_bid: float | None = None,
    bitget_ask: float | None = None,
    bitget_bid_qty: float | None = None,
    bitget_ask_qty: float | None = None,
    spread_b2b: float | None = None,
    spread_btog: float | None = None,
    deposits_open_binance: bool = True,
    deposits_open_bitget: bool = True,
    withdrawals_open_binance: bool = True,
    withdrawals_open_bitget: bool = True,
):
    """Persist a single observation sample."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO arbitrage_observations (
                symbol, binance_bid, binance_ask, binance_bid_qty, binance_ask_qty,
                bitget_bid, bitget_ask, bitget_bid_qty, bitget_ask_qty,
                spread_b2b, spread_btog,
                deposits_open_binance, deposits_open_bitget,
                withdrawals_open_binance, withdrawals_open_bitget
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, binance_bid, binance_ask, binance_bid_qty, binance_ask_qty,
            bitget_bid, bitget_ask, bitget_bid_qty, bitget_ask_qty,
            spread_b2b, spread_btog,
            int(deposits_open_binance), int(deposits_open_bitget),
            int(withdrawals_open_binance), int(withdrawals_open_bitget),
        ))
        conn.commit()


def get_observations_since(symbol: str, since_timestamp: str) -> list:
    """Get observations for a symbol since a given timestamp."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM arbitrage_observations
            WHERE symbol = ? AND timestamp >= ?
            ORDER BY timestamp ASC
        """, (symbol, since_timestamp))
        return [dict(row) for row in cur.fetchall()]


def get_all_observations_in_window(window_start: str) -> list:
    """Get all observations since a window start time."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM arbitrage_observations
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (window_start,))
        return [dict(row) for row in cur.fetchall()]


# === ARBITRAGE REFACTOR: rotation decision helpers ===

def insert_rotation_decision(
    current_asset: str,
    candidate_asset: str,
    current_score: float,
    candidate_score: float,
    estimated_rotation_cost: float,
    score_margin: float,
    config_margin: float,
    decision: str,
    reason: str = "",
):
    """Persist a rotation decision (executed or declined)."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO arbitrage_rotation_decisions (
                current_asset, candidate_asset, current_score, candidate_score,
                estimated_rotation_cost, score_margin, config_margin, decision, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            current_asset, candidate_asset, current_score, candidate_score,
            estimated_rotation_cost, score_margin, config_margin, decision, reason,
        ))
        conn.commit()


# === ARBITRAGE REFACTOR: run state helpers ===

def get_arbitrage_run_state() -> str:
    """Get the arbitrage loop run state ('running' or 'stopped')."""
    val = get_setting('arbitrage_run_state')
    return val if val in ('running', 'stopped') else 'running'


def set_arbitrage_run_state(state: str):
    """Set the arbitrage loop run state."""
    upsert_setting('arbitrage_run_state', state)


def clear_dex_asset_wallets():
    """Delete all manual dex/asset/wallet/chain mappings."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM dex_asset_wallets")
        conn.commit()
        logger.info("✓ Cleared dex_asset_wallets table")