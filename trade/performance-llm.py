import json
import os
import re
import sys
import ast
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
import requests

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logs.log_config import apolo_trader_logger as logger
from trade.get_trades import get_trades
from trade.get_binance_trades import get_binance_trades_for_analysis

# Parameters to extract from main.py — organized by category
REGIME_FILTER_PARAMETER_KEYS = [
    "REGIME_WINDOW_5M",
    "REGIME_WINDOW_1H",
    "SLOPE_THRESHOLD_5M",
    "SLOPE_THRESHOLD_1H",
    "VOLUME_THRESHOLD",
    "OBI_BULLISH_THRESHOLD",
    "OBI_BEARISH_THRESHOLD",
    "OBI_IMBALANCE_PCT_THRESHOLD",
]

RISK_PARAMETER_KEYS = [
    "TP_PCT",
    "SL_PCT_MIN",
    "SL_PCT_MAX",
    "SL_ATR_MULTIPLIER",
    "TP_ATR_MULTIPLIER",
    "CORRECTION_PCT",
    "BIG_CANDLE_MULTIPLIER",
    "SR_PROXIMITY_PCT",
    "MAX_TRADES_PER_DAY",
    "PREFERRED_DAYS",
    "PREFERRED_HOUR_START",
    "PREFERRED_HOUR_END",
]

ORDER_BOOK_PARAMETER_KEYS = [
    "OBI_THRESHOLD",
    "OBI_BULLISH_EXTREME",
    "OBI_BEARISH_EXTREME",
    "OB_DEPTH",
    "MIN_SIGNIFICANT_TRADE_QTY",
]

MANIPULATION_PARAMETER_KEYS = [
    "VOLUME_SPIKE_MULTIPLIER",
    "OB_DIVERGENCE_THRESHOLD",
    "SPREAD_ANOMALY_THRESHOLD",
    "SPIKE_CANDLE_MULTIPLIER",
    "SPIKE_VOLUME_MULTIPLIER",
    "LIVE_PRICE_MAX_DEVIATION",
]

PATTERN_PARAMETER_KEYS = [
    "CANDLE_COUNT",
    "CANDLE_COUNTS",
]


def _to_datetime_utc(timestamp_ms: Any) -> datetime | None:
    """Convert millisecond timestamp to UTC datetime."""
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _is_preferred_window(timestamp_ms: Any,
                         preferred_days: list[str] | None = None,
                         hour_start: int = 6,
                         hour_end: int = 11) -> bool:
    """
    Check if timestamp falls within preferred trading window.

    Default schedule: Sun-Thu 6am-11am UTC-4 (aligned with main.py ReversalScalper)._is_preferred_time()
    Can be overridden with extracted PREFERRED_DAYS / PREFERRED_HOUR_START / PREFERRED_HOUR_END parameters.
    """
    dt = _to_datetime_utc(timestamp_ms)
    if dt is None:
        return False
    # Convert to UTC-4
    dt_utc4 = dt.replace(tzinfo=None) - timedelta(hours=4)
    day_name = dt_utc4.strftime('%A')
    hour = dt_utc4.hour

    if preferred_days is None:
        preferred_days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday']

    if day_name not in preferred_days:
        return False

    # Sunday has shorter window: 8am-10am UTC-4
    if day_name == 'Sunday':
        return 8 <= hour < 10

    return hour_start <= hour < hour_end


def _resolve_parameter_value(raw_value: str) -> Any:
    """
    Resolve a parameter assignment RHS to its best value.
    Handles:
      - Literals: 0.003, 20, [2,3,4], True
      - self._setting_pct('key', default) → extracts default
      - float(get_setting('key') or default) → extracts default
      - Simple expressions: 1.0 / 1.10
    """
    raw_value = raw_value.strip().rstrip(",")  # remove trailing comma from multi-line

    # Try literal eval first (handles numbers, strings, lists, bools)
    try:
        return ast.literal_eval(raw_value)
    except (ValueError, SyntaxError):
        pass

    # Try: self._setting_pct('key', default)
    m = re.search(r"self\._setting_pct\(\s*['\"]([^'\"]+)['\"]\s*,\s*([^)]+)\)", raw_value)
    if m:
        try:
            return ast.literal_eval(m.group(2).strip())
        except (ValueError, SyntaxError):
            return m.group(2).strip()

    # Try: float(get_setting('key') or default)
    m = re.search(r"float\(\s*get_setting\(\s*['\"]([^'\"]+)['\"]\s*\)\s+or\s+([^)]+)\)", raw_value)
    if m:
        try:
            return ast.literal_eval(m.group(2).strip())
        except (ValueError, SyntaxError):
            return m.group(2).strip()

    # Try: simple float expression like 1.0 / 1.10 or arithmetic
    try:
        # Only evaluate simple arithmetic (safe subset)
        if re.match(r'^[\d\s.+\-*/()eE]+$', raw_value):
            return float(eval(raw_value))
    except Exception:
        pass

    return raw_value


