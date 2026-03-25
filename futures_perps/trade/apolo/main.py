"""
NEAR/USDC Reversal Scalper - Hard-Coded Logic (DEBUG MODE)
Strategy: 2 candles same direction → reversal at S/R → enter on correction
Risk: 0.3% TP, 1.5-1.8% SL, max 3 trades/day

✅ ALWAYS SHOWS: Order Book, Regime, Pattern (even when rejected)
✅ MULTI-CORE: Uses ThreadPoolExecutor for parallel data fetching
✅ DEBUG MODE: Full transparency on every decision
✅ MANIPULATION DETECTION: Volume spikes, OB divergence, spread anomalies
"""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import time
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Tuple, List

# Ensure project root is importable when running this file directly
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# === IMPORTS FROM YOUR EXISTING PROJECT ===
from db.db_ops import get_setting, initialize_database_tables, get_trades_today, increment_trades_today
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import (
    get_historical_data_limit_apolo, 
    get_orderbook,
    get_market_trades
)
from trading_bot.futures_executor_apolo import (
    place_futures_order, 
    get_close_price, 
    ORDERLY_ACCOUNT_ID
)
from trading_bot.send_bot_message import send_bot_message

# Initialize database tables on startup
initialize_database_tables()


class ReversalScalper:
    """
    Hard-coded reversal strategy with regime filter, order book imbalance,
    and manipulation detection.
    
    This class implements a rule-based trading system that:
    1. Only trades during preferred time windows (Mon-Thu 6am-10pm, Fri 6-11am, Sun 8-10pm UTC-4)
    2. Avoids trending markets using linear regression slope detection
    3. Requires order book imbalance confirmation for each trade
    4. Detects specific 2-candle reversal patterns at support/resistance
    5. Flags potential manipulation signals (volume spikes, OB divergence, spread)
    6. Uses multi-core processing for fast data fetching
    
    No LLM, no TensorFlow - pure deterministic logic for speed and reliability.
    """

    @staticmethod
    def _setting_pct(key: str, default_pct: float) -> float:
        """Read a percentage value from database settings and convert to decimal."""
        raw = get_setting(key)
        try:
            return float(raw) / 100.0 if raw is not None else default_pct
        except (TypeError, ValueError):
            return default_pct
    
    def __init__(self):
        """Initialize the scalper with all strategy parameters."""
        # === STRATEGY PARAMETERS (Risk Management) ===
        self.TP_PCT = self._setting_pct('take_profit', 0.003)    # 0.3% take profit
        self.SL_PCT_MIN = self._setting_pct('stop_loss', 0.015)  # 1.5% stop loss
        self.SL_PCT_MAX = 0.018                                   # 1.8% max stop loss
        self.MAX_TRADES_PER_DAY = 3                               # Daily trade limit
        
        # === REVERSAL PATTERN PARAMETERS ===
        self.CANDLE_COUNT = 2           # Need 2 consecutive candles same direction
        self.CORRECTION_PCT = 0.001     # 0.1% minimum pullback before entry
        self.BIG_CANDLE_MULTIPLIER = 1.2 # Candle must be 1.2x average range (NEAR-specific)
        
        # === SPIKE REVERSAL PARAMETERS ===
        self.SPIKE_CANDLE_MULTIPLIER = 2.0  # Candle range > 2x avg = spike
        self.SPIKE_VOLUME_MULTIPLIER = 3.0  # Volume > 3x avg confirms spike
        
        # === ORDER BOOK IMBALANCE PARAMETERS ===
        self.OBI_THRESHOLD = float(get_setting('order_book_threshold') or 1.0)
        self.OB_DEPTH = 20              # Analyze top 20 levels of order book
        self.MIN_SIGNIFICANT_TRADE_QTY = 100  # Filter retail noise trades below this qty
        
        # === REGIME FILTER PARAMETERS ===
        self.REGIME_WINDOW_5M = 20      # Lookback for 5-minute slope calculation
        self.REGIME_WINDOW_1H = 30      # Lookback for 1-hour slope (faster reaction)
        self.SLOPE_THRESHOLD_5M = 0.0015  # 0.15%/candle = trend threshold (5m)
        self.SLOPE_THRESHOLD_1H = 0.0018  # 0.18%/candle = trend threshold (1h)
        self.VOLUME_THRESHOLD = 1.2       # Volume must be 120% of average to confirm trend
        
        # === OBI-BASED REGIME BOOST ===
        self.OBI_BULLISH_THRESHOLD = 1.10  # OBI > 1.10 = bullish (10% more bids than asks)
        self.OBI_BEARISH_THRESHOLD = 1.0 / 1.10  # OBI < 0.909 = bearish (more asks than bids)
        self.OBI_IMBALANCE_PCT_THRESHOLD = 3.0  # Imbalance % > 3.0% = moderate strength signal
        
        # === TIME FILTER PARAMETERS (UTC-4) ===
        # Mon-Thu: 6am-10pm | Fri: 6am-11am | Sun: 8pm-10pm | Sat: off
        self.PREFERRED_WINDOWS = {
            'Sunday':    [(20, 22)],
            'Monday':    [(6, 22)],
            'Tuesday':   [(6, 22)],
            'Wednesday': [(6, 22)],
            'Thursday':  [(6, 22)],
            'Friday':    [(6, 11)],
        }
        
        # === LIVE PRICE VALIDATION ===
        self.LIVE_PRICE_MAX_DEVIATION = 0.002  # Max 0.2% deviation from candle close
        
        # === MANIPULATION DETECTION THRESHOLDS ===
        self.VOLUME_SPIKE_MULTIPLIER = 3.0    # Volume > 3x avg = spike
        self.OB_DIVERGENCE_THRESHOLD = 0.002  # 0.2% price move without OB confirmation
        self.SPREAD_ANOMALY_THRESHOLD = 0.003 # 0.3% spread = suspicious
        
        # === DEBUG MODE ===
        self.DEBUG_MODE = True

        # === MULTI-CORE PROCESSING ===
        self.MAX_WORKERS = max(1, os.cpu_count() or 1)
        logger.info(f"🖥️  ReversalScalper initialized with {self.MAX_WORKERS} CPU cores")
        
    def _get_user_time(self) -> datetime:
        """Get current time in UTC-4 timezone (user's local time)."""
        utc_now = datetime.now(timezone.utc)
        return utc_now - timedelta(hours=4)
    
    def _is_preferred_time(self) -> bool:
        """Check if current time is within preferred trading window."""
        now = self._get_user_time()
        day_name = now.strftime('%A')
        hour = now.hour
        windows = self.PREFERRED_WINDOWS.get(day_name, [])
        return any(start <= hour < end for start, end in windows)
    
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """
        Calculate Average True Range (ATR) for adaptive stop loss.
        
        ATR measures volatility:
        - Higher ATR = wider moves expected = wider SL
        - Lower ATR = tight moves = tighter SL
        
        Returns ATR in price units (not percentage).
        """
        if len(df) < period:
            return 0.0
        
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        
        # True Range = max of:
        # 1. High - Low
        # 2. |High - Previous Close|
        # 3. |Low - Previous Close|
        tr = []
        for i in range(len(df)):
            if i == 0:
                tr_val = highs[i] - lows[i]
            else:
                tr_val = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
            tr.append(tr_val)
        
        # ATR = simple moving average of TR
        atr = np.mean(tr[-period:])
        return atr
    
    def _calculate_normalized_slope(self, prices: np.ndarray, window: int) -> float:
        """Calculate linear regression slope normalized by price (percentage per candle)."""
        if len(prices) < window:
            return 0.0
        recent = prices[-window:]
        x = np.arange(window)
        slope, _ = np.polyfit(x, recent, 1)
        return slope / recent[-1]
    
    def _calculate_obi(self, orderbook: Dict) -> Tuple[float, Dict]:
        """Calculate Order Book Imbalance (OBI) ratio."""
        def _qty(value) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        bids = sum(_qty(qty) for _, qty in orderbook.get('bids', [])[:self.OB_DEPTH])
        asks = sum(_qty(qty) for _, qty in orderbook.get('asks', [])[:self.OB_DEPTH])
        total = bids + asks
        
        if total == 0:
            return 1.0, {'bids': 0, 'asks': 0, 'imbalance_pct': 0}
        
        obi = bids / asks if asks > 0 else 2.0
        imbalance_pct = (bids - asks) / total * 100
        
        return obi, {'bids': bids, 'asks': asks, 'imbalance_pct': imbalance_pct}
    
    def _check_manipulation_signals(self, df: pd.DataFrame, orderbook: Dict) -> List[str]:
        """
        Detect potential manipulation or smart money signals.
        
        Returns list of warning strings (empty if clean).
        
        Checks:
        1. Volume spike (>3x average) = smart money entering/exiting
        2. Price/OB divergence = possible trap
        3. Abnormal spread = manipulation attempt
        """
        warnings = []
        
        # === 1. Volume Spike Detection ===
        if len(df) >= 20:
            recent_vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].rolling(20).mean().iloc[-1]
            if avg_vol > 0 and recent_vol > avg_vol * self.VOLUME_SPIKE_MULTIPLIER:
                ratio = recent_vol / avg_vol
                warnings.append(f"⚠️ Volume spike ({ratio:.1f}x avg) - smart money?")
        
        # === 2. Order Book Divergence Detection ===
        obi, _ = self._calculate_obi(orderbook)
        if len(df) >= 2:
            price_change = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2]
            # Price up but OB bearish = possible bull trap
            if price_change > self.OB_DIVERGENCE_THRESHOLD and obi < 0.95:
                warnings.append("⚠️ Price up but OB bearish - possible bull trap")
            # Price down but OB bullish = possible bear trap
            elif price_change < -self.OB_DIVERGENCE_THRESHOLD and obi > 1.05:
                warnings.append("⚠️ Price down but OB bullish - possible bear trap")
        
        # === 3. Spread Anomaly Detection ===
        if orderbook.get('bids') and orderbook.get('asks'):
            try:
                best_bid = float(orderbook['bids'][0][0])
                best_ask = float(orderbook['asks'][0][0])
            except (TypeError, ValueError, IndexError):
                best_bid = 0.0
                best_ask = 0.0

            if best_bid > 0:
                spread = (best_ask - best_bid) / best_bid
                if spread > self.SPREAD_ANOMALY_THRESHOLD:
                    warnings.append(f"⚠️ Wide spread ({spread*100:.2f}%) - caution")
        
        return warnings
    
    def _detect_regime(self, df_5m: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None, obi: float = 1.0, obi_details: Optional[Dict] = None) -> Dict:
        """Detect market regime using linear regression slope + volume analysis + OBI confirmation."""
        closes_5m = df_5m['close'].values
        vols_5m = df_5m['volume'].values
        
        slope_5m = self._calculate_normalized_slope(closes_5m, self.REGIME_WINDOW_5M)
        vol_ratio_5m = np.mean(vols_5m[-5:]) / np.mean(vols_5m[-20:]) if len(vols_5m) >= 20 else 1.0
        
        regime_1h = None
        slope_1h = None
        if df_1h is not None and len(df_1h) >= self.REGIME_WINDOW_1H:
            slope_1h = self._calculate_normalized_slope(df_1h['close'].values, self.REGIME_WINDOW_1H)
            if abs(slope_1h) > self.SLOPE_THRESHOLD_1H:
                regime_1h = 'TREND_UP' if slope_1h > 0 else 'TREND_DOWN'
        
        ranges = df_5m['high'].values - df_5m['low'].values
        avg_range = np.mean(ranges[-20:])
        current_range = ranges[-1]
        is_high_vol = current_range > avg_range * 2.0
        
        if regime_1h:
            final_regime = regime_1h
        elif is_high_vol:
            final_regime = 'HIGH_VOL'
        elif abs(slope_5m) > self.SLOPE_THRESHOLD_5M and vol_ratio_5m > self.VOLUME_THRESHOLD:
            final_regime = 'TREND_UP' if slope_5m > 0 else 'TREND_DOWN'
        else:
            final_regime = 'RANGE'
        
        # === OBI REGIME BOOST: Override RANGE based on strong OBI signals ===
        # For LONGS: Override RANGE → TREND_UP when OBI bullish, if NOT already in downtrend
        # For SHORTS: Override RANGE → TREND_DOWN when OBI bearish, if NOT already in uptrend
        obi_boosted = False
        if final_regime == 'RANGE' and obi_details:
            imbalance_pct = abs(obi_details.get('imbalance_pct', 0))
            
            # Bullish override (for longs) - only if slope not already clearly down
            if obi >= self.OBI_BULLISH_THRESHOLD and imbalance_pct >= self.OBI_IMBALANCE_PCT_THRESHOLD and slope_5m >= -0.0005:
                final_regime = 'TREND_UP'
                obi_boosted = True
            
            # Bearish override (for shorts) - only if slope not already clearly up
            elif obi <= self.OBI_BEARISH_THRESHOLD and imbalance_pct >= self.OBI_IMBALANCE_PCT_THRESHOLD and slope_5m <= 0.0005:
                final_regime = 'TREND_DOWN'
                obi_boosted = True
        
        return {
            'regime': final_regime,
            'slope_5m': slope_5m,
            'slope_1h': slope_1h,
            'vol_ratio': vol_ratio_5m,
            'is_high_vol': is_high_vol,
            'obi_boosted': obi_boosted
        }
    
    def _detect_reversal_pattern(self, df: pd.DataFrame, live_price: float) -> Optional[Dict]:
        """Detect the specific 2-candle reversal pattern you trade manually."""
        if len(df) < 10:
            return None
            
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        c0, c1, c2 = closes[-3], closes[-2], closes[-1]
        h0, h1, h2 = highs[-3], highs[-2], highs[-1]
        l0, l1, l2 = lows[-3], lows[-2], lows[-1]
        
        avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
        
        # === PATTERN: 2 candles UP → look for SHORT reversal ===
        if c1 > c0 and c2 > c1:
            pullback = (h2 - live_price) / h2
            if pullback >= self.CORRECTION_PCT:
                recent_high = np.max(highs[-10:])
                if abs(h2 - recent_high) / recent_high < 0.005:
                    return {
                        'side': 'SELL',
                        'entry': live_price,
                        'reason': '2 up candles + pullback at resistance',
                        'details': {
                            'pullback_pct': pullback * 100,
                            'candle_range': h2 - l2,
                            'avg_range': avg_range
                        }
                    }
        
        # === PATTERN: 2 candles DOWN → look for LONG reversal ===
        elif c1 < c0 and c2 < c1:
            bounce = (live_price - l2) / l2
            if bounce >= self.CORRECTION_PCT:
                recent_low = np.min(lows[-10:])
                if abs(l2 - recent_low) / recent_low < 0.005:
                    return {
                        'side': 'BUY',
                        'entry': live_price,
                        'reason': '2 down candles + bounce at support',
                        'details': {
                            'bounce_pct': bounce * 100,
                            'candle_range': h2 - l2,
                            'avg_range': avg_range
                        }
                    }
        
        return None
    
    def _detect_spike_reversal(self, df: pd.DataFrame, live_price: float, obi: float) -> Optional[Dict]:
        """
        Detect spike-reversal pattern: single outsized candle + OB confirms reversal.
        
        After a big spike, the correction is the trade:
        - Spike UP + OB bearish → SHORT on pullback
        - Spike DOWN + OB bullish → LONG on bounce
        
        Volume is informational only (lagging on low-liquidity DEX like Orderly).
        """
        if len(df) < 20:
            return None
        
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        opens = df['open'].values
        volumes = df['volume'].values
        
        h_last = highs[-1]
        l_last = lows[-1]
        candle_range = h_last - l_last
        avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
        avg_vol = np.mean(volumes[-20:-1])
        last_vol = volumes[-1]
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0.0
        
        # Need outsized candle (range is real-time, not lagging like volume)
        if candle_range < avg_range * self.SPIKE_CANDLE_MULTIPLIER:
            return None
        
        is_bullish_candle = closes[-1] > opens[-1]
        
        # === SPIKE UP + OB bearish → SHORT on pullback ===
        if is_bullish_candle and obi < 1.0:
            pullback = (h_last - live_price) / h_last
            if pullback >= self.CORRECTION_PCT:
                return {
                    'side': 'SELL',
                    'entry': live_price,
                    'reason': 'Spike up + OB bearish → reversal SHORT',
                    'details': {
                        'pullback_pct': pullback * 100,
                        'candle_range': candle_range,
                        'avg_range': avg_range,
                        'vol_ratio': vol_ratio
                    }
                }
        
        # === SPIKE DOWN + OB bullish → LONG on bounce ===
        elif not is_bullish_candle and obi > 1.0:
            bounce = (live_price - l_last) / l_last
            if bounce >= self.CORRECTION_PCT:
                return {
                    'side': 'BUY',
                    'entry': live_price,
                    'reason': 'Spike down + OB bullish → reversal LONG',
                    'details': {
                        'bounce_pct': bounce * 100,
                        'candle_range': candle_range,
                        'avg_range': avg_range,
                        'vol_ratio': vol_ratio
                    }
                }
        
        return None
    
    def _validate_live_price(self, candle_close: float, live_price: float) -> float:
        """Calculate deviation between last candle close and current live price."""
        return abs(live_price - candle_close) / candle_close
    
    def _format_obi_display(self, obi: float, obi_details: Dict, live_price: float, orderbook: Dict, market_trades: List = None) -> str:
        """Format order book info for Telegram (NO markdown)."""
        direction = "🟢 BULLISH" if obi > 1.0 else "🔴 BEARISH" if obi < 1.0 else "⚪ NEUTRAL"
        leverage = int(get_setting("leverage") or 5)
        # Zero-impact max: smallest best level (fits entirely in top-of-book)
        best_bid_qty = 0.0
        best_ask_qty = 0.0
        try:
            if orderbook.get('bids'):
                best_bid_qty = float(orderbook['bids'][0][1])
            if orderbook.get('asks'):
                best_ask_qty = float(orderbook['asks'][0][1])
        except (TypeError, ValueError, IndexError):
            pass
        zero_impact_qty = min(best_bid_qty, best_ask_qty) if best_bid_qty > 0 and best_ask_qty > 0 else 0.0
        zero_impact_notional = zero_impact_qty * live_price
        zero_impact_margin = zero_impact_notional / leverage
        # Safe max: median of recent market trades, capped by OB thin side
        thin_side_qty = min(obi_details['bids'], obi_details['asks'])
        trade_sizes = []
        if market_trades:
            trade_sizes = sorted([float(t.get('executed_quantity', 0)) for t in market_trades if float(t.get('executed_quantity', 0)) >= self.MIN_SIGNIFICANT_TRADE_QTY])
        if trade_sizes:
            median_trade = trade_sizes[len(trade_sizes) // 2]
            p90_trade = trade_sizes[int(len(trade_sizes) * 0.90)]
            safe_qty = max(p90_trade, thin_side_qty * 0.25)
            trade_label = f"P90 recent trades: {p90_trade:.0f} | Median: {median_trade:.0f}"
        else:
            safe_qty = thin_side_qty * 0.25
            trade_label = "(25% OB depth - no trade data)"
        max_notional = safe_qty * live_price
        max_margin = max_notional / leverage
        lines = [
            f"📚 Order Book (top {self.OB_DEPTH}):",
            f"• Bids: {obi_details['bids']:.0f} | Asks: {obi_details['asks']:.0f}",
            f"• Imbalance: {obi_details['imbalance_pct']:+.1f}%",
            f"• OBI Ratio: {obi:.2f} → {direction}",
            f"• 🟢 No-impact max: {zero_impact_notional:.0f} USDC ({zero_impact_margin:.0f} margin @ {leverage}x)",
            f"• 💰 Safe max: {max_notional:.0f} USDC ({max_margin:.0f} margin @ {leverage}x)",
            f"• 📈 {trade_label}",
        ]
        return "\n".join(lines)
    
    def _format_regime_display(self, regime_info: Dict) -> str:
        """Format regime info for Telegram (NO markdown)."""
        regime = regime_info['regime']
        slope_5m = regime_info['slope_5m']
        slope_1h = regime_info['slope_1h']
        obi_boosted = regime_info.get('obi_boosted', False)
        
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
        if regime in ['TREND_UP', 'TREND_DOWN']:
            if obi_boosted:
                lines.append(f"• ✨ OBI bullish override (strong order book imbalance)")
            else:
                lines.append(f"• ℹ️ 1H slope > 0.18% = Trend detected (reversals disabled)")
        elif regime == 'HIGH_VOL':
            lines.append(f"• ℹ️ Candle range > 2x average = Spike detected (checking spike reversal)")
        if regime_info['is_high_vol']:
            lines.append("• ⚠️ High volatility detected")
        return "\n".join(lines)
    
    def _format_pattern_display(self, pattern: Optional[Dict], live_price: float, regime: str, df: pd.DataFrame) -> str:
        """Format pattern detection with regime context and failure reasons."""
        failure_reason = None
        wait_price = None
        wait_direction = None
        recent_high = None
        recent_low = None
        min_correction_entry = None
        
        if len(df) >= 10:
            closes = df['close'].values
            highs = df['high'].values
            lows = df['low'].values
            c0, c1, c2 = closes[-3], closes[-2], closes[-1]
            h2, l2 = highs[-1], lows[-1]
            
            if c2 > c1:
                min_correction_entry = h2 * (1 - self.CORRECTION_PCT)
            elif c2 < c1:
                min_correction_entry = l2 * (1 + self.CORRECTION_PCT)
            
            avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
            candle2_range = h2 - l2
            recent_high = np.max(highs[-10:])
            recent_low = np.min(lows[-10:])
            
            if not ((c1 > c0 and c2 > c1) or (c1 < c0 and c2 < c1)):
                failure_reason = "Last 2 candles not same direction"
            else:
                if c2 > c1:
                    pullback = (h2 - live_price) / h2
                    if pullback < self.CORRECTION_PCT:
                        failure_reason = "Waiting for pullback"
                        wait_price = h2 * (1 - self.CORRECTION_PCT)
                        wait_direction = "down"
                    else:
                        if abs(h2 - recent_high) / recent_high > 0.005:
                            failure_reason = "Pullback confirmed, but not at resistance"
                            wait_price = recent_high
                            wait_direction = "up"
                        else:
                            failure_reason = "At resistance, pattern ready"
                else:
                    bounce = (live_price - l2) / l2
                    if bounce < self.CORRECTION_PCT:
                        failure_reason = "Waiting for bounce"
                        wait_price = l2 * (1 + self.CORRECTION_PCT)
                        wait_direction = "up"
                    else:
                        if abs(l2 - recent_low) / recent_low > 0.005:
                            failure_reason = "Bounce confirmed, but not at support"
                            wait_price = recent_low
                            wait_direction = "down"
                        else:
                            failure_reason = "At support, pattern ready"
        
        lines = []
        if regime in ['TREND_UP', 'TREND_DOWN']:
            lines.append(f"⚠️ Pattern Status: BLOCKED by {regime}")
            if failure_reason:
                lines.append(f"• 🔍 Pattern check: {failure_reason}")
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            lines.append(f"• 🚫 Reversals disabled in trend")
            return "\n".join(lines)
        if regime == 'HIGH_VOL':
            lines.append(f"⚠️ 2-Candle Pattern: PAUSED (HIGH_VOL → spike reversal active)")
            if failure_reason:
                lines.append(f"• 🔍 Pattern check: {failure_reason}")
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            lines.append(f"• ⚡ Spike reversal takes over in HIGH_VOL")
            return "\n".join(lines)
        
        if pattern is not None:
            side_emoji = "🔴 SHORT" if pattern['side'] == 'SELL' else "🟢 LONG"
            lines.append(f"✅ Pattern detected: {side_emoji}")
            lines.append(f"• Reason: {pattern['reason']}")
            lines.append(f"• Suggested entry: {live_price:.6f}")
            if min_correction_entry is not None:
                lines.append(f"• 🎯 Minimum correction to enter: {min_correction_entry:.6f} ({self.CORRECTION_PCT*100:.2f}%)")
            if 'details' in pattern:
                details = pattern['details']
                if 'pullback_pct' in details:
                    lines.append(f"• Pullback: {details['pullback_pct']:.2f}%")
                if 'bounce_pct' in details:
                    lines.append(f"• Bounce: {details['bounce_pct']:.2f}%")
        else:
            lines.append("🔍 Reversal pattern: NOT DETECTED")
            if failure_reason:
                lines.append(f"• 🔍 Reason: {failure_reason}")
            if min_correction_entry is not None:
                lines.append(f"• 🎯 Minimum correction to enter: {min_correction_entry:.6f} ({self.CORRECTION_PCT*100:.2f}%)")
            if wait_price:
                lines.append(f"• 🎯 Wait for price {wait_direction} to {wait_price:.6f}")
            if recent_high and c2 > c1:
                lines.append(f"• 📊 Recent resistance (10 candles): {recent_high:.6f}")
            elif recent_low and c2 < c1:
                lines.append(f"• 📊 Recent support (10 candles): {recent_low:.6f}")
        return "\n".join(lines)
    
    def _format_manipulation_display(self, warnings: List[str]) -> Optional[str]:
        """Format manipulation warnings for Telegram display."""
        if not warnings:
            return None
        lines = ["🔍 Manipulation Checks:"]
        for w in warnings:
            lines.append(f"• {w}")
        if len(warnings) >= 2:
            lines.append("🚫 High risk - consider skipping trade")
        return "\n".join(lines)
    
    def analyze_signal(self, asset: str, interval: str = '5m') -> Dict:
        """Main analysis function - orchestrates all checks and returns decision."""
        result = {
            'approved': False,
            'symbol': asset,
            'side': 'NONE',
            'entry': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0,
            'resume_of_analysis': '',
            'rejection_reasons': [],
            'debug_info': {}
        }
        
        # === STEP 1: FETCH ALL DATA IN PARALLEL ===
        with ThreadPoolExecutor(max_workers=min(5, self.MAX_WORKERS)) as pool:
            f_df_5m = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='5m', limit=100)
            f_df_1h = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='1h', limit=100)
            f_live = pool.submit(get_close_price, ORDERLY_ACCOUNT_ID, asset, interval)
            f_orderbook = pool.submit(get_orderbook, asset, self.OB_DEPTH)
            f_trades = pool.submit(get_market_trades, asset, 50)
            df_5m = f_df_5m.result()
            df_1h = f_df_1h.result()
            live_price = f_live.result()
            orderbook = f_orderbook.result()
            market_trades = f_trades.result()
        
        if df_5m is None or len(df_5m) < 30:
            result['resume_of_analysis'] = "❌ Error: Insufficient 5m data"
            return result
        last_close = df_5m['close'].iloc[-1]
        if live_price is None:
            result['resume_of_analysis'] = "❌ Error: Could not fetch live price"
            return result
        
        # === STEP 2: ALWAYS CALCULATE Order Book ===
        if orderbook is None:
            orderbook = {'bids': [], 'asks': []}
        obi, obi_details = self._calculate_obi(orderbook)
        result['debug_info']['obi'] = obi
        result['debug_info']['obi_details'] = obi_details
        
        # === STEP 3: ALWAYS CALCULATE Regime ===
        regime_info = self._detect_regime(df_5m, df_1h, obi=obi, obi_details=obi_details)
        result['debug_info']['regime'] = regime_info
        
        # === STEP 4: CHECK MANIPULATION SIGNALS ===
        manipulation_warnings = self._check_manipulation_signals(df_5m, orderbook)
        result['debug_info']['manipulation_warnings'] = manipulation_warnings
        
        # === STEP 5: ALWAYS CALCULATE Patterns ===
        pattern = self._detect_reversal_pattern(df_5m, live_price)
        spike_reversal = self._detect_spike_reversal(df_5m, live_price, obi)
        result['debug_info']['pattern'] = pattern
        result['debug_info']['spike_reversal'] = spike_reversal
        result['debug_info']['live_price'] = live_price
        result['debug_info']['last_close'] = last_close
        
        # === CALCULATE ATR for display and SL/TP later ===
        atr = self._calculate_atr(df_5m, period=14)
        result['debug_info']['atr'] = atr
        
        # === STEP 6: BUILD DEBUG DISPLAY ===
        regime = regime_info['regime']
        display_lines = [
            f"📊 {asset} | {interval} | Price: {live_price:.6f}",
            "",
            self._format_obi_display(obi, obi_details, live_price, orderbook, market_trades),
            "",
            self._format_regime_display(regime_info),
            "",
            f"📏 Volatility (ATR 14): {atr:.6f} ({(atr/live_price)*100:.3f}%)\n• SL will be: {live_price - atr*2:.6f} (-{(atr*2/live_price)*100:.2f}%)\n• TP will be: {live_price + atr*3:.6f} (+{(atr*3/live_price)*100:.2f}%)",
            "",
            self._format_pattern_display(pattern, live_price, regime, df_5m),
            ""
        ]
        
        # Show spike reversal status
        if spike_reversal:
            side_emoji = "🔴 SHORT" if spike_reversal['side'] == 'SELL' else "🟢 LONG"
            details = spike_reversal['details']
            display_lines.append(
                f"⚡ Spike Reversal: {side_emoji}\n"
                f"• {spike_reversal['reason']}\n"
                f"• Volume: {details['vol_ratio']:.1f}x avg\n"
                f"• Range: {details['candle_range']:.6f} vs avg {details['avg_range']:.6f}"
            )
            display_lines.append("")
        
        # Add manipulation warnings if any
        manip_display = self._format_manipulation_display(manipulation_warnings)
        if manip_display:
            display_lines.append(manip_display)
            display_lines.append("")
        
        # === STEP 7: APPLY FILTERS ===
        if not self._is_preferred_time():
            result['rejection_reasons'].append("Outside preferred time window")
            display_lines.append("⏰ ❌ Outside preferred window (Mon-Thu 6am-10pm, Fri 6-11am, Sun 8-10pm UTC-4)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        price_dev = self._validate_live_price(last_close, live_price)
        if price_dev > self.LIVE_PRICE_MAX_DEVIATION:
            result['rejection_reasons'].append(f"Live price deviation {price_dev*100:.2f}%")
            display_lines.append(f"⚠️ ❌ Live price deviation: {price_dev*100:.2f}% (max: {self.LIVE_PRICE_MAX_DEVIATION*100:.1f}%)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Trend always blocks both patterns
        if regime in ['TREND_UP', 'TREND_DOWN']:
            result['rejection_reasons'].append(f"Market in {regime}")
            display_lines.append(f"🚫 ❌ Trend {regime} detected - Reversal strategy paused")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # HIGH_VOL blocks 2-candle pattern but allows spike reversal
        if regime == 'HIGH_VOL' and spike_reversal is None:
            result['rejection_reasons'].append("High volatility")
            display_lines.append("🌊 ❌ High volatility - Waiting for stabilization")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Manipulation warnings block 2-candle pattern but NOT spike reversal
        # (volume spike + OB divergence ARE the spike reversal confirmation)
        if len(manipulation_warnings) >= 2 and spike_reversal is None:
            result['rejection_reasons'].append("Multiple manipulation signals detected")
            display_lines.append("🚫 ❌ Trade blocked: Too many manipulation warnings")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Choose the active signal: spike reversal takes priority in HIGH_VOL,
        # otherwise use 2-candle pattern
        active_signal = None
        if regime == 'HIGH_VOL' and spike_reversal is not None:
            active_signal = spike_reversal
        elif pattern is not None:
            active_signal = pattern
        elif spike_reversal is not None:
            active_signal = spike_reversal
        
        if active_signal is None:
            result['rejection_reasons'].append("No valid pattern")
            display_lines.append("❌ No valid reversal pattern at this time")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # OBI is informational only — shown in display but does not block trades
        side = active_signal['side']
        if active_signal is pattern:
            if side == 'BUY' and obi < 1.0:
                display_lines.append(f"📚 ℹ️ OBI {obi:.2f} does not confirm BUY (reference only)")
            elif side == 'SELL' and obi > 1.0:
                display_lines.append(f"📚 ℹ️ OBI {obi:.2f} does not confirm SELL (reference only)")
        
        # === CHECK DAILY TRADES LIMIT ===
        trades_today = get_trades_today()
        max_trades = self.MAX_TRADES_PER_DAY
        
        if trades_today >= max_trades and get_setting("auto_trade") == "Automatic":
            result['rejection_reasons'].append(f"Daily limit reached ({trades_today}/{max_trades})")
            display_lines.append(f"🚫 ❌ Daily trade limit reached: {trades_today}/{max_trades} trades\n• Resume trading tomorrow")
            result['resume_of_analysis'] = "\n".join(display_lines)
            return result
        
        # Display trades count + reminder if approaching limit
        display_lines.append(f"📊 Trades today: {trades_today}/{max_trades}")
        if trades_today == max_trades - 1:
            display_lines.append(f"⚠️ WARNING: This is your final trade for today!")
        elif trades_today >= max_trades:
            display_lines.append(f"🚫 Daily limit reached: {trades_today}/{max_trades}\n• Wait until tomorrow for more trades")
        
        # === STEP 8: ALL CHECKS PASSED - APPROVE TRADE ===
        entry = live_price
        
        # === ATR-Based SL/TP (adapts to volatility) ===
        atr = self._calculate_atr(df_5m, period=14)
        
        if atr > 0:
            # Use ATR for adaptive stops
            if side == 'BUY':
                tp = entry + (atr * 3.0)  # Risk 1, Reward 3
                sl = entry - (atr * 2.0)  # Stop at 2x ATR below
            else:  # SELL
                tp = entry - (atr * 3.0)
                sl = entry + (atr * 2.0)
            atr_based = True
        else:
            # Fallback: use original fixed percentages
            tp = entry * (1 + self.TP_PCT) if side == 'BUY' else entry * (1 - self.TP_PCT)
            ranges = df_5m['high'].values - df_5m['low'].values
            avg_range_pct = np.mean(ranges[-20:]) / np.mean(df_5m['close'].values[-20:])
            sl_multiplier = min(1.2, max(1.0, avg_range_pct / 0.005))
            sl_dist = self.SL_PCT_MIN * sl_multiplier
            sl_dist = min(sl_dist, self.SL_PCT_MAX)
            sl = entry * (1 - sl_dist) if side == 'BUY' else entry * (1 + sl_dist)
            atr_based = False
        
        result.update({
            'approved': True,
            'side': side,
            'entry': round(entry, 6),
            'take_profit': round(tp, 6),
            'stop_loss': round(sl, 6),
        })
        
        sl_distance_pct = abs(sl - entry) / entry * 100
        tp_distance_pct = abs(tp - entry) / entry * 100
        
        display_lines.append(
            f"✅ ✅ ✅ TRADE APPROVED ✅ ✅ ✅\n"
            f"• Signal: {active_signal['reason']}\n"
            f"• Entry: {entry:.6f}\n"
            f"• SL: {sl:.6f} (-{sl_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
            f"• TP: {tp:.6f} (+{tp_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
            f"• Risk/Reward: {(tp_distance_pct / sl_distance_pct):.2f}:1"
            f"• TP: {tp:.6f} (+{self.TP_PCT*100:.1f}%)\n"
            f"• SL: {sl:.6f} (-{sl_dist*100:.1f}%)\n"
            f"• RR: 1:{(sl_dist/self.TP_PCT):.1f}"
        )
        
        result['resume_of_analysis'] = "\n".join(display_lines)
        logger.info(f"✅ Signal approved: {side} @ {entry} for {asset}")
        return result


# === BACKWARD-COMPATIBLE WRAPPER ===
def process_signal(asset_override: str = None) -> str:
    """Drop-in replacement - always returns full analysis for manual review."""
    try:
        asset = asset_override or get_setting("asset")
        interval = get_setting("interval") or "5m"
        scalper = ReversalScalper()
        result = scalper.analyze_signal(asset, interval)
        output = result['resume_of_analysis']
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
            
            # Increment daily trade counter
            trades_count = increment_trades_today()
            output += f"\n\n🚀 ORDER EXECUTED AUTOMATICALLY\n📊 Daily trade count: {trades_count}"
        return output
    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"


def autotrade():
    """Main autotrade loop - runs continuously when auto_trade = 'Automatic'."""
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
                                logger.info(f"Processed {asset}: {summary[:120]}...")
                            except Exception as e:
                                logger.error(f"Error processing {asset}: {e}")
                time.sleep(trade_interval.total_seconds())
            else:
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