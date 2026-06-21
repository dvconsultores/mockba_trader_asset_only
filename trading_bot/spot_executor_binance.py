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
from db.db_ops import get_setting, increment_trades_today
from trade.binance_data import get_binance_symbol

load_dotenv()

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# Time sync with Binance server (clock skew causes -1021 errors)
_BINANCE_TIME_OFFSET_MS = 0


def _sync_binance_time() -> None:
    """Sync local clock offset with Binance server time."""
    global _BINANCE_TIME_OFFSET_MS
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/time", timeout=5)
        r.raise_for_status()
        server_ms = int(r.json().get("serverTime", 0))
        local_ms = int(time.time() * 1000)
        _BINANCE_TIME_OFFSET_MS = server_ms - local_ms
        logger.info(f"🕐 Binance time synced: offset={_BINANCE_TIME_OFFSET_MS}ms")
    except Exception as e:
        logger.warning(f"⚠️ Could not sync Binance time: {e}")


def _binance_timestamp() -> int:
    """Return a timestamp safe for Binance signed requests (server time - 1.5s safety margin)."""
    return int(time.time() * 1000) + _BINANCE_TIME_OFFSET_MS - 1500


def _sign(params: dict) -> str:
    """Generate HMAC SHA256 signature for Binance."""
    # Binance expects signature from the exact query-string order being sent.
    query_string = '&'.join(f'{k}={v}' for k, v in params.items())
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


def _binance_get_order(symbol: str, order_id: str) -> dict | None:
    """Check status of a Binance order."""
    params = {
        "symbol": symbol,
        "orderId": order_id,
        "timestamp": _binance_timestamp(),
    }
    params["signature"] = _sign(params)
    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"⚠️ Binance get order error: {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Binance get order exception: {e}")
        return None


