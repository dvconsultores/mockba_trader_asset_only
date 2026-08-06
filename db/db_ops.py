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


def _run_migrations(cur):
    """Apply any pending migrations idempotently."""
    migrations_dir = os.path.join(PROJECT_ROOT, "db", "migrations")
    if not os.path.isdir(migrations_dir):
        return
    for fname in sorted(os.listdir(migrations_dir)):
        if not fname.endswith(".sql"):
            continue
        try:
            with open(os.path.join(migrations_dir, fname)) as f:
                cur.executescript(f.read())
        except Exception:
            pass  # column already exists, etc.


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
    # Run migrations (separate connection to avoid schema change issues)
    with get_db_connection() as conn:
        _run_migrations(conn.cursor())
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


# ── asset_configs CRUD (Amendment 004) ────────────────────────────────────────

def get_all_asset_configs() -> list[dict]:
    """Return all asset_configs rows as list of dicts."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, capital_dex, capital_cex, active_dex, active_cex, "
            "created_at, updated_at FROM asset_configs ORDER BY symbol"
        ).fetchall()
        return [dict(row) for row in rows]


def get_asset_config(symbol: str) -> dict | None:
    """Return one asset_config row or None."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT symbol, capital_dex, capital_cex, active_dex, active_cex, "
            "created_at, updated_at FROM asset_configs WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None


def upsert_asset_config(
    symbol: str,
    capital_dex: float = 0.0,
    capital_cex: float = 0.0,
    active_dex: bool = False,
    active_cex: bool = False,
):
    """Insert or update an asset_config row. Returns True on success."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO asset_configs (symbol, capital_dex, capital_cex, "
            "active_dex, active_cex) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "capital_dex = excluded.capital_dex, "
            "capital_cex = excluded.capital_cex, "
            "active_dex = excluded.active_dex, "
            "active_cex = excluded.active_cex, "
            "updated_at = datetime('now')",
            (symbol, capital_dex, capital_cex, int(active_dex), int(active_cex)),
        )
        conn.commit()
    return True


def delete_asset_config(symbol: str):
    """Remove an asset_config row. No-op if not found."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM asset_configs WHERE symbol = ?", (symbol,))
        conn.commit()


def get_active_pairs() -> list[tuple[str, str, float]]:
    """Return (asset, venue, capital) tuples for all active pairs.
    Active means active_<venue>=true. Capital may be 0 for signal-only pairs.

    LEGACY since Amendment 003 — the bot loop now derives pairs from the
    asset_universe table via get_tradeable_universe(). Kept for backward
    compatibility with telegram.py / startup validation.
    """
    pairs: list[tuple[str, str, float]] = []
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, capital_dex, capital_cex, active_dex, active_cex "
            "FROM asset_configs ORDER BY symbol"
        ).fetchall()
    for row in rows:
        if row["active_dex"]:
            pairs.append((row["symbol"], "orderly", float(row["capital_dex"])))
        if row["active_cex"]:
            pairs.append((row["symbol"], "binance", float(row["capital_cex"])))
    return pairs


# ── asset_universe CRUD (Amendment 003) ───────────────────────────────────────

def replace_universe(venue: str, rows: list[dict]):
    """Replace the stored universe for a venue wholesale.

    blacklisted is carried forward by (venue, asset) so the operator's
    override survives a rescan. Rows: list of dicts with keys asset, symbol,
    rank, scanned_at, quote_volume_24h, spread_pct, depth_bid_top10,
    depth_ask_top10, atr_pct_median, signals_count, recovery_rate,
    median_minutes_to_tp.
    """
    with get_db_connection() as conn:
        # Carry blacklist forward
        prev = {
            (r["venue"], r["asset"]): bool(r["blacklisted"])
            for r in conn.execute(
                "SELECT venue, asset, blacklisted FROM asset_universe WHERE venue = ?",
                (venue,),
            ).fetchall()
        }
        conn.execute("DELETE FROM asset_universe WHERE venue = ?", (venue,))
        now = time.time()
        for r in rows:
            asset = r["asset"]
            conn.execute(
                "INSERT INTO asset_universe (venue, asset, symbol, rank, scanned_at, "
                "quote_volume_24h, spread_pct, depth_bid_top10, depth_ask_top10, "
                "atr_pct_median, signals_count, recovery_rate, median_minutes_to_tp, "
                "blacklisted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    venue, asset, r.get("symbol", asset), int(r.get("rank", 0)),
                    float(r.get("scanned_at", now)),
                    r.get("quote_volume_24h"), r.get("spread_pct"),
                    r.get("depth_bid_top10"), r.get("depth_ask_top10"),
                    r.get("atr_pct_median"), r.get("signals_count"),
                    r.get("recovery_rate"), r.get("median_minutes_to_tp"),
                    int(prev.get((venue, asset), False)),
                ),
            )
        conn.commit()


def get_universe(venue: str, include_blacklisted: bool = False) -> list[dict]:
    """Return universe rows for a venue, ordered by rank."""
    q = "SELECT * FROM asset_universe WHERE venue = ?"
    params: list = [venue]
    if not include_blacklisted:
        q += " AND blacklisted = 0"
    q += " ORDER BY rank"
    with get_db_connection() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_universe_row(venue: str, asset: str) -> dict | None:
    """Return one universe row (including blacklisted) or None."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM asset_universe WHERE venue = ? AND asset = ?",
            (venue, asset),
        ).fetchone()
        return dict(row) if row else None


def get_universe_scan_age(venue: str) -> float | None:
    """Return the newest scanned_at timestamp for a venue, or None if empty."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT MAX(scanned_at) AS t FROM asset_universe WHERE venue = ?",
            (venue,),
        ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None


