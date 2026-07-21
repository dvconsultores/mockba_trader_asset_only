"""
NEAR/USDC Reversal Scalper - Hard-Coded Logic (DEBUG MODE)
Strategy: 2 candles same direction → reversal at S/R → enter on correction
Risk: 0.3% TP, 1.5-1.8% SL, max 3 trades/day

✅ ALWAYS SHOWS: Order Book, Regime, Pattern (even when rejected)
✅ MULTI-CORE: Uses ThreadPoolExecutor for parallel data fetching
✅ DEBUG MODE: Full transparency on every decision
✅ MANIPULATION DETECTION: Volume spikes, OB divergence, spread anomalies
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import time
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Tuple, List

# Ensure project root is importable when running this file directly
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# === IMPORTS FROM YOUR EXISTING PROJECT ===
from db.db_ops import get_db_connection, get_setting, initialize_database_tables, get_trades_today, increment_trades_today, save_signal_to_history
from logs.log_config import apolo_trader_logger as logger
from trade.historical_data import (
    get_historical_data_limit_apolo, 
    get_orderbook,
    get_market_trades
)
from trade.binance_data import (
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

# Ensure ml_threshold setting exists (CEX-only ML gate, default 0.80)
with get_db_connection() as conn:
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ml_threshold', '0.80')")
    conn.commit()

_LAST_SIGNAL_ALERTS: Dict[str, float] = {}
_LAST_EXCHANGE_SCAN_AT: Dict[str, float] = {"dex": 0.0, "cex": 0.0}
_LAST_LABELER_RUN_AT: float = 0.0
_LABELER_INTERVAL = 7200  # run outcome labeler every 2 hours

# === EXECUTION COOLDOWN (prevents repeated entries on same pattern) ===
_LAST_EXECUTION_AT: Dict[str, float] = {}  # key = "exchange:asset:side" → timestamp
EXECUTION_COOLDOWN_SECONDS = 300  # 5 minutes between same-side executions


def _should_allow_execution(exchange: str, asset: str, side: str) -> bool:
    """Check if enough time has passed since last execution on same exchange+asset+side."""
    now = time.time()
    key = f"{exchange}:{asset}:{side}"
    last_exec = _LAST_EXECUTION_AT.get(key, 0)
    if now - last_exec < EXECUTION_COOLDOWN_SECONDS:
        remaining = int(EXECUTION_COOLDOWN_SECONDS - (now - last_exec))
        logger.info(f"⏳ Cooldown active for {key}: {remaining}s remaining")
        return False
    return True


def _record_execution(exchange: str, asset: str, side: str):
    """Record execution time for cooldown tracking."""
    _LAST_EXECUTION_AT[f"{exchange}:{asset}:{side}"] = time.time()

# ML Gate — loaded lazily on first use
_ml_model = None


def _get_ml_threshold() -> float:
    """Read ML threshold from DB, fallback to 0.80."""
    try:
        val = get_setting("ml_threshold")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return 0.80

# LLM Gate — always active second-opinion layer after ML
_LLM_GATE_TIMEOUT = 8         # seconds before fallback to ML decision
_LLM_API_KEY = os.getenv("DEEP_SEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENROUTER_API_KEY")
_LLM_MODEL = "deepseek-chat"  # fast, cheap model for gating
_LLM_API_URL = "https://api.deepseek.com/v1/chat/completions"


def _get_ml_model():
    """Lazy-load the signal gate ML model (singleton)."""
    global _ml_model
    if _ml_model is None:
        try:
            from trade.signal_agent.model import get_model
            _ml_model = get_model()
            _ml_model.load()
            if _ml_model.is_loaded:
                logger.info("[ML GATE] Model loaded successfully")
            else:
                logger.info("[ML GATE] No model file found — ML gate disabled")
        except Exception as e:
            logger.warning(f"[ML GATE] Failed to load model: {e}")
            _ml_model = None
    return _ml_model


def _evaluate_ml_gate(regime: str, obi: float, atr: float, entry_price: float,
                       side: str, stop_loss: float, take_profit: float,
                       candle_count: int) -> tuple:
    """
    Score a signal through the ML gate.
    Returns (ml_score, ml_decision, reject_reason_or_none).
    """
    model = _get_ml_model()
    if model is None or not model.is_loaded:
        return None, None, None

    try:
        from trade.signal_agent.features import extract_features_live, features_to_array
        features = extract_features_live(
            regime=regime, obi=obi, atr=atr, entry_price=entry_price,
            side=side, stop_loss=stop_loss, take_profit=take_profit,
            candle_count=candle_count,
        )
        X = features_to_array(features)
        decision, score = model.decide(X, threshold=_get_ml_threshold())
        if decision == "rejected":
            reason = f"ML gate rejected: Score {score:.3f} < threshold {_get_ml_threshold()}"
        else:
            reason = None
        return round(score, 4), decision, reason
    except Exception as e:
        logger.warning(f"[ML GATE] Evaluation failed: {e}")
        return None, None, None


def _evaluate_llm_gate(signal_summary: str, ml_score: float | None, exchange: str = "dex") -> tuple[str | None, str | None]:
    """
    LLM second-opinion gate — always active when API key is set.
    Called for every approved signal after ML gate.
    Prompt adapts thresholds and context based on exchange (dex vs cex).

    Returns (decision, reason) where decision is "approved", "rejected", or None on timeout/error.
    """
    if not _LLM_API_KEY:
        return None, None

    is_dex = (exchange == "dex")

    # ── Exchange-specific context ──────────────────────────────────
    if is_dex:
        exchange_context = (
            "=== EXCHANGE: ORDERLY DEX FUTURES ===\n"
            "• Leveraged futures with mandatory SL/TP (bracket orders)\n"
            "• LONG and SHORT both allowed — direction matters\n"
            "• Thinner on-chain liquidity → spreads 0.1-0.3% (rejected if >0.3%)\n"
            "• OBI thresholds (same as code):\n"
            "   OBI > 1.10 = bullish | OBI < 0.909 = bearish | 0.909-1.10 = neutral\n"
            "   OBI > 1.30 = extreme bullish | OBI < 0.77 = extreme bearish → note, don't reject\n"
            "   (OBI threshold is user-configurable via order_book_threshold setting)\n"
            "• Targets: DB default TP=0.5%, SL=0.3% (before ATR adjustment).\n"
            "   Swing mode (wider): TP=2.5%, SL=3.5% → R:R ~0.7:1 is normal\n"
            "   Actual TP/SL are ATR-adjusted — check the signal data for final values\n"
            "• If SL looks too tight vs ATR (SL distance < 2x ATR) → flag as LOW confidence\n"
            "• Daily limit: 1 trade/day — make it count, but don't be paralyzed\n"
        )
    else:
        exchange_context = (
            "=== EXCHANGE: BINANCE CEX SPOT ===\n"
            "• Spot only — no leverage, no liquidation risk, can hold indefinitely\n"
            "• BUY only (LONG) — SHORT signals are rejected upstream\n"
            "• Deep centralized liquidity → tighter spreads (~0.05-0.1%)\n"
            "• OBI thresholds (same code for both exchanges):\n"
            "   OBI > 1.10 = bullish | OBI < 0.909 = bearish | 0.909-1.10 = neutral\n"
            "   OBI > 1.30 = extreme bullish | OBI < 0.77 = extreme bearish → note, don't reject\n"
            "• Targets: DB default TP=0.5% (before adjustment). No SL on spot.\n"
            "   Actual TP is in the signal data — check it clears spread + fees\n"
            "• TP below 0.3% → note as LOW confidence (tight vs spread)\n"
            "• No daily trade limit on spot — more permissive\n"
            "• No SL means TP must be realistic; if TP is extremely far from entry, flag it\n"
        )

    try:
        import requests as _requests
        prompt = (
            "You are a quantitative crypto scalping analyst reviewing a live trade signal. "
            "The strategy detects several pattern types:\n"
            "- 2/3/4-candle reversal at S/R: consecutive candles in one direction + reversal candle\n"
            "- Bullish/Bearish engulfing at support/resistance\n"
            "- Pin bar / Hammer / Shooting star (wick rejection) at S/R\n"
            "- Spike reversal: outsized candle + OB confirming reversal\n\n"
            "Your job: confirm the signal has real edge, or flag a clear reason to skip it.\n\n"
            "=== SIGNAL DATA ===\n"
            f"{signal_summary}\n\n"
            f"{exchange_context}\n"
            "=== REGIME DETECTION CONTEXT ===\n"
            "Regime is determined by linear regression slope over 5m and 1h candles:\n"
            "- RANGE: |slope| < 0.12%/candle on 5m AND < 0.14%/candle on 1h (flat/sideways)\n"
            "- TREND_UP: positive slope exceeding threshold + volume ≥ 120% avg\n"
            "- TREND_DOWN: negative slope exceeding threshold + volume ≥ 120% avg\n"
            "The signal_summary reports which regime was detected.\n\n"
            "=== ML MODEL CONTEXT ===\n"
            f"XGBoost score: {ml_score:.3f} (threshold: {_get_ml_threshold()}, trained on historical NEAR reversals)\n\n"
            "=== DECISION FRAMEWORK ===\n\n"
            "1. PATTERN VALIDATION — quick sanity-check:\n"
            "   Verify the detected pattern against its definition:\n"
            "   • Reversal: Do candles actually reverse at a logical S/R level?\n"
            "   • Engulfing: Does the engulfing candle body truly exceed the prior candle's body?\n"
            "   • Pin bar: Is the wick ≥ 2x the body and at a price extreme?\n"
            "   • Spike: Is the spike candle range ≥ 1.3x average AND volume ≥ 3x average?\n"
            "   • 'Mid-range' = price is between 25%-75% of the 20-period range — informational only\n"
            "   • 'Nearby S/R' = within 1.5% of a swing pivot. Missing S/R — informational only\n"
            "   • Only REJECT if the pattern is clearly misidentified (e.g., 'engulfing' where body is smaller)\n\n"
            "2. REGIME + OBI — guide confidence, not rejection:\n"
            "   Use the exchange-specific OBI thresholds above.\n"
            "   • RANGE + OBI supporting → ideal conditions, HIGH confidence\n"
            "   • TREND with the trade (pullback) → APPROVE, HIGH confidence\n"
            "   • TREND against the trade (counter-trend) → APPROVE with MEDIUM confidence\n"
            "   • Counter-trend + OBI strongly supporting → upgrade to HIGH confidence\n"
            "   • OBI neutral in any regime → no adjustment needed\n"
            "   • REGIME + OBI should guide your confidence level, NOT trigger rejection\n\n"
            "3. HIGHER TIMEFRAME (HTF) — the main rejection trigger:\n"
            "   Timeframe priority: 1d > 4h > 1h. Daily carries most weight.\n"
            "   • Price at 20-day HIGH + LONG signal → REJECT (buying the top)\n"
            "   • Price at 20-day LOW + SHORT signal → REJECT (shorting the bottom)\n"
            "   • Single TF disagreeing (e.g., 1d opposes, 4h/1h agree) → LOW confidence, but still APPROVE\n"
            "   • If HTF data is missing → skip this check completely\n"
            "   • ONLY reject on HTF when both 4h AND 1d clearly oppose the direction\n\n"
            "4. RISK/REWARD (R:R) — informational, almost never reject alone:\n"
            "   Use the exchange-specific R:R context above.\n"
            "   • Check that TP clears spread + fees (see exchange context for thresholds)\n"
            "   • R:R should NEVER be the sole reason to reject — only combine with HTF rejection\n"
            "   • If R:R is not computable (missing SL/TP), skip this check\n\n"
            "5. MISSING DATA — always default to ALLOW:\n"
            "   • Any missing field → treat that factor as NEUTRAL, not as a strike against the trade\n"
            "   • Never reject solely because data is missing\n\n"
            "6. OVERRIDING PRINCIPLE — DEFAULT TO APPROVE:\n"
            "   This is a scalping strategy. A missed winning trade costs more than a small loss.\n"
            "   Your job: catch the 1-2 OBVIOUSLY bad trades per day, not block everything questionable.\n"
            "   • REJECT only when: HTF clearly opposes (both 4h+1d) OR pattern is clearly misidentified\n"
            "   • For everything else: APPROVE and use confidence level to express doubt\n"
            "   • If you're unsure after 15 seconds of thinking → APPROVE with MEDIUM confidence\n"
            "   • The ML model and HTF filter already blocked the worst candidates before reaching you\n\n"
            "Reply with ONLY valid JSON (no markdown, no code blocks, no extra text):\n"
            '{"decision":"approved","confidence":"high|medium|low","reason":"<brief assessment, mention the strongest factor>"}\n'
            'or (RARELY — only for clear HTF contradiction or pattern misidentification)\n'
            '{"decision":"rejected","confidence":"high","reason":"<which specific factor clearly invalidates this trade>"}'
        )
        headers = {
            "Authorization": f"Bearer {_LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": _LLM_MODEL,
            "temperature": 0.1,
            "max_tokens": 350,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        r = _requests.post(_LLM_API_URL, headers=headers, json=body, timeout=_LLM_GATE_TIMEOUT)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]

        # Parse JSON
        import json as _json
        result = _json.loads(content)
        decision = result.get("decision", "").lower()
        reason = result.get("reason", "LLM gate decision")
        confidence = result.get("confidence", "medium")
        if decision in ("approved", "rejected"):
            logger.info(f"[LLM GATE] Decision: {decision} ({confidence}) — {reason}")
            # Embed confidence in reason for display
            full_reason = f"[{confidence.upper()}] {reason}"
            return decision, full_reason
        return None, None
    except Exception as e:
        logger.warning(f"[LLM GATE] Evaluation failed (falling through): {e}")
        return None, None

_LAST_SIGNAL_ALERTS: Dict[str, float] = {}
_LAST_EXCHANGE_SCAN_AT: Dict[str, float] = {"dex": 0.0, "cex": 0.0}
_LAST_LABELER_RUN_AT: float = 0.0
_LABELER_INTERVAL = 7200  # run outcome labeler every 2 hours


def _get_exchange_mode(exchange: str) -> str:
    """Return mode for exchange.

    Modes: False, Signal, Automatic.
    """
    key = "auto_trade_dex" if exchange == "dex" else "auto_trade_cex"
    mode = get_setting(key)
    if mode in ("False", "Signal", "Automatic", "True"):
        return "Automatic" if mode == "True" else mode
    return "False"


def _should_send_signal_alert(exchange: str, asset: str, side: str, signal_reason: str, cooldown_seconds: int = 300) -> bool:
    """Debounce repeated signal alerts across autotrade polling cycles."""
    now = time.time()
    key = f"{exchange}:{asset}:{side}:{signal_reason}"
    last_sent_at = _LAST_SIGNAL_ALERTS.get(key, 0)
    if now - last_sent_at < cooldown_seconds:
        return False
    _LAST_SIGNAL_ALERTS[key] = now
    return True


def _should_run_exchange_cycle(exchange: str, interval_seconds: int) -> bool:
    """Throttle exchange analysis loops independently."""
    now = time.time()
    last_run_at = _LAST_EXCHANGE_SCAN_AT.get(exchange, 0)
    if now - last_run_at < interval_seconds:
        return False
    _LAST_EXCHANGE_SCAN_AT[exchange] = now
    return True


def _should_run_labeler() -> bool:
    """Throttle outcome labeler to run every _LABELER_INTERVAL seconds."""
    global _LAST_LABELER_RUN_AT
    now = time.time()
    if now - _LAST_LABELER_RUN_AT < _LABELER_INTERVAL:
        return False
    _LAST_LABELER_RUN_AT = now
    return True


def _run_labeler_background():
    """Run the signal outcome labeler in a background thread."""
    import threading
    def _label():
        try:
            from trade.signal_agent.labeler import label_signals
            updated = label_signals(dry_run=False)
            try:
                from trading_bot.send_bot_message import send_bot_message
                chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
                if chat_id:
                    if updated:
                        send_bot_message(chat_id, f"🗄️ Database updated: {updated} signals labeled with real trade outcomes")
                    else:
                        # Inform user that labeler ran but found nothing new
                        logger.info("[LABELER] Run completed — no new signals to label")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[LABELER] Background run failed: {e}")
    t = threading.Thread(target=_label, daemon=True, name="labeler-bg")
    t.start()


class ReversalScalper:
    """
    Hard-coded reversal strategy with regime filter, order book imbalance,
    and manipulation detection.
    
    This class implements a rule-based trading system that:
    1. Avoids trending markets using linear regression slope detection
    2. Requires order book imbalance confirmation for each trade
    3. Detects specific 2-candle reversal patterns at support/resistance
    4. Flags potential manipulation signals (volume spikes, OB divergence, spread)
    5. Uses multi-core processing for fast data fetching
    
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
        self.SL_ATR_MULTIPLIER = 2.0    # SL at 2.0x ATR (gives corrections room to breathe)
        self.TP_ATR_MULTIPLIER = 2.0    # TP at 2.0x ATR (R:R ~1:1 before structure adjustment)
        
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

        # === DEX SWING PARAMETERS (wider targets to overcome DEX spread) ===
        # On Orderly, spreads are 0.1-0.3% — a 0.3% TP is eaten by spread.
        # Swing mode uses higher TF (4h/1d) patterns with wider targets.
        self.DEX_SWING_TP_PCT = 0.025      # 2.5% take profit (clears 0.3% spread with room)
        self.DEX_SWING_SL_PCT = 0.035      # 3.5% stop loss (R:R ~0.7:1 — acceptable for 4h swings)
        self.DEX_SWING_MIN_SPREAD = 0.003  # 0.3% max spread — reject if wider
        self.DEX_SWING_SL_ATR_MULT = 3.0   # Wider ATR multiplier for swing SL
        self.DEX_SWING_TP_ATR_MULT = 2.5   # ATR multiplier for swing TP

        # === SPREAD GATE (DEX only) ===
        self.DEX_MAX_SPREAD = 0.003  # Reject DEX trades if spread > 0.3%

        # === CEX SMART ENTRY GATES (no SL = must be conservative) ===
        # CEX spot has no stop-loss — the cost of a bad entry is unlimited.
        # These 4 gates emulate what a disciplined human trader asks before entering:
        #
        # Gate 1: "Did I just close a position?" → wait before re-entering
        self.CEX_POST_EXIT_COOLDOWN_MINUTES = 15
        # Gate 2: "Am I entering at nearly the same price as my last trade?"
        self.CEX_SAME_PRICE_ZONE_PCT = 0.005        # 0.5% = same zone
        self.CEX_SAME_PRICE_ZONE_MINUTES = 30        # within 30 minutes
        # Gate 3: "How many trades have I made today?"
        self.CEX_MAX_TRADES_PER_DAY = 20
        # Gate 4: "Is the macro trend against me?" (1d + 4h both bearish = stay out)
        self.CEX_MAX_1D_BEARISH_SLOPE = -0.001       # -0.1%/day
        self.CEX_MAX_4H_BEARISH_SLOPE = -0.002       # -0.2%/4h candle
        self.CEX_MIN_BOUNCE_RETRACEMENT = 0.25       # Bounce must retrace ≥25% of drop
        self.CEX_RECENT_DROP_LOOKBACK = 50            # Candles for drop measurement
        self.CEX_MAX_CONSECUTIVE_LOSSES = 3           # Stop after N consecutive losses
        self.CEX_AVERAGING_DOWN_ENTRIES = 3           # Flag if last N entries are lower

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
    
    def _calculate_structural_sl(self, df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
        """
        Calculate stop-loss based on market structure (swing low/high).
        
        For LONG trades: SL = recent swing low - 0.5x ATR buffer
        For SHORT trades: SL = recent swing high + 0.5x ATR buffer
        
        This places SL beyond the structural level that formed the reversal pattern.
        If the swing point breaks, the reversal thesis is genuinely invalidated.
        
        Falls back to ATR-based SL if no clear swing point is found.
        """
        if len(df) < 10 or atr <= 0:
            return 0.0
        
        highs = df['high'].values
        lows = df['low'].values
        
        # Look for swing points in recent candles (exclude current forming candle)
        lookback = min(20, len(df) - 1)
        
        if side == 'BUY':
            # Find the lowest low in the recent window (the swing low that formed the reversal)
            recent_lows = lows[-lookback:-1]  # exclude current candle
            if len(recent_lows) < 3:
                return 0.0
            swing_low = np.min(recent_lows)
            # Add 0.5x ATR buffer BELOW the swing low
            structural_sl = swing_low - (atr * 0.5)
            # But never tighter than 1.5x ATR from entry
            min_sl = entry - (atr * 1.5)
            return min(structural_sl, min_sl)
        else:  # SELL
            # Find the highest high in the recent window (the swing high that formed the reversal)
            recent_highs = highs[-lookback:-1]  # exclude current candle
            if len(recent_highs) < 3:
                return 0.0
            swing_high = np.max(recent_highs)
            # Add 0.5x ATR buffer ABOVE the swing high
            structural_sl = swing_high + (atr * 0.5)
            # But never tighter than 1.5x ATR from entry
            min_sl = entry + (atr * 1.5)
            return max(structural_sl, min_sl)
    
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
    
    def _format_obi_display(self, obi: float, obi_details: Dict, live_price: float, orderbook: Dict, market_trades: List = None, exchange: str = "dex") -> str:
        """Format order book info for Telegram (NO markdown)."""
        direction = "🟢 BULLISH" if obi > 1.0 else "🔴 BEARISH" if obi < 1.0 else "⚪ NEUTRAL"
        leverage = 1 if exchange == "cex" else int(get_setting("leverage") or 5)
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
        ]
        if exchange == "cex":
            lines.append(f"• 🟢 No-impact max: {zero_impact_notional:.0f} USDC (spot, no leverage)")
            lines.append(f"• 💰 Safe max: {max_notional:.0f} USDC (spot, no leverage)")
        else:
            lines.append(f"• 🟢 No-impact max: {zero_impact_notional:.0f} USDC ({zero_impact_margin:.0f} margin @ {leverage}x)")
            lines.append(f"• 💰 Safe max: {max_notional:.0f} USDC ({max_margin:.0f} margin @ {leverage}x)")
        lines.append(f"• 📈 {trade_label}")
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
    
    def _check_cex_smart_entry(
        self, df_5m, df_4h, df_1d, live_price: float, side: str,
        regime: str
    ) -> tuple[bool, list[str]]:
        """
        Human-like intelligence for CEX spot entries (no SL = must be conservative).

        A smart human asks 4 questions before buying spot with no stop-loss:
        1. Is the macro trend against me? (1d/4h bearish → don't buy)
        2. Is this bounce real or a dead cat? (weak bounce in downtrend = trap)
        3. Have I been losing recently? (3+ consecutive losses → stop and reassess)
        4. Am I averaging down? (each entry lower than the last = catching knife)

        Returns (should_enter: bool, warnings: list[str]).
        On CEX spot, the default answer is NO unless conditions are clearly favorable.
        """
        if side != 'BUY':
            return True, []  # Only apply to LONG entries (CEX is long-only anyway)

        warnings = []

        # ── 1. MACRO TREND CHECK: Don't buy in sustained downtrend ──
        slope_1d = 0.0
        slope_4h = 0.0
        if df_1d is not None and len(df_1d) >= 20:
            slope_1d = self._calculate_normalized_slope(df_1d['close'].values, min(20, len(df_1d)))
        if df_4h is not None and len(df_4h) >= 20:
            slope_4h = self._calculate_normalized_slope(df_4h['close'].values, min(20, len(df_4h)))

        # Both 1d AND 4h bearish → sustained downtrend. A smart human stays out.
        if slope_1d < self.CEX_MAX_1D_BEARISH_SLOPE and slope_4h < self.CEX_MAX_4H_BEARISH_SLOPE:
            warnings.append(
                f"🛑 Sustained downtrend: 1d slope={slope_1d*100:.3f}%/day, "
                f"4h slope={slope_4h*100:.3f}%/candle — buying here is catching a falling knife"
            )
            return False, warnings

        # 1d alone strongly bearish → high risk, flag but don't block alone
        if slope_1d < self.CEX_MAX_1D_BEARISH_SLOPE:
            warnings.append(
                f"⚠️ Macro headwind: 1d slope={slope_1d*100:.3f}%/day — "
                f"this bounce may be a dead cat, not a reversal"
            )

        # ── 2. BOUNCE QUALITY: Is this a real reversal or a trap? ──
        if df_5m is not None and len(df_5m) >= self.CEX_RECENT_DROP_LOOKBACK:
            closes = df_5m['close'].values
            lookback = min(self.CEX_RECENT_DROP_LOOKBACK, len(closes))
            recent = closes[-lookback:]
            recent_low = np.min(recent)
            recent_high_before_low = np.max(recent[:-5])  # high before the most recent candles

            if recent_low > 0 and recent_high_before_low > recent_low:
                drop_pct = (recent_high_before_low - recent_low) / recent_high_before_low
                bounce_pct = (live_price - recent_low) / recent_low

                # Significant drop (>2%) with weak bounce (<25% retracement) = likely dead cat
                if drop_pct > 0.02 and bounce_pct < drop_pct * self.CEX_MIN_BOUNCE_RETRACEMENT:
                    warnings.append(
                        f"🛑 Weak bounce: price retraced only {bounce_pct*100:.1f}% of the "
                        f"{drop_pct*100:.1f}% drop — classic dead cat, wait for confirmation"
                    )
                    return False, warnings

                # Moderate bounce — flag as questionable
                if drop_pct > 0.015 and bounce_pct < drop_pct * 0.5:
                    warnings.append(
                        f"⚠️ Questionable bounce: only {bounce_pct*100:.1f}% retracement of "
                        f"{drop_pct*100:.1f}% drop — this may not be the bottom"
                    )

        # ── 3. RECENT LOSS STREAK: Stop after consecutive losses ──
        consecutive_losses = self._count_consecutive_cex_losses()
        if consecutive_losses >= self.CEX_MAX_CONSECUTIVE_LOSSES:
            warnings.append(
                f"🛑 Loss streak: {consecutive_losses} consecutive CEX losses — "
                f"a smart human stops to reassess, so should the bot"
            )
            return False, warnings
        elif consecutive_losses >= 2:
            warnings.append(
                f"⚠️ Caution: {consecutive_losses} consecutive CEX losses — "
                f"consider waiting for stronger confirmation"
            )

        # ── 4. AVERAGING DOWN DETECTION: Each entry lower = catching knife ──
        if self._is_averaging_down_cex(live_price):
            warnings.append(
                f"🛑 Averaging down: last {self.CEX_AVERAGING_DOWN_ENTRIES} CEX entries "
                f"at progressively lower prices — this is catching a falling knife"
            )
            return False, warnings

        return True, warnings

    # ── CEX Gate Helpers ────────────────────────────────────────────────

    def _count_consecutive_cex_losses(self) -> int:
        """Count how many of the most recent CEX trades were losses (consecutive)."""
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT trade_outcome FROM signal_history "
                    "WHERE exchange='cex' AND approved=1 AND trade_outcome IS NOT NULL "
                    "ORDER BY id DESC LIMIT 10"
                )
                rows = cur.fetchall()
                count = 0
                for r in rows:
                    if r['trade_outcome'] == 'loss':
                        count += 1
                    else:
                        break
                return count
        except Exception:
            return 0

    def _is_averaging_down_cex(self, current_price: float) -> bool:
        """Check if recent CEX entries are at progressively lower prices."""
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT entry_price FROM signal_history "
                    "WHERE exchange='cex' AND approved=1 AND entry_price IS NOT NULL "
                    "ORDER BY id DESC LIMIT ?",
                    (self.CEX_AVERAGING_DOWN_ENTRIES,)
                )
                rows = cur.fetchall()
                if len(rows) < self.CEX_AVERAGING_DOWN_ENTRIES:
                    return False
                prices = [r['entry_price'] for r in rows]
                for i in range(len(prices) - 1):
                    if prices[i] >= prices[i + 1]:
                        return False
                if current_price >= min(prices):
                    return False
                return True
        except Exception:
            return False

    def _minutes_since_last_cex_exit(self) -> float | None:
        """
        Gate 1 helper: minutes since the most recent CEX SELL (exit).
        Returns None if no exit found.
        """
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                # Last approved CEX BUY that has a trade_outcome (was closed)
                cur.execute(
                    "SELECT timestamp FROM signal_history "
                    "WHERE exchange='cex' AND approved=1 AND trade_outcome IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                ts_str = row['timestamp']
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(str(ts_str).replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                return (now - ts).total_seconds() / 60.0
        except Exception:
            return None

    def _last_cex_entry_info(self) -> dict | None:
        """
        Gate 2 helper: price and age of the most recent approved CEX BUY.
        Returns None if no prior entry found.
        """
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT entry_price, timestamp FROM signal_history "
                    "WHERE exchange='cex' AND approved=1 AND entry_price IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row or row['entry_price'] is None:
                    return None
                from datetime import datetime, timezone
                ts_str = str(row['timestamp']).replace('Z', '+00:00')
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                return {
                    'price': float(row['entry_price']),
                    'minutes_ago': (now - ts).total_seconds() / 60.0,
                }
        except Exception:
            return None

    def _count_cex_trades_today(self) -> int:
        """
        Gate 3 helper: count approved CEX entries placed today (UTC).
        """
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM signal_history "
                    "WHERE exchange='cex' AND approved=1 "
                    "AND date(timestamp) = date('now')"
                )
                row = cur.fetchone()
                return int(row['cnt']) if row else 0
        except Exception:
            return 0

    def _check_htf_exhaustion(self, df_4h, df_1d, live_price: float, side: str) -> list[str]:
        """
        Check higher timeframe (4h, 1d) for exhaustion signals that would
        warn against entering a trade in the given direction.

        Returns list of warning strings (empty if clean).
        """
        warnings = []
        if live_price <= 0:
            return warnings

        # --- 1d slope check ---
        if df_1d is not None and len(df_1d) >= 20:
            slope_1d = self._calculate_normalized_slope(df_1d['close'].values, min(20, len(df_1d)))
            # Going LONG into a 1d downtrend → exhaustion risk
            if side == 'BUY' and slope_1d < -0.0005:  # -0.05%/day
                warnings.append(f"⚠️ LONG vs 1d downtrend (slope={slope_1d*100:.3f}%/day)")
            # Going SHORT into a 1d uptrend → exhaustion risk
            elif side == 'SELL' and slope_1d > 0.0005:
                warnings.append(f"⚠️ SHORT vs 1d uptrend (slope={slope_1d*100:.3f}%/day)")

            # 1d swing high/low proximity
            if len(df_1d) >= 30:
                d_highs = df_1d['high'].values
                d_lows = df_1d['low'].values
                d_high_20 = np.max(d_highs[-20:])
                d_low_20 = np.min(d_lows[-20:])
                if side == 'BUY' and live_price >= d_high_20 * 0.985:
                    warnings.append(f"⚠️ LONG near 1d resistance (within 1.5% of 20-day high)")
                elif side == 'SELL' and live_price <= d_low_20 * 1.015:
                    warnings.append(f"⚠️ SHORT near 1d support (within 1.5% of 20-day low)")

        # --- 4h overextension check ---
        if df_4h is not None and len(df_4h) >= 20:
            closes_4h = df_4h['close'].values
            ma_4h = np.mean(closes_4h[-20:])
            if ma_4h > 0:
                deviation = (live_price - ma_4h) / ma_4h
                # Price >4% above 4h MA → overbought, risky to go LONG
                if side == 'BUY' and deviation > 0.04:
                    warnings.append(f"⚠️ Price {deviation*100:.1f}% above 4h MA — overbought for LONG")
                # Price >4% below 4h MA → oversold, risky to go SHORT
                elif side == 'SELL' and deviation < -0.04:
                    warnings.append(f"⚠️ Price {abs(deviation)*100:.1f}% below 4h MA — oversold for SHORT")

            # 4h slope check
            slope_4h = self._calculate_normalized_slope(closes_4h, min(20, len(df_4h)))
            if side == 'BUY' and slope_4h < -0.001:  # -0.1%/4h candle
                warnings.append(f"⚠️ LONG vs 4h downtrend (slope={slope_4h*100:.3f}%/candle)")
            elif side == 'SELL' and slope_4h > 0.001:
                warnings.append(f"⚠️ SHORT vs 4h uptrend (slope={slope_4h*100:.3f}%/candle)")

        return warnings

    def _log_round_summary(self, exchange: str, asset: str, price: float, regime: str,
                           consecutive_up: int, consecutive_down: int,
                           approved: bool, reasons: list, ml_score, ml_decision):
        """Log ONE line per analysis round so logs show what happened every cycle."""
        exch = exchange.upper()
        cons = f"up={consecutive_up} down={consecutive_down}"
        ml_str = ""
        if ml_score is not None:
            ml_str = f" | ML: {ml_score:.3f} ({ml_decision})"
        if approved:
            logger.info(f"📊 [{exch}] {asset} @ {price:.6f} | {regime} | {cons} | ✅ APPROVED{ml_str}")
        else:
            reason_str = ', '.join(reasons) if reasons else 'unknown'
            logger.info(f"📊 [{exch}] {asset} @ {price:.6f} | {regime} | {cons} | ❌ REJECTED: {reason_str}{ml_str}")

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
        with ThreadPoolExecutor(max_workers=min(7, self.MAX_WORKERS)) as pool:
            if exchange == "cex":
                f_df_5m = pool.submit(get_historical_data_binance, symbol=asset, interval='5m', limit=100)
                f_df_1h = pool.submit(get_historical_data_binance, symbol=asset, interval='1h', limit=100)
                f_df_4h = pool.submit(get_historical_data_binance, symbol=asset, interval='4h', limit=100)
                f_df_1d = pool.submit(get_historical_data_binance, symbol=asset, interval='1d', limit=100)
                f_live = pool.submit(get_binance_price, asset)
                f_orderbook = pool.submit(get_orderbook_binance, asset, self.OB_DEPTH)
                f_trades = pool.submit(get_binance_market_trades, asset, 50)
            else:
                f_df_5m = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='5m', limit=100)
                f_df_1h = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='1h', limit=100)
                f_df_4h = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='4h', limit=100)
                f_df_1d = pool.submit(get_historical_data_limit_apolo, symbol=asset, interval='1d', limit=100)
                f_live = pool.submit(get_close_price, ORDERLY_ACCOUNT_ID, asset, interval)
                f_orderbook = pool.submit(get_orderbook, asset, self.OB_DEPTH)
                f_trades = pool.submit(get_market_trades, asset, 50)
            try:
                df_5m = f_df_5m.result(timeout=15)
                df_1h = f_df_1h.result(timeout=15)
                df_4h = f_df_4h.result(timeout=15)
                df_1d = f_df_1d.result(timeout=15)
                live_price = f_live.result(timeout=15)
                orderbook = f_orderbook.result(timeout=15)
                market_trades = f_trades.result(timeout=15)
            except TimeoutError:
                logger.error(f"[{exchange}] API timeout fetching data for {asset} — skipping cycle")
                result['resume_of_analysis'] = "❌ Error: API timeout"
                result['rejection_reasons'].append("API timeout")
                return result
        
        if df_5m is None or len(df_5m) < 30:
            result['resume_of_analysis'] = "❌ Error: Insufficient 5m data"
            result['rejection_reasons'].append("Insufficient 5m data")
            self._log_round_summary(exchange, asset, 0, 'N/A', 0, 0, False, result['rejection_reasons'], None, None)
            return result
        last_close = df_5m['close'].iloc[-1]
        if live_price is None:
            result['resume_of_analysis'] = "❌ Error: Could not fetch live price"
            result['rejection_reasons'].append("No live price")
            self._log_round_summary(exchange, asset, last_close, 'N/A', 0, 0, False, result['rejection_reasons'], None, None)
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

        # === STEP 5.5: CHECK HIGHER TIMEFRAME EXHAUSTION (4h, 1d) ===
        # Determine tentative side for HTF check
        tentative_side = None
        if pattern:
            tentative_side = pattern['side']
        elif spike_reversal:
            tentative_side = spike_reversal['side']
        elif engulfing:
            tentative_side = engulfing['side']
        elif pinbar:
            tentative_side = pinbar['side']

        htf_warnings = []
        if tentative_side:
            htf_warnings = self._check_htf_exhaustion(df_4h, df_1d, live_price, tentative_side)
        result['debug_info']['htf_warnings'] = htf_warnings
        # === STEP 6: BUILD DEBUG DISPLAY ===
        regime = regime_info['regime']
        exchange_label = "💱 CEX (Binance Spot)" if exchange == "cex" else "🌐 DEX (Orderly Futures)"
        display_symbol = get_binance_symbol(asset) if exchange == "cex" else asset
        display_lines = [
            f"📊 {display_symbol} | {interval} | Price: {live_price:.6f} | {exchange_label}",
            "",
            self._format_obi_display(obi, obi_details, live_price, orderbook, market_trades, exchange=exchange),
            "",
            self._format_regime_display(regime_info),
            "",
        ]
        
        if exchange != "cex":
            atr_sl_pct = max(atr * self.SL_ATR_MULTIPLIER / live_price, self.SL_PCT_MIN) if atr > 0 else self.SL_PCT_MIN
            atr_tp_pct = max(atr * self.TP_ATR_MULTIPLIER / live_price, self.TP_PCT) if atr > 0 else self.TP_PCT
            display_lines.append(
                f"📏 Volatility (ATR 14): {atr:.6f} ({(atr/live_price)*100:.3f}%)\n"
                f"• SL floor: {live_price - live_price*atr_sl_pct:.6f} (-{atr_sl_pct*100:.2f}%) [structure-based if signal found]\n"
                f"• TP target: {live_price + live_price*atr_tp_pct:.6f} (+{atr_tp_pct*100:.2f}%)"
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

        # Add HTF exhaustion warnings if any
        if htf_warnings:
            display_lines.append("📅 Higher Timeframe Check (4h/1d):")
            for w in htf_warnings:
                display_lines.append(f"• {w}")
            display_lines.append("")
        
        # === STEP 7: APPLY FILTERS ===
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
            self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                    False, result['rejection_reasons'], None, None)
            return result
        
        # === DEX SPREAD GATE: reject if spread is too wide for swing trading ===
        if exchange == "dex" and orderbook.get('bids') and orderbook.get('asks'):
            try:
                best_bid = float(orderbook['bids'][0][0])
                best_ask = float(orderbook['asks'][0][0])
                if best_bid > 0:
                    spread = (best_ask - best_bid) / best_bid
                    if spread > self.DEX_MAX_SPREAD:
                        result['rejection_reasons'].append(f"DEX spread {spread*100:.2f}% > {self.DEX_MAX_SPREAD*100:.1f}%")
                        display_lines.append(f"🚫 ❌ DEX spread {spread*100:.2f}% too wide (max {self.DEX_MAX_SPREAD*100:.1f}%) — swing blocked")
                        result['resume_of_analysis'] = "\n".join(display_lines)
                        consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
                        save_signal_to_history(
                            asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                            approved=False, rejection_reasons=result['rejection_reasons'],
                            manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                            candle_count=max(consecutive_up, consecutive_down)
                        )
                        self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                                False, result['rejection_reasons'], None, None)
                        return result
            except (TypeError, ValueError, IndexError):
                pass
        
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

        # === HTF EXHAUSTION FILTER: block if ≥2 HTF warnings (strong multi-timeframe disagreement) ===
        if len(htf_warnings) >= 2:
            result['rejection_reasons'].append(f"HTF exhaustion: {', '.join(htf_warnings)}")
            display_lines.append(f"🚫 ❌ Higher timeframe exhaustion ({len(htf_warnings)} warnings) — reversal blocked")
            result['resume_of_analysis'] = "\n".join(display_lines)

            consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                    False, result['rejection_reasons'], None, None)
            return result
        
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

        # === ML GATE: run for ANY signal with a pattern (not just approved) ===
        consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
        ml_score, ml_decision = None, None
        if active_signal is not None:
            side = active_signal['side']
            entry = live_price
            # Compute tentative SL/TP for ML features
            if atr > 0:
                if side == 'BUY':
                    tentative_sl = entry - (atr * self.SL_ATR_MULTIPLIER)
                    tentative_tp = entry + (atr * self.TP_ATR_MULTIPLIER)
                else:
                    tentative_sl = entry + (atr * self.SL_ATR_MULTIPLIER)
                    tentative_tp = entry - (atr * self.TP_ATR_MULTIPLIER)
            else:
                tentative_sl = entry * (1 - self.SL_PCT_MIN) if side == 'BUY' else entry * (1 + self.SL_PCT_MIN)
                tentative_tp = entry * (1 + self.TP_PCT) if side == 'BUY' else entry * (1 - self.TP_PCT)
            ml_score, ml_decision, _ = _evaluate_ml_gate(
                regime=regime, obi=obi, atr=atr, entry_price=entry,
                side=side, stop_loss=tentative_sl, take_profit=tentative_tp,
                candle_count=max(consecutive_up, consecutive_down),
            )
        result['ml_score'] = ml_score
        result['ml_decision'] = ml_decision
        
        if active_signal is None:
            result['rejection_reasons'].append("No valid pattern")
            display_lines.append("❌ No valid reversal pattern at this time")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            # === LOG REJECTED SIGNAL ===
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=None,
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                    False, result['rejection_reasons'], ml_score, ml_decision)
            return result
        
        # === CEX SPOT: Only BUY signals allowed (long-only, no shorting on spot) ===
        if exchange == "cex" and active_signal['side'] == 'SELL':
            result['rejection_reasons'].append("CEX spot: only BUY signals (long-only)")
            display_lines.append("❌ SHORT signal rejected — Binance spot is long-only (BUY only)")
            result['resume_of_analysis'] = "\n".join(display_lines)
            
            save_signal_to_history(
                asset=asset, exchange=exchange, regime=regime, obi=obi, pattern_type=active_signal.get('reason'),
                approved=False, rejection_reasons=result['rejection_reasons'],
                manipulation_warnings=manipulation_warnings, atr=atr, live_price=live_price,
                candle_count=max(consecutive_up, consecutive_down)
            )
            self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                    False, result['rejection_reasons'], ml_score, ml_decision)
            return result

        # === CEX SMART ENTRY GATE: 4 human-like checks before entering spot ===
        # No SL on CEX spot → the cost of a wrong entry is unlimited.
        # Gate 1: Did I just close? → 15min post-exit cooldown
        # Gate 2: Same price as last entry? → don't double down at same level
        # Gate 3: Traded too much today? → max 5 CEX trades/day
        # Gate 4: Macro trend against me? → don't buy in sustained downtrend
        if exchange == "cex" and active_signal['side'] == 'BUY':
            should_enter, cex_warnings = self._check_cex_smart_entry(
                df_5m=df_5m, df_4h=df_4h, df_1d=df_1d,
                live_price=live_price, side=active_signal['side'], regime=regime
            )
            result['debug_info']['cex_smart_warnings'] = cex_warnings
            if cex_warnings:
                display_lines.append("")
                display_lines.append("🧠 CEX Smart Entry Check:")
                for w in cex_warnings:
                    display_lines.append(f"• {w}")
            if not should_enter:
                result['rejection_reasons'].extend(cex_warnings)
                result['resume_of_analysis'] = "\n".join(display_lines)
                save_signal_to_history(
                    asset=asset, exchange=exchange, regime=regime, obi=obi,
                    pattern_type=active_signal.get('reason'),
                    approved=False, side=active_signal['side'],
                    entry_price=live_price, stop_loss=None, take_profit=None,
                    rejection_reasons=result['rejection_reasons'],
                    manipulation_warnings=manipulation_warnings, atr=atr,
                    live_price=live_price, candle_count=max(consecutive_up, consecutive_down),
                    ml_score=ml_score, ml_decision=ml_decision,
                )
                self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                        False, result['rejection_reasons'], ml_score, ml_decision)
                return result
        
        # OBI is reference only — counter-trend OBI logged as warning, not a blocker
        # In thin markets like Orderly, OBI is too volatile to use as hard filter
        side = active_signal['side']
        if side == 'BUY' and obi < 1.0:
            display_lines.append(f"📚 ℹ️ OBI {obi:.2f} (bearish book, thin market—reference only)")
        elif side == 'SELL' and obi > 1.0:
            display_lines.append(f"📚 ℹ️ OBI {obi:.2f} (bullish book, thin market—reference only)")

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
        elif exchange == "dex":
            # === DEX SWING MODE: wider TP/SL to overcome DEX spread ===
            # Uses 4h ATR for volatility-adaptive swing sizing
            swing_atr = self._calculate_atr(df_4h, period=14) if df_4h is not None and len(df_4h) >= 14 else atr
            if swing_atr > 0:
                # Structure-based SL with wider ATR multiplier
                structural_sl = self._calculate_structural_sl(df_4h if df_4h is not None and len(df_4h) >= 10 else df_5m, side, entry, swing_atr)
                if structural_sl > 0:
                    sl = structural_sl
                    sl_source = 'structure'
                else:
                    if side == 'BUY':
                        sl = entry - (swing_atr * self.DEX_SWING_SL_ATR_MULT)
                    else:
                        sl = entry + (swing_atr * self.DEX_SWING_SL_ATR_MULT)
                    sl_source = 'atr'
                # Floor: SL must be at least DEX_SWING_SL_PCT from entry
                sl_pct = abs(sl - entry) / entry
                if sl_pct < self.DEX_SWING_SL_PCT:
                    sl_pct = self.DEX_SWING_SL_PCT
                    sl = entry * (1 - sl_pct) if side == 'BUY' else entry * (1 + sl_pct)
                    sl_source = 'swing_floor'
                # TP: wider to clear spread
                if side == 'BUY':
                    tp = entry + (swing_atr * self.DEX_SWING_TP_ATR_MULT)
                else:
                    tp = entry - (swing_atr * self.DEX_SWING_TP_ATR_MULT)
                tp_pct = abs(tp - entry) / entry
                tp_pct = max(tp_pct, self.DEX_SWING_TP_PCT)
                tp = entry * (1 + tp_pct) if side == 'BUY' else entry * (1 - tp_pct)
                atr_based = True
            else:
                # Fallback: fixed swing percentages
                tp = entry * (1 + self.DEX_SWING_TP_PCT) if side == 'BUY' else entry * (1 - self.DEX_SWING_TP_PCT)
                sl = entry * (1 - self.DEX_SWING_SL_PCT) if side == 'BUY' else entry * (1 + self.DEX_SWING_SL_PCT)
                atr_based = False
                sl_source = 'swing_fixed'
        elif atr > 0:
            # === STRUCTURE-BASED SL (primary): place SL beyond recent swing point ===
            structural_sl = self._calculate_structural_sl(df_5m, side, entry, atr)
            if structural_sl > 0:
                sl = structural_sl
                sl_source = 'structure'
            else:
                # Fallback: ATR-based with wider multiplier
                if side == 'BUY':
                    sl = entry - (atr * self.SL_ATR_MULTIPLIER)
                else:  # SELL
                    sl = entry + (atr * self.SL_ATR_MULTIPLIER)
                sl_source = 'atr'
            # Apply absolute floor: SL never tighter than DB setting (SL_PCT_MIN)
            sl_pct = abs(sl - entry) / entry
            if sl_pct < self.SL_PCT_MIN:
                sl_pct = self.SL_PCT_MIN
                sl = entry * (1 - sl_pct) if side == 'BUY' else entry * (1 + sl_pct)
                sl_source = 'floor'
            # TP: ATR-based with floor
            if side == 'BUY':
                tp = entry + (atr * self.TP_ATR_MULTIPLIER)
            else:
                tp = entry - (atr * self.TP_ATR_MULTIPLIER)
            tp_pct = abs(tp - entry) / entry
            tp_pct = max(tp_pct, self.TP_PCT)
            tp = entry * (1 + tp_pct) if side == 'BUY' else entry * (1 - tp_pct)
            atr_based = True
        else:
            # Fallback: use original fixed percentages (ATR unavailable)
            tp = entry * (1 + self.TP_PCT) if side == 'BUY' else entry * (1 - self.TP_PCT)
            ranges = df_5m['high'].values - df_5m['low'].values
            avg_range_pct = np.mean(ranges[-20:]) / np.mean(df_5m['close'].values[-20:])
            sl_multiplier = min(1.5, max(1.0, avg_range_pct / 0.005))
            sl_dist = self.SL_PCT_MIN * sl_multiplier
            sl_dist = min(sl_dist, self.SL_PCT_MAX)
            sl = entry * (1 - sl_dist) if side == 'BUY' else entry * (1 + sl_dist)
            atr_based = False
            sl_source = 'fallback'
        
        result.update({
            'approved': True,
            'side': side,
            'signal_reason': active_signal.get('reason', 'Unknown'),
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
        elif exchange == "dex":
            sl_distance_pct = abs(sl - entry) / entry * 100
            sl_label = {'structure': '[Structure]', 'atr': '[Swing ATR]', 'swing_floor': '[Swing Floor]', 'swing_fixed': '[Swing Fixed]'}.get(sl_source, '')
            display_lines.append(
                f"✅ ✅ ✅ DEX SWING TRADE APPROVED ✅ ✅ ✅\n"
                f"• Signal: {active_signal['reason']}\n"
                f"• Entry: {entry:.6f}\n"
                f"• SL: {sl:.6f} (-{sl_distance_pct:.2f}%)  {sl_label}\n"
                f"• TP: {tp:.6f} (+{tp_distance_pct:.2f}%) {'[Swing ATR]' if atr_based else '[Swing Fixed]'}\n"
                f"• Risk/Reward: {(tp_distance_pct / sl_distance_pct):.2f}:1\n"
                f"• Mode: 🔄 DEX Swing (wider targets for thin liquidity)"
            )
        else:
            sl_distance_pct = abs(sl - entry) / entry * 100
            sl_label = {'structure': '[Structure]', 'atr': '[ATR]', 'floor': '[DB Floor]', 'fallback': '[Fallback]'}.get(sl_source, '')
            display_lines.append(
                f"✅ ✅ ✅ TRADE APPROVED ✅ ✅ ✅\n"
                f"• Signal: {active_signal['reason']}\n"
                f"• Entry: {entry:.6f}\n"
                f"• SL: {sl:.6f} (-{sl_distance_pct:.2f}%)  {sl_label}\n"
                f"• TP: {tp:.6f} (+{tp_distance_pct:.2f}%){'  [ATR-based]' if atr_based else '  [Fixed %]'}\n"
                f"• Risk/Reward: {(tp_distance_pct / sl_distance_pct):.2f}:1"
            )
        
        result['resume_of_analysis'] = "\n".join(display_lines)

        # === STEP 9: ML GATE (already evaluated above; apply decision now) ===
        ml_reason = None
        if ml_decision == "approved":
            display_lines.append(f"\n🤖 ML Gate: score={ml_score:.3f} → APPROVED ✅")
        elif ml_decision == "rejected":
            ml_reason = f"ML gate rejected: Score {ml_score:.3f} < threshold {_get_ml_threshold()}"
            display_lines.append(f"\n🤖 ML Gate: score={ml_score:.3f} → REJECTED ❌ ({ml_reason})")
            if _get_exchange_mode(exchange) != "Automatic":
                display_lines.append("  ℹ️ Signal mode — trade still allowed, ML verdict is reference")
            else:
                result['approved'] = False
                result['rejection_reasons'].append(ml_reason)
                display_lines.append("  ⛔ Automatic mode — trade BLOCKED by ML gate")
        else:
            display_lines.append("\n🤖 ML Gate: model not available — skipping")

        # === STEP 9.5: LLM GATE (always active second opinion) ===
        if result['approved']:
            llm_decision, llm_reason = _evaluate_llm_gate(
                signal_summary=result.get('resume_of_analysis', ''),
                ml_score=ml_score,
                exchange=exchange,
            )
            if llm_decision == "approved":
                display_lines.append(f"🧠 LLM Gate: APPROVED ✅ — {llm_reason}")
            elif llm_decision == "rejected":
                display_lines.append(f"🧠 LLM Gate: REJECTED ❌ — {llm_reason}")
                if _get_exchange_mode(exchange) == "Automatic":
                    result['approved'] = False
                    result['rejection_reasons'].append(f"LLM gate: {llm_reason}")
                    display_lines.append("  ⛔ Automatic mode — trade BLOCKED by LLM gate")
                    # Notify via Telegram so user can review the rejection
                    try:
                        chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
                        if chat_id:
                            send_bot_message(chat_id,
                                f"🧠 LLM Gate REJECTED a signal for {asset}\n"
                                f"Reason: {llm_reason}\n"
                                f"ML score: {ml_score:.3f}\n\n"
                                f"Full analysis:\n{result.get('resume_of_analysis', '')}")
                    except Exception:
                        pass
                else:
                    display_lines.append("  ℹ️ Signal mode — LLM verdict is reference only")

        result['resume_of_analysis'] = "\n".join(display_lines)

        # === SAVE SIGNAL TO DATABASE FOR LATER ANALYSIS ===
        consecutive_up, consecutive_down = self._count_consecutive_candles(df_5m['close'].values) if len(df_5m) >= 10 else (0, 0)
        signal_id = save_signal_to_history(
            asset=asset,
            exchange=exchange,
            regime=regime,
            obi=obi,
            pattern_type=active_signal.get('reason', 'Unknown'),
            approved=result['approved'],  # may be overridden by ML gate
            side=side,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            rejection_reasons=result['rejection_reasons'],
            manipulation_warnings=manipulation_warnings,
            atr=atr,
            live_price=live_price,
            candle_count=max(consecutive_up, consecutive_down),
            ml_score=ml_score,
            ml_decision=ml_decision,
        )
        logger.info(f"💾 Signal saved to DB with ID: {signal_id}")
        
        self._log_round_summary(exchange, asset, live_price, regime, consecutive_up, consecutive_down,
                                result['approved'], result['rejection_reasons'], ml_score, ml_decision)
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
                if has_open_orders_binance(fail_safe=True):
                    logger.info("📋 Binance has open order(s) — automatic CEX execution skipped")
                    output += "\n\n📋 Binance has open order(s) — CEX automatic execution skipped"
                    return output

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

    # ── Startup confirmation via Telegram ────────────────────────────
    try:
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
        if chat_id:
            dex_mode = _get_exchange_mode("dex")
            cex_mode = _get_exchange_mode("cex")
            asset = get_setting("current_asset") or "N/A"
            startup_msg = (
                f"🤖 Mockba Bot Started\n"
                f"• Asset: {asset}\n"
                f"• DEX mode: {dex_mode}\n"
                f"• CEX mode: {cex_mode}\n"
                f"• Interval: {get_setting('interval') or '5m'}"
            )
            send_bot_message(chat_id, startup_msg)
    except Exception:
        pass

    _last_health_check_at = time.time()
    _HEALTH_CHECK_INTERVAL = 21600  # 6 hours

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

            has_dex_position = get_user_statistics() > 0 if dex_mode in ("Signal", "Automatic") else False
            has_cex_order = has_open_orders_binance(fail_safe=True) if cex_mode in ("Signal", "Automatic") else False

            scalper = ReversalScalper()

            # DEX cycle
            if dex_mode in ("Signal", "Automatic") and _should_run_exchange_cycle("dex", 30):
                if has_dex_position:
                    logger.info("📋 DEX has open position(s) — skipping DEX pattern search")
                else:
                    dex_result = scalper.analyze_signal(asset, interval, exchange_override="dex")
                    if dex_result.get("approved"):
                        ml_score = dex_result.get('ml_score')
                        if dex_mode == "Signal":
                            if ml_score is not None and ml_score < _get_ml_threshold():
                                logger.info(f"📡 DEX signal skipped — ML score {ml_score:.3f} < threshold {_get_ml_threshold()}")
                            elif _should_send_signal_alert("dex", asset, dex_result['side'], dex_result.get('signal_reason', 'Unknown')):
                                send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📡 DEX SIGNAL ALERT\n{dex_result['resume_of_analysis']}")
                                logger.info(f"📡 DEX signal alert sent for {asset}")
                        else:
                            # === COOLDOWN CHECK: prevent repeated entries on same pattern ===
                            if not _should_allow_execution("dex", asset, dex_result['side']):
                                logger.info(f"⏳ DEX execution skipped — cooldown active for {asset} {dex_result['side']}")
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
                                _record_execution("dex", asset, dex_result['side'])
                                logger.info(f"🚀 Orderly futures order placed: {order_payload}")

            # CEX cycle
            if cex_mode in ("Signal", "Automatic") and _should_run_exchange_cycle("cex", 60):
                if has_cex_order:
                    logger.info("📋 CEX has open order(s) — skipping CEX pattern search")
                else:
                    cex_result = scalper.analyze_signal(asset, interval, exchange_override="cex")
                    if cex_result.get("approved"):
                        ml_score = cex_result.get('ml_score')
                        if cex_mode == "Signal":
                            if ml_score is not None and ml_score < _get_ml_threshold():
                                logger.info(f"📡 CEX signal skipped — ML score {ml_score:.3f} < threshold {_get_ml_threshold()}")
                            elif _should_send_signal_alert("cex", asset, cex_result['side'], cex_result.get('signal_reason', 'Unknown')):
                                send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📡 CEX SIGNAL ALERT\n{cex_result['resume_of_analysis']}")
                                logger.info(f"📡 CEX signal alert sent for {asset}")
                        else:
                            # === COOLDOWN CHECK: prevent repeated entries on same pattern ===
                            if not _should_allow_execution("cex", asset, cex_result['side']):
                                logger.info(f"⏳ CEX execution skipped — cooldown active for {asset} {cex_result['side']}")
                            else:
                                order_payload = {
                                    "symbol": cex_result['symbol'],
                                    "side": cex_result['side'],
                                    "entry": cex_result['entry'],
                                    "take_profit": cex_result['take_profit'],
                                }
                                place_spot_order(order_payload)
                                _record_execution("cex", asset, cex_result['side'])
                                logger.info(f"🚀 Binance spot order placed: {order_payload}")

            # Periodic outcome labeling (background, every 2 hours)
            if _should_run_labeler():
                _run_labeler_background()

            # Periodic health check via Telegram (every 6 hours)
            now = time.time()
            if now - _last_health_check_at >= _HEALTH_CHECK_INTERVAL:
                _last_health_check_at = now
                try:
                    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
                    if chat_id:
                        dex_positions = get_user_statistics() if dex_mode in ("Signal", "Automatic") else 0
                        cex_has_open = has_open_orders_binance(fail_safe=False) if cex_mode in ("Signal", "Automatic") else False
                        health_msg = (
                            f"💚 Bot Health Check\n"
                            f"• DEX mode: {dex_mode} | Open positions: {dex_positions}\n"
                            f"• CEX mode: {cex_mode} | Open orders: {'Yes' if cex_has_open else 'No'}\n"
                            f"• Asset: {asset}"
                        )
                        send_bot_message(chat_id, health_msg)
                except Exception:
                    pass

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