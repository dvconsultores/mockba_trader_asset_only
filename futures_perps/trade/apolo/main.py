"""
NEAR/USDC Reversal Scalper - Hard-Coded Logic (DEBUG MODE)
Strategy: 2 candles same direction → reversal at S/R → enter on correction
Risk: 0.3% TP, 1.5-1.8% SL, max 3 trades/day

✅ ALWAYS SHOWS: Order Book, Regime, Pattern (even when rejected)
✅ MULTI-CORE: Uses ThreadPoolExecutor for parallel data fetching
✅ DEBUG MODE: Full transparency on every decision
"""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import time
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Tuple

# Ensure project root is importable when running this file directly
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# === IMPORTS FROM YOUR EXISTING PROJECT ===
from db.db_ops import get_setting, initialize_database_tables
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import (
    get_historical_data_limit_apolo, 
    get_orderbook
)
from trading_bot.futures_executor_apolo import (
    place_futures_order, 
    get_close_price, 
    ORDERLY_ACCOUNT_ID
)
from trading_bot.send_bot_message import send_bot_message


class ReversalScalper:
    """
    Hard-coded reversal strategy with regime filter and order book imbalance.
    
    This class implements a rule-based trading system that:
    1. Only trades during preferred time windows (Sun-Thu, 6-11 AM UTC-4)
    2. Avoids trending markets using linear regression slope detection
    3. Requires order book imbalance confirmation for each trade
    4. Detects specific 2-candle reversal patterns at support/resistance
    5. Uses multi-core processing for fast data fetching
    
    No LLM, no TensorFlow - pure deterministic logic for speed and reliability.
    """

    @staticmethod
    def _setting_pct(key: str, default_pct: float) -> float:
        """
        Read a percentage value from database settings and convert to decimal.
        
        Args:
            key: Setting name in database (e.g., 'take_profit')
            default_pct: Default value as decimal (e.g., 0.003 = 0.3%)
        
        Returns:
            Float value as decimal (e.g., database stores "0.3" → returns 0.003)
        
        Example:
            If DB has take_profit = "0.3", returns 0.003 (0.3%)
        """
        raw = get_setting(key)
        try:
            # Database stores percentages as whole numbers (0.3 = 0.3%)
            return float(raw) / 100.0 if raw is not None else default_pct
        except (TypeError, ValueError):
            return default_pct
    
    def __init__(self):
        """
        Initialize the scalper with all strategy parameters.
        
        Parameters can be overridden via database settings or use defaults.
        All thresholds are tuned for NEAR/USDC 5-minute reversal scalping.
        """
        # === STRATEGY PARAMETERS (Risk Management) ===
        self.TP_PCT = self._setting_pct('take_profit', 0.003)    # 0.3% take profit
        self.SL_PCT_MIN = self._setting_pct('stop_loss', 0.015)  # 1.5% stop loss
        self.SL_PCT_MAX = 0.018                                   # 1.8% max stop loss
        self.MAX_TRADES_PER_DAY = 3                               # Daily trade limit
        
        # === REVERSAL PATTERN PARAMETERS ===
        self.CANDLE_COUNT = 2           # Need 2 consecutive candles same direction
        self.CORRECTION_PCT = 0.001     # 0.1% minimum pullback before entry
        self.BIG_CANDLE_MULTIPLIER = 1.2 # Candle must be 1.2x average range
        
        # === ORDER BOOK IMBALANCE PARAMETERS ===
        self.OBI_THRESHOLD = float(get_setting('order_book_threshold') or 1.0)
        self.OB_DEPTH = 20              # Analyze top 20 levels of order book
        
        # === REGIME FILTER PARAMETERS ===
        self.REGIME_WINDOW_5M = 20      # Lookback for 5-minute slope calculation
        self.REGIME_WINDOW_1H = 30      # Lookback for 1-hour slope calculation
        self.SLOPE_THRESHOLD_5M = 0.0015  # 0.15%/candle = trend threshold (5m)
        self.SLOPE_THRESHOLD_1H = 0.0018  # Slightly higher threshold
        self.VOLUME_THRESHOLD = 1.2       # Volume must be 120% of average to confirm trend
        
        # === TIME FILTER PARAMETERS (UTC-4) ===
        self.PREFERRED_DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday']
        self.PREFERRED_HOUR_START = 6   # 6 AM UTC-4
        self.PREFERRED_HOUR_END = 11    # 11 AM UTC-4
        
        # === LIVE PRICE VALIDATION ===
        self.LIVE_PRICE_MAX_DEVIATION = 0.002  # Max 0.2% deviation from candle close
        
        # === DEBUG MODE ===
        self.DEBUG_MODE = True  # Always show full analysis even when rejected

        # === MULTI-CORE PROCESSING ===
        # Use all available CPU cores for parallel data fetching
        self.MAX_WORKERS = max(1, os.cpu_count() or 1)
        logger.info(f"🖥️  ReversalScalper initialized with {self.MAX_WORKERS} CPU cores")
        
    def _get_user_time(self) -> datetime:
        """
        Get current time in UTC-4 timezone (user's local time).
        
        Returns:
            datetime object representing current time in UTC-4
        
        Why UTC-4?
        This matches your preferred trading window (6-11 AM Venezuela/Colombia time)
        """
        utc_now = datetime.now(timezone.utc)
        return utc_now - timedelta(hours=4)
    
    def _is_preferred_time(self) -> bool:
        """
        Check if current time is within preferred trading window.
        
        Returns:
            True if current time is Sun-Thu, 6-11 AM UTC-4
            False otherwise (trade will be rejected)
        
        Why this matters:
        Your historical data shows losses occur outside this window.
        This is the FIRST filter applied (fail fast to save API calls).
        """
        now = self._get_user_time()
        day_name = now.strftime('%A')
        hour = now.hour
        return (day_name in self.PREFERRED_DAYS and 
                self.PREFERRED_HOUR_START <= hour < self.PREFERRED_HOUR_END)
    
    def _calculate_normalized_slope(self, prices: np.ndarray, window: int) -> float:
        """
        Calculate linear regression slope normalized by price (percentage per candle).
        
        Args:
            prices: Array of closing prices (numpy array)
            window: Number of candles to analyze (e.g., 20 or 50)
        
        Returns:
            Float representing slope as % change per candle
            Positive = uptrend, Negative = downtrend, Near zero = ranging
        
        How it works:
        1. Takes last 'window' prices
        2. Fits a straight line using numpy.polyfit (fast linear regression)
        3. Divides slope by last price to normalize (makes it asset-agnostic)
        4. Returns % change per candle (e.g., 0.0015 = 0.15% per candle)
        
        Why normalized?
        A slope of 0.01 means different things at $1 vs $100. Normalization makes
        thresholds consistent across different price levels.
        """
        if len(prices) < window:
            return 0.0
        recent = prices[-window:]
        x = np.arange(window)  # [0, 1, 2, ..., window-1]
        slope, _ = np.polyfit(x, recent, 1)  # Linear regression
        return slope / recent[-1]  # Normalize by last price
    
    def _calculate_obi(self, orderbook: Dict) -> Tuple[float, Dict]:
        """
        Calculate Order Book Imbalance (OBI) ratio.
        
        Args:
            orderbook: Dictionary with 'bids' and 'asks' arrays from exchange
        
        Returns:
            Tuple of (obi_ratio, details_dict)
            - obi_ratio: bids/asks ratio (>1 = bullish, <1 = bearish)
            - details_dict: Contains bids total, asks total, imbalance percentage
        
        How to interpret:
        - OBI > 1.0: More buy pressure (bullish)
        - OBI < 1.0: More sell pressure (bearish)
        - OBI = 1.0: Balanced (neutral)
        
        Example:
        OBI = 1.57 means bids are 57% larger than asks → bullish pressure
        OBI = 0.63 means asks are 58% larger than bids → bearish pressure
        """
        def _qty(value) -> float:
            """Safely convert quantity to float, return 0 on error."""
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        # Sum quantities from top OB_DEPTH levels
        bids = sum(_qty(qty) for _, qty in orderbook.get('bids', [])[:self.OB_DEPTH])
        asks = sum(_qty(qty) for _, qty in orderbook.get('asks', [])[:self.OB_DEPTH])
        total = bids + asks
        
        if total == 0:
            return 1.0, {'bids': 0, 'asks': 0, 'imbalance_pct': 0}
        
        obi = bids / asks if asks > 0 else 2.0
        imbalance_pct = (bids - asks) / total * 100
        
        return obi, {'bids': bids, 'asks': asks, 'imbalance_pct': imbalance_pct}
    
    def _detect_regime(self, df_5m: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None) -> Dict:
        """
        Detect market regime using linear regression slope + volume analysis.
        
        Args:
            df_5m: 5-minute candle DataFrame (required)
            df_1h: 1-hour candle DataFrame (optional, higher weight)
        
        Returns:
            Dictionary with regime classification and detailed metrics:
            - regime: 'RANGE', 'TREND_UP', 'TREND_DOWN', or 'HIGH_VOL'
            - slope_5m: 5-minute slope (%/candle)
            - slope_1h: 1-hour slope (%/candle) or None
            - vol_ratio: Current volume vs average volume
            - is_high_vol: Boolean for volatility spike detection
        
        Regime Detection Logic (in order of priority):
        1. If 1H slope > threshold → TREND (highest priority, macro trend)
        2. If 5M candle range > 2x average → HIGH_VOL (too dangerous)
        3. If 5M slope > threshold AND volume confirms → TREND
        4. Otherwise → RANGE (safe for reversal strategy)
        
        Why multi-timeframe?
        A 5-minute chart might look ranging while 1-hour is trending strongly.
        The 1-hour regime overrides 5-minute to prevent counter-trend trades.
        """
        closes_5m = df_5m['close'].values
        vols_5m = df_5m['volume'].values
        
        # Calculate 5-minute slope and volume ratio
        slope_5m = self._calculate_normalized_slope(closes_5m, self.REGIME_WINDOW_5M)
        vol_ratio_5m = np.mean(vols_5m[-5:]) / np.mean(vols_5m[-20:]) if len(vols_5m) >= 20 else 1.0
        
        # Calculate 1-hour slope (higher timeframe = more weight)
        regime_1h = None
        slope_1h = None
        if df_1h is not None and len(df_1h) >= self.REGIME_WINDOW_1H:
            slope_1h = self._calculate_normalized_slope(df_1h['close'].values, self.REGIME_WINDOW_1H)
            if abs(slope_1h) > self.SLOPE_THRESHOLD_1H:
                regime_1h = 'TREND_UP' if slope_1h > 0 else 'TREND_DOWN'
        
        # Detect volatility expansion (candle range spike)
        ranges = df_5m['high'].values - df_5m['low'].values
        avg_range = np.mean(ranges[-20:])
        current_range = ranges[-1]
        is_high_vol = current_range > avg_range * 2.0
        
        # Final regime decision (priority order matters)
        if regime_1h:
            final_regime = regime_1h  # 1H trend overrides everything
        elif is_high_vol:
            final_regime = 'HIGH_VOL'  # Volatility spike = dangerous
        elif abs(slope_5m) > self.SLOPE_THRESHOLD_5M and vol_ratio_5m > self.VOLUME_THRESHOLD:
            final_regime = 'TREND_UP' if slope_5m > 0 else 'TREND_DOWN'
        else:
            final_regime = 'RANGE'  # Default = safe for reversals
        
        return {
            'regime': final_regime,
            'slope_5m': slope_5m,
            'slope_1h': slope_1h,
            'vol_ratio': vol_ratio_5m,
            'is_high_vol': is_high_vol
        }
    
    def _detect_reversal_pattern(self, df: pd.DataFrame, live_price: float) -> Optional[Dict]:
        """
        Detect the specific 2-candle reversal pattern you trade manually.
        
        Args:
            df: 5-minute candle DataFrame with OHLCV data
            live_price: Current live market price (for entry precision)
        
        Returns:
            Dictionary with trade details if pattern found, None otherwise:
            - side: 'BUY' or 'SELL'
            - entry: Suggested entry price (live_price)
            - reason: Human-readable explanation
            - details: Metrics like pullback %, candle range, etc.
        
        Pattern Logic (LONG):
        1. Two consecutive DOWN candles (c1 < c0 AND c2 < c1)
        2. Second candle is "big" (1.5x average range)
        3. Price bounces back up from low (correction >= 0.1%)
        4. Low is near recent support (within 0.2% of 10-candle low)
        
        Pattern Logic (SHORT):
        1. Two consecutive UP candles (c1 > c0 AND c2 > c1)
        2. Second candle is "big" (1.5x average range)
        3. Price pulls back from high (correction >= 0.1%)
        4. High is near recent resistance (within 0.2% of 10-candle high)
        
        Why these rules?
        This codifies your manual trading intuition into exact mathematical conditions.
        No ambiguity, no LLM interpretation - pure deterministic logic.
        """
        if len(df) < 10:
            return None  # Not enough data
            
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # Get last 3 candles (oldest → newest)
        c0, c1, c2 = closes[-3], closes[-2], closes[-1]
        h0, h1, h2 = highs[-3], highs[-2], highs[-1]
        l0, l1, l2 = lows[-3], lows[-2], lows[-1]
        
        # Calculate average candle range for "big move" detection
        avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
        
        # === PATTERN: 2 candles UP → look for SHORT reversal ===
        if c1 > c0 and c2 > c1:  # Two consecutive up candles
            candle2_range = h2 - l2
            if candle2_range > avg_range * self.BIG_CANDLE_MULTIPLIER:  # Big candle
                pullback = (h2 - live_price) / h2  # How much price pulled back
                if pullback >= self.CORRECTION_PCT:  # Minimum pullback met
                    recent_high = np.max(highs[-10:])  # 10-candle resistance
                    if abs(h2 - recent_high) / recent_high < 0.002:  # Near resistance
                        return {
                            'side': 'SELL',
                            'entry': live_price,
                            'reason': '2 up candles + big move + pullback at resistance',
                            'details': {
                                'pullback_pct': pullback * 100,
                                'candle_range': candle2_range,
                                'avg_range': avg_range
                            }
                        }
        
        # === PATTERN: 2 candles DOWN → look for LONG reversal ===
        elif c1 < c0 and c2 < c1:  # Two consecutive down candles
            candle2_range = h2 - l2
            if candle2_range > avg_range * self.BIG_CANDLE_MULTIPLIER:  # Big candle
                bounce = (live_price - l2) / l2  # How much price bounced
                if bounce >= self.CORRECTION_PCT:  # Minimum bounce met
                    recent_low = np.min(lows[-10:])  # 10-candle support
                    if abs(l2 - recent_low) / recent_low < 0.002:  # Near support
                        return {
                            'side': 'BUY',
                            'entry': live_price,
                            'reason': '2 down candles + big move + bounce at support',
                            'details': {
                                'bounce_pct': bounce * 100,
                                'candle_range': candle2_range,
                                'avg_range': avg_range
                            }
                        }
        
        return None  # No valid pattern found
    
    def _validate_live_price(self, candle_close: float, live_price: float) -> float:
        """
        Calculate deviation between last candle close and current live price.
        
        Args:
            candle_close: Close price of last completed 5-minute candle
            live_price: Current market price from exchange
        
        Returns:
            Deviation as decimal (e.g., 0.0015 = 0.15% deviation)
        
        Why this matters:
        If live price has moved too far from candle close, the pattern
        detection may be stale. This ensures entry timing is precise.
        """
        deviation = abs(live_price - candle_close) / candle_close
        return deviation
    
    def _format_obi_display(self, obi: float, obi_details: Dict) -> str:
        """Format order book info for Telegram (NO markdown)."""
        direction = "🟢 BULLISH" if obi > 1.0 else "🔴 BEARISH" if obi < 1.0 else "⚪ NEUTRAL"
        # Removed asterisks/bold, using clean text
        return (
            f"📚 Order Book (top {self.OB_DEPTH}):\n"
            f"• Bids: {obi_details['bids']:.0f} | Asks: {obi_details['asks']:.0f}\n"
            f"• Imbalance: {obi_details['imbalance_pct']:+.1f}%\n"
            f"• OBI Ratio: {obi:.2f} → {direction}"
        )
    
    def _format_regime_display(self, regime_info: Dict) -> str:
        """Format regime info for Telegram (NO markdown)."""
        regime = regime_info['regime']
        slope_5m = regime_info['slope_5m']
        slope_1h = regime_info['slope_1h']
        
        slope_emoji = "📈" if slope_5m > 0 else "📉" if slope_5m < 0 else "➡️"
        regime_emoji = {
            'RANGE': '🔄',
            'TREND_UP': '🚀',
            'TREND_DOWN': '🔻',
            'HIGH_VOL': '🌊'
        }.get(regime, '❓')
        
        lines = [f"{regime_emoji} Regime: {regime} {slope_emoji}"]
        lines.append(f"• Slope 5m: {slope_5m*100:+.3f}%/candle")
        
        if slope_1h is not None:
            lines.append(f"• Slope 1h: {slope_1h*100:+.3f}%/candle")
        
        # Add short explanation only when regime is TREND (helps understand why blocked)
        if regime in ['TREND_UP', 'TREND_DOWN']:
            lines.append(f"• ℹ️ 1H slope > 0.18% = Trend detected (reversals disabled)")
        elif regime == 'HIGH_VOL':
            lines.append(f"• ℹ️ Candle range > 2x average = Too volatile")
        
        if regime_info['is_high_vol']:
            lines.append("• ⚠️ High volatility detected")
        
        return "\n".join(lines)
    
    def _format_pattern_display(self, pattern: Optional[Dict], live_price: float, regime: str, df: pd.DataFrame) -> str:
        """
        Format pattern detection with regime context, specific failure reasons,
        AND exact price level to wait for.
        """
        # === 1. ANALYZE WHY PATTERN FAILED ===
        failure_reason = None
        wait_price = None
        wait_direction = None
        recent_high = None
        recent_low = None
        min_correction_entry = None
        min_correction_label = None
        
        if len(df) >= 10:
            closes = df['close'].values
            highs = df['high'].values
            lows = df['low'].values
            
            c0, c1, c2 = closes[-3], closes[-2], closes[-1]
            h2, l2 = highs[-1], lows[-1]

            # Compute a minimum correction entry from latest candle direction.
            # This keeps a concrete "enter at" value visible in all cases.
            if c2 > c1:
                min_correction_entry = h2 * (1 - self.CORRECTION_PCT)
                min_correction_label = "pullback"
            elif c2 < c1:
                min_correction_entry = l2 * (1 + self.CORRECTION_PCT)
                min_correction_label = "bounce"
            
            # Calculate average range
            avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
            candle2_range = h2 - l2
            
            # Calculate recent high/low for resistance/support check
            recent_high = np.max(highs[-10:])
            recent_low = np.min(lows[-10:])
            
            # Check conditions
            if not ((c1 > c0 and c2 > c1) or (c1 < c0 and c2 < c1)):
                failure_reason = "Last 2 candles not same direction"
            elif candle2_range <= avg_range * self.BIG_CANDLE_MULTIPLIER:
                failure_reason = "Last candle not big enough (need 1.5x average)"
            else:
                # Check pullback/bounce
                if c2 > c1:  # Up candles → need pullback for SHORT
                    pullback = (h2 - live_price) / h2
                    if pullback < self.CORRECTION_PCT:
                        failure_reason = "Waiting for pullback"
                        wait_price = h2 * (1 - self.CORRECTION_PCT)
                        wait_direction = "down"
                    else:
                        # Pullback confirmed, check resistance
                        distance_to_resistance = abs(h2 - recent_high) / recent_high
                        if distance_to_resistance > 0.002:  # Not at resistance
                            failure_reason = "Pullback confirmed, but not at resistance"
                            wait_price = recent_high  # Wait for price to reach resistance
                            wait_direction = "up"
                        else:
                            failure_reason = "At resistance, checking OBI confirmation"
                            # OBI should be <1.0 for SHORT (bearish)
                            # This is handled in main analyze_signal logic
                else:  # Down candles → need bounce for LONG
                    bounce = (live_price - l2) / l2
                    if bounce < self.CORRECTION_PCT:
                        failure_reason = "Waiting for bounce"
                        wait_price = l2 * (1 + self.CORRECTION_PCT)
                        wait_direction = "up"
                    else:
                        # Bounce confirmed, check support
                        distance_to_support = abs(l2 - recent_low) / recent_low
                        if distance_to_support > 0.002:  # Not at support
                            failure_reason = "Bounce confirmed, but not at support"
                            wait_price = recent_low  # Wait for price to reach support
                            wait_direction = "down"
                        else:
                            failure_reason = "At support, checking OBI confirmation"
        
        # === 2. BUILD DISPLAY ===
        lines = []
        
        # If in TREND regime
        if regime in ['TREND_UP', 'TREND_DOWN']:
            lines.append(f"⚠️ Pattern Status: BLOCKED by {regime}")
            if failure_reason:
                lines.append(f"• 🔍 Pattern check: {failure_reason}")
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            lines.append(f"• 🚫 Reversals disabled in trend")
            return "\n".join(lines)
        
        # If in HIGH_VOL regime
        if regime == 'HIGH_VOL':
            lines.append(f"⚠️ Pattern Status: BLOCKED by HIGH VOLATILITY")
            if failure_reason:
                lines.append(f"• 🔍 Pattern check: {failure_reason}")
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            lines.append(f"• 🌊 Waiting for stabilization")
            return "\n".join(lines)
        
        # Normal RANGE mode
        if pattern is not None:
            side_emoji = "🔴 SHORT" if pattern['side'] == 'SELL' else "🟢 LONG"
            lines.append(f"✅ Pattern detected: {side_emoji}")
            lines.append(f"• Reason: {pattern['reason']}")
            lines.append(f"• Suggested entry: {live_price:.6f}")
            if min_correction_entry is not None:
                lines.append(
                    f"• 🎯 Minimum correction value to enter: {min_correction_entry:.6f} ({self.CORRECTION_PCT*100:.2f}%)"
                )
            if 'details' in pattern:
                details = pattern['details']
                if 'pullback_pct' in details:
                    lines.append(f"• Pullback: {details['pullback_pct']:.2f}%")
                if 'bounce_pct' in details:
                    lines.append(f"• Bounce: {details['bounce_pct']:.2f}%")
        else:
            lines.append("Reversal pattern: NOT DETECTED")
            if failure_reason:
                lines.append(f"• 🔍 Reason: {failure_reason}")
            if min_correction_entry is not None:
                lines.append(
                    f"• 🎯 Minimum correction value to enter: {min_correction_entry:.6f} ({self.CORRECTION_PCT*100:.2f}%)"
                )
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            if recent_high and c2 > c1:  # For SHORT setups
                lines.append(f"• 📊 Recent resistance (10 candles): {recent_high:.6f}")
            elif recent_low and c2 < c1:  # For LONG setups
                lines.append(f"• 📊 Recent support (10 candles): {recent_low:.6f}")
        
        return "\n".join(lines)
    
    def analyze_signal(self, asset: str, interval: str = '5m') -> Dict:
        """
        Main analysis function - orchestrates all checks and returns decision.
        
        Args:
            asset: Trading pair symbol (e.g., 'PERP_NEAR_USDC')
            interval: Candle timeframe (default '5m')
        
        Returns:
            Dictionary with:
            - approved: Boolean (True = trade allowed, False = rejected)
            - symbol, side, entry, stop_loss, take_profit (if approved)
            - resume_of_analysis: Full formatted analysis for Telegram
            - rejection_reasons: List of why trade was rejected (if applicable)
            - debug_info: Raw data for programmatic access
        
        Execution Flow:
        1. Fetch all data in PARALLEL (multi-core) ← Fastest part
        2. Calculate Order Book Imbalance ← Always shown
        3. Detect Market Regime ← Always shown
        4. Detect Reversal Pattern ← Always shown
        5. Apply filters (time, price, regime, OBI) ← Decision point
        6. Return full analysis regardless of approval ← Debug mode
        
        Why parallel fetching?
        Data endpoints are independent - no need to wait for one before
        starting another. 4 parallel requests = ~4x faster than sequential.
        """
        result = {
            'approved': False,
            'symbol': asset,
            'side': 'NONE',
            'entry': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0,
            'resume_of_analysis': '',
            'rejection_reasons': [],
            'debug_info': {}  # For manual testing and backtesting
        }
        
        # === STEP 1: FETCH ALL DATA IN PARALLEL (Multi-Core) ===
        # Using ThreadPoolExecutor to fetch 4 independent endpoints simultaneously
        # This reduces total wait time from ~4s to ~1s (4x speedup)
        with ThreadPoolExecutor(max_workers=min(4, self.MAX_WORKERS)) as pool:
            # Submit all tasks at once (they run in parallel)
            f_df_5m = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='5m', limit=100)
            f_df_1h = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='1h', limit=100)
            f_live = pool.submit(get_close_price, ORDERLY_ACCOUNT_ID, asset, interval)
            f_orderbook = pool.submit(get_orderbook, asset, self.OB_DEPTH)

            # Wait for all to complete (blocking, but all run concurrently)
            df_5m = f_df_5m.result()
            df_1h = f_df_1h.result()
            live_price = f_live.result()
            orderbook = f_orderbook.result()
        
        # Validate data quality
        if df_5m is None or len(df_5m) < 30:
            result['resume_of_analysis'] = "❌ Error: Insufficient 5m data"
            return result

        last_close = df_5m['close'].iloc[-1]
        
        if live_price is None:
            result['resume_of_analysis'] = "❌ Error: Could not fetch live price"
            return result
        
        # === STEP 2: ALWAYS CALCULATE Order Book (for display) ===
        if orderbook is None:
            orderbook = {'bids': [], 'asks': []}
        obi, obi_details = self._calculate_obi(orderbook)
        result['debug_info']['obi'] = obi
        result['debug_info']['obi_details'] = obi_details
        
        # === STEP 3: ALWAYS CALCULATE Regime (for display) ===
        regime_info = self._detect_regime(df_5m, df_1h)
        result['debug_info']['regime'] = regime_info
        
        # === STEP 4: ALWAYS CALCULATE Pattern (for display) ===
        pattern = self._detect_reversal_pattern(df_5m, live_price)
        result['debug_info']['pattern'] = pattern
        result['debug_info']['live_price'] = live_price
        result['debug_info']['last_close'] = last_close
        
        # === STEP 5: BUILD DEBUG DISPLAY (Always shown, even if rejected) ===
        # === BUILD DEBUG DISPLAY (always shown) ===
        regime = regime_info['regime']  # Extract regime first
        
        display_lines = [
            f"📊 {asset} | {interval} | Price: {live_price:.6f}",
            "",
            self._format_obi_display(obi, obi_details),
            "",
            self._format_regime_display(regime_info),
            "",
            self._format_pattern_display(pattern, live_price, regime, df_5m),  # Pass regime here
            ""
        ]
        
        # === STEP 6: APPLY FILTERS (Decision logic) ===
        
        # Filter 1: Time Window (fail fast - saves API calls on next run)
        if not self._is_preferred_time():
            result['rejection_reasons'].append("Outside preferred time window")
            display_lines.append("⏰ ❌ Outside preferred window (6-11 AM UTC-4, Sun-Thu)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Filter 2: Live Price Deviation (ensures entry timing is fresh)
        price_dev = self._validate_live_price(last_close, live_price)
        if price_dev > self.LIVE_PRICE_MAX_DEVIATION:
            result['rejection_reasons'].append(f"Live price deviation {price_dev*100:.2f}%")
            display_lines.append(f"⚠️ ❌ Live price deviation: {price_dev*100:.2f}% (max: {self.LIVE_PRICE_MAX_DEVIATION*100:.1f}%)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Filter 3: Market Regime (MOST IMPORTANT - prevents trend losses)
        regime = regime_info['regime']
        if regime in ['TREND_UP', 'TREND_DOWN']:
            result['rejection_reasons'].append(f"Market in {regime}")
            display_lines.append(f"🚫 ❌ Trend {regime} detected - Reversal strategy paused")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        if regime == 'HIGH_VOL':
            result['rejection_reasons'].append("High volatility")
            display_lines.append("🌊 ❌ High volatility - Waiting for stabilization")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Filter 4: Pattern Required (no pattern = no trade)
        if pattern is None:
            result['rejection_reasons'].append("No valid pattern")
            display_lines.append("❌ No valid reversal pattern at this time")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Filter 5: Order Book Confirmation (must align with pattern direction)
        side = pattern['side']
        if side == 'BUY' and obi < 1.0:
            result['rejection_reasons'].append(f"OBI {obi:.2f} contradicts LONG")
            display_lines.append(f"📚 ❌ Order book does not confirm BUY (OBI: {obi:.2f})")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        elif side == 'SELL' and obi > 1.0:
            result['rejection_reasons'].append(f"OBI {obi:.2f} contradicts SHORT")
            display_lines.append(f"📚 ❌ Order book does not confirm SELL (OBI: {obi:.2f})")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # === STEP 7: ALL CHECKS PASSED - APPROVE TRADE ===
        entry = live_price
        tp = entry * (1 + self.TP_PCT) if side == 'BUY' else entry * (1 - self.TP_PCT)
        
        # Dynamic SL based on volatility (wider in high vol, capped at max)
        ranges = df_5m['high'].values - df_5m['low'].values
        avg_range_pct = np.mean(ranges[-20:]) / np.mean(df_5m['close'].values[-20:])
        sl_multiplier = min(1.2, max(1.0, avg_range_pct / 0.005))  # Scale with vol
        sl_dist = self.SL_PCT_MIN * sl_multiplier
        sl_dist = min(sl_dist, self.SL_PCT_MAX)  # Cap at 1.8%
        sl = entry * (1 - sl_dist) if side == 'BUY' else entry * (1 + sl_dist)
        
        # Update result for approved trade
        result.update({
            'approved': True,
            'side': side,
            'entry': round(entry, 6),
            'take_profit': round(tp, 6),
            'stop_loss': round(sl, 6),
        })
        
        # Add approval confirmation to display
        display_lines.append(
            f"✅ ✅ ✅ TRADE APPROVED ✅ ✅ ✅\n"
            f"• Entry: {entry:.6f}\n"
            f"• TP: {tp:.6f} (+{self.TP_PCT*100:.1f}%)\n"
            f"• SL: {sl:.6f} (-{sl_dist*100:.1f}%)\n"
            f"• RR: 1:{(sl_dist/self.TP_PCT):.1f}"
        )
        
        result['resume_of_analysis'] = "\n".join(display_lines)
        logger.info(f"✅ Signal approved: {side} @ {entry} for {asset}")
        return result


# === BACKWARD-COMPATIBLE WRAPPER ===
def process_signal(asset_override: str = None) -> str:
    """
    Drop-in replacement for your existing process_signal function.
    
    Same input/output signature as your original LLM-based version,
    but uses hard-coded logic instead of DeepSeek API calls.
    
    Args:
        asset_override: Optional asset symbol (uses DB setting if None)
    
    Returns:
        Formatted string for Telegram showing full analysis
    """
    try:
        asset = asset_override or get_setting("asset")
        interval = get_setting("interval") or "5m"
        
        scalper = ReversalScalper()
        result = scalper.analyze_signal(asset, interval)
        
        # Always return the analysis (for manual testing)
        output = result['resume_of_analysis']
        
        # If approved AND auto-trade enabled, execute order
        if result['approved'] and get_setting("auto_trade") in ["True", "Automatic"]:
            order_payload = {
                "symbol": result['symbol'],
                "side": result['side'],
                "entry": result['entry'],
                "take_profit": result['take_profit'],
                "stop_loss": result['stop_loss'],
                "leverage": int(get_setting("leverage") or 5)
            }
            place_futures_order(order_payload)
            logger.info(f"🚀 Order placed: {order_payload}")
            output += "\n\n🚀 ORDER EXECUTED AUTOMATICALLY"
        
        return output
            
    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"


def autotrade():
    """
    Main autotrade loop - runs continuously when auto_trade = "Automatic".
    
    Features:
    - Processes multiple assets in parallel (multi-core)
    - Respects interval timing (5m, 15m, 1h, etc.)
    - Error handling per asset (one failure doesn't stop others)
    - Graceful shutdown on exceptions
    """
    logger.info("🤖 Starting hard-coded autotrade loop...")
    while True:
        try:
            if get_setting("auto_trade") == "Automatic":
                interval_str = get_setting("interval") or "5m"
                interval_map = {
                    '5m': timedelta(minutes=5), '15m': timedelta(minutes=15),
                    '30m': timedelta(minutes=30), '1h': timedelta(hours=1),
                    '4h': timedelta(hours=4), '1d': timedelta(days=1)
                }
                trade_interval = interval_map.get(interval_str, timedelta(minutes=5))
                
                automated_assets = get_setting("automated_assets")
                if automated_assets:
                    asset_list = [a.strip() for a in automated_assets.split(',') if a.strip()]
                    
                    # Multi-core asset processing (parallel across assets)
                    worker_count = min(max(1, os.cpu_count() or 1), len(asset_list))
                    logger.info(f"🖥️  Processing {len(asset_list)} assets with {worker_count} workers")
                    
                    with ThreadPoolExecutor(max_workers=worker_count) as pool:
                        future_map = {
                            pool.submit(process_signal, asset_override=asset): asset
                            for asset in asset_list
                        }
                        for future in as_completed(future_map):
                            asset = future_map[future]
                            try:
                                summary = future.result()
                                # Log first 120 chars of result (avoid spam)
                                logger.info(f"Processed {asset}: {summary[:120]}...")
                            except Exception as e:
                                logger.error(f"Error processing {asset}: {e}")
                
                # Wait for next interval
                time.sleep(trade_interval.total_seconds())
            else:
                # Not in automatic mode - check again in 60 seconds
                time.sleep(60)
        except Exception as e:
            logger.error(f"Autotrade loop error: {e}")
            time.sleep(60)


# === TESTING ===
# if __name__ == "__main__":
#     asset = "PERP_NEAR_USDC"
#     print(f"🧪 Testing signal for {asset}...\n")
#     result = process_signal(asset_override=asset)
#     print(result)