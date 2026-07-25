"""
Futures Grid Scalper — Orderly DEX mean-reversion strategy for RANGE regimes.

Rules:
- Only active when regime = RANGE
- Price dip ≥ GRID_PRICE_DIP_PCT AND OBI < GRID_OBI_BUY_THRESHOLD → LONG (bracket order with SL/TP)
- Fixed TP at GRID_TP_PCT above fill price
- Mandatory SL (futures liquidation risk)
- Cooldown between entries
- Leverage support via dex_leverage setting
- Uses dex_capital setting for position sizing

No ML gate, no LLM gate, no pattern detection — edge is statistical (dips snap back, OBI confirms).
DEX-only: RANGE regime + grid scalper exclusively.
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import get_setting
from logs.log_config import apolo_trader_logger as logger
from trading_bot.futures_executor_apolo import (
    place_futures_order,
    get_close_price,
    get_user_statistics,
)
from trading_bot.send_bot_message import send_bot_message
from trade.binance_data import get_orderbook  # Orderly DEX orderbook

# ── Configuration (DB settings with .env fallback) ────────────────────────────
def _grid_setting(key: str, default: str) -> float:
    """Read a grid scalper setting from DB, falling back to env, then default."""
    try:
        val = get_setting(key)
        if val is not None:
            return float(val)
    except Exception:
        pass
    return float(os.getenv(key.upper(), default))


GRID_OBI_BUY_THRESHOLD  = _grid_setting("grid_obi_buy", "0.96")
GRID_OBI_SELL_THRESHOLD = _grid_setting("grid_obi_sell", "1.22")
GRID_TP_PCT             = _grid_setting("grid_tp_pct", "0.5")
GRID_COOLDOWN_SEC       = _grid_setting("grid_cooldown_sec", "300")
GRID_PRICE_DIP_PCT      = _grid_setting("grid_price_dip_pct", "0.4")
GRID_MAX_POSITIONS      = int(_grid_setting("grid_max_positions", "1"))
GRID_SL_PCT             = _grid_setting("grid_sl_pct", "0.8")  # DEX-specific: SL percentage
GRID_POSITION_CAPITAL   = float(_grid_setting("grid_position_capital", "15"))  # USDC per grid position
GRID_DIRECTION           = _grid_setting("grid_direction", "long")  # "long", "short", or "both"

# ── Leverage & capital ────────────────────────────────────────────────────────
def _dex_leverage() -> int:
    try:
        return int(get_setting("leverage") or 5)
    except Exception:
        return 5


def _dex_capital() -> float:
    """Position size in USDC for DEX grid scalper.
    Prefers grid_position_capital; falls back to dex_capital; then cex_capital."""
    # Prefer dedicated grid position capital
    if GRID_POSITION_CAPITAL > 0:
        return GRID_POSITION_CAPITAL

    try:
        val = get_setting("dex_capital")
        if val is not None:
            return float(val)
    except Exception:
        pass
    # Fallback: use cex_capital or 50 USDC
    try:
        return float(get_setting("cex_capital") or 50)
    except Exception:
        return 50.0


# ── Price-dip tracking (rolling memory) ──────────────────────────────────────
_price_history: deque = deque(maxlen=40)
_peak_price: float = 0.0
_trough_price: float = float('inf')


def _update_price_memory(price: float) -> None:
    global _peak_price, _trough_price
    if price > 0:
        _price_history.append(price)
        _peak_price = max(_price_history)
        _trough_price = min(_price_history)


def _is_price_dip(price: float) -> bool:
    if _peak_price <= 0 or len(_price_history) < 10:
        return False
    return (_peak_price - price) / _peak_price * 100 >= GRID_PRICE_DIP_PCT


def _is_price_pump(price: float) -> bool:
    """True if price has pumped GRID_PRICE_DIP_PCT % above the rolling trough."""
    if _trough_price == float('inf') or len(_price_history) < 10:
        return False
    return (price - _trough_price) / _trough_price * 100 >= GRID_PRICE_DIP_PCT


# ── State ────────────────────────────────────────────────────────────────────
_last_buy_at: float = 0.0
_last_sell_at: float = 0.0
_open_position_count: int = 0  # Track via Orderly API (get_user_statistics)


def _grid_direction_ok(direction: str) -> bool:
    """Check if the given direction is enabled by GRID_DIRECTION config."""
    gd = GRID_DIRECTION.strip().lower()
    if gd == "both":
        return True
    return gd == direction


def _compute_obi(orderbook: dict, depth: int = 10) -> tuple[float, dict]:
    """Compute Order Book Imbalance from Orderly order book snapshot."""
    def _qty(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    bids = sum(_qty(qty) for _, qty in orderbook.get('bids', [])[:depth])
    asks = sum(_qty(qty) for _, qty in orderbook.get('asks', [])[:depth])

    if asks == 0:
        return 2.0, {'bids': bids, 'asks': 0}

    obi = bids / asks
    return obi, {'bids': bids, 'asks': asks}


def _refresh_open_positions() -> int:
    """Check Orderly API for current open positions count."""
    global _open_position_count
    try:
        _open_position_count = get_user_statistics()
    except Exception:
        pass  # Keep last known count
    return _open_position_count


def futures_grid_scalp_cycle(asset: str = "PERP_NEAR_USDC", regime: str = "RANGE",
                              obi: float = 1.0, live_price: float = 0.0) -> Optional[str]:
    """
    Run one grid scalper cycle for Orderly DEX futures.

    Args:
        asset: Full symbol (e.g., "PERP_NEAR_USDC")
        regime: Current market regime (only acts if RANGE)
        obi: Current Order Book Imbalance
        live_price: Current price

    Returns: "buy", "sell", or None if no action taken.
    """
    global _last_buy_at, _last_sell_at

    if regime != "RANGE":
        return None

    now = time.time()
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

    # ── Check open positions ──────────────────────────────────────────────
    open_count = _refresh_open_positions()
    if open_count >= GRID_MAX_POSITIONS:
        return None  # already at max positions

    # ── Balance pre-check: ensure enough free collateral for another position ─
    from trading_bot.futures_executor_apolo import get_available_balance
    free_collateral = get_available_balance()
    capital = _dex_capital()
    leverage = _dex_leverage()
    margin_per_position = capital  # approx margin = capital at current leverage
    required = (open_count + 1) * margin_per_position
    if free_collateral is not None and required > free_collateral:
        logger.info(
            f"📉 DEX Grid: insufficient free collateral for position #{open_count + 1} "
            f"(need ${required:.0f}, have ${free_collateral:.0f})"
        )
        return None

    # ── Update price memory for dip/pump detection ────────────────────────
    if live_price > 0:
        _update_price_memory(live_price)

    # ── Common pre-conditions ─────────────────────────────────────────────
    if capital < 10:
        logger.info(f"📉 DEX Grid: insufficient capital (${capital:.0f})")
        return None

    if live_price <= 0:
        logger.warning("DEX Grid: no live price available")
        return None

    # ── Calculate position size ───────────────────────────────────────────
    notional = capital * leverage
    qty = notional / live_price

    # ── LONG entry: price dip OR OBI < buy threshold ──────────────────────
    if _grid_direction_ok("long"):
        long_cooldown_ok = (now - _last_buy_at) >= GRID_COOLDOWN_SEC
        is_dip = _is_price_dip(live_price)
        obi_buy_ok = obi < GRID_OBI_BUY_THRESHOLD

        if long_cooldown_ok and (is_dip or obi_buy_ok):
            tp_price = live_price * (1 + GRID_TP_PCT / 100)
            sl_price = live_price * (1 - GRID_SL_PCT / 100)

            order_payload = {
                "symbol": asset,
                "side": "BUY",
                "entry": live_price,
                "take_profit": tp_price,
                "stop_loss": sl_price,
                "leverage": leverage,
            }

            dip_pct = (_peak_price - live_price) / _peak_price * 100 if _peak_price > 0 else 0
            trigger = f"dip {dip_pct:.1f}%" if is_dip else f"OBI {obi:.3f}"
            logger.info(
                f"📉 DEX Grid LONG: price=${live_price:.4f}, dip={dip_pct:.2f}%, "
                f"OBI={obi:.3f}, capital=${capital:.0f}, lev={leverage}x, "
                f"TP=+{GRID_TP_PCT}%, SL=-{GRID_SL_PCT}%"
            )

            try:
                place_futures_order(order_payload)
            except Exception as e:
                logger.error(f"❌ DEX Grid LONG order failed: {e}")
                return None

            _last_buy_at = now

            if chat_id:
                send_bot_message(chat_id,
                    f"📉 DEX Grid Scalp LONG\n"
                    f"   {asset}: qty≈{qty:.4f} @ ${live_price:.4f}\n"
                    f"   TP: ${tp_price:.4f} (+{GRID_TP_PCT}%) | SL: ${sl_price:.4f} (-{GRID_SL_PCT}%)\n"
                    f"   Leverage: {leverage}x | Notional: ${notional:.0f}\n"
                    f"   Trigger: {trigger}"
                )
            return "buy"

    # ── SHORT entry: price pump OR OBI > sell threshold ───────────────────
    if _grid_direction_ok("short"):
        short_cooldown_ok = (now - _last_sell_at) >= GRID_COOLDOWN_SEC
        is_pump = _is_price_pump(live_price)
        obi_sell_ok = obi > GRID_OBI_SELL_THRESHOLD

        if short_cooldown_ok and (is_pump or obi_sell_ok):
            tp_price = live_price * (1 - GRID_TP_PCT / 100)  # TP below entry
            sl_price = live_price * (1 + GRID_SL_PCT / 100)  # SL above entry

            order_payload = {
                "symbol": asset,
                "side": "SELL",
                "entry": live_price,
                "take_profit": tp_price,
                "stop_loss": sl_price,
                "leverage": leverage,
            }

            pump_pct = (live_price - _trough_price) / _trough_price * 100 if _trough_price != float('inf') else 0
            trigger = f"pump {pump_pct:.1f}%" if is_pump else f"OBI {obi:.3f}"
            logger.info(
                f"📈 DEX Grid SHORT: price=${live_price:.4f}, pump={pump_pct:.2f}%, "
                f"OBI={obi:.3f}, capital=${capital:.0f}, lev={leverage}x, "
                f"TP=-{GRID_TP_PCT}%, SL=+{GRID_SL_PCT}%"
            )

            try:
                place_futures_order(order_payload)
            except Exception as e:
                logger.error(f"❌ DEX Grid SHORT order failed: {e}")
                return None

            _last_sell_at = now

            if chat_id:
                send_bot_message(chat_id,
                    f"📈 DEX Grid Scalp SHORT\n"
                    f"   {asset}: qty≈{qty:.4f} @ ${live_price:.4f}\n"
                    f"   TP: ${tp_price:.4f} (-{GRID_TP_PCT}%) | SL: ${sl_price:.4f} (+{GRID_SL_PCT}%)\n"
                    f"   Leverage: {leverage}x | Notional: ${notional:.0f}\n"
                    f"   Trigger: {trigger}"
                )
            return "sell"

    # ── Log why we're not entering (only when close to a signal) ──────────
    if _grid_direction_ok("long"):
        is_dip = _is_price_dip(live_price) if live_price > 0 else False
        obi_ok = obi < GRID_OBI_BUY_THRESHOLD
        if is_dip and not obi_ok:
            logger.debug(f"📊 DEX Grid: price dip detected but OBI {obi:.3f} ≥ {GRID_OBI_BUY_THRESHOLD} — waiting")
        elif obi_ok and not is_dip:
            logger.debug(f"📊 DEX Grid: OBI {obi:.3f} ok but no price dip (peak=${_peak_price:.4f}, current=${live_price:.4f})")

    if _grid_direction_ok("short"):
        is_pump = _is_price_pump(live_price) if live_price > 0 else False
        obi_sell = obi > GRID_OBI_SELL_THRESHOLD
        if is_pump and not obi_sell:
            logger.debug(f"📊 DEX Grid: price pump detected but OBI {obi:.3f} ≤ {GRID_OBI_SELL_THRESHOLD} — waiting")
        elif obi_sell and not is_pump:
            logger.debug(f"📊 DEX Grid: OBI {obi:.3f} ok but no price pump (trough=${_trough_price:.4f}, current=${live_price:.4f})")

    return None
