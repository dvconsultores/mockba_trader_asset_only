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
from db.db_ops import get_setting, initialize_database_tables, get_trades_today, increment_trades_today, save_signal_to_history
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import (
    get_historical_data_limit_apolo, 
    get_orderbook,
    get_market_trades
)
from futures_perps.trade.apolo.binance_data import (
    get_historical_data_binance,
    get_orderbook_binance,
    get_binance_price,
    get_binance_market_trades,
    get_binance_symbol
)
from trading_bot.futures_executor_apolo import (
    place_futures_order, 
    get_close_price, 
    ORDERLY_ACCOUNT_ID,
    get_user_statistics
)
from trading_bot.spot_executor_binance import place_spot_order, has_open_orders_binance
from trading_bot.send_bot_message import send_bot_message

# Initialize database tables on startup
initialize_database_tables()


def _get_exchange_mode(exchange: str) -> str:
    """Return mode for exchange.

    Modes: False, Signal, Automatic.
    """
    key = "auto_trade_dex" if exchange == "dex" else "auto_trade_cex"
    mode = get_setting(key)
    if mode in ("False", "Signal", "Automatic", "True"):
        return "Automatic" if mode == "True" else mode
    return "False"


class ReversalScalper:
    """
    Hard-coded reversal strategy with regime filter, order book imbalance,
    and manipulation detection.
    
    This class implements a rule-based trading system that:
    1. Only trades during preferred time windows (Mon-Fri 6am-12pm, Sun 8-10am UTC-4)
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
        self.SL_PCT_MIN = self._setting_pct('stop_loss', 0.010)  # 1.0% stop loss (tighter for scalp-style)
        self.SL_PCT_MAX = 0.012                                   # 1.2% max stop loss
        self.MAX_TRADES_PER_DAY = 2                               # Max 2 positive trades daily
        self.SL_ATR_MULTIPLIER = 1.2    # SL at 1.2x ATR (tight, capital-preserving)
        self.TP_ATR_MULTIPLIER = 2.0    # TP at 2.0x ATR (R:R ~1.67:1)
        
        # === REVERSAL PATTERN PARAMETERS ===
        self.CANDLE_COUNT = 2           # Minimum 2 candles same direction
        self.CANDLE_COUNTS = [2, 3, 4]  # Support 2, 3, 4 candle reversals
        self.CORRECTION_PCT = 0.0003    # 0.03% minimum pullback (balanced: catches entries on 30s poll without being too early)
        self.BIG_CANDLE_MULTIPLIER = 1.2 # Candle must be 1.2x average range (NEAR-specific)
        self.SR_PROXIMITY_PCT = 0.015   # 1.5% proximity to S/R level (wider for low-liquidity DEX like Orderly)
        
        # === SPIKE REVERSAL PARAMETERS ===
        self.SPIKE_CANDLE_MULTIPLIER = 1.3  # Candle range > 1.3x avg = spike (sensitive for low-liquidity DEX)
        self.SPIKE_VOLUME_MULTIPLIER = 3.0  # Volume > 3x avg confirms spike
        
        # === ORDER BOOK IMBALANCE PARAMETERS ===
        self.OBI_THRESHOLD = float(get_setting('order_book_threshold') or 1.0)
        self.OBI_BULLISH_EXTREME = 1.30    # OBI > 1.30 = extreme bullish (whale pump detection)
        self.OBI_BEARISH_EXTREME = 0.77    # OBI < 0.77 = extreme bearish (whale dump detection)
        self.OB_DEPTH = 20              # Analyze top 20 levels of order book
        self.MIN_SIGNIFICANT_TRADE_QTY = 100  # Filter retail noise trades below this qty
        
        # === REGIME FILTER PARAMETERS ===
        self.REGIME_WINDOW_5M = 20      # Lookback for 5-minute slope calculation
        self.REGIME_WINDOW_1H = 30      # Lookback for 1-hour slope (faster reaction)
        self.SLOPE_THRESHOLD_5M = 0.0012  # 0.12%/candle = trend threshold (5m, softened for early detection)
        self.SLOPE_THRESHOLD_1H = 0.0014  # 0.14%/candle = trend threshold (1h, softened for early detection)
        self.VOLUME_THRESHOLD = 1.2       # Volume must be 120% of average to confirm trend
        
        # === OBI-BASED REGIME BOOST ===
        self.OBI_BULLISH_THRESHOLD = 1.10  # OBI > 1.10 = bullish (10% more bids than asks)
        self.OBI_BEARISH_THRESHOLD = 1.0 / 1.10  # OBI < 0.909 = bearish (more asks than bids)
        self.OBI_IMBALANCE_PCT_THRESHOLD = 3.0  # Imbalance % > 3.0% = moderate strength signal
        
        # === TIME FILTER PARAMETERS (UTC-4) ===
        # Mon-Fri: 6am-12pm | Sun: 8am-10am | Sat: off
        self.PREFERRED_WINDOWS = {
            'Monday':    [(6, 11)],
            'Tuesday':   [(6, 11)],
            'Wednesday': [(6, 11)],
            'Thursday':  [(6, 11)],
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
    
    def _check_manipulation_signals(self, df: pd.DataFrame, orderbook: Dict, obi: float = 1.0) -> List[str]:
        """
        Detect potential manipulation or smart money signals using volume, OBI, and price action.
        
        Returns list of warning strings (empty if clean).
        
        Checks:
        1. OBI extreme values (>1.3 or <0.77) = whale accumulation/dump
        2. Volume spike (>3x average) = smart money entering/exiting
        3. Price/OB divergence = possible trap
        4. Abnormal spread = manipulation attempt
        """
        warnings = []
        
        # === 0. OBI Extreme Values Detection ===
        # OBI > 1.3 = extremely bullish (90%+ more bids than asks) = whale pump attempt
        if obi > self.OBI_BULLISH_EXTREME:
            warnings.append(f"🔴 EXTREME BULLISH OBI ({obi:.2f}) - potential whale pump")
        # OBI < 0.77 = extremely bearish (23%+ fewer bids than asks) = whale dump attempt
        elif obi < self.OBI_BEARISH_EXTREME:
            warnings.append(f"🔴 EXTREME BEARISH OBI ({obi:.2f}) - potential whale dump")
        
        # === 1. Volume Spike Detection ===
        if len(df) >= 20:
            recent_vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].rolling(20).mean().iloc[-1]
            if avg_vol > 0 and recent_vol > avg_vol * self.VOLUME_SPIKE_MULTIPLIER:
                ratio = recent_vol / avg_vol
                warnings.append(f"⚠️ Volume spike ({ratio:.1f}x avg) - smart money?")
        
        # === 2. Order Book Divergence Detection (with OBI context) ===
        if len(df) >= 2:
            price_change = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2]
            # Price up but OB bearish = possible bull trap
            if price_change > self.OB_DIVERGENCE_THRESHOLD and obi < 0.95:
                warnings.append(f"⚠️ Price up ({price_change*100:.2f}%) but OB bearish (OBI {obi:.2f}) - possible bull trap")
            # Price down but OB bullish = possible bear trap
            elif price_change < -self.OB_DIVERGENCE_THRESHOLD and obi > 1.05:
                warnings.append(f"⚠️ Price down ({price_change*100:.2f}%) but OB bullish (OBI {obi:.2f}) - possible bear trap")
        
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
        """Detect market regime using real-time price action (slopes only)."""
        closes_5m = df_5m['close'].values
        
        slope_5m = self._calculate_normalized_slope(closes_5m, self.REGIME_WINDOW_5M)
        
        regime_1h = None
        slope_1h = None
        if df_1h is not None and len(df_1h) >= self.REGIME_WINDOW_1H:
            slope_1h = self._calculate_normalized_slope(df_1h['close'].values, self.REGIME_WINDOW_1H)
            if abs(slope_1h) > self.SLOPE_THRESHOLD_1H:
                regime_1h = 'TREND_UP' if slope_1h > 0 else 'TREND_DOWN'
        
        if regime_1h:
            final_regime = regime_1h
        elif abs(slope_5m) > self.SLOPE_THRESHOLD_5M:
            final_regime = 'TREND_UP' if slope_5m > 0 else 'TREND_DOWN'
        else:
            final_regime = 'RANGE'
        
        return {
            'regime': final_regime,
            'slope_5m': slope_5m,
            'slope_1h': slope_1h,
        }
    
    def _count_consecutive_candles(self, closes: np.ndarray) -> Tuple[int, int]:
        """Count consecutive candles in same direction from most recent backward."""
        consecutive_up = 0
        consecutive_down = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i-1]:
                if consecutive_down > 0:
                    break
                consecutive_up += 1
            elif closes[i] < closes[i-1]:
                if consecutive_up > 0:
                    break
                consecutive_down += 1
            else:
                break
        return consecutive_up, consecutive_down
    
    def _detect_reversal_pattern(self, df: pd.DataFrame, live_price: float) -> Optional[Dict]:
        """
        SIMPLIFIED: Detect 2-3 consecutive candles + reversal candle.
        
        Entry logic:
        - 2+ candles same direction (up or down)
        - Current candle is opposite color (reversal confirmation)
        - No S/R proximity or pullback % required
        """
        if len(df) < 10:
            return None
            
        closes = df['close'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        
        consecutive_up, consecutive_down = self._count_consecutive_candles(closes)
        avg_range = np.mean([highs[i] - lows[i] for i in range(-20, -1)])
        
        # Current candle direction (for reversal confirmation)
        current_is_bullish = closes[-1] > opens[-1]
        current_is_bearish = closes[-1] < opens[-1]
        
        # === PATTERN: 2+ candles UP + bearish reversal candle → SHORT ===
        if consecutive_up >= 2 and current_is_bearish:
            return {
                'side': 'SELL',
                'entry': live_price,
                'reason': f'{consecutive_up} up candles + bearish reversal',
                'details': {
                    'consecutive': consecutive_up,
                    'pattern': min(consecutive_up, 4),
                    'candle_range': highs[-1] - lows[-1],
                    'avg_range': avg_range
                }
            }
        
        # === PATTERN: 2+ candles DOWN + bullish reversal candle → LONG ===
        if consecutive_down >= 2 and current_is_bullish:
            return {
                'side': 'BUY',
                'entry': live_price,
                'reason': f'{consecutive_down} down candles + bullish reversal',
                'details': {
                    'consecutive': consecutive_down,
                    'pattern': min(consecutive_down, 4),
                    'candle_range': highs[-1] - lows[-1],
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
        
        # === SPIKE UP → SHORT on pullback (softened OBI) ===
        if is_bullish_candle and obi < 1.15:
            pullback = (h_last - live_price) / h_last
            if pullback >= self.CORRECTION_PCT:
                return {
                    'side': 'SELL',
                    'entry': live_price,
                    'reason': f'Spike up (OBI {obi:.2f}) → reversal SHORT',
                    'details': {
                        'pullback_pct': pullback * 100,
                        'candle_range': candle_range,
                        'avg_range': avg_range,
                        'vol_ratio': vol_ratio
                    }
                }
        
        # === SPIKE DOWN → LONG on bounce (softened OBI) ===
        elif not is_bullish_candle and obi > 0.85:
            bounce = (live_price - l_last) / l_last
            if bounce >= self.CORRECTION_PCT:
                return {
                    'side': 'BUY',
                    'entry': live_price,
                    'reason': f'Spike down (OBI {obi:.2f}) → reversal LONG',
                    'details': {
                        'bounce_pct': bounce * 100,
                        'candle_range': candle_range,
                        'avg_range': avg_range,
                        'vol_ratio': vol_ratio
                    }
                }
        
        return None
    
    def _detect_engulfing_pattern(self, df: pd.DataFrame, live_price: float) -> Optional[Dict]:
        """
        Detect engulfing candle reversal at S/R levels.
        
        Bearish engulfing at resistance → SHORT
        Bullish engulfing at support → LONG
        """
        if len(df) < 10:
            return None
        
        opens = df['open'].values
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # Previous and current candle
        o1, c1 = opens[-2], closes[-2]
        o2, c2, h2, l2 = opens[-1], closes[-1], highs[-1], lows[-1]
        
        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        
        if body1 == 0 or body2 == 0:
            return None
        
        recent_high = np.max(highs[-10:])
        recent_low = np.min(lows[-10:])
        
        # === Bearish engulfing at resistance → SHORT ===
        if (c1 > o1 and          # prev candle bullish
            c2 < o2 and          # current candle bearish
            body2 > body1 and    # current body larger
            o2 >= c1 and         # open at or above prev close
            c2 <= o1 and         # close at or below prev open
            abs(h2 - recent_high) / recent_high < self.SR_PROXIMITY_PCT):
            pullback = (h2 - live_price) / h2
            if pullback >= self.CORRECTION_PCT:
                return {
                    'side': 'SELL',
                    'entry': live_price,
                    'reason': 'Bearish engulfing at resistance',
                    'details': {
                        'pullback_pct': pullback * 100,
                        'body_ratio': body2 / body1,
                        'candle_range': h2 - l2,
                    }
                }
        
        # === Bullish engulfing at support → LONG ===
        if (c1 < o1 and          # prev candle bearish
            c2 > o2 and          # current candle bullish
            body2 > body1 and    # current body larger
            o2 <= c1 and         # open at or below prev close
            c2 >= o1 and         # close at or above prev open
            abs(l2 - recent_low) / recent_low < self.SR_PROXIMITY_PCT):
            bounce = (live_price - l2) / l2
            if bounce >= self.CORRECTION_PCT:
                return {
                    'side': 'BUY',
                    'entry': live_price,
                    'reason': 'Bullish engulfing at support',
                    'details': {
                        'bounce_pct': bounce * 100,
                        'body_ratio': body2 / body1,
                        'candle_range': h2 - l2,
                    }
                }
        
        return None
    
    def _detect_pinbar_pattern(self, df: pd.DataFrame, live_price: float) -> Optional[Dict]:
        """
        Detect pin bar (wick rejection) at S/R levels.
        
        Shooting star (long upper wick) at resistance → SHORT
        Hammer (long lower wick) at support → LONG
        """
        if len(df) < 10:
            return None
        
        opens = df['open'].values
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
        body = abs(c - o)
        total_range = h - l
        
        if total_range == 0:
            return None
        
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        
        recent_high = np.max(highs[-10:])
        recent_low = np.min(lows[-10:])
        
        # === Shooting star at resistance → SHORT ===
        # Upper wick > 2x body, wick is majority of candle, near resistance
        if (upper_wick > body * 2 and
            upper_wick > total_range * 0.6 and
            abs(h - recent_high) / recent_high < self.SR_PROXIMITY_PCT):
            pullback = (h - live_price) / h
            if pullback >= self.CORRECTION_PCT:
                return {
                    'side': 'SELL',
                    'entry': live_price,
                    'reason': 'Shooting star (wick rejection) at resistance',
                    'details': {
                        'pullback_pct': pullback * 100,
                        'wick_body_ratio': upper_wick / body if body > 0 else 99,
                        'wick_pct': (upper_wick / total_range) * 100,
                    }
                }
        
        # === Hammer at support → LONG ===
        # Lower wick > 2x body, wick is majority of candle, near support
        if (lower_wick > body * 2 and
            lower_wick > total_range * 0.6 and
            abs(l - recent_low) / recent_low < self.SR_PROXIMITY_PCT):
            bounce = (live_price - l) / l
            if bounce >= self.CORRECTION_PCT:
                return {
                    'side': 'BUY',
                    'entry': live_price,
                    'reason': 'Hammer (wick rejection) at support',
                    'details': {
                        'bounce_pct': bounce * 100,
                        'wick_body_ratio': lower_wick / body if body > 0 else 99,
                        'wick_pct': (lower_wick / total_range) * 100,
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
            f"📚 Order Book (reference only):",
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
        
        slope_emoji = "📈" if slope_5m > 0 else "📉" if slope_5m < 0 else "➡️"
        regime_emoji = {
            'RANGE': '🔄',
            'TREND_UP': '🚀',
            'TREND_DOWN': '🔻',
        }.get(regime, '❓')
        
        lines = [f"{regime_emoji} Regime: {regime} {slope_emoji} (reference only)"]
        lines.append(f"• Slope 5m: {slope_5m*100:+.3f}%/candle")
        if slope_1h is not None:
            lines.append(f"• Slope 1h: {slope_1h*100:+.3f}%/candle")
        return "\n".join(lines)
    
    def _format_pattern_display(self, pattern: Optional[Dict], live_price: float, regime: str, df: pd.DataFrame) -> str:
        """Format pattern detection - simplified for 2-3 candles + reversal strategy."""
        if len(df) < 10:
            return "🔍 Reversal pattern: Insufficient data"
            
        closes = df['close'].values
        opens = df['open'].values
        consecutive_up, consecutive_down = self._count_consecutive_candles(closes)
        
        # Current candle direction
        current_is_bullish = closes[-1] > opens[-1]
        current_is_bearish = closes[-1] < opens[-1]
        current_dir = "🟢 bullish" if current_is_bullish else "🔴 bearish" if current_is_bearish else "⚪ doji"
        
        lines = []
        if pattern is not None:
            side_emoji = "🔴 SHORT" if pattern['side'] == 'SELL' else "🟢 LONG"
            lines.append(f"✅ Pattern detected: {side_emoji}")
            lines.append(f"• {pattern['reason']}")
            lines.append(f"• Entry: {live_price:.6f}")
            if 'details' in pattern:
                details = pattern['details']
                lines.append(f"• Pattern: {details.get('consecutive', '?')}-candle reversal")
        else:
            lines.append("🔍 Reversal pattern: NOT DETECTED")
            lines.append(f"• Consecutive up: {consecutive_up} | down: {consecutive_down}")
            lines.append(f"• Current candle: {current_dir}")
            if consecutive_up >= 2 and not current_is_bearish:
                lines.append(f"• ⏳ Have {consecutive_up} up candles, waiting for bearish reversal candle")
            elif consecutive_down >= 2 and not current_is_bullish:
                lines.append(f"• ⏳ Have {consecutive_down} down candles, waiting for bullish reversal candle")
            elif consecutive_up < 2 and consecutive_down < 2:
                lines.append("• Need 2+ consecutive candles same direction first")
        return "\n".join(lines)
    
    def _format_manipulation_display(self, warnings: List[str]) -> Optional[str]:
        """Format manipulation warnings for Telegram display (reference only, not blocking)."""
        if not warnings:
            return None
        lines = ["🔍 Manipulation Checks (reference only):"]
        for w in warnings:
            lines.append(f"• {w}")
        if len(warnings) >= 2:
            lines.append("⚠️ High risk signals detected (FYI)")
        return "\n".join(lines)
    
    def analyze_signal(self, asset: str, interval: str = '5m', exchange_override: str = None) -> Dict:
        """Main analysis function - orchestrates all checks and returns decision."""
        exchange = exchange_override or get_setting("exchange") or "dex"
        
        result = {
            'approved': False,
            'symbol': asset,
            'side': 'NONE',
            'entry': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0,
            'exchange': exchange,
            'resume_of_analysis': '',
            'rejection_reasons': [],
            'debug_info': {}
        }

        # === STEP 1: FETCH ALL DATA IN PARALLEL ===
        with ThreadPoolExecutor(max_workers=min(5, self.MAX_WORKERS)) as pool:
            if exchange == "cex":
                f_df_5m = pool.submit(get_historical_data_binance, symbol=asset, interval='5m', limit=100)
                f_df_1h = pool.submit(get_historical_data_binance, symbol=asset, interval='1h', limit=100)
                f_live = pool.submit(get_binance_price, asset)
                f_orderbook = pool.submit(get_orderbook_binance, asset, self.OB_DEPTH)
                f_trades = pool.submit(get_binance_market_trades, asset, 50)
            else:
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
        manipulation_warnings = self._check_manipulation_signals(df_5m, orderbook, obi=obi)
        result['debug_info']['manipulation_warnings'] = manipulation_warnings
        
        # === STEP 5: ALWAYS CALCULATE Patterns ===
        pattern = self._detect_reversal_pattern(df_5m, live_price)
        spike_reversal = self._detect_spike_reversal(df_5m, live_price, obi)
        engulfing = self._detect_engulfing_pattern(df_5m, live_price)
        pinbar = self._detect_pinbar_pattern(df_5m, live_price)
        result['debug_info']['pattern'] = pattern
        result['debug_info']['spike_reversal'] = spike_reversal
        result['debug_info']['engulfing'] = engulfing
        result['debug_info']['pinbar'] = pinbar
        result['debug_info']['live_price'] = live_price
        result['debug_info']['last_close'] = last_close
        
        # === CALCULATE ATR for display and SL/TP later ===
        atr = self._calculate_atr(df_5m, period=14)
        result['debug_info']['atr'] = atr
        
        # === STEP 6: BUILD DEBUG DISPLAY ===
        regime = regime_info['regime']
        exchange_label = "💱 CEX (Binance Spot)" if exchange == "cex" else "🌐 DEX (Orderly Futures)"
        display_symbol = get_binance_symbol(asset) if exchange == "cex" else asset
        display_lines = [
            f"📊 {display_symbol} | {interval} | Price: {live_price:.6f} | {exchange_label}",
            "",
            self._format_obi_display(obi, obi_details, live_price, orderbook, market_trades),
            "",
            self._format_regime_display(regime_info),
            "",
        ]
        
        if exchange != "cex":
            display_lines.append(
                f"📏 Volatility (ATR 14): {atr:.6f} ({(atr/live_price)*100:.3f}%)\n"
                + (lambda sl_pct, tp_pct: (
                    f"• SL will be: {live_price - live_price*sl_pct:.6f} (-{sl_pct*100:.2f}%)\n"
                    f"• TP will be: {live_price + live_price*tp_pct:.6f} (+{tp_pct*100:.2f}%)"
                ))(
                    max(atr * self.SL_ATR_MULTIPLIER / live_price, self.SL_PCT_MIN),
                    max(atr * self.TP_ATR_MULTIPLIER / live_price, self.TP_PCT)
                )
            )
        
        display_lines.extend([
            "",
            self._format_pattern_display(pattern, live_price, regime, df_5m),
            ""
        ])
        
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
        
        # Show engulfing pattern status
        if engulfing:
            side_emoji = "🔴 SHORT" if engulfing['side'] == 'SELL' else "🟢 LONG"
            display_lines.append(
                f"🔄 Engulfing Pattern: {side_emoji}\n"
                f"• {engulfing['reason']}"
            )
            display_lines.append("")
        
        # Show pin bar pattern status
        if pinbar:
            side_emoji = "🔴 SHORT" if pinbar['side'] == 'SELL' else "🟢 LONG"
            display_lines.append(
                f"📌 Pin Bar Pattern: {side_emoji}\n"
                f"• {pinbar['reason']}"
            )
            display_lines.append("")
        
        # Add manipulation warnings if any
        manip_display = self._format_manipulation_display(manipulation_warnings)
        if manip_display:
            display_lines.append(manip_display)
            display_lines.append("")
        
        # === STEP 7: APPLY FILTERS ===
        if exchange != "cex" and not self._is_preferred_time():
            result['rejection_reasons'].append("Outside preferred time window")
            display_lines.append("⏰ ❌ Outside preferred window (Mon-Fri 6am-12pm, Sun 8-10am UTC-4) — TRADE BLOCKED")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            # === LOG REJECTED SIGNAL ===
            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            
            return result
        
        price_dev = self._validate_live_price(last_close, live_price)
        if price_dev > self.LIVE_PRICE_MAX_DEVIATION:
            result['rejection_reasons'].append(f"Live price deviation {price_dev*100:.2f}%")
            display_lines.append(f"⚠️ ❌ Live price deviation: {price_dev*100:.2f}% (max: {self.LIVE_PRICE_MAX_DEVIATION*100:.1f}%)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            # === LOG REJECTED SIGNAL ===
            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            
            return result
        
        # ✅ REMOVED: Trend filter — reversals trade in any regime now
        # if regime in ['TREND_UP', 'TREND_DOWN']:
        #     result['rejection_reasons'].append(f"Market in {regime}")
        #     display_lines.append(f"🚫 ❌ Trend {regime} detected - Reversal strategy paused")
        #     result['resume_of_analysis'] = "\n".join(display_lines)
        #     return result
        
        # ✅ REMOVED: HIGH_VOL filter — reversals allowed in all volatility regimes
        # if regime == 'HIGH_VOL' and spike_reversal is None:
        #     result['rejection_reasons'].append("High volatility")
        #     display_lines.append("🌊 ❌ High volatility - Waiting for stabilization")
        #     result['resume_of_analysis'] = "\n".join(display_lines)
        #     return result
        
        # ✅ CHANGED: Manipulation warnings are now REFERENCE ONLY (display, don't block)
        # User prefers simple price action without filters
        if len(manipulation_warnings) >= 2:
            display_lines.append("⚠️ ℹ️ Manipulation signals detected (reference only, not blocking)")
        
        # Choose the active signal: spike reversal takes priority in HIGH_VOL,
        # then 2+ candle pattern, then engulfing, then pin bar, then spike
        active_signal = None
        if regime == 'HIGH_VOL' and spike_reversal is not None:
            active_signal = spike_reversal
        elif pattern is not None:
            active_signal = pattern
        elif engulfing is not None:
            active_signal = engulfing
        elif pinbar is not None:
            active_signal = pinbar
        elif spike_reversal is not None:
            active_signal = spike_reversal
        
        if active_signal is None:
            result['rejection_reasons'].append("No valid pattern")
            display_lines.append("❌ No valid reversal pattern at this time")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            # === LOG REJECTED SIGNAL ===
            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            
            return result
        
        # === CEX SPOT: Only BUY signals allowed (long-only, no shorting on spot) ===
        if exchange == "cex" and active_signal['side'] == 'SELL':
            result['rejection_reasons'].append("CEX spot: only BUY signals (long-only)")
            display_lines.append("❌ SHORT signal rejected — Binance spot is long-only (BUY only)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=active_signal.get('reason'),
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            
            return result
        
        # OBI is reference only — counter-trend OBI logged as warning, not a blocker
        # In thin markets like Orderly, OBI is too volatile to use as hard filter
        side = active_signal['side']
        if side == 'BUY' and obi < 1.0:
            display_lines.append(f"📚 ℹ️ OBI {obi:.2f} (bearish book, thin market—reference only)")
        elif side == 'SELL' and obi > 1.0:
            display_lines.append(f"📚 ℹ️ OBI {obi:.2f} (bullish book, thin market—reference only)")
        
        # === CHECK DAILY TRADES LIMIT (DEX only, CEX unlimited) ===
        trades_today = get_trades_today()
        max_trades = self.MAX_TRADES_PER_DAY
        
        if exchange != "cex" and trades_today >= max_trades and _get_exchange_mode("dex") == "Automatic":
            result['rejection_reasons'].append(f"Daily limit reached ({trades_today}/{max_trades})")
            display_lines.append(f"🚫 ❌ Daily trade limit reached: {trades_today}/{max_trades} trades\n• Resume trading tomorrow")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            # === LOG REJECTED SIGNAL ===
            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=active_signal.get('reason') if active_signal else None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            
            return result
        
        # Display trades count + reminder if approaching limit (DEX only)
        if exchange != "cex":
            display_lines.append(f"📊 Trades today: {trades_today}/{max_trades}")
            if trades_today == max_trades - 1:
                display_lines.append(f"⚠️ WARNING: This is your final trade for today!")
            elif trades_today >= max_trades:
                display_lines.append(f"🚫 Daily limit reached: {trades_today}/{max_trades}\n• Wait until tomorrow for more trades")
        
        # === STEP 8: ALL CHECKS PASSED - APPROVE TRADE ===
        entry = live_price
        
        # === ATR-Based SL/TP (adapts to volatility) ===
        atr = self._calculate_atr(df_5m, period=14)
        
        if exchange == "cex":
            # CEX Spot: TP only, no SL (can hold position indefinitely)
            if atr > 0:
                tp = entry + (atr * self.TP_ATR_MULTIPLIER)
                tp_pct = abs(tp - entry) / entry
                tp_pct = max(tp_pct, self.TP_PCT)
                tp = entry * (1 + tp_pct)
                atr_based = True
            else:
                tp = entry * (1 + self.TP_PCT)
                atr_based = False
            sl = 0  # No stop loss for spot
        elif atr > 0:
            # Use ATR for adaptive stops (tight SL to preserve capital)
            if side == 'BUY':
                tp = entry + (atr * self.TP_ATR_MULTIPLIER)
                sl = entry - (atr * self.SL_ATR_MULTIPLIER)
            else:  # SELL
                tp = entry - (atr * self.TP_ATR_MULTIPLIER)
                sl = entry + (atr * self.SL_ATR_MULTIPLIER)
            # Apply floor guards: ATR values cannot be tighter than DB settings
            sl_pct = abs(sl - entry) / entry
            sl_pct = max(sl_pct, self.SL_PCT_MIN)
            sl = entry * (1 - sl_pct) if side == 'BUY' else entry * (1 + sl_pct)
            tp_pct = abs(tp - entry) / entry
            tp_pct = max(tp_pct, self.TP_PCT)
            tp = entry * (1 + tp_pct) if side == 'BUY' else entry * (1 - tp_pct)
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
        
        tp_distance_pct = abs(tp - entry) / entry * 100
        
        if exchange == "cex":
            display_lines.append(
                f"✅ ✅ ✅ TRADE APPROVED (Binance Spot) ✅ ✅ ✅\n"
                f"• Signal: {active_signal['reason']}\n"
                f"• Entry: {entry:.6f}\n"
                f"• TP: {tp:.6f} (+{tp_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
                f"• Mode: 🟢 BUY only (long-only spot)"
            )
        else:
            sl_distance_pct = abs(sl - entry) / entry * 100
            display_lines.append(
                f"✅ ✅ ✅ TRADE APPROVED ✅ ✅ ✅\n"
                f"• Signal: {active_signal['reason']}\n"
                f"• Entry: {entry:.6f}\n"
                f"• SL: {sl:.6f} (-{sl_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
                f"• TP: {tp:.6f} (+{tp_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
                f"• Risk/Reward: {(tp_distance_pct / sl_distance_pct):.2f}:1"
            )
        
        result['resume_of_analysis'] = "\n".join(display_lines)
        logger.info(f"✅ Signal approved: {side} @ {entry} for {asset}")
        
        # === SAVE SIGNAL TO DATABASE FOR LATER ANALYSIS ===
        consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
        signal_id = save_signal_to_history(
            asset=asset,
            exchange=exchange,
            regime=regime,
            obi=obi,
            pattern_type=active_signal.get('reason', 'Unknown'),
            approved=True,
            side=side,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            rejection_reasons=[],
            manipulation_warnings=manipulation_warnings,
            atr=atr,
            live_price=live_price,
            candle_count=max(consecutive_up, consecutive_down)
        )
        logger.info(f"💾 Signal saved to DB with ID: {signal_id}")
        
        return result


# === BACKWARD-COMPATIBLE WRAPPER ===
def process_signal(asset_override: str = None, exchange_override: str = None) -> str:
    """Drop-in replacement - always returns full analysis for manual review."""
    try:
        asset = asset_override or get_setting("current_asset")
        interval = get_setting("interval") or "5m"
        exchange = exchange_override or get_setting("exchange") or "dex"
        scalper = ReversalScalper()
        result = scalper.analyze_signal(asset, interval, exchange_override=exchange)
        output = result['resume_of_analysis']
        if result['approved'] and _get_exchange_mode(exchange) == "Automatic":
            if exchange == "cex":
                order_payload = {
                    "symbol": result['symbol'],
                    "side": result['side'],
                    "entry": result['entry'],
                    "take_profit": result['take_profit'],
                }
                place_spot_order(order_payload)
                logger.info(f"🚀 Binance spot order placed: {order_payload}")
            else:
                order_payload = {
                    "symbol": result['symbol'],
                    "side": result['side'],
                    "entry": result['entry'],
                    "take_profit": result['take_profit'],
                    "stop_loss": result['stop_loss'],
                    "leverage": int(get_setting("leverage") or 5)
                }
                place_futures_order(order_payload)
                logger.info(f"🚀 Orderly futures order placed: {order_payload}")

            output += "\n\n🚀 ORDER EXECUTED AUTOMATICALLY"
        return output
    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"


def autotrade():
    """Main autotrade loop - supports independent modes for DEX and CEX."""
    logger.info("🤖 Starting hard-coded autotrade loop...")
    while True:
        try:
            dex_mode = _get_exchange_mode("dex")
            cex_mode = _get_exchange_mode("cex")
            active_any = dex_mode in ("Signal", "Automatic") or cex_mode in ("Signal", "Automatic")

            if not active_any:
                time.sleep(60)
                continue

            asset = get_setting("current_asset")
            interval = get_setting("interval") or "5m"
            if not asset:
                time.sleep(30)
                continue

            scalper = ReversalScalper()

            # DEX cycle
            if dex_mode in ("Signal", "Automatic"):
                if scalper._is_preferred_time():
                    if get_user_statistics() > 0:
                        logger.info("📋 DEX has open position(s) — skipping pattern search")
                    else:
                        dex_result = scalper.analyze_signal(asset, interval, exchange_override="dex")
                        if dex_result.get("approved"):
                            if dex_mode == "Signal":
                                send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📡 DEX SIGNAL ALERT\n{dex_result['resume_of_analysis']}")
                                logger.info(f"📡 DEX signal alert sent for {asset}")
                            else:
                                order_payload = {
                                    "symbol": dex_result['symbol'],
                                    "side": dex_result['side'],
                                    "entry": dex_result['entry'],
                                    "take_profit": dex_result['take_profit'],
                                    "stop_loss": dex_result['stop_loss'],
                                    "leverage": int(get_setting("leverage") or 5)
                                }
                                place_futures_order(order_payload)
                                logger.info(f"🚀 Orderly futures order placed: {order_payload}")
                                send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"🚀 DEX ORDER EXECUTED\n{dex_result['resume_of_analysis']}")
                else:
                    logger.info("⏰ DEX mode active but outside preferred window")

            # CEX cycle
            if cex_mode in ("Signal", "Automatic"):
                if has_open_orders_binance():
                    logger.info("📋 Binance has open order(s) — skipping pattern search")
                else:
                    cex_result = scalper.analyze_signal(asset, interval, exchange_override="cex")
                    if cex_result.get("approved"):
                        if cex_mode == "Signal":
                            send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📡 CEX SIGNAL ALERT\n{cex_result['resume_of_analysis']}")
                            logger.info(f"📡 CEX signal alert sent for {asset}")
                        else:
                            order_payload = {
                                "symbol": cex_result['symbol'],
                                "side": cex_result['side'],
                                "entry": cex_result['entry'],
                                "take_profit": cex_result['take_profit'],
                            }
                            place_spot_order(order_payload)
                            logger.info(f"🚀 Binance spot order placed: {order_payload}")
                            send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"🚀 CEX ORDER EXECUTED\n{cex_result['resume_of_analysis']}")

            time.sleep(30)
        except Exception as e:
            logger.error(f"Autotrade loop error: {e}")
            time.sleep(60)


# === TESTING ===
# if __name__ == "__main__":
#     asset = "PERP_NEAR_USDC"
#     print(f"🧪 Testing signal for {asset}...\n")
#     result = process_signal(asset_override=asset)
#     print(result)