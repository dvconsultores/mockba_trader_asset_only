"""
Binance Spot Executor - Market BUY + Limit SELL (TP only, no SL).
For spot trading: buy on reversal, sell on bounce. No liquidation risk.
"""
import os
import sys
import time
import hmac
import hashlib
import math
import requests
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logs.log_config import apolo_trader_logger as logger
from trading_bot.send_bot_message import send_bot_message
from db.db_ops import get_setting, get_trades_today, increment_trades_today
from futures_perps.trade.apolo.binance_data import get_binance_symbol

load_dotenv()

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

MAX_TRADES_PER_DAY = 1


def _sign(params: dict) -> str:
    """Generate HMAC SHA256 signature for Binance."""
    query_string = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


def _round_step(value: float, step: float) -> float:
    """Round down to step size."""
    if step <= 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    return round(value - (value % step), precision)


def get_binance_exchange_info(symbol: str) -> dict:
    """Get exchange info for a Binance symbol (precision, filters)."""
    binance_symbol = get_binance_symbol(symbol)
    url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo"
    params = {"symbol": binance_symbol}

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            logger.error(f"❌ Binance exchangeInfo error: {r.status_code}")
            return None
        data = r.json()
        symbols = data.get("symbols", [])
        if not symbols:
            return None
        symbol_info = symbols[0]

        filters = {f['filterType']: f for f in symbol_info.get('filters', [])}

        lot_size = filters.get('LOT_SIZE', {})
        price_filter = filters.get('PRICE_FILTER', {})
        notional = filters.get('NOTIONAL', {}) or filters.get('MIN_NOTIONAL', {})

        return {
            "base_tick": float(lot_size.get('stepSize', 0.01)),
            "base_min": float(lot_size.get('minQty', 0.01)),
            "base_max": float(lot_size.get('maxQty', 999999)),
            "quote_tick": float(price_filter.get('tickSize', 0.01)),
            "min_notional": float(notional.get('minNotional', 10)),
        }
    except Exception as e:
        logger.error(f"❌ Binance exchange info error: {e}")
        return None


def has_open_orders_binance(symbol: str = None) -> bool:
    """Check if there are any open limit orders on Binance.
    Returns True if pending orders exist, False otherwise.
    """
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False

    params = {"timestamp": int(time.time() * 1000)}
    if symbol:
        params["symbol"] = get_binance_symbol(symbol)
    params["signature"] = _sign(params)

    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/openOrders",
            params=params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance open orders check error: {r.status_code}")
            return False

        orders = r.json()
        if orders:
            logger.info(f"📋 Binance has {len(orders)} open order(s) — skipping pattern search")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Binance open orders error: {e}")
        return False


def get_binance_balance(asset: str = "USDT") -> float:
    """Get available balance for an asset on Binance spot."""
    params = {"timestamp": int(time.time() * 1000)}
    params["signature"] = _sign(params)

    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/account",
            params=params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance balance error: {r.status_code} {r.text[:200]}")
            return 0.0

        balances = r.json().get("balances", [])
        for b in balances:
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0
    except Exception as e:
        logger.error(f"❌ Binance balance error: {e}")
        return 0.0