def _extract_parameters_from_main(pattern_keys: list[str], class_name: str = "ReversalScalper") -> dict[str, Any]:
    """
    Extract parameter values from main.py by parsing self.PARAM = value assignments.

    Args:
        pattern_keys: List of parameter names to search for
        class_name: Class name to scope the search (default: ReversalScalper)

    Returns:
        Dict of {param_name: resolved_value}
    """
    main_path = Path(__file__).resolve().parent / "main.py"
    params: dict[str, Any] = {}

    try:
        code = main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read main.py for parameters: %s", exc)
        return params

    # Find the class definition to scope our search
    class_start = code.find(f"class {class_name}")
    if class_start == -1:
        logger.warning(f"Class {class_name} not found in main.py")
        return params

    # Find __init__ method
    init_start = code.find("def __init__", class_start)
    if init_start == -1:
        return params

    # Find the end of __init__ by locating next method at same indent level
    next_method = re.search(r"\n    def (?!__init__)\w+", code[init_start + 1:])
    if next_method:
        init_end = init_start + 1 + next_method.start()
    else:
        # Fallback: look for next class or end of file
        next_class = code.find("\nclass ", init_start + 1)
        init_end = next_class if next_class != -1 else len(code)

    init_block = code[init_start:init_end]

    for key in pattern_keys:
        # Match: self.KEY = value (handles multi-line with continuation)
        match = re.search(rf"self\.{key}\s*=\s*(.+?)(?:\n|#)", init_block)
        if not match:
            continue

        raw_value = match.group(1).strip()
        params[key] = _resolve_parameter_value(raw_value)

    return params


def _extract_process_signal_context(max_chars: int = 3200) -> str:
    """Extract process_signal function from main.py for LLM context."""
    main_path = Path(__file__).resolve().parent / "main.py"
    try:
        code = main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read main.py for process_signal context: %s", exc)
        return "process_signal context unavailable"
    
    start = code.find("def process_signal(")
    if start == -1:
        return "process_signal function not found"
    
    end = code.find("\ndef autotrade(", start)
    if end == -1:
        end = min(len(code), start + max_chars)
    
    snippet = code[start:end].strip()
    return snippet[:max_chars]


def _extract_main_strategy_context(max_chars: int = 7000) -> str:
    """Extract ReversalScalper class from main.py for LLM context."""
    main_path = Path(__file__).resolve().parent / "main.py"
    try:
        code = main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read main.py strategy context: %s", exc)
        return "strategy context unavailable"
    
    start = code.find("class ReversalScalper")
    if start == -1:
        return "ReversalScalper class not found"
    
    end = code.find("\ndef autotrade(", start)
    if end == -1:
        end = min(len(code), start + max_chars)
    
    snippet = code[start:end].strip()
    return snippet[:max_chars]


def _read_full_main_source() -> str:
    """Read full main.py source for comprehensive analysis."""
    main_path = Path(__file__).resolve().parent / "main.py"
    try:
        return main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read full main.py source: %s", exc)
        return "main.py full source unavailable"


def _pearson_correlation(x: list[float], y: list[float]) -> float | None:
    """Compute Pearson correlation coefficient. Returns None if insufficient data."""
    if len(x) < 3 or len(y) < 3:
        return None
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    std_x = (sum((xi - mean_x) ** 2 for xi in x) ** 0.5)
    std_y = (sum((yi - mean_y) ** 2 for yi in y) ** 0.5)
    if std_x == 0 or std_y == 0:
        return None
    return round(cov / (std_x * std_y), 4)


