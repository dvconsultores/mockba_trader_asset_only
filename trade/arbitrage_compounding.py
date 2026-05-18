import requests
import time
import sqlite3
import importlib.util
import logging
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
from db.db_ops import initialize_database_tables

# Import real trading executors
try:
    from trading_executor import buy_binance_sell_bitget, buy_bitget_sell_binance
    TRADING_EXECUTOR_AVAILABLE = True
except ImportError:
    logger.info("⚠️  trading_executor not available - running in SIMULATION mode")
    TRADING_EXECUTOR_AVAILABLE = False

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
BITGET_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trading.db"

# Configuration (read from .env, with defaults)
TRADE_AMOUNT = 100  # USDT per trade
MIN_SPREAD_PCT = float(os.getenv("SPREAD_MIN_PCT", "0.5"))  # Minimum spread to execute (from .env)
MIN_PROFIT_USD = float(os.getenv("MIN_PROFIT_USD", "0.16"))  # Target minimum gain per cycle
ESTIMATED_FIXED_FEES_USD = float(os.getenv("ESTIMATED_FIXED_FEES_USD", "0.45"))  # Withdraw/network fees buffer
SPREAD_SAFETY_USD = float(os.getenv("SPREAD_SAFETY_USD", "0.10"))  # Extra safety against slippage
TRADING_FEE_PCT = 0.1  # 0.1% on each exchange
HAIRCUT_PCT = 0.7  # Conservative sell price estimate
TRANSFER_TIMEOUT = 300  # 5 minutes timeout for transfer
CYCLE_SLEEP = 30  # Seconds to wait between cycles (3-5 min transfer + 30s check)


