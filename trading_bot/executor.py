"""
MockbaV4 — Unified exchange executor.

Two concrete implementations: BinanceSpot (CEX) and OrderlyFutures (DEX).
Both return Fill objects with actual fill prices (constitution V).
SymbolFilters cached per asset per session.
dry_run short-circuits all order paths.
"""

from __future__ import annotations
import os
import time
import json
import math
import hmac
import hashlib
import uuid
import requests
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from dataclasses import replace

from trading_bot.types import Fill, SymbolFilters
from db.db_ops import get_setting_bool, get_setting_float, load_all_positions
from logs.log_config import apolo_trader_logger as logger


# ── Shared utilities ──────────────────────────────────────────────────────────

def _floor(value: float, tick: float) -> float:
    """Floor to tick step."""
    if tick <= 0:
        return value
    return math.floor(value / tick) * tick


def _ceil(value: float, tick: float) -> float:
    """Ceil to tick step."""
    if tick <= 0:
        return value
    return math.ceil(value / tick) * tick


def _precision(tick: float) -> int:
    """Decimal places in a tick size."""
    if tick <= 0:
        return 8
    return max(0, int(round(-math.log10(tick))))


def _fmt(value: float, tick: float) -> str:
    """Format a number to tick precision for API payloads."""
    return f"{value:.{_precision(tick)}f}"


def _client_order_id(name: str, asset: str, position_id: str, kind: str) -> str:
    """Short client order ID valid for exchange rules (≤36 chars, [a-zA-Z0-9-_]).

    The full position UUID would overflow Binance's 36-char limit, so we use a
    short hex prefix of it. The exchange-returned order ID is what the bot
    tracks positions by, not this field.
    """
    short = position_id.replace("-", "")[:10]
    return f"m{name[:4]}-{asset}-{short}-{kind[:2]}"[:36]


_dry_run_cache: dict[str, SymbolFilters] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Binance Spot
# ═══════════════════════════════════════════════════════════════════════════════