def _build_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build comprehensive trade statistics including fee impact, position sizing,
    side-specific breakdown, and maker/taker analysis.
    """
    positives = 0
    negatives = 0
    neutral = 0
    pnl_sum = 0.0
    fee_sum = 0.0
    hour_counter: Counter[int] = Counter()
    day_counter: Counter[str] = Counter()
    strategy_counter: Counter[str] = Counter()
    side_counter: Counter[str] = Counter()

    # Side-specific tracking
    side_pnl: dict[str, float] = {}
    side_wins: dict[str, int] = {}
    side_losses: dict[str, int] = {}
    side_fees: dict[str, float] = {}

    # Maker vs taker tracking
    maker_pnl = 0.0
    taker_pnl = 0.0
    maker_count = 0
    taker_count = 0
    maker_fees = 0.0
    taker_fees = 0.0

    # Position sizing analysis
    quantities = []
    pnls = []
    win_quantities = []
    loss_quantities = []

    # Exchange tracking
    exchange_counter: Counter[str] = Counter()
    exchange_pnl: dict[str, float] = {}
    exchange_fees: dict[str, float] = {}
    exchange_wins: dict[str, int] = {}
    exchange_losses: dict[str, int] = {}

    for trade in trades:
        pnl = float(trade.get("realized_pnl") or 0.0)
        fee = float(trade.get("fee") or 0.0)
        qty = float(trade.get("executed_quantity") or 0.0)
        side = str(trade.get("side", "UNKNOWN")).upper()
        is_maker = int(trade.get("is_maker") or 0)
        exchange = str(trade.get("exchange", "orderly_dex"))

        pnl_sum += pnl
        fee_sum += fee
        quantities.append(qty)
        pnls.append(pnl)

        if pnl > 0:
            positives += 1
            win_quantities.append(qty)
        elif pnl < 0:
            negatives += 1
            loss_quantities.append(qty)
        else:
            neutral += 1

        # Side breakdown
        side_counter[side] += 1
        side_pnl[side] = side_pnl.get(side, 0.0) + pnl
        side_fees[side] = side_fees.get(side, 0.0) + fee
        if pnl > 0:
            side_wins[side] = side_wins.get(side, 0) + 1
        elif pnl < 0:
            side_losses[side] = side_losses.get(side, 0) + 1

        # Maker vs taker
        if is_maker:
            maker_count += 1
            maker_pnl += pnl
            maker_fees += fee
        else:
            taker_count += 1
            taker_pnl += pnl
            taker_fees += fee

        # Exchange breakdown
        exchange_counter[exchange] += 1
        exchange_pnl[exchange] = exchange_pnl.get(exchange, 0.0) + pnl
        exchange_fees[exchange] = exchange_fees.get(exchange, 0.0) + fee
        if pnl > 0:
            exchange_wins[exchange] = exchange_wins.get(exchange, 0) + 1
        elif pnl < 0:
            exchange_losses[exchange] = exchange_losses.get(exchange, 0) + 1

        dt = _to_datetime_utc(trade.get("executed_timestamp"))
        if dt is not None:
            # Convert to UTC-4 for analysis
            dt_utc4 = dt.replace(tzinfo=None) - timedelta(hours=4)
            hour_counter[dt_utc4.hour] += 1
            day_counter[dt_utc4.strftime("%A")] += 1

        strategy_value = (
            trade.get("strategy")
            or trade.get("indicator")
            or trade.get("source")
            or "process_signal_reversal_scalper"
        )
        strategy_counter[str(strategy_value)] += 1

    total_trades = len(trades)
    qty_pnl_corr = _pearson_correlation(quantities, pnls)

    return {
        "total_trades": total_trades,
        "positive_trades": positives,
        "negative_trades": negatives,
        "neutral_trades": neutral,
        "win_rate_pct": round(positives / total_trades * 100, 2) if total_trades > 0 else 0,
        "pnl_total": round(pnl_sum, 8),
        "fee_total": round(fee_sum, 8),
        "net_pnl_after_fees": round(pnl_sum - fee_sum, 8),
        "avg_fee_per_trade": round(fee_sum / total_trades, 4) if total_trades > 0 else 0,
        "avg_win_pnl": round(sum(pnls) / positives, 4) if positives > 0 else 0,
        "avg_loss_pnl": round(sum(p for p in pnls if p < 0) / negatives, 4) if negatives > 0 else 0,
        "side_breakdown": {
            side: {
                "count": side_counter.get(side, 0),
                "wins": side_wins.get(side, 0),
                "losses": side_losses.get(side, 0),
                "win_rate_pct": round(
                    side_wins.get(side, 0) / max(side_counter.get(side, 1), 1) * 100, 2
                ),
                "total_pnl": round(side_pnl.get(side, 0.0), 4),
                "total_fees": round(side_fees.get(side, 0.0), 4),
            }
            for side in sorted(side_counter.keys())
        },
        "maker_vs_taker": {
            "maker": {
                "count": maker_count,
                "total_pnl": round(maker_pnl, 4),
                "total_fees": round(maker_fees, 4),
                "avg_fee": round(maker_fees / maker_count, 4) if maker_count > 0 else 0,
            },
            "taker": {
                "count": taker_count,
                "total_pnl": round(taker_pnl, 4),
                "total_fees": round(taker_fees, 4),
                "avg_fee": round(taker_fees / taker_count, 4) if taker_count > 0 else 0,
            },
        },
        "position_size_analysis": {
            "avg_quantity_win": round(sum(win_quantities) / len(win_quantities), 2) if win_quantities else 0,
            "avg_quantity_loss": round(sum(loss_quantities) / len(loss_quantities), 2) if loss_quantities else 0,
            "max_quantity_loss": max(loss_quantities) if loss_quantities else 0,
            "quantity_pnl_pearson_correlation": qty_pnl_corr,
            "correlation_interpretation": (
                "Strong negative: larger positions → larger losses"
                if qty_pnl_corr is not None and qty_pnl_corr < -0.5
                else "Weak/neutral" if qty_pnl_corr is not None else "Insufficient data"
            ),
        },
        "hours_enter_distribution": dict(sorted(hour_counter.items())),
        "days_enter_distribution": dict(day_counter),
        "strategy_used_distribution": dict(strategy_counter),
        "exchange_breakdown": {
            exch: {
                "count": exchange_counter.get(exch, 0),
                "wins": exchange_wins.get(exch, 0),
                "losses": exchange_losses.get(exch, 0),
                "win_rate_pct": round(
                    exchange_wins.get(exch, 0) / max(exchange_counter.get(exch, 1), 1) * 100, 2
                ),
                "total_pnl": round(exchange_pnl.get(exch, 0.0), 4),
                "total_fees": round(exchange_fees.get(exch, 0.0), 4),
            }
            for exch in sorted(exchange_counter.keys())
        },
    }


def _enrich_trades_with_context(trades: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    """
    Enrich trade sample with derived context fields for LLM analysis.

    Adds:
    - hour_utc4, day_utc4: Time in user's timezone
    - in_preferred_window: Whether trade was in preferred time
    - is_large_loss: Whether loss > $10
    - fee_impact_pct: Fee as % of |PnL|
    - side_label: BUY (LONG) or SELL (SHORT)
    - is_maker: Whether maker order (lower fees)
    - fee_impact_on_net: Fee drag on net outcome
    """
    enriched = []

    for trade in trades[:limit]:
        t = {**trade}  # Copy to avoid modifying original

        dt = _to_datetime_utc(t.get("executed_timestamp"))
        if dt:
            dt_utc4 = dt.replace(tzinfo=None) - timedelta(hours=4)
            t["hour_utc4"] = dt_utc4.hour
            t["day_utc4"] = dt_utc4.strftime("%A")
            t["in_preferred_window"] = _is_preferred_window(t.get("executed_timestamp"))

        pnl = float(t.get("realized_pnl") or 0.0)
        fee = float(t.get("fee") or 0.0)
        side_raw = str(t.get("side", "")).upper()
        is_maker = int(t.get("is_maker") or 0)

        t["side_label"] = "LONG" if side_raw == "BUY" else ("SHORT" if side_raw == "SELL" else side_raw)
        t["is_maker"] = bool(is_maker)
        t["is_large_loss"] = pnl < -10.0
        t["fee_impact_pct"] = round(abs(fee / pnl) * 100, 2) if abs(pnl) > 1e-8 else 0
        t["fee_impact_on_net"] = round(fee, 6) if abs(pnl) > 1e-8 else 0

        enriched.append(t)

    return enriched


def _build_llm_prompt(
    symbol_filter: str,
    trades: list[dict[str, Any]],
    stats: dict[str, Any],
    regime_params: dict[str, Any],
    risk_params: dict[str, Any],
    orderbook_params: dict[str, Any],
    manipulation_params: dict[str, Any],
    pattern_params: dict[str, Any],
    process_signal_context: str,
    main_strategy_context: str,
    full_main_source: str,
) -> str:
    """
    Build comprehensive LLM prompt with all critical context, using dynamic
    values computed from actual trade data.
    """
    enriched_trades = _enrich_trades_with_context(trades)
    avg_win = stats.get("avg_win_pnl", 0)
    avg_loss = stats.get("avg_loss_pnl", 0)
    avg_fee = stats.get("avg_fee_per_trade", 0)
    net_pnl = stats.get("net_pnl_after_fees", 0)
    win_rate = stats.get("win_rate_pct", 0)
    total = stats.get("total_trades", 0)
    pos = stats.get("positive_trades", 0)
    neg = stats.get("negative_trades", 0)
    fee_pct_of_avg_win = round(avg_fee / avg_win * 100, 1) if avg_win > 0 else 0

    # Build side breakdown summary line
    side_lines = []
    for side, sd in stats.get("side_breakdown", {}).items():
        side_lines.append(
            f"{side}: {sd['count']} trades, {sd['win_rate_pct']}% win, "
            f"PnL ${sd['total_pnl']}, fees ${sd['total_fees']}"
        )

    # Build exchange breakdown summary lines
    exchange_lines = []
    for exch, ed in stats.get("exchange_breakdown", {}).items():
        exchange_lines.append(
            f"{exch}: {ed['count']} trades, {ed['win_rate_pct']}% win, "
            f"PnL ${ed['total_pnl']}, fees ${ed['total_fees']}"
        )

    instructions = [
        "=== ROLE ===",
        "You are a quantitative trading performance analyst specializing in crypto futures scalping.",
        "Analyze this trading performance report based on REAL executed trades and strategy code from main.py.",
        "",
        "=== STRATEGY CONTEXT ===",
        "Strategy: Price Action Reversal Scalper (hard-coded logic, NO LLM/ML in execution)",
        f"Asset: {symbol_filter} on Orderly Network (DEX futures) + Binance (CEX spot)",
        "Timeframe: 5-minute candles with regime detection on 1-hour",
        "",
        "=== EXECUTIVE SUMMARY (computed from actual data) ===",
        f"Total trades: {total} | Win rate: {win_rate}% ({pos}W / {neg}L)",
        f"Net PnL after fees: ${net_pnl}",
        f"Avg win: ${avg_win} | Avg loss: ${avg_loss} | Avg fee: ${avg_fee}",
        f"Fee impact: avg fee is {fee_pct_of_avg_win}% of avg win — fee drag is {'SIGNIFICANT' if fee_pct_of_avg_win > 10 else 'moderate'}",
        "",
        "=== EXCHANGE BREAKDOWN ===",
        *exchange_lines,
        "",
        "=== SIDE DIRECTION BREAKDOWN ===",
        *side_lines,
        "",
        "=== ALL STRATEGY PARAMETERS (from main.py ReversalScalper.__init__) ===",
        "--- Risk ---",
        json.dumps(risk_params, indent=2),
        "--- Regime Filter ---",
        json.dumps(regime_params, indent=2),
        "--- Order Book ---",
        json.dumps(orderbook_params, indent=2),
        "--- Manipulation Detection ---",
        json.dumps(manipulation_params, indent=2),
        "--- Pattern Recognition ---",
        json.dumps(pattern_params, indent=2),
        "",
        "=== OPTIMIZATION PRIORITIES (in order) ===",
        "1) PREVENT LARGE LOSSES: Block trades that could lose >$10, even at cost of missed wins",
        f"2) IMPROVE NET PnL: Current net = ${net_pnl}. Each trade costs ~${avg_fee} in fees.",
        f"3) MAINTAIN / IMPROVE WIN RATE: Current = {win_rate}%. Target >50% in RANGE regime.",
        "4) PRESERVE LATENCY: Keep execution <100ms — prefer hard-coded rules over ML/LLM.",
        "5) RESPECT TIME WINDOW: Sun-Thu 6-11am UTC-4 (Sun 8-10am only).",
        "",
        "=== REQUIRED ANALYSIS DIMENSIONS ===",
        "1) WIN CONDITIONS: What common factors (regime, OBI, time, pattern, position size, side)",
        "   were present in winning trades? Extract actionable patterns.",
        "2) LOSS CONDITIONS: What common factors preceded losses? Could regime filter or",
        "   manipulation detection have blocked them BEFORE entry?",
        "3) TIME ANALYSIS: Are losses clustered outside preferred window (Sun-Thu 6-11am UTC-4)?",
        "   Are certain hours systematically worse? Should the time filter tighten?",
        "4) DAY ANALYSIS: Any day-of-week patterns? (e.g., Monday reversals, Wednesday chop)",
        "5) SIDE BIAS: Does the strategy perform better LONG or SHORT? Is there a directional",
        "   bias that should inform trade filtering?",
        "6) FEE IMPACT: Given avg fee=${avg_fee} and avg win=${avg_win}, is there a minimum",
        f"   profit threshold below which trades aren't worth taking? ({fee_pct_of_avg_win}% drag)",
        "7) POSITION SIZING: Pearson correlation between quantity and PnL. If negative,",
        "   suggest dynamic position sizing based on signal confidence or volatility.",
        "8) MAKER vs TAKER: Are maker orders (lower fees) performing differently? Should",
        "   the strategy favor limit orders over market orders?",
        "9) REGIME SHIFT: Did market conditions (volatility, liquidity, trend strength) change",
        "   during the analysis period, degrading a previously-working strategy?",
        "10) PATTERN EFFECTIVENESS: Is the 2/3/4-candle reversal detecting valid setups?",
        "    Are SPIKE reversals (volume-confirmed) outperforming regular reversals?",
        "",
        "=== OUTPUT FORMAT ===",
        "Return ONLY valid JSON (no markdown fences, no extra text) with this EXACT schema:",
        "",
        '{',
        '  "summary": "string — one-sentence overview with key numbers",',
        '  "positive_trades_analysis": "string — conditions present in wins",',
        '  "negative_trades_analysis": "string — conditions present in losses",',
        '  "hour_enter_analysis": "string — time-of-day patterns and recommendation",',
        '  "days_enter_analysis": "string — day-of-week patterns",',
        '  "side_bias_analysis": "string — LONG vs SHORT performance comparison",',
        '  "strategy_used_analysis": "string — pattern detection effectiveness",',
        '  "fee_impact_analysis": "string — fee drag assessment + min-profit recommendation",',
        '  "position_size_analysis": "string — sizing vs PnL correlation + dynamic sizing suggestion",',
        '  "maker_vs_taker_analysis": "string — limit vs market order performance",',
        '  "regime_shift_analysis": "string — whether regime change degraded the plan",',
        '  "comparison_past_vs_current_strategy": "string — would current code block past losses?",',
        '  "strategy_improvements": ["actionable improvement 1", "improvement 2", ...],',
        '  "code_change_proposals": [',
        '    {',
        '      "target": "exact variable/function in main.py (e.g., ReversalScalper.SLOPE_THRESHOLD_1H)",',
        '      "current_behavior": "what the code does now",',
        '      "proposed_change": "exact code or parameter change",',
        '      "reason": "data-driven justification referencing trade stats",',
        '      "expected_impact": "quantified estimate (e.g., filter out 3 of 6 losing trades)"',
        '    }',
        '  ],',
        '  "regime_filter_parameter_recommendations": [',
        '    {',
        '      "parameter": "REGIME_WINDOW_1H",',
        '      "decision": "adjust|keep",',
        '      "current_value": 30,',
        '      "suggested_value": 30,',
        '      "reason": "data-driven explanation"',
        '    }',
        '  ],',
        '  "final_verdict": "improve|adapt|keep"',
        '}',
        "",
        "=== CRITICAL RULES ===",
        "- Reference EXACT variable/function names from main.py (e.g., ReversalScalper._detect_regime, SLOPE_THRESHOLD_1H)",
        "- Propose changes ONLY if trade data shows clear improvement potential",
        "- If data is insufficient for a parameter recommendation, set decision='keep' and explain why",
        "- Prioritize HARD-CODED logic over ML/LLM additions (latency <100ms requirement)",
        "- For code_change_proposals: include the EXACT line(s) of code to change",
        f"- Fee context: every trade costs ~${avg_fee} in fees. A ${avg_win} win nets only ${round(avg_win - avg_fee, 2)}.",
        "- Large losses (>$10) are priority #1 — propose guardrails that would have blocked them",
        f"- Current regime parameters are tuned for NEAR/USDC on Orderly (low liquidity DEX)",
        "",
        "=== DATA ===",
    ]

    payload = {
        "symbol_filter": symbol_filter,
        "strategy_parameters": {
            "risk": risk_params,
            "regime_filter": regime_params,
            "order_book": orderbook_params,
            "manipulation_detection": manipulation_params,
            "pattern_recognition": pattern_params,
        },
        "trade_stats": stats,
        "strategy_context_from_process_signal": process_signal_context,
        "strategy_context_from_main": main_strategy_context,
        "strategy_main_full_source": full_main_source,
        "trades_sample": enriched_trades,
    }

    return "\n".join(instructions) + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _clean_llm_json(raw: str) -> str:
    """Clean common LLM JSON formatting issues before parsing."""
    import re as _re
    # Remove markdown code fences if present
    cleaned = _re.sub(r'^```(?:json)?\s*', '', raw.strip())
    cleaned = _re.sub(r'\s*```$', '', cleaned)
    # Remove trailing commas before closing brackets/braces
    cleaned = _re.sub(r',\s*([}\]])', r'\1', cleaned)
    return cleaned


def _call_llm(prompt: str) -> str:
    """Call LLM API with proper error handling."""
    api_key = (
        os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("DEEP_SEEK_API_KEY")
        or os.getenv("DEEPSEEK_KEY")
        or os.getenv("DEEPSEEK_API")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_API_KEY")
    )
    model = os.getenv("LLM_MODEL", "deepseek-v4-pro")
    api_url = os.getenv("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions").strip()
    
    if not api_key:
        raise RuntimeError(
            "Missing API key. Set DEEPSEEK_API_KEY/DEEPSEEK_KEY (preferred) or OPENROUTER_API_KEY/OPENAI_API_KEY/LLM_API_KEY."
        )
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": "You are a quantitative trading performance analyst. Return strict JSON only, no markdown, no extra text.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "response_format": {"type": "json_object"},
    }
    
    response = requests.post(api_url, headers=headers, json=body, timeout=120)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _build_md_report(output: dict[str, Any]) -> str:
    """Build a Markdown report from the analysis output for LLM consumption."""
    lines = []
    ts = output.get("timestamp", "")
    symbol = output.get("symbol_filter", "")
    stats = output.get("trade_stats", {})
    params = output.get("parameters_extracted", {})
    llm_parsed = output.get("llm_parsed", {})

    lines.append(f"# Trading Performance Analysis — {symbol}")
    lines.append(f"\n**Generated:** {ts}")
    
    # Show exchanges analyzed
    exch_bd = stats.get("exchange_breakdown", {})
    exchanges = list(exch_bd.keys())
    if exchanges:
        lines.append(f"**Exchanges:** {', '.join(exchanges)}")
    lines.append("")

    lines.append("## Trade Statistics")
    lines.append(f"- Total trades: {stats.get('total_trades', 0)}")
    lines.append(f"- Positive: {stats.get('positive_trades', 0)}")
    lines.append(f"- Negative: {stats.get('negative_trades', 0)}")
    lines.append(f"- Neutral: {stats.get('neutral_trades', 0)}")
    lines.append(f"- Win rate: {stats.get('win_rate_pct', 0)}%")
    lines.append(f"- PnL total: {stats.get('pnl_total', 0)}")
    lines.append(f"- Fee total: {stats.get('fee_total', 0)}")
    lines.append(f"- Net PnL after fees: {stats.get('net_pnl_after_fees', 0)}")
    lines.append(f"- Avg fee/trade: {stats.get('avg_fee_per_trade', 0)}")
    lines.append(f"- Avg win: {stats.get('avg_win_pnl', 0)}")
    lines.append(f"- Avg loss: {stats.get('avg_loss_pnl', 0)}")
    side_bd = stats.get("side_breakdown", {})
    if side_bd:
        lines.append("\n### Side Breakdown")
        for side, sd in side_bd.items():
            lines.append(f"- **{side}**: {sd.get('count', 0)} trades, {sd.get('win_rate_pct', 0)}% win, PnL ${sd.get('total_pnl', 0)}")
    
    exch_bd = stats.get("exchange_breakdown", {})
    if exch_bd and len(exch_bd) > 1:
        lines.append("\n### Exchange Breakdown")
        for exch, ed in exch_bd.items():
            lines.append(f"- **{exch}**: {ed.get('count', 0)} trades, {ed.get('win_rate_pct', 0)}% win, PnL ${ed.get('total_pnl', 0)}, fees ${ed.get('total_fees', 0)}")
    
    maker_taker = stats.get("maker_vs_taker", {})
    if maker_taker:
        lines.append("\n### Maker vs Taker")
        for role, rd in maker_taker.items():
            lines.append(f"- **{role}**: {rd.get('count', 0)} trades, PnL ${rd.get('total_pnl', 0)}, fees ${rd.get('total_fees', 0)}")

    lines.append("\n## Parameters (from main.py)")
    for category, title in [
        ("risk_parameters", "Risk Parameters"),
        ("regime_filter", "Regime Filter Parameters"),
        ("order_book", "Order Book Parameters"),
        ("manipulation_detection", "Manipulation Detection Parameters"),
        ("pattern_recognition", "Pattern Recognition Parameters"),
    ]:
        cat_params = params.get(category, {})
        if cat_params:
            lines.append(f"### {title}")
            for k, v in cat_params.items():
                lines.append(f"- {k}: {v}")

    if not isinstance(llm_parsed, dict) or "raw_response" in llm_parsed:
        lines.append("\n## LLM Raw Response")
        lines.append(output.get("llm_response", ""))
        return "\n".join(lines)

    lines.append("\n## LLM Analysis")
    lines.append(f"\n**Verdict:** {str(llm_parsed.get('final_verdict', 'N/A')).upper()}\n")
    lines.append(f"**Summary:** {llm_parsed.get('summary', '')}\n")

    for field, title in [
        ("positive_trades_analysis", "Positive Trades Analysis"),
        ("negative_trades_analysis", "Negative Trades Analysis"),
        ("hour_enter_analysis", "Hour Entry Analysis"),
        ("days_enter_analysis", "Days Entry Analysis"),
        ("side_bias_analysis", "Side Bias Analysis (LONG vs SHORT)"),
        ("strategy_used_analysis", "Strategy Pattern Analysis"),
        ("fee_impact_analysis", "Fee Impact Analysis"),
        ("position_size_analysis", "Position Size Analysis"),
        ("maker_vs_taker_analysis", "Maker vs Taker Analysis"),
        ("regime_shift_analysis", "Regime Shift Analysis"),
        ("comparison_past_vs_current_strategy", "Past vs Current Strategy"),
    ]:
        val = llm_parsed.get(field, "")
        if val:
            lines.append(f"### {title}")
            lines.append(f"{val}\n")

    improvements = llm_parsed.get("strategy_improvements", [])
    if improvements:
        lines.append("### Strategy Improvements")
        for item in improvements:
            lines.append(f"- {item}")
        lines.append("")

    proposals = llm_parsed.get("code_change_proposals", [])
    if proposals:
        lines.append("### Code Change Proposals")
        for p in proposals:
            if not isinstance(p, dict):
                continue
            lines.append(f"#### `{p.get('target', '')}`")
            lines.append(f"- **Current behavior:** {p.get('current_behavior', '')}")
            lines.append(f"- **Proposed change:** {p.get('proposed_change', '')}")
            lines.append(f"- **Reason:** {p.get('reason', '')}")
            lines.append(f"- **Expected impact:** {p.get('expected_impact', '')}")
            lines.append("")

    rec_params = llm_parsed.get("regime_filter_parameter_recommendations", [])
    if rec_params:
        lines.append("### Regime Filter Parameter Recommendations")
        for p in rec_params:
            if not isinstance(p, dict):
                continue
            lines.append(
                f"- **{p.get('parameter', '')}**: {str(p.get('decision', '')).upper()} → "
                f"`{p.get('suggested_value', '-')}` — {p.get('reason', '')}"
            )
        lines.append("")

    return "\n".join(lines)


def analyze_trade_performance(symbol_filter: str = "NEAR_USDC") -> dict[str, Any]:
    """
    Main function: Analyze trade performance and get LLM recommendations.
    Merges both Orderly DEX (futures) and Binance CEX (spot) trades.
    
    Returns dict with analysis results, saves to Markdown file.
    """
    # Extract base asset name (e.g., "NEAR" from "NEAR_USDC" or "PERP_NEAR_USDC")
    base_asset = symbol_filter.replace("PERP_", "").replace("_USDC", "").strip()

    # Fetch Orderly DEX trades
    dex_trades = get_trades(symbol_filter=symbol_filter)
    for t in dex_trades:
        t["exchange"] = "orderly_dex"

    # Fetch Binance spot trades
    binance_trades = get_binance_trades_for_analysis(base_asset)

    # Merge
    trades = dex_trades + binance_trades

    if not trades:
        return {
            "ok": False,
            "error": f"No trades found for symbol filter: {symbol_filter}",
        }
    trade_source = "DEX + CEX" if binance_trades else "DEX only"
    logger.info(f"📊 Analyzing {len(trades)} trades ({len(dex_trades)} DEX + {len(binance_trades)} CEX) for {base_asset}")
    
    # Build statistics
    stats = _build_trade_stats(trades)
    
    # Extract parameters from main.py
    regime_params = _extract_parameters_from_main(REGIME_FILTER_PARAMETER_KEYS)
    risk_params = _extract_parameters_from_main(RISK_PARAMETER_KEYS)
    orderbook_params = _extract_parameters_from_main(ORDER_BOOK_PARAMETER_KEYS)
    manipulation_params = _extract_parameters_from_main(MANIPULATION_PARAMETER_KEYS)
    pattern_params = _extract_parameters_from_main(PATTERN_PARAMETER_KEYS)
    
    # Extract code context
    process_signal_context = _extract_process_signal_context()
    main_strategy_context = _extract_main_strategy_context()
    full_main_source = _read_full_main_source()
    
    # Build prompt
    prompt = _build_llm_prompt(
        symbol_filter,
        trades,
        stats,
        regime_params,
        risk_params,
        orderbook_params,
        manipulation_params,
        pattern_params,
        process_signal_context,
        main_strategy_context,
        full_main_source,
    )
    
    # Call LLM
    try:
        llm_response = _call_llm(prompt)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return {
            "ok": False,
            "error": f"LLM API error: {str(e)}",
            "trade_stats": stats,
            "parameters_extracted": {
                "risk_parameters": risk_params,
                "regime_filter": regime_params,
                "order_book": orderbook_params,
                "manipulation_detection": manipulation_params,
                "pattern_recognition": pattern_params,
            },
        }
    
    # Parse and validate JSON response
    try:
        llm_parsed = json.loads(_clean_llm_json(llm_response))
    except json.JSONDecodeError as e:
        logger.warning(f"LLM response not valid JSON: {e}")
        llm_parsed = {"raw_response": llm_response, "parse_error": str(e)}
    
    # Build output
    output = {
        "ok": True,
        "symbol_filter": symbol_filter,
        "trade_stats": stats,
        "parameters_extracted": {
            "risk_parameters": risk_params,
            "regime_filter": regime_params,
            "order_book": orderbook_params,
            "manipulation_detection": manipulation_params,
            "pattern_recognition": pattern_params,
        },
        "llm_prompt_preview": prompt[:1000] + "...",
        "llm_response": llm_response,
        "llm_parsed": llm_parsed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    # Save to Markdown file
    md_content = _build_md_report(output)
    output_path = PROJECT_ROOT / "data" / "performance_llm_analysis.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md_content, encoding="utf-8")
    logger.info("Performance analysis saved to %s", output_path)

    output["md_path"] = str(output_path)
    return output


# === TESTING ===
if __name__ == "__main__":
    symbol = os.getenv("PERFORMANCE_SYMBOL_FILTER", "NEAR_USDC")
    print(f"🔍 Analyzing trades for {symbol}...")
    result = analyze_trade_performance(symbol_filter=symbol)
    
    if result["ok"]:
        print("\n✅ Analysis complete!")
        print(f"📊 Trade stats: {result['trade_stats']['total_trades']} trades, "
              f"{result['trade_stats']['win_rate_pct']}% win rate, "
              f"net PnL: ${result['trade_stats']['net_pnl_after_fees']}")
        print(f"\n🤖 LLM verdict: {result['llm_parsed'].get('final_verdict', 'N/A')}")
        print(f"\n💡 Top recommendation: {result['llm_parsed'].get('strategy_improvements', ['None'])[0]}")
    else:
        print(f"❌ Error: {result.get('error', 'Unknown error')}")
    
    print(f"\n📄 Full report saved to: data/performance_llm_analysis.md")