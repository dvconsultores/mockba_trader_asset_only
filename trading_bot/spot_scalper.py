"""
MockbaV4 — Spot grid scalper (Binance).

Rules:
- Only enters when BOTH price extreme AND OBI confirm (AND, never OR).
- Regime gates direction: RANGE→buy, TREND_UP→buy, TREND_DOWN→no entry.
- No stop-loss. Time stop is the exit of last resort.
- Every cycle: manage_open_positions() runs BEFORE entry evaluation.
"""

from __future__ import annotations
import time
import uuid
from collections import deque
from typing import Optional

from trading_bot.executor import BinanceSpot
from trading_bot.types import Fill, Position
from trade.pnl import record_closed_trade, is_entry_blocked, compute_slot_size, can_trade_venue
from db.db_ops import (
    get_setting_float, get_setting_int, get_setting_bool,
    save_position, load_all_positions, update_position, delete_position,
    save_signal,
)


# ── Price memory (rolling window) ─────────────────────────────────────────────

_price_memory: dict[str, deque] = {}       # key=asset → deque of prices
_peak: dict[str, float] = {}               # key=asset → rolling peak
_trough: dict[str, float] = {}             # key=asset → rolling trough
WINDOW_SIZE = 40


def _ensure_memory(asset: str):
    if asset not in _price_memory:
        _price_memory[asset] = deque(maxlen=WINDOW_SIZE)
        _peak[asset] = 0.0
        _trough[asset] = float("inf")


def _update_price_memory(asset: str, price: float):
    _ensure_memory(asset)
    if price > 0:
        _price_memory[asset].append(price)
        _peak[asset] = max(_price_memory[asset])
        _trough[asset] = min(_price_memory[asset])


def _is_price_dip(asset: str, price: float, dip_pct: float) -> bool:
    if _peak.get(asset, 0) <= 0 or len(_price_memory.get(asset, [])) < 10:
        return False
    return (_peak[asset] - price) / _peak[asset] * 100 >= dip_pct


def _is_price_pump(asset: str, price: float, pump_pct: float) -> bool:
    t = _trough.get(asset, float("inf"))
    if t == float("inf") or len(_price_memory.get(asset, [])) < 10:
        return False
    return (price - t) / t * 100 >= pump_pct


# ── Cooldown & spacing ────────────────────────────────────────────────────────

_last_entry: dict[str, float] = {}  # key="venue:asset:side" → timestamp


def _cooldown_ok(asset: str, side: str, cooldown_sec: float) -> bool:
    key = f"binance:{asset}:{side}"
    return time.time() - _last_entry.get(key, 0) >= cooldown_sec


def _spacing_ok(asset: str, price: float, spacing_pct: float) -> bool:
    positions = load_all_positions(asset=asset, venue="binance")
    for pos in positions:
        ep = float(pos.get("entry_price", 0))
        if ep > 0 and abs(price - ep) / ep * 100 < spacing_pct:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Exit management — runs FIRST every cycle
# ═══════════════════════════════════════════════════════════════════════════════

def manage_open_positions(asset: str, exchange: BinanceSpot):
    """Check TP fills, time stops. Spot has no SL."""
    positions = load_all_positions(asset=asset, venue="binance")
    if not positions:
        return

    symbol = f"{asset}USDT"
    max_hold = get_setting_int("max_hold_minutes_spot", 120) * 60
    now = time.time()

    for pos_dict in positions:
        pos_id = pos_dict["id"]
        tp_id = pos_dict.get("tp_order_id")
        entry = float(pos_dict["entry_price"])
        signal = float(pos_dict["signal_price"])
        qty = float(pos_dict["qty"])
        opened = float(pos_dict["opened_at"])

        # Check TP fill
        if tp_id:
            status = exchange.get_order_status(symbol, tp_id)
            if status == "FILLED":
                # Get fill price — query the order for actual fill
                # For simplicity, use TP price as exit (close enough; exact fill from API if available)
                exit_price = float(pos_dict["tp_price"])
                _close_position(asset, "binance", "long", entry, exit_price, signal, qty,
                                fee_rate=0.001, pos_id=pos_id, tp_id=tp_id, reason="tp")
                continue

        # Check time stop
        if (now - opened) > max_hold:
            logger_msg = f"[EXIT] asset={asset} venue=binance reason=time_stop age={int((now-opened)/60)}m"
            # Cancel TP order first
            if tp_id:
                exchange.cancel_order(symbol, tp_id)
            # Market close — for spot, this means market sell
            # In dry_run, simulate: exit at current price
            exit_price = entry  # placeholder; real code uses live price
            _close_position(asset, "binance", "long", entry, exit_price, signal, qty,
                            fee_rate=0.001, pos_id=pos_id, tp_id=tp_id, reason="time_stop")
            continue

        # Check vanished TP order
        if tp_id:
            status = exchange.get_order_status(symbol, tp_id)
            if status in ("CANCELED", "EXPIRED", "UNKNOWN"):
                # Re-place TP at original price
                # Not implemented in dry_run phase — log warning
                pass