class BinanceSpot:
    """Binance spot exchange: market buy + limit sell TP. No leverage, no SL."""

    def __init__(self):
        self.base_url = "https://api.binance.com"
        self.api_key = os.getenv("BINANCE_API_KEY", "").strip()
        self.secret = os.getenv("BINANCE_SECRET_KEY", "").strip()
        self._symbol_cache: dict[str, SymbolFilters] = {}

    @property
    def name(self) -> str:
        return "binance"

    # ── Auth ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    def _sign(self, params: dict) -> str:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _get(self, path: str, params: dict | None = None, signed: bool = False):
        if params is None:
            params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000) - 1500
            params["signature"] = self._sign(params)
        r = requests.get(f"{self.base_url}{path}", params=params, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, params: dict):
        params["timestamp"] = int(time.time() * 1000) - 1500
        params["signature"] = self._sign(params)
        r = requests.post(f"{self.base_url}{path}", params=params, headers=self._headers(), timeout=10)
        return r

    # ── Symbol info ───────────────────────────────────────────────────────

    def get_symbol_info(self, asset: str) -> SymbolFilters | None:
        symbol = f"{asset}USDT"
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        try:
            data = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
            sym = data["symbols"][0]
            filters = {f["filterType"]: f for f in sym.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price_f = filters.get("PRICE_FILTER", {})
            notional = filters.get("NOTIONAL", {}) or filters.get("MIN_NOTIONAL", {})
            info = SymbolFilters(
                symbol=symbol,
                base_tick=float(lot.get("stepSize", 0.01)),
                quote_tick=float(price_f.get("tickSize", 0.01)),
                min_qty=float(lot.get("minQty", 0.001)),
                min_notional=float(notional.get("minNotional", 10)),
            )
            self._symbol_cache[symbol] = info
            return info
        except Exception as e:
            logger.warning(f"Binance symbol info failed for {symbol}: {e}")
            return None

    # ── Equity ────────────────────────────────────────────────────────────

    def get_equity(self) -> float | None:
        """Total venue equity, or None when the exchange is unreachable.

        Feature 015 (Constitution IV): unknown is NEVER zero — the old 0.0
        return silently disabled the percentage daily-loss limit during API
        outages (limit = equity × pct = 0 ⇒ check skipped).

        Equity = free+locked USDT plus open positions valued at their entry
        fill from the local DB (audit #12: coins were invisible, so equity
        collapsed toward free cash while positions were open, shrinking every
        percent-of-equity number). DB quantities/prices are real fills; the
        valuation error is bounded by the TP/SL band. A DB read failure
        degrades to USDT-only (direction-safe), never to None — only the
        exchange being unreachable is unknown state.
        """
        try:
            data = self._get("/api/v3/account", signed=True)
        except Exception:
            return None
        usdt = 0.0
        for bal in data.get("balances", []):
            if bal["asset"] == "USDT":
                usdt = float(bal["free"]) + float(bal["locked"])
                break
        try:
            positions = sum(float(p["qty"]) * float(p["entry_price"])
                            for p in load_all_positions(venue="binance"))
        except Exception:
            logger.warning("get_equity: position valuation failed — reporting USDT only")
            positions = 0.0
        return usdt + positions

    def get_asset_balance(self, asset: str) -> float | None:
        """Free balance of the base asset; None if the account query failed."""
        try:
            data = self._get("/api/v3/account", signed=True)
            for bal in data.get("balances", []):
                if bal["asset"] == asset:
                    return float(bal["free"])
            return 0.0  # asset not listed → balance is zero
        except Exception:
            return None

    def get_price(self, asset: str) -> float | None:
        """Current live price for an asset (Binance ticker)."""
        try:
            r = requests.get(f"{self.base_url}/api/v3/ticker/price",
                             params={"symbol": f"{asset}USDT"}, timeout=5)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            return None

    def _fee_to_usdt(self, fee_amount: float, fee_asset: str,
                     ref_price: float, notional: float) -> float:
        """Commission expressed in USDT (Constitution V: recorded fees are dollars).

        BNB commissions (fee discount, feature 017) are valued at the live BNB
        price; base-asset commissions at the trade's own fill price. If the
        BNB ticker is unreachable, the configured per-leg rate estimates the
        fee — an estimate beats recording 0 for a fee that was really paid.
        """
        if get_setting_bool("cex_fee_bnb", False) and fee_asset != "BNB" and fee_amount > 0:
            logger.warning(
                f"cex_fee_bnb=true but commission paid in {fee_asset} — "
                f"BNB reserve may be exhausted; full fee rate applies")
        if fee_amount <= 0 or fee_asset in ("USDT", "USDC"):
            return fee_amount
        if fee_asset == "BNB":
            bnb_price = self.get_price("BNB")
            if bnb_price:
                return fee_amount * bnb_price
            return notional * get_setting_float("cex_round_trip_fee_pct", 0.15) / 200.0
        return fee_amount * ref_price if ref_price > 0 else fee_amount

    # ── Order placement ───────────────────────────────────────────────────

    def place_entry(
        self, asset: str, side: str, qty: float, price: float,
        tp_pct: float, position_id: str, sl_pct: float = 0.0,
    ) -> Fill | None:
        """Market buy + limit sell TP + optional stop-loss. Returns Fill with actual fill price."""
        dry = get_setting_bool("dry_run", True)
        symbol = f"{asset}USDT"
        cid = _client_order_id(self.name, asset, position_id, "entry")

        if dry:
            return Fill(
                filled_qty=qty, fill_price=price, fee_amount=qty * price * 0.001,
                fee_asset="USDT", sellable_qty=qty, order_id="dry-entry",
                client_order_id=cid, raw={"dry_run": True}, tp_order_id="dry-entry",
            )

        info = self.get_symbol_info(asset)
        if info is None:
            return None
        else:
            buy_side = "BUY" if side == "long" else "SELL"
            params = {
                "symbol": symbol, "side": buy_side, "type": "MARKET",
                "quantity": _fmt(qty, info.base_tick),
                "newClientOrderId": cid,
            }
            r = self._post("/api/v3/order", params)
            if r.status_code != 200:
                logger.warning(f"Binance entry failed: {r.status_code} {r.text[:200]}")
                return None
            data = r.json()
            filled_qty = sum(float(f["qty"]) for f in data.get("fills", []))
            quote_qty = sum(float(f["price"]) * float(f["qty"]) for f in data.get("fills", []))
            if filled_qty == 0:
                filled_qty = float(data.get("executedQty", 0))
            if quote_qty == 0 and filled_qty > 0:
                quote_qty = float(data.get("cummulativeQuoteQty", 0))
            fill_price = quote_qty / filled_qty if filled_qty > 0 else 0.0

            # Fee — Binance may take fee from base asset
            fee_asset = "USDT"
            fee_amount = 0.0
            for f in data.get("fills", []):
                fee_amount += float(f.get("commission", 0))
                fee_asset = f.get("commissionAsset", "USDT")

            # Only a base-asset commission reduces what we can sell — BNB-paid
            # fees (feature 017) leave the full fill sellable.
            sellable = filled_qty
            if fee_asset not in ("USDT", "USDC", "BNB"):
                sellable = filled_qty - fee_amount

            fee_amount = self._fee_to_usdt(fee_amount, fee_asset, fill_price, quote_qty)
            fee_asset = "USDT"

            fill = Fill(
                filled_qty=filled_qty, fill_price=fill_price if filled_qty > 0 else price,
                fee_amount=fee_amount, fee_asset=fee_asset,
                sellable_qty=_floor(sellable, info.base_tick),
                order_id=str(data.get("orderId", "")), client_order_id=cid,
                raw=data,
            )

        # Place TP/SL as a single OCO order. Placing a separate TP limit + SL stop
        # fails with -2010: the TP limit locks the entire base balance, so Binance
        # rejects the SL stop placement. OCO places both atomically and auto-cancels
        # the other leg when one fills.
        tp_price = fill.fill_price * (1 + tp_pct / 100)
        tp_price = _floor(tp_price, info.quote_tick)  # toward entry = floor for sell
        sl_price = 0.0
        if sl_pct > 0:
            sl_price = fill.fill_price * (1 - sl_pct / 100)
            sl_price = _floor(sl_price, info.quote_tick)  # away from entry = lower = wider

        tp_order_id = None
        sl_order_id = None
        if not dry:
            oco_placed = False
            if sl_pct > 0:
                oco_params = {
                    "symbol": symbol, "side": "SELL",
                    "quantity": _fmt(fill.sellable_qty, info.base_tick),
                    "aboveType": "LIMIT_MAKER",
                    "abovePrice": _fmt(tp_price, info.quote_tick),
                    "aboveClientOrderId": _client_order_id(self.name, asset, position_id, "tp"),
                    "belowType": "STOP_LOSS",
                    "belowStopPrice": _fmt(sl_price, info.quote_tick),
                    "belowClientOrderId": _client_order_id(self.name, asset, position_id, "sl"),
                    "newOrderRespType": "RESULT",
                }
                r = self._post("/api/v3/orderList/oco", oco_params)
                if r.status_code == 200:
                    oco_placed = True
                    for rep in r.json().get("orderReports", []):
                        if rep.get("type") == "LIMIT_MAKER":
                            tp_order_id = str(rep.get("orderId", ""))
                        elif rep.get("type") == "STOP_LOSS":
                            sl_order_id = str(rep.get("orderId", ""))
                    if not tp_order_id:
                        logger.warning(f"Binance OCO placed but TP order id missing: {r.text[:200]}")
                    if not sl_order_id:
                        logger.warning(f"Binance OCO placed but SL order id missing: {r.text[:200]}")
                else:
                    logger.warning(f"Binance OCO placement failed: {r.status_code} {r.text[:200]}")
            # Fallback: TP limit only (software SL in spot_scalper still protects).
            # Only when the OCO was NOT placed — never stack a second TP on a live OCO.
            if not oco_placed:
                tp_params = {
                    "symbol": symbol, "side": "SELL", "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": _fmt(fill.sellable_qty, info.base_tick),
                    "price": _fmt(tp_price, info.quote_tick),
                    "newClientOrderId": _client_order_id(self.name, asset, position_id, "tp"),
                }
                r = self._post("/api/v3/order", tp_params)
                if r.status_code != 200:
                    logger.warning(f"Binance TP placement failed: {r.status_code} {r.text[:200]}")
                else:
                    tp_order_id = str(r.json().get("orderId", ""))

        return replace(fill, tp_order_id=tp_order_id, sl_order_id=sl_order_id)

    def market_sell(self, asset: str, qty: float) -> Fill | None:
        """Market-sell a spot position (time-stop exit). Returns Fill or None."""
        dry = get_setting_bool("dry_run", True)
        symbol = f"{asset}USDT"
        if dry:
            return Fill(filled_qty=qty, fill_price=0.0, fee_amount=0.0, fee_asset="USDT",
                        sellable_qty=qty, order_id="dry-sell",
                        client_order_id=_client_order_id(self.name, asset, "exit", "sl"),
                        raw={"dry_run": True})
        info = self.get_symbol_info(asset)
        if info is None:
            return None
        cid = _client_order_id(self.name, asset, "exit", "sl")
        params = {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": _fmt(qty, info.base_tick),
            "newClientOrderId": cid,
        }
        r = self._post("/api/v3/order", params)
        if r.status_code != 200:
            logger.warning(f"Binance market sell failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        filled_qty = sum(float(f["qty"]) for f in data.get("fills", []))
        quote_qty = sum(float(f["price"]) * float(f["qty"]) for f in data.get("fills", []))
        if filled_qty == 0:
            filled_qty = float(data.get("executedQty", 0))
        if quote_qty == 0 and filled_qty > 0:
            quote_qty = float(data.get("cummulativeQuoteQty", 0))
        fill_price = quote_qty / filled_qty if filled_qty > 0 else 0.0
        fee_asset = "USDT"
        fee_amount = 0.0
        for f in data.get("fills", []):
            fee_amount += float(f.get("commission", 0))
            fee_asset = f.get("commissionAsset", "USDT")
        fee_amount = self._fee_to_usdt(fee_amount, fee_asset, fill_price, quote_qty)
        return Fill(
            filled_qty=filled_qty, fill_price=fill_price if filled_qty > 0 else 0.0,
            fee_amount=fee_amount, fee_asset="USDT", sellable_qty=filled_qty,
            order_id=str(data.get("orderId", "")), client_order_id=cid,
            raw=data,
        )

    def get_order_status(self, symbol: str, order_id: str) -> str:
        try:
            data = self._get("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)
            return data.get("status", "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def get_order_fills(self, symbol: str, order_id: str) -> tuple[float, float] | None:
        """Real fill for a closed order: (average_price, total_commission)."""
        try:
            data = self._get("/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)
            if not isinstance(data, list) or not data:
                return None
            qty = sum(float(t["qty"]) for t in data)
            notional = sum(float(t["price"]) * float(t["qty"]) for t in data)
            comm = 0.0
            comm_asset = "USDT"
            for t in data:
                comm += float(t.get("commission", 0))
                comm_asset = t.get("commissionAsset", "USDT")
            if qty <= 0:
                return None
            avg = notional / qty
            comm = self._fee_to_usdt(comm, comm_asset, avg, notional)
            return (avg, comm)
        except Exception:
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            params = {"symbol": symbol, "orderId": order_id}
            params["timestamp"] = int(time.time() * 1000) - 1500
            params["signature"] = self._sign(params)
            r = requests.delete(f"{self.base_url}/api/v3/order", params=params, headers=self._headers(), timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def get_open_orders(self, asset: str) -> list[dict]:
        symbol = f"{asset}USDT"
        try:
            data = self._get("/api/v3/openOrders", {"symbol": symbol}, signed=True)
            return data if isinstance(data, list) else []
        except Exception:
            return []


# ═══════════════════════════════════════════════════════════════════════════════
# Orderly Futures
# ═══════════════════════════════════════════════════════════════════════════════

class OrderlyFutures:
    """Orderly DEX futures: bracket orders (entry + TP + SL) with leverage.
    Constitution III: SL must be verified present after entry fill."""

    def __init__(self):
        self.base_url = os.getenv("ORDERLY_BASE_URL", "https://api-evm.orderly.org")
        self.account_id = os.getenv("ORDERLY_ACCOUNT_ID", "")
        self.secret = os.getenv("ORDERLY_SECRET", "")
        self.public_key = os.getenv("ORDERLY_PUBLIC_KEY", "")
        self._symbol_cache: dict[str, SymbolFilters] = {}

    @property
    def name(self) -> str:
        return "orderly"

    # ── Auth ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "orderly-account-id": self.account_id,
            "orderly-key": self.public_key,
            "Content-Type": "application/json",
        }

    def _sign(self, params: str) -> str:
        from base58 import b58decode
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from base64 import urlsafe_b64encode
        key = Ed25519PrivateKey.from_private_bytes(b58decode(self.secret.replace("ed25519:", "")))
        sig = key.sign(params.encode())
        return urlsafe_b64encode(sig).decode().rstrip("=")

    def _get(self, path: str, params: dict | None = None) -> dict:
        ts = str(int(time.time() * 1000))
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        payload = f"{ts}GET{path}?{qs}" if qs else f"{ts}GET{path}"
        hdrs = {**self._headers(), "orderly-timestamp": ts, "orderly-signature": self._sign(payload)}
        r = requests.get(f"{self.base_url}{path}", params=params, headers=hdrs, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        import json as _json
        body_str = _json.dumps(body, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        payload = f"{ts}POST{path}{body_str}"
        hdrs = {**self._headers(), "orderly-timestamp": ts, "orderly-signature": self._sign(payload)}
        r = requests.post(f"{self.base_url}{path}", data=body_str, headers=hdrs, timeout=10)
        return r

    # ── Symbol info ───────────────────────────────────────────────────────

    def get_symbol_info(self, asset: str) -> SymbolFilters | None:
        symbol = f"PERP_{asset}_USDC"
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        try:
            data = self._get("/v1/public/info", {"symbol": symbol})
            d = data.get("data", {})
            info = SymbolFilters(
                symbol=symbol,
                base_tick=float(d.get("base_tick", 0.01)),
                quote_tick=float(d.get("quote_tick", 0.0001)),
                min_qty=float(d.get("min_order_quantity", 0.01)),
                min_notional=float(d.get("min_notional", 10)),
            )
            self._symbol_cache[symbol] = info
            return info
        except Exception as e:
            logger.warning(f"Orderly symbol info failed for {symbol}: {e}")
            return None

    # ── Equity ────────────────────────────────────────────────────────────

    def get_equity(self) -> float | None:
        """Total collateral, or None when unreachable (feature 015 —
        Constitution IV: unknown is never zero). Orderly's holding already
        reports total account value, so no position valuation is needed."""
        try:
            data = self._get("/v1/client/holding")
            return float(data.get("data", {}).get("holding", 0))
        except Exception:
            return None

    # ── Order placement ───────────────────────────────────────────────────

    def place_entry(
        self, asset: str, side: str, qty: float, price: float,
        tp_pct: float, sl_pct: float, leverage: int, position_id: str,
    ) -> Fill | None:
        """Bracket order: market entry + TP + SL. Verifies SL exists after fill."""
        dry = get_setting_bool("dry_run", True)
        cid = _client_order_id(self.name, asset, position_id, "entry")

        if dry:
            return Fill(
                filled_qty=qty, fill_price=price,
                fee_amount=qty * price * 0.0003, fee_asset="USDC",
                sellable_qty=qty, order_id="dry-entry",
                client_order_id=cid, raw={"dry_run": True}, tp_order_id="dry-entry",
            )

        info = self.get_symbol_info(asset)
        if info is None:
            return None

        symbol = info.symbol
        buy_sell = "BUY" if side == "long" else "SELL"

        # Compute TP/SL from signal price first, then adjust from fill
        tp_price = price * (1 + tp_pct / 100) if side == "long" else price * (1 - tp_pct / 100)
        sl_price = price * (1 - sl_pct / 100) if side == "long" else price * (1 + sl_pct / 100)

        # Round: stop away from entry, TP toward entry
        # "Away" = further from entry → wider stop, more room before triggering
        # "Toward" = closer to entry → conservative TP
        if side == "long":
            tp_price = _floor(tp_price, info.quote_tick)   # toward entry = lower (conservative)
            sl_price = _floor(sl_price, info.quote_tick)   # away from entry = lower (wider stop)
        else:
            tp_price = _ceil(tp_price, info.quote_tick)    # toward entry = higher (conservative)
            sl_price = _ceil(sl_price, info.quote_tick)    # away from entry = higher (wider stop)

        body = {
            "symbol": symbol, "side": buy_sell, "order_type": "MARKET",
            "order_quantity": _fmt(qty, info.base_tick),
            "leverage": leverage,
            "client_order_id": cid,
        }
        r = self._post("/v1/order", body)
        if r.status_code != 200:
            logger.warning(f"Orderly entry failed: {r.status_code} {r.text[:200]}")
            return None

        data = r.json().get("data", r.json())
        fill_price = float(data.get("average_executed_price", price))
        filled_qty = float(data.get("executed_quantity", qty))
        fee = float(data.get("total_fee", 0))

        fill = Fill(
            filled_qty=filled_qty, fill_price=fill_price,
            fee_amount=fee, fee_asset="USDC",
            sellable_qty=filled_qty,
            order_id=str(data.get("order_id", "")), client_order_id=cid,
            raw=data,
        )

        # Recompute TP/SL from actual fill
        if side == "long":
            actual_tp = fill.fill_price * (1 + tp_pct / 100)
            actual_sl = fill.fill_price * (1 - sl_pct / 100)
            actual_tp = _floor(actual_tp, info.quote_tick)
            actual_sl = _floor(actual_sl, info.quote_tick)  # away from entry = lower
        else:
            actual_tp = fill.fill_price * (1 - tp_pct / 100)
            actual_sl = fill.fill_price * (1 + sl_pct / 100)
            actual_tp = _ceil(actual_tp, info.quote_tick)
            actual_sl = _ceil(actual_sl, info.quote_tick)   # away from entry = higher

        # Place TP/SL bracket
        tp_body = {
            "symbol": symbol, "side": "SELL" if side == "long" else "BUY",
            "order_type": "LIMIT", "order_quantity": _fmt(fill.sellable_qty, info.base_tick),
            "order_price": _fmt(actual_tp, info.quote_tick),
            "client_order_id": _client_order_id(self.name, asset, position_id, "tp"),
        }
        sl_body = {
            "symbol": symbol, "side": "SELL" if side == "long" else "BUY",
            "order_type": "STOP_MARKET", "order_quantity": _fmt(fill.sellable_qty, info.base_tick),
            "trigger_price": _fmt(actual_sl, info.quote_tick),
            "client_order_id": _client_order_id(self.name, asset, position_id, "sl"),
        }
        tp_r = self._post("/v1/order", tp_body)
        sl_r = self._post("/v1/order", sl_body)
        tp_order_id = str(tp_r.json().get("data", tp_r.json()).get("order_id", "")) if tp_r.status_code == 200 else None
        sl_order_id = str(sl_r.json().get("data", sl_r.json()).get("order_id", "")) if sl_r.status_code == 200 else None

        # Verify SL exists (constitution III)
        if sl_r.status_code != 200:
            logger.warning(f"SL placement failed: {sl_r.status_code}")
            # Emergency: place standalone stop
            sl_r2 = self._post("/v1/order", sl_body)
            if sl_r2.status_code != 200:
                logger.critical(f"Emergency SL also failed — market closing position")
                self._post("/v1/order", {
                    "symbol": symbol,
                    "side": "SELL" if side == "long" else "BUY",
                    "order_type": "MARKET",
                    "order_quantity": _fmt(fill.sellable_qty, info.base_tick),
                })
                return None

        return replace(fill, tp_order_id=tp_order_id, sl_order_id=sl_order_id)

    def get_open_positions(self, asset: str | None = None) -> list[dict]:
        try:
            data = self._get("/v1/positions")
            positions = data.get("data", {}).get("rows", [])
            if asset:
                symbol = f"PERP_{asset}_USDC"
                positions = [p for p in positions if p.get("symbol") == symbol and float(p.get("position_qty", 0)) != 0]
            return positions
        except Exception:
            return []

    def get_order_status(self, order_id: str) -> str:
        try:
            data = self._get(f"/v1/order/{order_id}")
            return data.get("data", {}).get("status", "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def get_order_fills(self, order_id: str) -> tuple[float, float] | None:
        """Real fill for a closed order: (average_price, total_fee).

        Mirrors BinanceSpot.get_order_fills. Field names verified against the
        Orderly EVM API (average_executed_price / total_fee) — the same fields
        place_entry already reads. None when unavailable, so the caller can fall
        back to a logged estimate rather than recording a fabricated number.
        """
        try:
            data = self._get(f"/v1/order/{order_id}")
            d = data.get("data", data) or {}
            price = float(d.get("average_executed_price") or 0)
            if price <= 0:
                return None
            return (price, float(d.get("total_fee") or 0))
        except Exception:
            return None

    def market_close(self, asset: str, side: str, qty: float) -> Fill | None:
        """Reduce-only market close for an open futures position.

        `side` is the POSITION side ("long"/"short"); the order is sent in the
        opposite direction. reduce_only guarantees the order can never flip the
        position into the other direction if the size is stale.
        """
        dry = get_setting_bool("dry_run", True)
        cid = _client_order_id(self.name, asset, "exit", "cl")
        if dry:
            return Fill(filled_qty=qty, fill_price=0.0, fee_amount=0.0, fee_asset="USDC",
                        sellable_qty=qty, order_id="dry-close",
                        client_order_id=cid, raw={"dry_run": True})
        info = self.get_symbol_info(asset)
        if info is None:
            return None
        body = {
            "symbol": info.symbol,
            "side": "SELL" if side == "long" else "BUY",
            "order_type": "MARKET",
            "order_quantity": _fmt(qty, info.base_tick),
            "reduce_only": True,
            "client_order_id": cid,
        }
        r = self._post("/v1/order", body)
        if r.status_code != 200:
            logger.warning(f"Orderly market close failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json().get("data", r.json())
        return Fill(
            filled_qty=float(data.get("executed_quantity", qty)),
            fill_price=float(data.get("average_executed_price", 0) or 0),
            fee_amount=float(data.get("total_fee", 0) or 0), fee_asset="USDC",
            sellable_qty=float(data.get("executed_quantity", qty)),
            order_id=str(data.get("order_id", "")), client_order_id=cid, raw=data,
        )

    def place_tp(self, asset: str, side: str, qty: float, tp_price: float,
                 position_id: str) -> str | None:
        """(Re-)place the take-profit limit. Returns the order id.

        Used by the regime exit to move the TP to breakeven on the EXCHANGE —
        updating only the DB would leave the real order where it was.
        """
        dry = get_setting_bool("dry_run", True)
        if dry:
            return "dry-tp"
        info = self.get_symbol_info(asset)
        if info is None:
            return None
        body = {
            "symbol": info.symbol,
            "side": "SELL" if side == "long" else "BUY",
            "order_type": "LIMIT",
            "order_quantity": _fmt(qty, info.base_tick),
            "order_price": _fmt(tp_price, info.quote_tick),
            "reduce_only": True,
            "client_order_id": _client_order_id(self.name, asset, position_id, "tp"),
        }
        r = self._post("/v1/order", body)
        if r.status_code != 200:
            logger.error(f"Orderly TP re-placement failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json().get("data", r.json())
        return str(data.get("order_id", "")) or None

    def place_stop(self, asset: str, side: str, qty: float, stop_price: float,
                   position_id: str) -> str | None:
        """(Re-)place a STOP_MARKET protective stop. Returns the order id.

        Used to restore protection when a close attempt fails — Constitution III
        admits no cycle ending with an unprotected leveraged position.
        """
        dry = get_setting_bool("dry_run", True)
        if dry:
            return "dry-sl"
        info = self.get_symbol_info(asset)
        if info is None:
            return None
        body = {
            "symbol": info.symbol,
            "side": "SELL" if side == "long" else "BUY",
            "order_type": "STOP_MARKET",
            "order_quantity": _fmt(qty, info.base_tick),
            "trigger_price": _fmt(stop_price, info.quote_tick),
            "reduce_only": True,
            "client_order_id": _client_order_id(self.name, asset, position_id, "sl"),
        }
        r = self._post("/v1/order", body)
        if r.status_code != 200:
            logger.error(f"Orderly stop re-placement failed: {r.status_code} {r.text[:200]}")
            return None
        data = r.json().get("data", r.json())
        return str(data.get("order_id", "")) or None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            ts = str(int(time.time() * 1000))
            payload = f"{ts}DELETE/v1/order"
            hdrs = {**self._headers(), "orderly-timestamp": ts, "orderly-signature": self._sign(payload)}
            r = requests.delete(
                f"{self.base_url}/v1/order",
                params={"order_id": order_id, "symbol": symbol},
                headers=hdrs, timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False
