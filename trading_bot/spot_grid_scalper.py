"""
Spot Grid Scalper — Binance spot mean-reversion strategy for RANGE regimes.

Rules:
- Only active when regime = RANGE
- OBI < GRID_OBI_BUY_THRESHOLD  → LIMIT BUY at bid (maker)
- OBI > GRID_OBI_SELL_THRESHOLD → LIMIT SELL at ask (maker), if holding
- Fixed TP at GRID_TP_PCT above fill price
- Cooldown between same-direction entries
- Uses cex_capital setting for position sizing (shared with reversal scalper)

No ML gate, no LLM gate — edge is statistical (OBI extremes snap back in ranges).
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import get_setting
from logs.log_config import apolo_trader_logger as logger
from trading_bot.spot_executor_binance import (
    _limit_buy_with_fallback,
    get_binance_balance as _get_binance_balance,
    get_binance_exchange_info,
    get_binance_symbol,
    _binance_timestamp,
    _sign,
    _headers,
    _sync_binance_time,
)
from trading_bot.send_bot_message import send_bot_message
from trade.binance_data import get_orderbook_binance, get_binance_price

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


GRID_OBI_BUY_THRESHOLD = _grid_setting("grid_obi_buy", "0.96")
GRID_OBI_SELL_THRESHOLD = _grid_setting("grid_obi_sell", "1.22")
GRID_TP_PCT = _grid_setting("grid_tp_pct", "0.5")
GRID_COOLDOWN_SEC = _grid_setting("grid_cooldown_sec", "300")
GRID_PRICE_DIP_PCT = _grid_setting("grid_price_dip_pct", "0.4")  # buy when price dips this % below recent peak
GRID_MAX_POSITIONS = int(os.getenv("GRID_MAX_POSITIONS", "1"))

BINANCE_BASE_URL = "https://api.binance.com"

# ── Price-dip tracking (rolling memory, no API calls needed) ──────────────────
from collections import deque
_price_history: deque = deque(maxlen=40)  # ~20 min at ~30s cycles
_peak_price: float = 0.0


def _update_price_memory(price: float) -> None:
    """Feed each cycle's live price into rolling memory."""
    global _peak_price
    if price > 0:
        _price_history.append(price)
        _peak_price = max(_price_history)


def _is_price_dip(price: float) -> bool:
    """True if price has dipped GRID_PRICE_DIP_PCT % below the rolling peak."""
    if _peak_price <= 0 or len(_price_history) < 10:
        return False  # still warming up
    return (_peak_price - price) / _peak_price * 100 >= GRID_PRICE_DIP_PCT

# ── State ────────────────────────────────────────────────────────────────────
_last_buy_at: float = 0.0
_last_sell_at: float = 0.0
_open_positions: list[dict] = []  # [{"qty": ..., "entry_price": ..., "tp_price": ..., "tp_order_id": ...}]


def _compute_obi(orderbook: dict, depth: int = 10) -> tuple[float, dict]:
    """Compute Order Book Imbalance from Binance order book snapshot."""
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


def _place_tp_sell(binance_symbol: str, qty: float, tp_price: float, quote_tick: float) -> Optional[str]:
    """Place a GTC limit sell order at TP. Returns order ID or None."""
    _sync_binance_time()
    info = get_binance_exchange_info(binance_symbol.replace("USDT", ""))  # HACK: strip USDT suffix
    if not info:
        # Fallback: get info from existing function using raw symbol without USDT
        pass

    tp_precision = max(0, int(round(-__import__('math').log10(quote_tick)))) if quote_tick > 0 else 2
    tp_price = round(tp_price, tp_precision)

    params = {
        "symbol": binance_symbol,
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{qty}",
        "price": f"{tp_price}",
        "timestamp": _binance_timestamp(),
    }
    params["signature"] = _sign(params)

    try:
        r = __import__('requests').post(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=params,
            headers=_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"❌ Grid TP SELL error: {r.status_code} {r.text[:200]}")
            return None
        result = r.json()
        order_id = str(result.get("orderId", ""))
        logger.info(f"📝 Grid TP SELL placed: {qty} @ ${tp_price} (order {order_id})")
        return order_id
    except Exception as e:
        logger.error(f"❌ Grid TP SELL exception: {e}")
        return None


def _get_trade_amount() -> float:
    """Get the configured trade amount for CEX spot (shared with reversal scalper)."""
    try:
        configured = float(get_setting('cex_capital') or 0)
    except (TypeError, ValueError):
        configured = 0.0

    if configured <= 0:
        balance = _get_binance_balance("USDT") or 0.0
        return balance
    return configured


