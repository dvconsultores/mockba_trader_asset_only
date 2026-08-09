"""
MockbaV4 — PnL tracking, kill switches, and equity-based position sizing.

Constitution principle V: every number comes from actual fills.
Kill switches block entries, never exits.
Slot sizing is equity-based (compounds automatically), recomputed once daily.
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Optional

from db.db_ops import (
    save_closed_trade,
    get_daily_pnl,
    get_consecutive_losses,
    get_setting_float,
    get_setting_int,
    get_setting_bool,
    upsert_setting,
)


# ── Per-venue state (module-level cache, reset at day boundary) ───────────────

_day_cache: dict[str, float] = {}          # venue -> cached slot_size for today
_day_cache_date: dict[str, str] = {}       # venue -> date string when cached


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _midnight_utc() -> float:
    """UNIX timestamp of the next UTC midnight."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp() + 86400


# ── Trade recording ───────────────────────────────────────────────────────────

def record_closed_trade(
    asset: str, venue: str, side: str,
    entry_price: float, exit_price: float, signal_price: float,
    qty: float, fee_entry: float, fee_exit: float,
    opened_at: float, closed_at: float, exit_reason: str,
) -> int:
    """Record a completed trade. Returns the row ID."""
    # PnL always from actual fills (constitution V)
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty

    pnl_net = gross - fee_entry - fee_exit
    deployed = entry_price * qty
    pnl_pct = (pnl_net / deployed) * 100 if deployed > 0 else 0.0

    return save_closed_trade({
        "asset": asset, "venue": venue, "side": side,
        "entry_price": entry_price, "exit_price": exit_price,
        "signal_price": signal_price, "qty": qty,
        "fee_entry": fee_entry, "fee_exit": fee_exit,
        "pnl_net": pnl_net, "pnl_pct": pnl_pct,
        "opened_at": opened_at, "closed_at": closed_at,
        "exit_reason": exit_reason,
    })


# ── Kill switches ─────────────────────────────────────────────────────────────

def is_entry_blocked(venue: str, equity: float = 0.0) -> tuple[bool, str]:
    """Check if new entries should be blocked. Returns (blocked, reason).
    
    If equity > 0, also checks daily_loss_limit_pct against equity.
    """
    # Check global trading enabled
    if not get_setting_bool("trading_enabled", True):
        return True, "trading_enabled is off"

    # Daily loss limit — absolute (override) takes priority, then percentage
    limit_abs = get_setting_float("daily_loss_limit", 0.0)
    limit_pct = get_setting_float("daily_loss_limit_pct", 5.0)
    limit = limit_abs if limit_abs > 0 else (equity * limit_pct / 100 if equity > 0 else 0)
    
    if limit > 0:
        pnl = get_daily_pnl(venue)
        if pnl <= -limit:
            return True, f"daily_loss_limit breached: {pnl:.2f} <= -{limit:.2f}"

    # Consecutive losses
    max_consec = get_setting_int("max_consecutive_losses", 4)
    if max_consec > 0:
        consec = get_consecutive_losses(venue)
        if consec >= max_consec:
            return True, f"max_consecutive_losses breached: {consec} >= {max_consec}"

    return False, ""


def disable_trading(reason: str):
    """Trip the kill switch. Existing positions run to normal exits."""
    upsert_setting("trading_enabled", "0")
    # Log would go here — caller logs


def is_entry_blocked_per_pair(asset: str, venue: str, equity: float = 0.0) -> tuple[bool, str]:
    """Check if new entries for a specific (asset, venue) pair should be blocked.
    
    Amendment 004: per-pair kill switches. Returns (blocked, reason).
    """
    if not get_setting_bool("trading_enabled", True):
        return True, "trading_enabled is off"

    # Daily loss limit — absolute (override) takes priority, then percentage
    limit_abs = get_setting_float("daily_loss_limit", 0.0)
    limit_pct = get_setting_float("daily_loss_limit_pct", 5.0)
    limit = limit_abs if limit_abs > 0 else (equity * limit_pct / 100 if equity > 0 else 0)

    if limit > 0:
        pnl = _get_asset_daily_pnl(asset, venue)
        if pnl <= -limit:
            return True, f"daily_loss_limit breached for {venue}:{asset}: {pnl:.2f} <= -{limit:.2f}"

    # Consecutive losses (per pair)
    max_consec = get_setting_int("max_consecutive_losses", 4)
    if max_consec > 0:
        consec = _get_asset_consecutive_losses(asset, venue)
        if consec >= max_consec:
            return True, f"max_consecutive_losses breached for {venue}:{asset}: {consec} >= {max_consec}"

    return False, ""


