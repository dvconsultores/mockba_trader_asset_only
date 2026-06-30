"""
Binance Spot Trade Fetcher - Fetches myTrades from Binance API,
pairs BUY+SELL to calculate realized PnL, and exports normalized records
compatible with the Orderly trade format used by performance-llm.py.
"""
import os
import sys
import time
import json
import hmac
import hashlib
from pathlib import Path
from collections import deque
from typing import Any
import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logs.log_config import apolo_trader_logger as logger

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
    query_string = '&'.join(f'{k}={v}' for k, v in params.items())
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


def fetch_binance_my_trades(symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
    """
    Fetch trade history from Binance for a given symbol.

    Args:
        symbol: Binance symbol (e.g., NEARUSDT)
        limit: Max trades to fetch (default 1000)

    Returns:
        List of raw trade dicts from Binance
    """
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        logger.warning("⚠️ Binance API keys missing — cannot fetch spot trades")
        return []

    params = {
        "symbol": symbol,
        "limit": limit,
        "timestamp": _binance_timestamp(),
        "recvWindow": 60000,
    }
    params["signature"] = _sign(params)

    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/myTrades",
            params=params,
            headers=_headers(),
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"❌ Binance myTrades error: {r.status_code} {r.text[:300]}")
            return []
        return r.json()
    except Exception as e:
        logger.error(f"❌ Binance myTrades exception: {e}")
        return []


def _pair_binance_trades(raw_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Pair BUY and SELL trades using FIFO matching to calculate realized PnL.

    Spot is buy-low-sell-high only. Each completed BUY→SELL round-trip produces
    ONE record (not two) with the full PnL, representing one trading decision.

    Unmatched BUYs (still-open positions) are kept in the queue and not exported.

    Returns normalized records with the same schema as Orderly trades:
      - symbol, side, executed_price, executed_quantity, executed_timestamp,
        fee, realized_pnl, is_maker, exchange, order_id
    """
    if not raw_trades:
        return []

    # Sort by trade time ascending
    sorted_trades = sorted(raw_trades, key=lambda t: t.get("time", 0))

    # FIFO queue of open BUY positions
    buy_queue: deque = deque()
    completed_trades: list[dict[str, Any]] = []

    for t in sorted_trades:
        price = float(t.get("price", 0))
        qty = float(t.get("qty", 0))
        commission = float(t.get("commission", 0))
        is_buyer = t.get("isBuyer", False)
        is_maker = t.get("isMaker", False)
        trade_time = t.get("time", 0)
        symbol = t.get("symbol", "")

        if is_buyer:
            # BUY: queue the position, don't emit a record yet
            buy_queue.append({
                "price": price,
                "qty": qty,
                "fee": commission,
                "time": trade_time,
                "symbol": symbol,
                "is_maker": is_maker,
                "order_id": str(t.get("orderId", "")),
            })
        else:
            # SELL: close one or more BUY positions (FIFO)
            sell_qty_remaining = qty

            while sell_qty_remaining > 0 and buy_queue:
                buy = buy_queue[0]
                match_qty = min(sell_qty_remaining, buy["qty"])

                # Allocate proportional buy fee to this match
                buy_fee_ratio = match_qty / buy["qty"] if buy["qty"] > 0 else 0
                allocated_buy_fee = buy["fee"] * buy_fee_ratio

                # PnL for this match: (sell_price - buy_price) * qty
                match_pnl = (price - buy["price"]) * match_qty

                # Net PnL = gross PnL - buy fees - sell fees
                # Sell fee is proportional to matched qty
                sell_fee_ratio = match_qty / qty if qty > 0 else 0
                allocated_sell_fee = commission * sell_fee_ratio
                net_pnl = match_pnl - allocated_buy_fee - allocated_sell_fee

                # Emit ONE record per completed round-trip
                # side="BUY" because spot is long-only (buy low → sell high)
                completed_trades.append({
                    "symbol": f"PERP_{symbol.replace('USDT', '_USDC')}",
                    "side": "BUY",
                    "executed_price": buy["price"],
                    "executed_quantity": match_qty,
                    "executed_timestamp": buy["time"],
                    "fee": round(allocated_buy_fee + allocated_sell_fee, 8),
                    "realized_pnl": round(net_pnl, 8),
                    "is_maker": 1 if is_maker else 0,
                    "exchange": "cex",
                    "order_id": buy["order_id"],
                })

                # Reduce or remove the BUY position
                buy["qty"] -= match_qty
                buy["fee"] -= allocated_buy_fee
                sell_qty_remaining -= match_qty

                if buy["qty"] <= 1e-12:
                    buy_queue.popleft()

    # Unmatched BUYs are still-open positions — not exported
    if buy_queue:
        logger.info(f"📭 {len(buy_queue)} unpaired BUY(s) remain open (still-held positions)")

    return completed_trades


def get_binance_trades_for_analysis(symbol_filter: str = "NEAR") -> list[dict[str, Any]]:
    """
    Main entry point: fetch and normalize Binance spot trades for performance analysis.

    Args:
        symbol_filter: Base asset name (e.g., "NEAR") — will be converted to Binance symbol

    Returns:
        List of normalized trade dicts compatible with Orderly trade schema
    """
    binance_symbol = f"{symbol_filter}USDT"

    # Sync time BEFORE any signed request (clock skew causes -1021 errors)
    _sync_binance_time()

    raw_trades = fetch_binance_my_trades(binance_symbol)

    if not raw_trades:
        logger.info(f"📭 No Binance spot trades found for {binance_symbol}")
        return []

    paired = _pair_binance_trades(raw_trades)
    logger.info(f"✅ Fetched {len(raw_trades)} raw Binance trades → {len(paired)} paired records for {symbol_filter}")

    # Export to JSON for reference
    output_path = PROJECT_ROOT / "data" / "binance_trades.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(paired, fp, ensure_ascii=False, indent=2)

    return paired


if __name__ == "__main__":
    trades = get_binance_trades_for_analysis("NEAR")
    print(f"Fetched {len(trades)} Binance spot trades for NEAR")
    for t in trades[:5]:
        print(f"  {t['side']} {t['executed_quantity']} @ {t['executed_price']} | PnL: {t['realized_pnl']} | Fee: {t['fee']}")
