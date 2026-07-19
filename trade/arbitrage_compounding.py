"""
Compounding Arbitrage Orchestrator — inventory-based simultaneous execution.

Refactored from transfer-based model to:
- Concurrent two-leg execution (FR-04)
- Inventory ledger & capital allocation gating (FR-02, FR-03, FR-13)
- Unified break-even threshold (FR-06)
- Statistical asset rotation (FR-07, FR-08)
- Direction preference as passive rebalancing (FR-09)
- State persistence & crash recovery (FR-10)
- Simulation mode parity (FR-11)
- Telegram notifications & remote start/stop (FR-12)
"""

import requests
import time
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict
import os
from dotenv import load_dotenv
import sys

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "arbitrage.log"

logger = logging.getLogger("arbitrage_compounding")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)
    logger.propagate = False

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.db_ops import (
    initialize_database_tables,
    insert_inventory_snapshot,
    get_latest_inventory,
    get_current_capital_allocation,
    record_capital_change,
    initialize_capital_allocation,
    insert_rotation_decision,
    get_arbitrage_run_state,
    set_arbitrage_run_state,
    upsert_setting,
    get_setting,
)
from trade.spread_llm_analyzer import (
    calculate_break_even_threshold,
    executable_spread,
    executable_spread_for_direction,
    observe_and_score,
    get_best_spread_asset,
    get_binance_book_tickers_bulk,
    get_bitget_book_tickers_bulk,
    get_binance_quote_volume_bulk,
)

# Import real trading executor
try:
    from trade.trading_executor import execute_simultaneous_legs
    TRADING_EXECUTOR_AVAILABLE = True
except ImportError:
    logger.info("⚠️  trading_executor not available - running in SIMULATION mode")
    TRADING_EXECUTOR_AVAILABLE = False

# Import Telegram notification sender (non-blocking)
try:
    from trading_bot.send_bot_message import send_bot_message
    TELEGRAM_AVAILABLE = True
except ImportError:
    logger.info("⚠️  Telegram bot not available for notifications")
    TELEGRAM_AVAILABLE = False

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trading.db"

# ── Configuration (from .env with defaults) ──────────────────────────────
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT", "100"))
CYCLE_SLEEP_SEC = float(os.getenv("ARB_CYCLE_SLEEP_SEC", "5"))

# New tunables (NFR-03)
INITIAL_CAPITAL_BINANCE = float(os.getenv("ARB_INITIAL_CAPITAL_BINANCE", "100"))
INITIAL_CAPITAL_BITGET = float(os.getenv("ARB_INITIAL_CAPITAL_BITGET", "100"))
INVENTORY_IMBALANCE_RATIO = float(os.getenv("ARB_INVENTORY_IMBALANCE_RATIO", "2.0"))
INVENTORY_IMBALANCE_PERIOD_SEC = float(os.getenv("ARB_INVENTORY_IMBALANCE_PERIOD_SEC", "3600"))
ROTATION_CADENCE_SEC = float(os.getenv("ARB_ROTATION_CADENCE_SEC", "300"))
DRY_SPELL_DURATION_SEC = float(os.getenv("ARB_DRY_SPELL_DURATION_SEC", "1800"))
ROTATION_SCORE_MARGIN = float(os.getenv("ARB_ROTATION_SCORE_MARGIN", "1.5"))
SAMPLING_INTERVAL_SEC = float(os.getenv("ARB_SAMPLING_INTERVAL_SEC", "30"))


# ── Notification dispatch (non-blocking) ─────────────────────────────────

def _send_notification(message: str):
    """Send a Telegram notification without blocking trade execution."""
    if not TELEGRAM_AVAILABLE:
        return
    try:
        t = threading.Thread(target=_send_notification_impl, args=(message,), daemon=True)
        t.start()
    except Exception as e:
        logger.warning(f"Failed to dispatch notification: {e}")


def _send_notification_impl(message: str):
    """Actual notification send, runs in background thread."""
    try:
        send_bot_message(message)
    except Exception as e:
        logger.warning(f"Notification delivery failed: {e}")


# ── Inventory helpers ────────────────────────────────────────────────────