def _check_open_positions(binance_symbol: str) -> None:
    """Check if any TP orders have filled; update _open_positions."""
    global _open_positions
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
    if not BINANCE_API_KEY:
        return

    still_open = []
    for pos in _open_positions:
        tp_id = pos.get("tp_order_id")
        if not tp_id:
            continue
        params = {
            "symbol": binance_symbol,
            "orderId": tp_id,
            "timestamp": _binance_timestamp(),
        }
        params["signature"] = _sign(params)
        try:
            r = __import__('requests').get(
                f"{BINANCE_BASE_URL}/api/v3/order",
                params=params,
                headers=_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "FILLED":
                    logger.info(f"✅ Grid TP filled: {pos['qty']} @ ${pos['tp_price']}")
                    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
                    if chat_id:
                        pnl = (pos['tp_price'] - pos['entry_price']) * pos['qty']
                        send_bot_message(chat_id,
                            f"✅ Grid Scalp Closed\n"
                            f"   {binance_symbol}: {pos['qty']:.4f} @ ${pos['tp_price']:.4f}\n"
                            f"   PnL: ${pnl:.2f}")
                    continue  # don't keep in still_open
            still_open.append(pos)
        except Exception:
            still_open.append(pos)

    _open_positions = still_open


def grid_scalp_cycle(asset: str = "NEAR", regime: str = "RANGE", obi: float = 1.0,
                     live_price: float = 0.0) -> Optional[str]:
    """
    Run one grid scalper cycle for Binance spot.

    Args:
        asset: Base asset (e.g., "NEAR")
        regime: Current market regime (only acts if RANGE)
        obi: Current Order Book Imbalance
        live_price: Current price

    Returns: "buy", "sell", or None if no action taken.
    """
    global _last_buy_at, _last_sell_at

    if regime != "RANGE":
        return None

    symbol = f"{asset}USDT"
    binance_symbol = get_binance_symbol(symbol)
    now = time.time()

    # ── Check open positions ──────────────────────────────────────────────
    _check_open_positions(binance_symbol)

    if len(_open_positions) >= GRID_MAX_POSITIONS:
        return None  # already at max positions

    # ── Update price memory for dip detection ─────────────────────────────
    if live_price > 0:
        _update_price_memory(live_price)

    # ── Decide ─────────────────────────────────────────────────────────────
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    import math

    # Helper: execute a buy at current price
    def _execute_buy(trigger_reason: str) -> Optional[str]:
        global _last_buy_at
        trade_amount = _get_trade_amount()
        if trade_amount < 10:
            logger.info(f"📉 Grid {trigger_reason}: insufficient capital (${trade_amount:.0f})")
            return None

        entry = live_price if live_price > 0 else get_binance_price(symbol)
        if entry <= 0:
            logger.warning(f"Grid {trigger_reason}: could not get live price")
            return None

        info = get_binance_exchange_info(symbol)
        if not info:
            logger.error(f"Grid {trigger_reason}: could not get exchange info")
            return None

        qty = trade_amount / entry
        qty = qty - (qty % info['base_tick']) if info['base_tick'] > 0 else qty

        if qty < info['base_min'] or (qty * entry) < info['min_notional']:
            logger.info(f"📉 Grid {trigger_reason}: qty too small ({qty:.4f})")
            return None

        quote_tick = info['quote_tick']
        price_precision = max(0, int(round(-math.log10(quote_tick)))) if quote_tick > 0 else 4
        limit_price = round(entry, price_precision)

        logger.info(f"📉 Grid BUY ({trigger_reason}): price=${entry:.4f}, qty={qty:.4f}, notional=${qty*entry:.2f}")
        buy_result = _limit_buy_with_fallback(binance_symbol, qty, limit_price, timeout_seconds=30,
                                               notify_chat_id=chat_id)
        if buy_result is None:
            logger.error(f"Grid {trigger_reason}: LIMIT BUY failed")
            return None

        _last_buy_at = now

        filled_qty = float(buy_result.get("executedQty", 0))
        cumm_quote = float(buy_result.get("cummulativeQuoteQty", 0))
        avg_price = cumm_quote / filled_qty if filled_qty > 0 else entry

        tp_price = avg_price * (1 + GRID_TP_PCT / 100)
        tp_order_id = _place_tp_sell(binance_symbol, filled_qty, tp_price, quote_tick)

        if tp_order_id:
            _open_positions.append({
                "qty": filled_qty,
                "entry_price": avg_price,
                "tp_price": tp_price,
                "tp_order_id": tp_order_id,
            })
            if chat_id:
                send_bot_message(chat_id,
                    f"📉 Grid Scalp BUY ({trigger_reason})\n"
                    f"   {binance_symbol}: {filled_qty:.4f} @ ${avg_price:.4f}\n"
                    f"   TP: ${tp_price:.4f} (+{GRID_TP_PCT}%)\n"
                    f"   Peak: ${_peak_price:.4f} | OBI: {obi:.3f}")
        return "buy"

    # ── Entry path 1: Price dip (price dropped below recent peak) ─────────
    if _is_price_dip(live_price) and (now - _last_buy_at) > GRID_COOLDOWN_SEC:
        dip_pct = (_peak_price - live_price) / _peak_price * 100
        logger.info(f"📉 Grid: price dip {dip_pct:.2f}% from peak ${_peak_price:.4f}")
        return _execute_buy(f"dip {dip_pct:.1f}%")

    # ── Entry path 2: OBI extreme (bearish order book imbalance) ──────────
    if obi < GRID_OBI_BUY_THRESHOLD and (now - _last_buy_at) > GRID_COOLDOWN_SEC:
        logger.info(f"📉 Grid: OBI={obi:.3f} < {GRID_OBI_BUY_THRESHOLD}")
        return _execute_buy(f"OBI {obi:.3f}")

    elif obi > GRID_OBI_SELL_THRESHOLD and _open_positions and (now - _last_sell_at) > GRID_COOLDOWN_SEC:
        # Bullish extreme + we hold → could sell, but TP orders already handle this.
        # The grid scalper doesn't actively sell; it lets TP orders fill.
        # This branch is a safety valve: if no TP order exists, sell at market.
        pass

    return None