def place_spot_order(signal: dict):
    """
    Place a Binance spot order: market BUY, then limit SELL at TP.
    No stop loss for spot (user can wait days).
    """
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        logger.error("❌ BINANCE_API_KEY or BINANCE_SECRET_KEY not set!")
        return None

    # Check daily limit
    trades_today = get_trades_today()
    if trades_today >= MAX_TRADES_PER_DAY:
        logger.warning(f"⛔ Daily trade limit reached ({trades_today}/{MAX_TRADES_PER_DAY})")
        send_bot_message(
            int(os.getenv("TELEGRAM_CHAT_ID")),
            f"⛔ Daily limit reached ({trades_today}/{MAX_TRADES_PER_DAY}). No more trades today."
        )
        return None

    symbol = signal['symbol']
    binance_symbol = get_binance_symbol(symbol)
    entry = float(signal['entry'])
    tp = float(signal['take_profit'])

    # Get exchange info
    info = get_binance_exchange_info(symbol)
    if not info:
        logger.error(f"❌ Failed to get Binance exchange info for {symbol}")
        return None

    # Get balance
    balance = get_binance_balance("USDT")
    if balance < info['min_notional']:
        logger.error(f"❌ Insufficient USDT balance: {balance}")
        return None

    # Position sizing: use risk_level % of balance
    try:
        risk_pct = float(get_setting('risk_level') or 10)
    except (TypeError, ValueError):
        risk_pct = 10.0

    trade_amount = balance * (risk_pct / 100)
    trade_amount = max(trade_amount, info['min_notional'])
    trade_amount = min(trade_amount, balance)

    qty = trade_amount / entry
    qty = _round_step(qty, info['base_tick'])

    if qty < info['base_min']:
        logger.error(f"❌ Quantity {qty} below minimum {info['base_min']}")
        return None

    notional = qty * entry
    if notional < info['min_notional']:
        logger.error(f"❌ Notional ${notional:.2f} below min ${info['min_notional']}")
        return None

    # --- STEP 1: Market BUY ---
    buy_params = {
        "symbol": binance_symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": f"{qty}",
        "timestamp": int(time.time() * 1000),
    }
    buy_params["signature"] = _sign(buy_params)

    try:
        r = requests.post(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=buy_params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance BUY error: {r.status_code} {r.text[:400]}")
            return None

        buy_result = r.json()
        filled_qty = float(buy_result.get("executedQty", 0))
        cumm_quote = float(buy_result.get("cummulativeQuoteQty", 0))
        avg_price = cumm_quote / filled_qty if filled_qty > 0 else entry

        logger.info(f"✅ Binance BUY filled: {filled_qty} @ {avg_price:.6f}")

    except Exception as e:
        logger.error(f"❌ Binance BUY error: {e}")
        return None

    if filled_qty <= 0:
        logger.error("❌ BUY order filled 0 quantity")
        return None

    # --- STEP 2: Limit SELL at TP (no SL for spot) ---
    quote_tick = info['quote_tick']
    tp_precision = max(0, int(round(-math.log10(quote_tick)))) if quote_tick > 0 else 2
    tp_price = round(tp, tp_precision)
    sell_qty = _round_step(filled_qty, info['base_tick'])

    sell_params = {
        "symbol": binance_symbol,
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{sell_qty}",
        "price": f"{tp_price}",
        "timestamp": int(time.time() * 1000),
    }
    sell_params["signature"] = _sign(sell_params)

    try:
        r = requests.post(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=sell_params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance SELL (TP) error: {r.status_code} {r.text[:400]}")
            send_bot_message(
                int(os.getenv("TELEGRAM_CHAT_ID")),
                f"⚠️ BUY filled but TP SELL failed for {binance_symbol}. Place SELL manually at {tp_price}!"
            )
            return None

        sell_result = r.json()
        order_id = sell_result.get("orderId", "0")

    except Exception as e:
        logger.error(f"❌ Binance SELL error: {e}")
        send_bot_message(
            int(os.getenv("TELEGRAM_CHAT_ID")),
            f"⚠️ BUY filled but TP SELL failed for {binance_symbol}. Place SELL manually at {tp_price}!"
        )
        return None

    # --- Notify ---
    msg = (
        f"✅ Binance Spot Order: {binance_symbol}\n"
        f"Side: 🟢 BUY (Spot)\n"
        f"Qty: {round(filled_qty, 6)}\n"
        f"Buy Price: {round(avg_price, 6)}\n"
        f"TP Sell: {round(tp_price, 6)}\n"
        f"Notional: {round(filled_qty * avg_price, 2)}\n"
        f"No SL (spot - can hold)\n"
        f"Order ID: {order_id}"
    )
    send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), msg)
    logger.info(f"✅ Binance spot: BUY {filled_qty} @ {avg_price} → TP SELL @ {tp_price}")

    new_count = increment_trades_today()
    logger.info(f"📊 Daily trades: {new_count}/{MAX_TRADES_PER_DAY}")

    return buy_result