def _close_position(asset: str, venue: str, side: str,
                    entry: float, exit: float, signal: float, qty: float,
                    fee_rate: float, pos_id: str, tp_id: str | None, reason: str):
    fee_entry = entry * qty * fee_rate
    fee_exit = exit * qty * fee_rate
    record_closed_trade(
        asset=asset, venue=venue, side=side,
        entry_price=entry, exit_price=exit, signal_price=signal,
        qty=qty, fee_entry=fee_entry, fee_exit=fee_exit,
        opened_at=0, closed_at=time.time(), exit_reason=reason,
    )
    delete_position(asset, venue, pos_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry logic — runs AFTER manage_open_positions
# ═══════════════════════════════════════════════════════════════════════════════

def scalp_cycle(asset: str, exchange: BinanceSpot, regime: str, obi: float,
                live_price: float) -> Optional[str]:
    """Evaluate and execute one entry cycle. Returns 'buy' or None."""

    venue = "binance"

    # Direction gate by regime
    if regime == "TREND_DOWN":
        _log_signal(asset, venue, regime, obi, 0, "skipped", "regime=TREND_DOWN blocks spot entries")
        return None
    if regime not in ("RANGE", "TREND_UP"):
        _log_signal(asset, venue, regime, obi, 0, "skipped", f"regime={regime} unknown")
        return None

    # Kill switch
    equity = exchange.get_equity()
    blocked, reason = is_entry_blocked(venue, equity)
    if blocked:
        _log_signal(asset, venue, regime, obi, 0, "skipped", reason)
        return None

    # Slot limit
    positions = load_all_positions(asset=asset, venue=venue)
    max_slots = get_setting_int("max_slots", 1)
    if len(positions) >= max_slots:
        _log_signal(asset, venue, regime, obi, 0, "skipped", "max_slots reached")
        return None

    # Update price memory
    _update_price_memory(asset, live_price)

    dip_pct = get_setting_float("dip_pct", 0.4)
    pump_pct = get_setting_float("pump_pct", 0.4)
    obi_buy = get_setting_float("obi_buy_threshold", 0.96)
    obi_sell = get_setting_float("obi_sell_threshold", 1.22)
    cooldown_sec = get_setting_float("cooldown_sec", 300)
    spacing_pct = get_setting_float("min_entry_spacing_pct", 0.6)
    tp_pct = get_setting_float("tp_pct", 0.8)

    is_dip = _is_price_dip(asset, live_price, dip_pct)
    is_pump = _is_price_pump(asset, live_price, pump_pct)

    extreme_pct = 0.0
    if is_dip:
        extreme_pct = (_peak[asset] - live_price) / _peak[asset] * 100
    elif is_pump:
        extreme_pct = (live_price - _trough[asset]) / _trough[asset] * 100

    # LONG: dip + OBI buy
    if is_dip and obi < obi_buy:
        if not _cooldown_ok(asset, "long", cooldown_sec):
            _log_signal(asset, venue, regime, obi, extreme_pct, "skipped", "cooldown")
            return None
        if not _spacing_ok(asset, live_price, spacing_pct):
            _log_signal(asset, venue, regime, obi, extreme_pct, "skipped", "spacing")
            return None

        # Size — equity already fetched above for kill switch
        info = exchange.get_symbol_info(asset)
        if info is None:
            return None
        slot = compute_slot_size(venue, equity, info.min_notional)
        qty = slot / live_price
        qty = qty - (qty % info.base_tick) if info.base_tick > 0 else qty
        if qty < info.min_qty or (qty * live_price) < info.min_notional:
            _log_signal(asset, venue, regime, obi, extreme_pct, "skipped", "qty too small")
            return None

        pos_id = str(uuid.uuid4())
        fill = exchange.place_entry(asset, "long", qty, live_price, tp_pct, pos_id)
        if fill is None:
            return None

        _save_open(asset, venue, "long", fill, live_price, tp_pct, pos_id)
        _last_entry[f"{venue}:{asset}:long"] = time.time()
        _log_signal(asset, venue, regime, obi, extreme_pct, "entered", f"dip {extreme_pct:.2f}%")
        return "buy"

    # Skip reasons
    if is_dip and obi >= obi_buy:
        _log_signal(asset, venue, regime, obi, extreme_pct, "skipped", f"OBI {obi:.3f} >= {obi_buy}")
    elif is_pump and obi <= obi_sell:
        _log_signal(asset, venue, regime, obi, extreme_pct, "skipped", f"OBI {obi:.3f} <= {obi_sell}")
    elif not is_dip and not is_pump:
        pass  # no extreme → debug only

    return None


def _save_open(asset: str, venue: str, side: str, fill: Fill, signal_price: float,
               tp_pct: float, pos_id: str):
    tp_price = fill.fill_price * (1 + tp_pct / 100)
    save_position({
        "id": pos_id, "asset": asset, "venue": venue, "side": side,
        "qty": fill.sellable_qty, "entry_price": fill.fill_price,
        "signal_price": signal_price, "tp_price": tp_price,
        "sl_price": None, "tp_order_id": fill.order_id,
        "sl_order_id": None, "opened_at": time.time(),
    })


def _log_signal(asset: str, venue: str, regime: str, obi: float,
                extreme_pct: float, action: str, reason: str):
    save_signal({
        "timestamp": time.time(), "asset": asset, "venue": venue,
        "regime": regime, "obi": obi, "extreme_pct": extreme_pct,
        "action": action, "reason": reason,
    })