class CompoundingArbitrage:
    """
    Executes arbitrage cycles continuously, alternating directions.
    Each cycle: buy on one exchange, transfer (3-5 min), sell on other.
    Dynamically picks best asset from spread_llm_analyzer.
    Tracks capital growth and alternates direction after each cycle.
    """
    
    def __init__(self):
        self.pair = None  # Will be dynamically set
        self.last_failure_reason = ""
        self._ensure_db()
        self._load_spread_analyzer()
    
    def _load_spread_analyzer(self):
        """Load spread_llm_analyzer module."""
        try:
            module_path = Path(__file__).with_name("spread-llm-analizer.py")
            spec = importlib.util.spec_from_file_location("spread_llm_analizer", str(module_path))
            if not spec or not spec.loader:
                logger.info("✗ Failed to load spread_llm_analizer")
                self.spread_analyzer = None
                return
            
            self.spread_analyzer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.spread_analyzer)
            logger.info("✓ Spread analyzer loaded")
        except Exception as e:
            logger.info(f"✗ Error loading spread analyzer: {e}")
            self.spread_analyzer = None
    
    def _calculate_dynamic_min_spread_pct(self, capital: float) -> float:
        """Calculate minimum spread required to target net minimum gain."""
        if capital <= 0:
            return MIN_SPREAD_PCT

        # Trading fees are charged on both buy and sell sides.
        estimated_trading_fees = capital * (TRADING_FEE_PCT / 100) * 2
        required_usd = MIN_PROFIT_USD + ESTIMATED_FIXED_FEES_USD + SPREAD_SAFETY_USD + estimated_trading_fees
        dynamic_pct = (required_usd / capital) * 100
        return max(MIN_SPREAD_PCT, dynamic_pct)

    def _get_best_asset(self, direction: str, capital: float) -> Optional[str]:
        """Get best asset from spread analyzer."""
        if not self.spread_analyzer:
            logger.info("  ✗ Spread analyzer not available, using fallback assets")
            return None
        
        try:
            dynamic_min_spread_pct = self._calculate_dynamic_min_spread_pct(capital)
            logger.info(
                f"  Dynamic spread threshold: {dynamic_min_spread_pct:.3f}% "
                f"(floor {MIN_SPREAD_PCT:.3f}%, target ${MIN_PROFIT_USD:.2f})"
            )
            best_asset = self.spread_analyzer.get_best_spread_asset(
                sample_size=100,
                min_spread_pct=dynamic_min_spread_pct,
                trade_direction=direction
            )
            if not best_asset:
                logger.info("  ⚠ No asset found from analyzer (no wallet or insufficient spread)")
                return None
            return best_asset
        except Exception as e:
            logger.info(f"  ✗ Error getting best asset: {e}")
            return None
    
    def _get_fallback_assets(self) -> list:
        """Fallback assets if analyzer fails."""
        return ["NEARUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"]
    
    def _ensure_db(self):
        """Initialize all database tables (including autonomy tables)."""
        # Call central initialization function to create ALL tables
        initialize_database_tables()
    
    def _load_state(self) -> Tuple[int, float, str]:
        """Load last cycle number, capital, and current direction from DB."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT cycle_num, capital_end, direction 
                FROM arbitrage_compounding 
                ORDER BY cycle_num DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                cycle_num, capital, direction = row
                next_direction = "bitget_to_binance" if direction == "binance_to_bitget" else "binance_to_bitget"
                return cycle_num + 1, capital, next_direction
            else:
                # First cycle
                return 1, float(TRADE_AMOUNT), "binance_to_bitget"
    
    def _save_cycle(self, cycle_num: int, asset: str, direction: str, capital_start: float, 
                    capital_end: float, gain: float, spread_pct: float, 
                    buy_price: float, sell_price: float, qty: float,
                    buy_fee: float, sell_fee: float):
        """Save cycle results to DB."""
        gain_pct = (gain / capital_start) * 100 if capital_start > 0 else 0
        
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO arbitrage_compounding 
                (cycle_num, asset, direction, capital_start, capital_end, gain, gain_pct, 
                 spread_pct, buy_price, sell_price, qty, buy_fee, sell_fee, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
            """, (cycle_num, asset, direction, capital_start, capital_end, gain, gain_pct,
                  spread_pct, buy_price, sell_price, qty, buy_fee, sell_fee))
            conn.commit()
    
    def _log_cycle_step(self, cycle_num: int, step_order: int, step_name: str, step_details: str, direction: str = "binance_to_bitget"):
        """Log individual step in cycle execution."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO arbitrage_cycle_steps 
                (cycle_num, step_order, step_name, step_details, direction, status)
                VALUES (?, ?, ?, ?, ?, 'completed')
            """, (cycle_num, step_order, step_name, step_details, direction))
            conn.commit()
    
    def _safe_get_json(self, url: str, params: dict) -> Optional[dict]:
        """Safely fetch JSON from API."""
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.info(f"  ✗ API error: {e}")
            return None
    
    def _get_binance_price(self, symbol: str) -> Optional[float]:
        """Get Binance price."""
        data = self._safe_get_json(BINANCE_URL, {"symbol": symbol})
        if not data:
            return None
        price = data.get("price")
        return float(price) if price is not None else None
    
    def _get_bitget_price(self, symbol: str) -> Optional[float]:
        """Get Bitget price."""
        data = self._safe_get_json(BITGET_URL, {"symbol": symbol})
        if not data:
            return None
        rows = data.get("data")
        if not isinstance(rows, list) or not rows:
            return None
        price = rows[0].get("lastPr")
        return float(price) if price is not None else None
    
    def _get_prices(self, pair: str) -> Tuple[Optional[float], Optional[float]]:
        """Get both exchange prices."""
        symbol = pair
        bin_price = self._get_binance_price(symbol)
        bg_price = self._get_bitget_price(symbol)
        return bin_price, bg_price
    
    def _simulate_sale(self, qty: float, sell_price: float) -> float:
        """Simulate selling with haircut."""
        effective_price = sell_price * (1 - HAIRCUT_PCT / 100)
        return qty * effective_price
    
    def execute_cycle(self, cycle_num: int, direction: str, capital: float, pair: str) -> Optional[Dict]:
        """
        Execute one complete arbitrage cycle.
        
        binance_to_bitget: buy Binance → transfer → sell Bitget
        bitget_to_binance: buy Bitget → transfer → sell Binance
        
        Tracks: BUY → TRANSFER_OUT → RECEIVED → SELL → DIRECTION_CHANGE
        """
        self.last_failure_reason = ""
        logger.info(f"\n{'='*70}")
        logger.info(f"Cycle {cycle_num} starting (direction: {direction}, asset: {pair})")
        logger.info(f"Capital: ${capital:.2f}")
        
        # Fetch prices
        bin_price, bg_price = self._get_prices(pair)
        
        if bin_price is None or bg_price is None:
            logger.info("  ✗ Failed to fetch prices")
            self.last_failure_reason = f"price_fetch_failed (asset={pair})"
            return None
        
        logger.info(f"  Binance: ${bin_price:.8f}")
        logger.info(f"  Bitget:  ${bg_price:.8f}")
        
        # Calculate spread
        if direction == "binance_to_bitget":
            spread_pct = ((bg_price - bin_price) / bin_price) * 100
            buy_price, sell_price = bin_price, bg_price
            buy_exchange, sell_exchange = "Binance", "Bitget"
        else:
            spread_pct = ((bin_price - bg_price) / bg_price) * 100
            buy_price, sell_price = bg_price, bin_price
            buy_exchange, sell_exchange = "Bitget", "Binance"
        
        logger.info(f"  Spread: {spread_pct:.2f}%")
        
        if spread_pct < MIN_SPREAD_PCT:
            logger.info(f"  ✗ Spread {spread_pct:.2f}% < minimum {MIN_SPREAD_PCT}%")
            self.last_failure_reason = (
                f"spread_below_min (asset={pair}, spread={spread_pct:.2f}%, min={MIN_SPREAD_PCT:.2f}%)"
            )
            return None
        
        # PRE-CALCULATE gain to ensure minimum $0.16 profit
        buy_fee = capital * (TRADING_FEE_PCT / 100)
        total_buy_cost = capital + buy_fee
        qty = capital / buy_price
        
        gross_revenue = self._simulate_sale(qty, sell_price)
        sell_fee = gross_revenue * (TRADING_FEE_PCT / 100)
        net_revenue = gross_revenue - sell_fee
        
        expected_gain = net_revenue - total_buy_cost
        
        logger.info(f"  Expected gain: ${expected_gain:.4f}")
        
        if expected_gain < 0.16:
            logger.info(f"  ✗ Expected gain ${expected_gain:.4f} < minimum $0.16")
            self.last_failure_reason = (
                f"expected_gain_below_min (asset={pair}, expected=${expected_gain:.4f}, min=$0.16)"
            )
            return None
        
        # STEP 1: BUY
        logger.info(f"\n  [1/5] BUY on {buy_exchange}")
        logger.info(f"        Buying {qty:.4f} {pair} @ ${buy_price:.8f} = ${capital:.2f}")
        logger.info(f"        Fee: ${buy_fee:.4f}")
        self._log_cycle_step(cycle_num, 1, "BUY", 
            f"Bought {qty:.4f} {pair} on {buy_exchange} @ ${buy_price:.8f} for ${capital:.2f} (fee ${buy_fee:.4f})", direction)
        
        # Extract base asset (e.g., "SYS" from "SYSUSDT")
        base_asset = pair.replace("USDT", "").replace("USDC", "")
        
        # EXECUTE REAL TRADES if executor available
        if TRADING_EXECUTOR_AVAILABLE:
            logger.info(f"\n  ⚙️  EXECUTING REAL TRADES on {base_asset}...")
            
            if direction == "binance_to_bitget":
                trade_result = buy_binance_sell_bitget(base_asset, bin_price, bg_price, capital)
            else:
                trade_result = buy_bitget_sell_binance(base_asset, bg_price, bin_price, capital)
            
            if not trade_result:
                logger.info(f"  ✗ Trade execution failed")
                self.last_failure_reason = (
                    f"trade_executor_failed (asset={base_asset}, direction={direction}, check trading_executor logs)"
                )
                self._log_cycle_step(
                    cycle_num,
                    2,
                    "EXECUTION_FAILED",
                    f"Trade execution failed for {base_asset}. Reason: {self.last_failure_reason}",
                    direction,
                )
                return None
            
            # Extract actual results
            actual_gain = trade_result.get("profit", expected_gain)
            actual_capital = capital + actual_gain
            
            logger.info(f"  ✓ Trade executed successfully")
            logger.info(f"    Actual gain: ${actual_gain:.4f}")
            logger.info(f"    Actual capital: ${actual_capital:.2f}")
            
            self._log_cycle_step(cycle_num, 2, "TRANSFER_OUT", 
                f"Transferred {qty:.4f} {pair} from {buy_exchange} to {sell_exchange}", direction)
            self._log_cycle_step(cycle_num, 3, "RECEIVED", 
                f"Received {qty:.4f} {pair} on {sell_exchange} (ready to sell)", direction)
            self._log_cycle_step(cycle_num, 4, "SELL", 
                f"Sold {qty:.4f} {pair} on {sell_exchange} for actual gain ${actual_gain:.4f}", direction)
            
            new_capital = actual_capital
            expected_gain = actual_gain
        else:
            # Simulation mode
            # STEP 2: TRANSFER_OUT
            logger.info(f"\n  [2/5] TRANSFER_OUT from {buy_exchange}")
            logger.info(f"        Transferring {qty:.4f} {pair} to {sell_exchange}")
            self._log_cycle_step(cycle_num, 2, "TRANSFER_OUT", 
                f"Transferred {qty:.4f} {pair} from {buy_exchange} to {sell_exchange}", direction)
            
            # STEP 3: RECEIVED
            logger.info(f"\n  [3/5] RECEIVED on {sell_exchange}")
            logger.info(f"        Received {qty:.4f} {pair} on {sell_exchange}")
            self._log_cycle_step(cycle_num, 3, "RECEIVED", 
                f"Received {qty:.4f} {pair} on {sell_exchange} (ready to sell)", direction)
            
            # STEP 4: SELL
            logger.info(f"\n  [4/5] SELL on {sell_exchange}")
            logger.info(f"        Selling {qty:.4f} {pair} @ ${sell_price:.8f}")
            logger.info(f"        Gross revenue: ${gross_revenue:.2f}")
            logger.info(f"        Fee: ${sell_fee:.4f}")
            logger.info(f"        Net revenue: ${net_revenue:.2f}")
            self._log_cycle_step(cycle_num, 4, "SELL", 
                f"Sold {qty:.4f} {pair} on {sell_exchange} @ ${sell_price:.8f} for ${gross_revenue:.2f} (fee ${sell_fee:.4f})", direction)
            
            new_capital = capital + expected_gain
            logger.info(f"  [SIMULATION MODE]")
        
        # STEP 5: DIRECTION_CHANGE
        new_direction = "bitget_to_binance" if direction == "binance_to_bitget" else "binance_to_bitget"
        logger.info(f"\n  [5/5] DIRECTION_CHANGE")
        logger.info(f"        Next direction: {new_direction}")
        self._log_cycle_step(cycle_num, 5, "DIRECTION_CHANGE", 
            f"Direction changed from {direction} to {new_direction}", direction)
        
        logger.info(f"\n  ✓ CYCLE COMPLETE")
        logger.info(f"    Gain: ${expected_gain:.4f} ({(expected_gain/capital)*100:.3f}%)")
        logger.info(f"    New capital: ${new_capital:.2f}")
        
        return {
            "direction": direction,
            "capital_start": capital,
            "capital_end": new_capital,
            "gain": expected_gain,
            "spread_pct": spread_pct,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "qty": qty,
            "buy_fee": buy_fee,
            "sell_fee": sell_fee,
        }
    
    def print_statistics(self):
        """Print compounding statistics from DB."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            
            # Total cycles
            cur.execute("SELECT COUNT(*) FROM arbitrage_compounding")
            total_cycles = cur.fetchone()[0]
            
            if total_cycles == 0:
                logger.info("  No cycles yet")
                return
            
            # Latest state
            cur.execute("""
                SELECT cycle_num, asset, capital_end, gain, timestamp 
                FROM arbitrage_compounding 
                ORDER BY cycle_num DESC LIMIT 1
            """)
            cycle, asset, capital, last_gain, last_time = cur.fetchone()
            
            # Total gain
            cur.execute("SELECT SUM(gain) FROM arbitrage_compounding")
            total_gain = cur.fetchone()[0] or 0
            
            # Average gain per cycle
            avg_gain = total_gain / total_cycles if total_cycles > 0 else 0
            
            # ROI
            initial_capital = float(TRADE_AMOUNT)
            roi = ((capital - initial_capital) / initial_capital) * 100
            
            # Cycles per day
            cur.execute("""
                SELECT COUNT(*) FROM arbitrage_compounding
                WHERE timestamp > datetime('now', '-1 day')
            """)
            cycles_today = cur.fetchone()[0]
            
            # Top assets
            cur.execute("""
                SELECT asset, COUNT(*) as count, SUM(gain) as total_gain, AVG(spread_pct) as avg_spread
                FROM arbitrage_compounding
                GROUP BY asset
                ORDER BY total_gain DESC
                LIMIT 5
            """)
            top_assets = cur.fetchall()
            
            logger.info(f"\n{'='*70}")
            logger.info(f"Compounding Arbitrage Statistics")
            logger.info(f"{'='*70}")
            logger.info(f"  Total cycles: {total_cycles}")
            logger.info(f"  Today cycles: {cycles_today}")
            logger.info(f"  Current capital: ${capital:.2f}")
            logger.info(f"  Initial capital: ${initial_capital:.2f}")
            logger.info(f"  Total gain: ${total_gain:.2f}")
            logger.info(f"  ROI: {roi:.2f}%")
            logger.info(f"  Avg gain/cycle: ${avg_gain:.4f}")
            logger.info(f"  Last trade: {last_time} ({asset})")
            
            # Top assets
            if top_assets:
                logger.info(f"\n  Top Assets:")
                for asset, count, gain, spread in top_assets:
                    logger.info(f"    {asset}: {count} cycles, ${gain:.2f} total, {spread:.3f}% avg spread")
            
            # Projection
            if avg_gain > 0 and cycles_today > 0:
                cycles_per_day = cycles_today
                projected_gain_day = avg_gain * cycles_per_day
                projected_capital_month = capital + (projected_gain_day * 30)
                logger.info(f"\n  Projected (if pattern continues):")
                logger.info(f"    Cycles/day: {cycles_per_day}")
                logger.info(f"    Gain/day: ${projected_gain_day:.2f}")
                logger.info(f"    Capital in 30 days: ${projected_capital_month:.2f}")
    
    def print_cycle_steps(self, cycle_num: int):
        """Print detailed steps for a specific cycle."""
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.cursor()
            
            cur.execute("""
                SELECT step_order, step_name, step_details, timestamp
                FROM arbitrage_cycle_steps
                WHERE cycle_num = ?
                ORDER BY step_order
            """, (cycle_num,))
            
            steps = cur.fetchall()
            
            if not steps:
                logger.info(f"  No steps found for cycle {cycle_num}")
                return
            
            logger.info(f"\n{'='*70}")
            logger.info(f"Cycle {cycle_num} - Detailed Steps")
            logger.info(f"{'='*70}")
            
            for step_order, step_name, step_details, timestamp in steps:
                logger.info(f"\n  [{step_order}/5] {step_name}")
                logger.info(f"      {step_details}")
                logger.info(f"      Time: {timestamp}")
    
    def run(self, max_cycles: Optional[int] = None, report_every: int = 10):
        """Run arbitrage cycles continuously."""
        cycle_num, capital, direction = self._load_state()
        cycles_run = 0
        
        logger.info(f"\n{'='*70}")
        logger.info(f"Starting Compounding Arbitrage (Dynamic Asset Selection)")
        logger.info(f"Initial cycle: {cycle_num}")
        logger.info(f"Initial capital: ${capital:.2f}")
        logger.info(f"Initial direction: {direction}")
        logger.info(f"{'='*70}")
        
        try:
            while max_cycles is None or cycles_run < max_cycles:
                # Get best asset for current direction
                best_asset = self._get_best_asset(direction, capital)
                
                if not best_asset:
                    # Try fallback assets if analyzer fails
                    fallback_assets = self._get_fallback_assets()
                    logger.info(f"  ➜ Trying fallback assets: {fallback_assets}")
                    best_asset = fallback_assets[0]  # Use first fallback
                    logger.info(f"  ➤ Using fallback: {best_asset}")
                else:
                    logger.info(f"  ➤ Selected asset: {best_asset}")
                
                pair = best_asset
                
                # Execute cycle
                result = self.execute_cycle(cycle_num, direction, capital, pair)
                
                if result:
                    # Save to DB
                    self._save_cycle(
                        cycle_num,
                        pair,
                        result["direction"],
                        result["capital_start"],
                        result["capital_end"],
                        result["gain"],
                        result["spread_pct"],
                        result["buy_price"],
                        result["sell_price"],
                        result["qty"],
                        result["buy_fee"],
                        result["sell_fee"]
                    )
                    
                    # Update for next cycle
                    capital = result["capital_end"]
                    direction = "bitget_to_binance" if direction == "binance_to_bitget" else "binance_to_bitget"
                    cycles_run += 1
                    cycle_num += 1
                    
                    # Print statistics every N cycles
                    if cycles_run % report_every == 0:
                        self.print_statistics()
                else:
                    reason = self.last_failure_reason or "unknown_failure"
                    logger.info(f"  ⚠ Cycle failed: {reason}, retrying in 30 seconds...")
                    time.sleep(30)
                    continue
                
                # Wait before next cycle (simulates transfer time + check interval)
                logger.info(f"  ⏳ Waiting {CYCLE_SLEEP}s before next cycle (3-5 min transfer in progress)...")
                time.sleep(CYCLE_SLEEP)
        
        except KeyboardInterrupt:
            logger.info("\n\n✓ Arbitrage stopped by user")
            self.print_statistics()


def main():
    arb = CompoundingArbitrage()
    
    # Run continuous cycles (or set max_cycles for testing)
    # arb.run(max_cycles=5)  # Test mode: 5 cycles
    arb.run()  # Production: infinite cycles


if __name__ == "__main__":
    main()
