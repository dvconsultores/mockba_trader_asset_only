"""
MockbaV4 — Database operations (schema v2).

Four tables: settings (legacy, preserved), open_positions, closed_trades, signals.
All writes are parameterized. Settings read fresh each cycle.

Backward-compatible with telegram.py imports: get_setting, upsert_setting,
get_all_settings, initialize_database_tables, get_asset_list, add_asset,
remove_asset.
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "trading.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── Migration ─────────────────────────────────────────────────────────────────

def _run_schema_v2(cur):
    """Create v2 tables idempotently. Does NOT drop legacy tables."""
    schema_path = os.path.join(PROJECT_ROOT, "db", "schema_v2.sql")
    with open(schema_path) as f:
        cur.executescript(f.read())


def initialize_database_tables():
    """Idempotent: creates legacy + v2 tables. Called by telegram.py on startup."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _run_schema_v2(cur)
        conn.commit()


# ── Settings CRUD (backward-compatible signatures) ────────────────────────────

def get_setting(key: str) -> str | None:
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def upsert_setting(key: str, value: str):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (key, str(value)),
        )
        conn.commit()


def get_all_settings() -> dict:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


# ── Typed setting helpers ─────────────────────────────────────────────────────

def get_setting_float(key: str, default: float) -> float:
    try:
        val = get_setting(key)
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def get_setting_int(key: str, default: int) -> int:
    try:
        val = get_setting(key)
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def get_setting_bool(key: str, default: bool) -> bool:
    val = get_setting(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "yes")


# ── Asset list helpers (backward-compatible) ──────────────────────────────────

def get_asset_list() -> list:
    val = get_setting("assets") or get_setting("asset") or ""
    return [x.strip() for x in val.split(",") if x.strip()]


def add_asset(asset: str):
    assets = get_asset_list()
    if asset not in assets:
        assets.append(asset)
        upsert_setting("assets", ",".join(assets))


def remove_asset(asset: str):
    assets = get_asset_list()
    if asset in assets:
        assets.remove(asset)
        upsert_setting("assets", ",".join(assets))


# ── open_positions CRUD ──────────────────────────────────────────────────────

def save_position(pos: dict) -> bool:
    """Insert a new open position. Returns False on UNIQUE conflict."""
    with get_db_connection() as conn:
        try:
            conn.execute("""
                INSERT INTO open_positions (id, asset, venue, side, qty,
                    entry_price, signal_price, tp_price, sl_price,
                    tp_order_id, sl_order_id, opened_at)
                VALUES (:id, :asset, :venue, :side, :qty,
                    :entry_price, :signal_price, :tp_price, :sl_price,
                    :tp_order_id, :sl_order_id, :opened_at)
            """, pos)
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def load_position(asset: str, venue: str, position_id: str) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM open_positions WHERE asset=? AND venue=? AND id=?",
            (asset, venue, position_id),
        ).fetchone()
        return dict(row) if row else None


def load_all_positions(asset: str | None = None, venue: str | None = None) -> list[dict]:
    with get_db_connection() as conn:
        query = "SELECT * FROM open_positions WHERE 1=1"
        params = []
        if asset:
            query += " AND asset=?"
            params.append(asset)
        if venue:
            query += " AND venue=?"
            params.append(venue)
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def update_position(position_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [position_id]
    with get_db_connection() as conn:
        conn.execute(f"UPDATE open_positions SET {sets} WHERE id=?", values)
        conn.commit()


def delete_position(asset: str, venue: str, position_id: str):
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM open_positions WHERE asset=? AND venue=? AND id=?",
            (asset, venue, position_id),
        )
        conn.commit()


# ── closed_trades ─────────────────────────────────────────────────────────────

def save_closed_trade(trade: dict) -> int:
    with get_db_connection() as conn:
        cur = conn.execute("""
            INSERT INTO closed_trades (asset, venue, side, entry_price, exit_price,
                signal_price, qty, fee_entry, fee_exit, pnl_net, pnl_pct,
                opened_at, closed_at, exit_reason)
            VALUES (:asset, :venue, :side, :entry_price, :exit_price,
                :signal_price, :qty, :fee_entry, :fee_exit, :pnl_net, :pnl_pct,
                :opened_at, :closed_at, :exit_reason)
        """, trade)
        conn.commit()
        return cur.lastrowid


def get_daily_pnl(venue: str, date_str: str | None = None) -> float:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_net), 0) AS total FROM closed_trades "
            "WHERE venue=? AND date(datetime(closed_at, 'unixepoch')) = ?",
            (venue, date_str),
        ).fetchone()
        return row["total"] if row else 0.0


def get_consecutive_losses(venue: str) -> int:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT pnl_net FROM closed_trades WHERE venue=? "
            "ORDER BY closed_at DESC LIMIT 20",
            (venue,),
        ).fetchall()
    count = 0
    for row in rows:
        if row["pnl_net"] < 0:
            count += 1
        else:
            break
    return count


# ── signals ───────────────────────────────────────────────────────────────────

def save_signal(sig: dict):
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO signals (timestamp, asset, venue, regime, obi, extreme_pct, action, reason)
            VALUES (:timestamp, :asset, :venue, :regime, :obi, :extreme_pct, :action, :reason)
        """, sig)
        conn.commit()