def _binance_cancel_order(symbol: str, order_id: str) -> bool:
    """Cancel a Binance order. Returns True on success."""
    params = {
        "symbol": symbol,
        "orderId": order_id,
        "timestamp": _binance_timestamp(),
    }
    params["signature"] = _sign(params)
    try:
        r = requests.delete(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code == 200:
            logger.info(f"🗑️ Binance order {order_id} cancelled")
            return True
        logger.warning(f"⚠️ Binance cancel error: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"⚠️ Binance cancel exception: {e}")
        return False


def _limit_buy_with_fallback(
    binance_symbol: str,
    qty: float,
    limit_price: float,
    timeout_seconds: int = 30,
    notify_chat_id: int = 0,
) -> dict | None:
    """
    Place a LIMIT BUY order and wait for fill. If unfilled after timeout_seconds,
    cancel and fall back to MARKET BUY.

    Sends Telegram notifications at each stage when notify_chat_id is provided.

    Returns the fill result dict (same format as Binance order response), or None on failure.
    """
    notional = qty * limit_price

    # --- STEP A: Place LIMIT BUY ---
    buy_params = {
        "symbol": binance_symbol,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{qty}",
        "price": f"{limit_price}",
        "timestamp": _binance_timestamp(),
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
            logger.error(f"❌ Binance LIMIT BUY error: {r.status_code} {r.text[:400]}")
            if notify_chat_id:
                send_bot_message(notify_chat_id, f"❌ LIMIT BUY failed for {binance_symbol}: {r.text[:200]}")
            return None

        order_result = r.json()
        order_id = str(order_result.get("orderId", ""))
        status = order_result.get("status", "")
        logger.info(f"📝 LIMIT BUY placed: orderId={order_id}, status={status}, price={limit_price}")

        if notify_chat_id:
            send_bot_message(notify_chat_id,
                f"📝 LIMIT BUY placed: {qty} {binance_symbol} @ ${limit_price}\n"
                f"   Notional: ${notional:.2f}\n"
                f"   Order ID: {order_id}")

    except Exception as e:
        logger.error(f"❌ Binance LIMIT BUY exception: {e}")
        if notify_chat_id:
            send_bot_message(notify_chat_id, f"❌ LIMIT BUY exception: {e}")
        return None

    # If already filled immediately, return
    if status == "FILLED":
        executed_qty = float(order_result.get("executedQty", qty))
        cumm_quote = float(order_result.get("cummulativeQuoteQty", notional))
        avg_price = cumm_quote / executed_qty if executed_qty > 0 else limit_price
        logger.info(f"✅ LIMIT BUY filled immediately: {order_id}")
        if notify_chat_id:
            send_bot_message(notify_chat_id,
                f"✅ LIMIT BUY filled: {executed_qty} {binance_symbol} @ ${avg_price:.4f}\n"
                f"   Maker ✅ (lower fees)")
        return order_result

    # --- STEP B: Poll for fill ---
    poll_interval = 3  # seconds
    elapsed = 0
    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        order_info = _binance_get_order(binance_symbol, order_id)
        if order_info is None:
            logger.warning(f"⚠️ Could not check order {order_id}, falling back to MARKET")
            break

        current_status = order_info.get("status", "")
        executed_qty = float(order_info.get("executedQty", 0))

        if current_status == "FILLED":
            cumm_quote = float(order_info.get("cummulativeQuoteQty", 0))
            avg_price = cumm_quote / executed_qty if executed_qty > 0 else limit_price
            logger.info(f"✅ LIMIT BUY filled after {elapsed}s: {executed_qty} @ avg {avg_price}")
            if notify_chat_id:
                send_bot_message(notify_chat_id,
                    f"✅ LIMIT BUY filled after {elapsed}s: {executed_qty} {binance_symbol} @ ${avg_price:.4f}\n"
                    f"   Maker ✅ (lower fees)")
            return order_info

        if current_status in ("CANCELED", "EXPIRED", "REJECTED"):
            logger.warning(f"⚠️ LIMIT BUY {current_status} after {elapsed}s, falling back to MARKET")
            break

        logger.info(f"⏳ LIMIT BUY pending ({elapsed}s/{timeout_seconds}s): filled={executed_qty}/{qty}")

    # --- STEP C: Cancel and fall back to MARKET ---
    if status != "FILLED":
        _binance_cancel_order(binance_symbol, order_id)
        if notify_chat_id:
            send_bot_message(notify_chat_id,
                f"⏰ LIMIT BUY cancelled after {timeout_seconds}s — switching to MARKET BUY\n"
                f"   {qty} {binance_symbol}")

    logger.info(f"🔄 Falling back to MARKET BUY for {binance_symbol}")
    market_params = {
        "symbol": binance_symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": f"{qty}",
        "timestamp": _binance_timestamp(),
    }
    market_params["signature"] = _sign(market_params)

    try:
        r = requests.post(
            f"{BINANCE_BASE_URL}/api/v3/order",
            params=market_params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance MARKET BUY fallback error: {r.status_code} {r.text[:400]}")
            if notify_chat_id:
                send_bot_message(notify_chat_id, f"❌ MARKET BUY fallback failed: {r.text[:200]}")
            return None

        market_result = r.json()
        executed_qty = float(market_result.get("executedQty", 0))
        cumm_quote = float(market_result.get("cummulativeQuoteQty", 0))
        avg_price = cumm_quote / executed_qty if executed_qty > 0 else limit_price
        logger.info(f"✅ MARKET BUY filled: {executed_qty}")
        if notify_chat_id:
            send_bot_message(notify_chat_id,
                f"✅ MARKET BUY filled: {executed_qty} {binance_symbol} @ ${avg_price:.4f}\n"
                f"   Taker ⚡ (standard fees)")
        return market_result

    except Exception as e:
        logger.error(f"❌ Binance MARKET BUY fallback exception: {e}")
        if notify_chat_id:
            send_bot_message(notify_chat_id, f"❌ MARKET BUY exception: {e}")
        return None


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


def has_open_orders_binance(symbol: str = None, fail_safe: bool = False) -> bool:
    """Check if there are any open limit orders on Binance.
    Returns True if pending orders exist, False otherwise.
    """
    _sync_binance_time()
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        if fail_safe:
            logger.warning("⚠️ Binance API keys missing — fail-safe assumes open orders exist")
            return True
        return False

    binance_symbol = get_binance_symbol(symbol) if symbol else None
    params = {"timestamp": _binance_timestamp()}
    if binance_symbol:
        params["symbol"] = binance_symbol
    params["signature"] = _sign(params)

    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/openOrders",
            params=params,
            headers=_headers(),
            timeout=10
        )
        if r.status_code != 200:
            error_body = (r.text or "")[:300]
            body_lower = error_body.lower()
            if '"code":-1022' in body_lower or "signature for this request is not valid" in body_lower:
                logger.warning("⚠️ Open orders check ongoing, can't send signal right now.")
                if fail_safe:
                    return True
                return False

            logger.warning(f"⚠️ Binance open orders check issue: {r.status_code} {error_body}")

            # Some assets are valid in analysis but not in Binance spot openOrders(symbol).
            # Retry once without symbol to avoid false blocking.
            if binance_symbol and r.status_code == 400:
                if "invalid symbol" in body_lower or '"code":-1121' in body_lower:
                    logger.warning(
                        f"⚠️ Binance symbol {binance_symbol} rejected in openOrders; retrying without symbol filter"
                    )
                    retry_params = {"timestamp": _binance_timestamp()}
                    retry_params["signature"] = _sign(retry_params)
                    retry_resp = requests.get(
                        f"{BINANCE_BASE_URL}/api/v3/openOrders",
                        params=retry_params,
                        headers=_headers(),
                        timeout=10
                    )
                    if retry_resp.status_code == 200:
                        orders = retry_resp.json()
                        if orders:
                            logger.info(f"📋 Binance has {len(orders)} open order(s) — skipping pattern search")
                            return True
                        return False
                    logger.warning(
                        f"⚠️ Binance open orders retry issue: {retry_resp.status_code} {(retry_resp.text or '')[:300]}"
                    )

            if fail_safe:
                logger.warning("⚠️ Binance open-order check failed — fail-safe suppressing signals")
                return True
            return False

        orders = r.json()
        if orders:
            logger.info(f"📋 Binance has {len(orders)} open order(s) — skipping pattern search")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Binance open orders error: {e}")
        if fail_safe:
            logger.warning("⚠️ Binance open-order exception — fail-safe suppressing signals")
            return True
        return False


def get_binance_balance(asset: str = "USDT") -> float:
    """Get available balance for an asset on Binance spot."""
    params = {"timestamp": _binance_timestamp()}
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
    Place a Binance spot order: LIMIT BUY (with MARKET fallback), then LIMIT SELL at TP.
    No stop loss for spot (user can wait days).
    Sends Telegram notifications at each stage.
    """
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        logger.error("❌ BINANCE_API_KEY or BINANCE_SECRET_KEY not set!")
        return None

    _sync_binance_time()

    symbol = signal['symbol']
    binance_symbol = get_binance_symbol(symbol)
    entry = float(signal['entry'])
    tp = float(signal['take_profit'])
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

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

    # Position sizing for CEX: use configured capital, or all available balance
    try:
        configured_capital = float(get_setting('cex_capital') or 0)
    except (TypeError, ValueError):
        configured_capital = 0.0

    if configured_capital <= 0:
        # Use all available USDT balance
        trade_amount = balance
        logger.info(f"💰 Using full balance: ${trade_amount:.2f} (cex_capital not set)")
    else:
        trade_amount = min(configured_capital, balance)

    trade_amount = max(trade_amount, info['min_notional'])

    qty = trade_amount / entry
    qty = _round_step(qty, info['base_tick'])

    if qty < info['base_min']:
        logger.error(f"❌ Quantity {qty} below minimum {info['base_min']}")
        return None

    notional = qty * entry
    if notional < info['min_notional']:
        logger.error(f"❌ Notional ${notional:.2f} below min ${info['min_notional']}")
        return None

    # --- STEP 1: LIMIT BUY with fallback to MARKET after 30s ---
    quote_tick = info['quote_tick']
    price_precision = max(0, int(round(-math.log10(quote_tick)))) if quote_tick > 0 else 4
    limit_price = round(entry, price_precision)

    buy_result = _limit_buy_with_fallback(binance_symbol, qty, limit_price, timeout_seconds=30, notify_chat_id=chat_id)
    if buy_result is None:
        logger.error("❌ LIMIT BUY and MARKET fallback both failed")
        return None

    filled_qty = float(buy_result.get("executedQty", 0))
    cumm_quote = float(buy_result.get("cummulativeQuoteQty", 0))
    avg_price = cumm_quote / filled_qty if filled_qty > 0 else entry

    logger.info(f"✅ Binance BUY filled: {filled_qty} @ {avg_price:.6f}")

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
        "timestamp": _binance_timestamp(),
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
            if chat_id:
                send_bot_message(chat_id,
                    f"⚠️ BUY filled but TP SELL failed for {binance_symbol}.\n"
                    f"   Place SELL manually: {sell_qty} @ ${tp_price}!")
            return None

        sell_result = r.json()
        order_id = sell_result.get("orderId", "0")

    except Exception as e:
        logger.error(f"❌ Binance SELL error: {e}")
        if chat_id:
            send_bot_message(chat_id,
                f"⚠️ BUY filled but TP SELL failed for {binance_symbol}.\n"
                f"   Place SELL manually: {sell_qty} @ ${tp_price}!")
        return None

    # --- Final notification with TP sell info ---
    is_maker = buy_result.get("type") != "MARKET"  # LIMIT fills are maker
    maker_label = "Maker ✅ (lower fees)" if is_maker else "Taker ⚡ (standard fees)"
    tp_notional = sell_qty * tp_price
    msg = (
        f"✅ Binance Spot Trade: {binance_symbol}\n"
        f"   Side: 🟢 BUY\n"
        f"   Qty: {round(filled_qty, 4)}\n"
        f"   Entry: ${round(avg_price, 6)}\n"
        f"   TP Sell: ${round(tp_price, 6)} ({round((tp_price/avg_price - 1)*100, 2)}% gain)\n"
        f"   TP Notional: ${tp_notional:.2f}\n"
        f"   Notional: ${round(filled_qty * avg_price, 2)}\n"
        f"   Type: {maker_label}\n"
        f"   No SL (spot — can hold)\n"
        f"   Order ID: {order_id}"
    )
    if chat_id:
        send_bot_message(chat_id, msg)
    logger.info(f"✅ Binance spot: BUY {filled_qty} @ {avg_price} → TP SELL @ {tp_price}")

    new_count = increment_trades_today()
    logger.info(f"📊 Daily trades today: {new_count}")

    return buy_result