def refresh_inventory(exchange: str, asset: str) -> Optional[float]:
    """Fetch free balance from exchange API and persist to inventory ledger."""
    try:
        if exchange == "binance":
            from trade.trading_executor import binance_get_balance
            balance = binance_get_balance(asset)
        else:
            from trade.trading_executor import bitget_get_balance
            balance = bitget_get_balance(asset)

        if balance is not None:
            insert_inventory_snapshot(exchange, asset, balance, "api")
            logger.info(f"  Inventory refresh: {exchange}/{asset} = {balance}")
        else:
            logger.warning(f"  Inventory refresh FAILED for {exchange}/{asset}")
        return balance
    except Exception as e:
        logger.error(f"  Inventory refresh error for {exchange}/{asset}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════

class CompoundingArbitrage:
    """
    Inventory-based simultaneous arbitrage with statistical asset rotation.

    Key changes from the transfer-based model:
    - Capital pre-positioned on both exchanges; no on-chain transfer per trade.
    - Concurrent buy/sell dispatch (FR-04).
    - Pre-trade inventory gate bounded by capital allocation (FR-03).
    - Statistical asset scoring over rolling observation window (FR-07, FR-08).
    - Direction preference as passive rebalancing aid (FR-09).
    - Telegram notifications and remote start/stop (FR-12).
    - Capital allocation compounding (FR-13).
    """

    def __init__(self):
        self.session_asset: Optional[str] = None
        self.session_direction: str = "binance_to_bitget"
        self.last_failure_reason: str = ""
        self._in_flight: bool = False
        self._stop_requested: bool = False
        self._last_rotation_check: float = 0.0
        self._last_opportunity_at: float = time.time()
        self._same_direction_skips: int = 0
        self._imbalance_flagged_at: Optional[float] = None
        self._ensure_db()

    # ── Database & state ──────────────────────────────────────────────────

    def _ensure_db(self):
        """Initialize all database tables."""
        initialize_database_tables()
        initialize_capital_allocation("binance", INITIAL_CAPITAL_BINANCE)
        initialize_capital_allocation("bitget", INITIAL_CAPITAL_BITGET)
        logger.info(f"✓ Capital allocation: binance=${INITIAL_CAPITAL_BINANCE}, bitget=${INITIAL_CAPITAL_BITGET}")

    def _load_state(self) -> Tuple[int, str]:
        """Load last cycle number and current direction from DB."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT cycle_num, direction
                FROM arbitrage_compounding
                ORDER BY cycle_num DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                cycle_num, direction = row
                next_direction = "bitget_to_binance" if direction == "binance_to_bitget" else "binance_to_bitget"
                return cycle_num + 1, next_direction
            return 1, "binance_to_bitget"

    def _get_session_asset(self) -> Optional[str]:
        """Get the current session asset from DB settings (persisted across restarts)."""
        return get_setting("arbitrage_session_asset")

    def _set_session_asset(self, asset: str):
        """Persist the current session asset."""
        upsert_setting("arbitrage_session_asset", asset)
        self.session_asset = asset

    # ── Startup reconciliation (FR-10) ────────────────────────────────────

    def _startup_reconciliation(self):
        """On startup, reconcile persisted inventory against live exchange balances."""
        logger.info("Startup reconciliation: checking exchange balances...")
        for exchange in ["binance", "bitget"]:
            for asset in ["USDT"]:
                persisted = get_latest_inventory(exchange, asset)
                live = refresh_inventory(exchange, asset)
                if persisted is not None and live is not None and abs(persisted - live) > 0.01:
                    logger.warning(
                        f"  DISCREPANCY: {exchange}/{asset} persisted={persisted:.4f} "
                        f"live={live:.4f} (diff={live - persisted:.4f})"
                    )
        logger.info("Startup reconciliation complete.")

    # ── Pre-trade inventory gate (FR-03) ──────────────────────────────────

    def _check_inventory_gate(
        self, direction: str, asset: str,
    ) -> Tuple[bool, str, float, float]:
        """Verify both legs have sufficient inventory within capital allocation.

        Returns (passed, failure_reason, buy_amount_usdt, sell_qty).
        """
        symbol = f"{asset}USDT"
        if direction == "binance_to_bitget":
            buy_exchange, sell_exchange = "binance", "bitget"
        else:
            buy_exchange, sell_exchange = "bitget", "binance"

        allocation_buy = get_current_capital_allocation(buy_exchange)
        allocation_sell = get_current_capital_allocation(sell_exchange)
        if allocation_buy <= 0 or allocation_sell <= 0:
            return False, f"zero_allocation ({buy_exchange}={allocation_buy}, {sell_exchange}={allocation_sell})", 0, 0

        usdt_buy = get_latest_inventory(buy_exchange, "USDT") or 0.0
        base = symbol.replace("USDT", "")
        asset_sell = get_latest_inventory(sell_exchange, base) or 0.0
        if asset_sell == 0.0:
            # Try the full symbol name as stored
            rows = None
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT free_balance FROM arbitrage_inventory WHERE exchange=? AND asset=? ORDER BY timestamp DESC LIMIT 1",
                    (sell_exchange, base))
                row = cur.fetchone()
                if row:
                    asset_sell = float(row[0])

        available_usdt = min(allocation_buy, usdt_buy)
        min_trade_usdt = 10.0
        if available_usdt < min_trade_usdt:
            return False, f"insufficient_usdt_{buy_exchange} (avail={available_usdt:.2f}, min={min_trade_usdt})", 0, 0

        # Estimate sell qty from reference price
        try:
            if buy_exchange == "binance":
                resp = requests.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol}, timeout=10)
                resp.raise_for_status()
                ref_price = float(resp.json().get("price", 0))
            else:
                resp = requests.get(
                    "https://api.bitget.com/api/v2/spot/market/tickers",
                    params={"symbol": symbol}, timeout=10)
                resp.raise_for_status()
                tdata = resp.json().get("data", [])
                ref_price = float(tdata[0].get("lastPr", 0)) if tdata else 0
        except Exception:
            ref_price = 0

        if ref_price <= 0:
            return False, f"price_fetch_failed ({symbol})", 0, 0

        buy_amount = available_usdt
        sell_qty = available_usdt / ref_price

        if asset_sell < sell_qty * 0.95:
            return False, f"insufficient_asset_{sell_exchange} (have={asset_sell:.4f}, need={sell_qty:.4f})", 0, 0

        return True, "", buy_amount, sell_qty

    # ── Rotation policy (FR-08) ──────────────────────────────────────────

    def _evaluate_rotation(
        self, current_asset: str, observation_result: dict,
    ) -> Tuple[bool, Optional[str], float]:
        """Evaluate whether to rotate to a different *tradable* asset.

        Returns (should_rotate, new_asset, estimated_rotation_cost).
        Only considers tradable_candidates (Tier 1), not observe-only.
        """
        candidates = observation_result.get("tradable_candidates", {})
        current_score = candidates.get(current_asset, {}).get("score", 0.0) if current_asset else 0.0

        best_candidate = None
        best_score = 0.0
        for sym, data in candidates.items():
            if sym != current_asset and data.get("score", 0) > best_score:
                best_score = data["score"]
                best_candidate = sym

        if not best_candidate:
            return False, None, 0.0

        score_margin = best_score - current_score
        est_rotation_cost = 0.5  # percentage points (fees + spread crossing)
        should_rotate = score_margin >= ROTATION_SCORE_MARGIN

        insert_rotation_decision(
            current_asset=current_asset or "none",
            candidate_asset=best_candidate,
            current_score=current_score,
            candidate_score=best_score,
            estimated_rotation_cost=est_rotation_cost,
            score_margin=score_margin,
            config_margin=ROTATION_SCORE_MARGIN,
            decision="executed" if should_rotate else "declined",
            reason=f"margin={score_margin:.2f} vs config={ROTATION_SCORE_MARGIN:.2f}"
            if not should_rotate else f"margin={score_margin:.2f} >= config={ROTATION_SCORE_MARGIN:.2f}",
        )

        if should_rotate:
            logger.info(
                f"Rotation: {current_asset} → {best_candidate} "
                f"(score {current_score:.2f} → {best_score:.2f}, margin {score_margin:.2f})")
            _send_notification(
                f"🔄 Arbitrage Rotation\n"
                f"Old: {current_asset} (score: {current_score:.2f})\n"
                f"New: {best_candidate} (score: {best_score:.2f})\n"
                f"Est. cost: ~{est_rotation_cost:.1f}% pts")
        else:
            logger.info(
                f"Rotation declined: {current_asset}→{best_candidate} "
                f"margin={score_margin:.2f} < config={ROTATION_SCORE_MARGIN:.2f}")

        return should_rotate, best_candidate, est_rotation_cost

    def _execute_rotation(self, old_asset: str, new_asset: str):
        """Execute rotation: sell old asset on both exchanges, buy new asset on both."""
        logger.info(f"Executing rotation: {old_asset} → {new_asset}")
        old_symbol = f"{old_asset}USDT"
        new_symbol = f"{new_asset}USDT"

        for exchange in ["binance", "bitget"]:
            try:
                bal = get_latest_inventory(exchange, old_asset) or 0.0
                if bal > 0:
                    if exchange == "binance":
                        from trade.trading_executor import binance_market_sell
                        binance_market_sell(old_symbol, bal)
                    else:
                        from trade.trading_executor import bitget_market_sell
                        bitget_market_sell(old_symbol, bal)
                    logger.info(f"  Sold {bal:.4f} {old_asset} on {exchange}")
                    time.sleep(1)
                    refresh_inventory(exchange, "USDT")
            except Exception as e:
                logger.error(f"  Rotation sell failed on {exchange}: {e}")

        allocation_binance = get_current_capital_allocation("binance")
        allocation_bitget = get_current_capital_allocation("bitget")
        usdt_binance = min(allocation_binance, get_latest_inventory("binance", "USDT") or 0.0)
        usdt_bitget = min(allocation_bitget, get_latest_inventory("bitget", "USDT") or 0.0)

        for exchange, usdt_amount in [("binance", usdt_binance), ("bitget", usdt_bitget)]:
            if usdt_amount <= 5:
                continue
            try:
                if exchange == "binance":
                    from trade.trading_executor import binance_market_buy
                    binance_market_buy(new_symbol, usdt_amount)
                else:
                    from trade.trading_executor import bitget_market_buy
                    bitget_market_buy(new_symbol, usdt_amount)
                logger.info(f"  Bought ~${usdt_amount:.2f} {new_asset} on {exchange}")
                time.sleep(1)
                refresh_inventory(exchange, new_asset)
            except Exception as e:
                logger.error(f"  Rotation buy failed on {exchange}: {e}")

        self._set_session_asset(new_asset)
        logger.info(f"Rotation complete. Session asset: {new_asset}")

    # ── Direction preference & inventory imbalance (FR-09) ───────────────

    def _check_inventory_imbalance(self) -> Tuple[bool, str]:
        """Check if inventory is critically imbalanced. Returns (is_imbalanced, reason)."""
        binance_usdt = get_latest_inventory("binance", "USDT") or 0.0
        bitget_usdt = get_latest_inventory("bitget", "USDT") or 0.0
        if min(binance_usdt, bitget_usdt) <= 0:
            return False, ""
        ratio = max(binance_usdt, bitget_usdt) / min(binance_usdt, bitget_usdt)
        if ratio > INVENTORY_IMBALANCE_RATIO:
            return True, f"usdt_ratio={ratio:.2f} (bn=${binance_usdt:.2f}, bg=${bitget_usdt:.2f})"
        return False, ""

    # ── Simulation mode (FR-11) ──────────────────────────────────────────

    def _simulate_simultaneous_trade(
        self, asset: str, direction: str, capital: float,
    ) -> Optional[Dict]:
        """Simulate simultaneous leg execution using live top-of-book bid/ask with fees."""
        symbol = f"{asset}USDT"
        binance_books = get_binance_book_tickers_bulk()
        bitget_books = get_bitget_book_tickers_bulk()
        bn = binance_books.get(symbol, {})
        bg = bitget_books.get(symbol, {})
        if not bn or not bg:
            logger.warning("Simulation: no book data available")
            return None

        spread_data = executable_spread(bn, bg)
        trading_fee = 0.001
        if direction == "binance_to_bitget":
            buy_price = spread_data.get("binance_ask") or 0
            sell_price = spread_data.get("bitget_bid") or 0
            buy_exchange, sell_exchange = "binance", "bitget"
        else:
            buy_price = spread_data.get("bitget_ask") or 0
            sell_price = spread_data.get("binance_bid") or 0
            buy_exchange, sell_exchange = "bitget", "binance"

        if buy_price <= 0 or sell_price <= 0:
            logger.warning("Simulation: invalid book prices")
            return None

        buy_qty = capital / buy_price
        buy_fee = capital * trading_fee
        sell_gross = buy_qty * sell_price
        sell_fee = sell_gross * trading_fee
        net_gain = sell_gross - capital - buy_fee - sell_fee
        spread_at_fill = ((sell_price - buy_price) / buy_price) * 100

        logger.info(f"  [SIM] Buy:  {buy_qty:.4f} @ ${buy_price:.8f} on {buy_exchange}")
        logger.info(f"  [SIM] Sell: {buy_qty:.4f} @ ${sell_price:.8f} on {sell_exchange}")
        logger.info(f"  [SIM] Net gain: ${net_gain:.4f}")

        return {
            "symbol": symbol, "direction": direction,
            "buy_leg": {"order_id": "sim", "side": "BUY", "exchange": buy_exchange,
                        "filled_qty": buy_qty, "avg_fill_price": buy_price, "fee": buy_fee, "status": "FILLED"},
            "sell_leg": {"order_id": "sim", "side": "SELL", "exchange": sell_exchange,
                         "filled_qty": buy_qty, "avg_fill_price": sell_price, "fee": sell_fee, "status": "FILLED"},
            "net_gain": net_gain, "spread_at_fill": spread_at_fill,
            "buy_exchange": buy_exchange, "sell_exchange": sell_exchange,
            "trade_amount_usdt": capital, "is_simulation": True,
        }

    # ── Cycle execution ──────────────────────────────────────────────────

    def execute_cycle(
        self, cycle_num: int, direction: str, asset: str,
    ) -> Optional[Dict]:
        """Execute one complete arbitrage cycle (simultaneous two-leg model)."""
        self._in_flight = True
        self.last_failure_reason = ""
        symbol = f"{asset}USDT"
        break_even = calculate_break_even_threshold()

        logger.info(f"\n{'='*70}")
        logger.info(f"Cycle {cycle_num}: {direction} — {asset}")
        logger.info(f"Break-even: {break_even:.4f}%")
        logger.info(f"{'='*70}")

        # STEP 1: DETECT — fetch executable prices
        binance_books = get_binance_book_tickers_bulk()
        bitget_books = get_bitget_book_tickers_bulk()
        bn = binance_books.get(symbol, {})
        bg = bitget_books.get(symbol, {})

        if not bn or not bg:
            self.last_failure_reason = f"book_fetch_failed (asset={asset})"
            logger.warning(f"  ✗ Failed to fetch book tickers for {asset}")
            self._log_cycle_step(cycle_num, 1, "DETECT", f"Book fetch failed for {asset}", direction)
            self._in_flight = False
            return None

        spread_data = executable_spread(bn, bg)
        executable_spread_pct = executable_spread_for_direction(bn, bg, direction)

        logger.info(f"  Binance bid/ask: {spread_data['binance_bid']}/{spread_data['binance_ask']}")
        logger.info(f"  Bitget  bid/ask: {spread_data['bitget_bid']}/{spread_data['bitget_ask']}")
        logger.info(f"  Executable spread ({direction}): {executable_spread_pct:.4f}%")

        if executable_spread_pct is None or executable_spread_pct < break_even:
            self.last_failure_reason = (
                f"spread_below_be (asset={asset}, spread={executable_spread_pct:.4f}%, be={break_even:.4f}%)")
            logger.warning(f"  ✗ Spread {executable_spread_pct:.4f}% < break-even {break_even:.4f}%")
            self._log_cycle_step(cycle_num, 1, "DETECT",
                f"Spread {executable_spread_pct:.4f}% < BE {break_even:.4f}% for {asset}", direction)
            self._in_flight = False
            return None

        self._log_cycle_step(cycle_num, 1, "DETECT",
            f"Executable spread {executable_spread_pct:.4f}% for {asset} ({direction})", direction)

        # STEP 2: GATE — pre-trade inventory check
        gate_ok, gate_reason, buy_amount, sell_qty = self._check_inventory_gate(direction, asset)
        if not gate_ok:
            self.last_failure_reason = f"inventory_gate ({gate_reason})"
            logger.warning(f"  ✗ Inventory gate: {gate_reason}")
            self._log_cycle_step(cycle_num, 2, "GATE", f"FAILED: {gate_reason}", direction)
            self._same_direction_skips += 1
            if self._same_direction_skips >= 3:
                imbalance, reason = self._check_inventory_imbalance()
                if imbalance:
                    logger.warning(f"  ⚠ REBALANCE NEEDED: {reason}")
                    _send_notification(f"⚠️ Arbitrage Rebalance Needed\n{reason}")
                    self._same_direction_skips = 0
            self._in_flight = False
            return None

        self._same_direction_skips = 0
        self._log_cycle_step(cycle_num, 2, "GATE",
            f"PASSED: buy=${buy_amount:.2f}, sell={sell_qty:.4f} {asset}", direction)

        # STEP 3: EXECUTE LEGS
        self._log_cycle_step(cycle_num, 3, "EXECUTE_LEGS",
            f"Dispatching concurrent buy/sell for {asset}", direction)

        if TRADING_EXECUTOR_AVAILABLE:
            trade_result = execute_simultaneous_legs(
                base_asset=asset, direction=direction,
                trade_amount_usdt=buy_amount, cycle_num=cycle_num)
        else:
            trade_result = self._simulate_simultaneous_trade(asset, direction, buy_amount)

        if not trade_result:
            self.last_failure_reason = "execution_failed"
            logger.error(f"  ✗ Trade execution failed")
            self._log_cycle_step(cycle_num, 3, "EXECUTE_LEGS", f"FAILED for {asset}", direction)
            self._in_flight = False
            self._refresh_all_inventory(asset)
            return None

        # STEP 4: RECONCILE
        self._log_cycle_step(cycle_num, 4, "RECONCILE",
            f"Trade complete, net gain=${trade_result['net_gain']:.4f}", direction)
        self._refresh_all_inventory(asset)

        # Update capital allocation (compounding)
        net_gain = trade_result["net_gain"]
        buy_exchange = trade_result.get("buy_exchange", "")
        sell_exchange = trade_result.get("sell_exchange", "")
        for exchange in [buy_exchange, sell_exchange]:
            if not exchange:
                continue
            current_alloc = get_current_capital_allocation(exchange)
            new_alloc = current_alloc + (net_gain / 2)
            record_capital_change(exchange, new_alloc, net_gain / 2,
                f"cycle_{cycle_num}_{'gain' if net_gain >= 0 else 'loss'}")

        self._in_flight = False
        self._last_opportunity_at = time.time()
        logger.info(f"\n  ✓ CYCLE {cycle_num} COMPLETE — Net gain: ${net_gain:.4f}")
        return trade_result

    # ── Inventory refresh ─────────────────────────────────────────────────

    def _refresh_all_inventory(self, working_asset: str):
        """Refresh inventory for USDT and working asset on both exchanges."""
        for exchange in ["binance", "bitget"]:
            refresh_inventory(exchange, "USDT")
            refresh_inventory(exchange, working_asset)

    # ── Persistence ──────────────────────────────────────────────────────

    def _save_cycle(
        self, cycle_num: int, asset: str, direction: str,
        trade_result: Dict, spread_at_detect: float,
    ):
        """Save cycle results to DB with new two-leg model."""
        buy_leg = trade_result.get("buy_leg", {})
        sell_leg = trade_result.get("sell_leg", {})
        net_gain = trade_result.get("net_gain", 0)
        is_sim = 1 if trade_result.get("is_simulation") else 0
        buy_exchange = trade_result.get("buy_exchange", "")
        sell_exchange = trade_result.get("sell_exchange", "")

        capital_start = (get_current_capital_allocation(buy_exchange) +
                         get_current_capital_allocation(sell_exchange))
        capital_end = capital_start + net_gain
        gain_pct = (net_gain / capital_start) * 100 if capital_start > 0 else 0

        inventory_snapshot = str({
            "binance_usdt": get_latest_inventory("binance", "USDT"),
            "binance_asset": get_latest_inventory("binance", asset),
            "bitget_usdt": get_latest_inventory("bitget", "USDT"),
            "bitget_asset": get_latest_inventory("bitget", asset),
        })

        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO arbitrage_compounding
                (cycle_num, asset, direction, capital_start, capital_end, gain, gain_pct,
                 spread_pct, buy_price, sell_price, qty, buy_fee, sell_fee,
                 buy_exchange, sell_exchange,
                 buy_leg_order_id, sell_leg_order_id,
                 buy_leg_fill_price, sell_leg_fill_price,
                 buy_leg_fill_qty, sell_leg_fill_qty,
                 buy_leg_fee, sell_leg_fee,
                 spread_at_detect, spread_at_fill,
                 is_simulation, inventory_snapshot, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
            """, (
                cycle_num, asset, direction, capital_start, capital_end, net_gain, gain_pct,
                trade_result.get("spread_at_fill", 0),
                buy_leg.get("avg_fill_price", 0), sell_leg.get("avg_fill_price", 0),
                buy_leg.get("filled_qty", 0), buy_leg.get("fee", 0), sell_leg.get("fee", 0),
                buy_exchange, sell_exchange,
                buy_leg.get("order_id", ""), sell_leg.get("order_id", ""),
                buy_leg.get("avg_fill_price", 0), sell_leg.get("avg_fill_price", 0),
                buy_leg.get("filled_qty", 0), sell_leg.get("filled_qty", 0),
                buy_leg.get("fee", 0), sell_leg.get("fee", 0),
                spread_at_detect, trade_result.get("spread_at_fill", 0),
                is_sim, inventory_snapshot,
            ))
            conn.commit()

    def _log_cycle_step(
        self, cycle_num: int, step_order: int, step_name: str,
        step_details: str, direction: str = "binance_to_bitget",
    ):
        """Log individual step in cycle execution (new step sequence)."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO arbitrage_cycle_steps
                (cycle_num, step_order, step_name, step_details, direction, status)
                VALUES (?, ?, ?, ?, ?, 'completed')
            """, (cycle_num, step_order, step_name, step_details, direction))
            conn.commit()

    # ── Telegram notifications per trade (FR-12) ─────────────────────────

    def _notify_trade(self, cycle_num: int, asset: str, direction: str, trade_result: Dict):
        """Send Telegram notification for a completed trade (non-blocking)."""
        buy_leg = trade_result.get("buy_leg", {})
        sell_leg = trade_result.get("sell_leg", {})
        net_gain = trade_result.get("net_gain", 0)
        total_capital = (get_current_capital_allocation("binance") +
                         get_current_capital_allocation("bitget"))
        message = (
            f"✅ Arbitrage Trade #{cycle_num}\n"
            f"Asset: {asset}\nDirection: {direction}\n"
            f"Buy:  {buy_leg.get('exchange','?'):>7} {buy_leg.get('filled_qty',0):.4f} "
            f"@ ${buy_leg.get('avg_fill_price',0):.6f} (fee ${buy_leg.get('fee',0):.4f})\n"
            f"Sell: {sell_leg.get('exchange','?'):>7} {sell_leg.get('filled_qty',0):.4f} "
            f"@ ${sell_leg.get('avg_fill_price',0):.6f} (fee ${sell_leg.get('fee',0):.4f})\n"
            f"Net gain: ${net_gain:.4f}\nCapital: ${total_capital:.2f}")
        _send_notification(message)

    def _notify_failure(self, cycle_num: int, reason: str):
        """Send Telegram notification for a failed trade."""
        _send_notification(f"❌ Arbitrage Trade #{cycle_num} FAILED\nReason: {reason}")

    # ── Statistics ───────────────────────────────────────────────────────

    def print_statistics(self):
        """Print compounding statistics from DB."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM arbitrage_compounding")
            total_cycles = cur.fetchone()[0]
            if total_cycles == 0:
                logger.info("  No cycles yet")
                return

            cur.execute("""
                SELECT cycle_num, asset, capital_end, gain, timestamp
                FROM arbitrage_compounding ORDER BY cycle_num DESC LIMIT 1
            """)
            cycle, asset, capital, last_gain, last_time = cur.fetchone()
            cur.execute("SELECT SUM(gain) FROM arbitrage_compounding")
            total_gain = cur.fetchone()[0] or 0
            avg_gain = total_gain / total_cycles if total_cycles > 0 else 0
            initial_capital = float(TRADE_AMOUNT_USDT)
            roi = ((capital - initial_capital) / initial_capital) * 100 if initial_capital > 0 else 0

            cur.execute("""
                SELECT COUNT(*) FROM arbitrage_compounding
                WHERE timestamp > datetime('now', '-1 day')
            """)
            cycles_today = cur.fetchone()[0]

            cur.execute("""
                SELECT asset, COUNT(*) as count, SUM(gain) as total_gain, AVG(spread_pct) as avg_spread
                FROM arbitrage_compounding GROUP BY asset ORDER BY total_gain DESC LIMIT 5
            """)
            top_assets = cur.fetchall()

            logger.info(f"\n{'='*70}")
            logger.info(f"Compounding Arbitrage Statistics")
            logger.info(f"{'='*70}")
            logger.info(f"  Total cycles: {total_cycles}")
            logger.info(f"  Today cycles: {cycles_today}")
            logger.info(f"  Current capital: ${capital:.2f}")
            logger.info(f"  Total gain: ${total_gain:.2f}")
            logger.info(f"  ROI: {roi:.2f}%")
            logger.info(f"  Avg gain/cycle: ${avg_gain:.4f}")
            logger.info(f"  Last trade: {last_time} ({asset})")
            if top_assets:
                logger.info(f"\n  Top Assets:")
                for a, cnt, gain, spread in top_assets:
                    logger.info(f"    {a}: {cnt} cycles, ${gain:.2f} total, {spread:.3f}% avg spread")

    # ── Main loop ────────────────────────────────────────────────────────

    def run(self, max_cycles: Optional[int] = None, report_every: int = 10):
        """Run arbitrage cycles continuously (refactored model)."""
        cycle_num, direction = self._load_state()
        self.session_asset = self._get_session_asset()
        cycles_run = 0

        logger.info(f"\n{'='*70}")
        logger.info(f"Starting Inventory-Based Simultaneous Arbitrage")
        logger.info(f"Initial cycle: {cycle_num}")
        logger.info(f"Initial direction: {direction}")
        logger.info(f"Session asset: {self.session_asset or 'auto-select'}")
        logger.info(f"Break-even threshold: {calculate_break_even_threshold():.4f}%")
        logger.info(f"{'='*70}")

        if self.session_asset:
            self._startup_reconciliation()

        last_observation_time = 0.0
        observation_cache: dict = {}

        try:
            while max_cycles is None or cycles_run < max_cycles:
                # ── Check run state (FR-12) ──────────────────────────────
                run_state = get_arbitrage_run_state()
                if run_state == "stopped":
                    if not self._stop_requested:
                        logger.info("⏸  Arbitrage stopped (persisted state). Waiting...")
                        self._stop_requested = True
                    time.sleep(5)
                    continue
                else:
                    if self._stop_requested:
                        logger.info("▶  Arbitrage resumed")
                        self._stop_requested = False

                if self._in_flight:
                    time.sleep(1)
                    continue

                # ── Observation cycle ────────────────────────────────────
                now = time.time()
                if now - last_observation_time >= SAMPLING_INTERVAL_SEC:
                    logger.info(f"\n--- Observation at {datetime.utcnow().isoformat()} ---")
                    observation_cache = observe_and_score(
                        session_asset=self.session_asset, trade_direction=direction)
                    last_observation_time = now
                    # Log observe-only Tier 2 highlights
                    obs_cands = observation_cache.get("observe_candidates", {})
                    if obs_cands:
                        sorted_obs = sorted(obs_cands.items(), key=lambda x: x[1]["score"], reverse=True)
                        best_obs = sorted_obs[0]
                        logger.info(f"  [Observe-only] Top Tier-2: {best_obs[0]} score={best_obs[1]['score']:.2f} "
                                    f"({len(obs_cands)} tracked, not traded)")

                # ── Asset selection / rotation ────────────────────────────
                if self.session_asset:
                    time_since_opportunity = now - self._last_opportunity_at
                    should_check_rotation = (
                        now - self._last_rotation_check >= ROTATION_CADENCE_SEC
                        or time_since_opportunity >= DRY_SPELL_DURATION_SEC)

                    if should_check_rotation and observation_cache.get("candidates"):
                        self._last_rotation_check = now
                        rotate, new_asset, _ = self._evaluate_rotation(
                            self.session_asset, observation_cache)
                        if rotate and new_asset:
                            self._execute_rotation(self.session_asset, new_asset)
                            self._refresh_all_inventory(new_asset)
                            continue

                    best_asset = self.session_asset
                else:
                    best_asset = observation_cache.get("best_asset")
                    if best_asset:
                        self._set_session_asset(best_asset)
                        logger.info(f"Initial session asset selected: {best_asset}")
                        self._execute_rotation("USDT", best_asset)
                        continue
                    else:
                        logger.info("  No suitable asset found, waiting...")
                        time.sleep(CYCLE_SLEEP_SEC)
                        continue

                if not best_asset:
                    logger.info("  No asset available, waiting...")
                    time.sleep(CYCLE_SLEEP_SEC)
                    continue

                # ── Check inventory imbalance ────────────────────────────
                imbalance, imbalance_reason = self._check_inventory_imbalance()
                if imbalance and self._imbalance_flagged_at is None:
                    self._imbalance_flagged_at = now
                    logger.warning(f"  ⚠ Inventory imbalance: {imbalance_reason}")
                    _send_notification(f"⚠️ Arbitrage Rebalance Needed\n{imbalance_reason}")
                    # Try opposite direction
                    alt_direction = ("bitget_to_binance" if direction == "binance_to_bitget"
                                     else "binance_to_bitget")
                    logger.warning(f"  Switching direction: {direction} → {alt_direction}")
                    direction = alt_direction
                    continue
                if not imbalance:
                    self._imbalance_flagged_at = None

                # ── Execute cycle ────────────────────────────────────────
                logger.info(f"  ➤ Trading: {best_asset} ({direction})")
                result = self.execute_cycle(cycle_num, direction, best_asset)

                if result:
                    binance_books = get_binance_book_tickers_bulk()
                    bitget_books = get_bitget_book_tickers_bulk()
                    symbol = f"{best_asset}USDT"
                    bn = binance_books.get(symbol, {})
                    bg = bitget_books.get(symbol, {})
                    spread_at_detect = executable_spread_for_direction(bn, bg, direction) or 0.0

                    self._save_cycle(cycle_num, best_asset, direction, result, spread_at_detect)
                    self._notify_trade(cycle_num, best_asset, direction, result)

                    direction = ("bitget_to_binance" if direction == "binance_to_bitget"
                                 else "binance_to_bitget")
                    cycles_run += 1
                    cycle_num += 1
                    if cycles_run % report_every == 0:
                        self.print_statistics()
                else:
                    reason = self.last_failure_reason or "unknown_failure"
                    logger.warning(f"  ⚠ Cycle failed: {reason}")
                    self._notify_failure(cycle_num, reason)
                    time.sleep(CYCLE_SLEEP_SEC)
                    continue

                time.sleep(CYCLE_SLEEP_SEC)

        except KeyboardInterrupt:
            logger.info("\n\n✓ Arbitrage stopped by user")
            self.print_statistics()


def main():
    arb = CompoundingArbitrage()
    arb.run()


if __name__ == "__main__":
    main()