def set_blacklist(venue: str, asset: str, blacklisted: bool) -> bool:
    """Set the operator blacklist flag for a universe row.

    Only applies to rows already in the universe. Returns False if the row
    does not exist (nothing to toggle).
    """
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE asset_universe SET blacklisted = ? WHERE venue = ? AND asset = ?",
            (int(blacklisted), venue, asset),
        )
        conn.commit()
        return cur.rowcount > 0


def get_tradeable_universe(venue: str) -> list[dict]:
    """Return non-blacklisted universe rows for the bot loop, by rank.

    Also excludes assets in the operator `binance_blocklist` setting
    (comma-separated) — e.g. Binance 'Monitoring' tokens, which exchangeInfo
    does not flag but a human operator knows about. Applies immediately,
    without waiting for the next universe scan.
    """
    rows = get_universe(venue, include_blacklisted=False)
    raw = get_setting("binance_blocklist") or ""
    block = {a.strip().upper() for a in raw.split(",") if a.strip()}
    if block:
        rows = [r for r in rows if str(r["asset"]).upper() not in block]
    return rows


# ── venue_state CRUD (Amendment 003) ──────────────────────────────────────────

def set_venue_equity(venue: str, equity: float):
    """Cache the venue's live equity (written by bot.py each cycle)."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO venue_state (venue, equity, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(venue) DO UPDATE SET equity = excluded.equity, "
            "updated_at = excluded.updated_at",
            (venue, float(equity), time.time()),
        )
        conn.commit()


def get_venue_equity(venue: str) -> dict | None:
    """Return {venue, equity, updated_at} for a venue, or None."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT venue, equity, updated_at FROM venue_state WHERE venue = ?",
            (venue,),
        ).fetchone()
        return dict(row) if row else None


# ── Declared capital pools (Amendment 003) ────────────────────────────────────

def get_capital_pool(venue: str) -> float:
    """Return the operator's declared capital pool for a venue.

    capital_cex_usdt for binance, capital_dex_usdc for orderly.
    Used for UI display/validation only — never for sizing.
    """
    key = "capital_cex_usdt" if venue == "binance" else "capital_dex_usdc"
    return get_setting_float(key, 0.0)


# ── Migration: legacy global settings → per-asset configs (Amendment 004) ─────

def migrate_legacy_assets(dex_equity: float = 0.0, cex_equity: float = 0.0) -> dict:
    """Migrate legacy global capital settings to asset_configs table.

    Detects legacy keys (dex_slot_pct, cex_slot_pct, auto_trade_binance,
    auto_trade_orderly). Computes absolute capital from percentages × equity.
    Returns dict with migration summary.

    Idempotent: skips if asset_configs already has rows.
    """
    # Check if migration already ran
    with get_db_connection() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM asset_configs").fetchone()["c"]
        if count > 0:
            return {"migrated": False, "reason": "asset_configs already populated", "asset_count": count}

    # Detect legacy keys
    legacy_dex_pct = get_setting_float("dex_slot_pct", 0.0)
    legacy_cex_pct = get_setting_float("cex_slot_pct", 0.0)
    legacy_auto_dex = get_setting_bool("auto_trade_orderly", False)
    legacy_auto_cex = get_setting_bool("auto_trade_binance", False)

    if legacy_dex_pct == 0.0 and legacy_cex_pct == 0.0:
        return {"migrated": False, "reason": "no legacy capital settings found"}

    # Get legacy asset list
    assets = get_asset_list()
    primary = assets[0] if assets else None

    migrated = []
    for i, asset in enumerate(assets):
        is_primary = (i == 0)
        capital_dex = (legacy_dex_pct / 100.0 * dex_equity) if (is_primary and dex_equity > 0) else 0.0
        capital_cex = (legacy_cex_pct / 100.0 * cex_equity) if (is_primary and cex_equity > 0) else 0.0
        active_dex = legacy_auto_dex if is_primary else False
        active_cex = legacy_auto_cex if is_primary else False

        upsert_asset_config(asset, round(capital_dex, 2), round(capital_cex, 2), active_dex, active_cex)
        migrated.append({
            "symbol": asset,
            "capital_dex": round(capital_dex, 2),
            "capital_cex": round(capital_cex, 2),
            "active_dex": active_dex,
            "active_cex": active_cex,
            "primary": is_primary,
        })

    # Remove legacy keys from settings
    legacy_keys = ["dex_slot_pct", "cex_slot_pct", "max_slots",
                   "auto_trade_binance", "auto_trade_orderly", "capital"]
    with get_db_connection() as conn:
        for key in legacy_keys:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()

    return {"migrated": True, "assets": migrated, "legacy_keys_removed": legacy_keys,
            "dex_equity": dex_equity, "cex_equity": cex_equity}


# ── open_positions CRUD ──────────────────────────────────────────────────────

def save_position(pos: dict) -> bool:
    """Insert a new open position. Returns False on UNIQUE conflict."""
    pos = dict(pos)
    pos.setdefault("fee_entry", 0.0)
    with get_db_connection() as conn:
        try:
            conn.execute("""
                INSERT INTO open_positions (id, asset, venue, side, qty,
                    entry_price, signal_price, tp_price, sl_price,
                    tp_order_id, sl_order_id, opened_at, fee_entry)
                VALUES (:id, :asset, :venue, :side, :qty,
                    :entry_price, :signal_price, :tp_price, :sl_price,
                    :tp_order_id, :sl_order_id, :opened_at, :fee_entry)
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
