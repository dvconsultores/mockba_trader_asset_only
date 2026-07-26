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

def is_entry_blocked(venue: str) -> tuple[bool, str]:
    """Check if new entries should be blocked. Returns (blocked, reason)."""
    # Check global trading enabled
    if not get_setting_bool("trading_enabled", True):
        return True, "trading_enabled is off"

    # Check venue-specific auto-trade
    auto_key = f"auto_trade_{venue}"
    auto_mode = get_setting_float(auto_key, 0) if venue in ("binance",) else None
    if auto_mode is not None:
        # For now, just check trading_enabled — auto_trade_* is set by Telegram
        pass

    # Daily loss limit
    limit = get_setting_float("daily_loss_limit", 10.0)
    if limit > 0:
        pnl = get_daily_pnl(venue)
        if pnl <= -limit:
            return True, f"daily_loss_limit breached: {pnl:.2f} <= -{limit}"

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


# ── Slot sizing ───────────────────────────────────────────────────────────────

def compute_slot_size(
    venue: str,
    equity: float,
    min_notional: float,
) -> float:
    """
    Equity-based position size for a single slot.

    Recomputed once per UTC day. Uses realized PnL compounding for DEX.
    Returns the slot size in quote currency (USDT/USDC).
    """
    today = _today_utc()

    # Return cached value if still valid
    if _day_cache_date.get(venue) == today and venue in _day_cache:
        return _day_cache[venue]

    slot_pct = get_setting_float(f"{venue}_slot_pct", 15.0)
    raw = equity * (slot_pct / 100)

    floored = max(raw, min_notional * 1.5)
    _day_cache[venue] = floored
    _day_cache_date[venue] = today

    return floored


def max_effective_slots(venue: str, equity: float, slot_size: float) -> int:
    """Return how many slots equity can support. Never more than configured max."""
    configured = get_setting_int("max_slots", 1)
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