def get_global_daily_pnl() -> float:
    """Sum PnL across all pairs for today (UTC)."""
    today = _today_utc()
    from db.db_ops import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_net), 0) AS total FROM closed_trades "
            "WHERE date(datetime(closed_at, 'unixepoch')) = ?",
            (today,),
        ).fetchone()
        return float(row["total"]) if row else 0.0


def check_global_daily_loss() -> tuple[bool, str]:
    """Check global daily loss limit. Returns (blocked, reason).

    Sums PnL across ALL venues for the current UTC day. Supports both the
    absolute ($) limit and the percentage-of-total-equity limit (0 disables
    that limit). Soft block — entries only, resets at UTC midnight.
    """
    limit_abs = get_setting_float("global_daily_loss_limit", 0.0)
    limit_pct = get_setting_float("global_daily_loss_limit_pct", 0.0)
    if limit_abs <= 0 and limit_pct <= 0:
        return False, ""

    total_pnl = get_global_daily_pnl()
    if limit_abs > 0 and total_pnl <= -limit_abs:
        return True, f"global_daily_loss_limit breached: {total_pnl:.2f} <= -{limit_abs:.2f}"
    if limit_pct > 0:
        # Percentage of total live equity across all venues
        from db.db_ops import get_venue_equity
        total_eq = 0.0
        for v in ("binance", "orderly"):
            st = get_venue_equity(v)
            total_eq += float(st["equity"]) if st else 0.0
        if total_eq > 0:
            limit = total_eq * limit_pct / 100
            if total_pnl <= -limit:
                return True, f"global_daily_loss_limit breached: {total_pnl:.2f} <= -{limit:.2f}"

    return False, ""


def _get_asset_daily_pnl(asset: str, venue: str) -> float:
    """Daily PnL for a specific (asset, venue) pair."""
    today = _today_utc()
    from db.db_ops import get_db_connection
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_net), 0) AS total FROM closed_trades "
            "WHERE asset=? AND venue=? AND date(datetime(closed_at, 'unixepoch')) = ?",
            (asset, venue, today),
        ).fetchone()
        return float(row["total"]) if row else 0.0


def _get_asset_consecutive_losses(asset: str, venue: str) -> int:
    """Consecutive losses for a specific (asset, venue) pair since UTC day start."""
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    from db.db_ops import get_db_connection
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT pnl_net FROM closed_trades WHERE asset=? AND venue=? AND closed_at >= ? "
            "ORDER BY closed_at DESC LIMIT 20",
            (asset, venue, day_start),
        ).fetchall()
    count = 0
    for row in rows:
        if row["pnl_net"] < 0:
            count += 1
        else:
            break
    return count


# ── Slot sizing ───────────────────────────────────────────────────────────────

def compute_slot_size(
    venue: str,
    equity: float,
    min_notional: float,
    capital: float = 0.0,
) -> float:
    """
    Absolute-USD position size for a single slot (Amendment 003).

    Per-venue pool model: slot = {venue}_slot_pct × LIVE exchange equity,
    floored at min_notional × 1.5, recomputed once per UTC day.

    Sizing never reads the declared capital_* pools (Amendment 003: the
    exchange wins on disagreement). The `capital` argument is retained for
    backward compatibility but is no longer used for sizing.
    """
    today = _today_utc()

    # Return cached value if still valid
    if _day_cache_date.get(venue) == today and venue in _day_cache:
        return _day_cache[venue]

    slot_pct_key = "cex_slot_pct" if venue == "binance" else "dex_slot_pct"
    slot_pct = get_setting_float(slot_pct_key, 10.0)
    raw = equity * (slot_pct / 100)

    floored = max(raw, min_notional * 1.5)
    _day_cache[venue] = floored
    _day_cache_date[venue] = today

    return floored


def max_effective_slots(venue: str, equity: float, slot_size: float) -> int:
    """Return how many slots equity can support. Never more than configured max (Amendment 004: max_concurrent_positions)."""
    configured = get_setting_int("max_concurrent_positions", get_setting_int("max_slots", 9))
    if slot_size <= 0 or equity <= 0:
        return 0
    affordable = int(equity / slot_size)
    return max(0, min(configured, affordable))


def can_trade_venue(venue: str, equity: float, min_notional: float) -> tuple[bool, str]:
    """Check if a venue has enough equity for at least one slot. Returns (ok, reason)."""
    slot = compute_slot_size(venue, equity, min_notional)
    max_slots = max_effective_slots(venue, equity, slot)
    if max_slots < 1:
        return False, f"insufficient equity: ${equity:.2f} for slot ${slot:.2f} (min {min_notional * 1.5:.2f})"
    return True, ""
