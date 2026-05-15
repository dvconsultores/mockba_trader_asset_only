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

# Parameters to extract from main.py
REGIME_FILTER_PARAMETER_KEYS = [
    "REGIME_WINDOW_5M",
    "REGIME_WINDOW_1H", 
    "SLOPE_THRESHOLD_5M",
    "SLOPE_THRESHOLD_1H",
    "VOLUME_THRESHOLD",
]

# Risk parameters to extract from main.py
RISK_PARAMETER_KEYS = [
    "TP_PCT",
    "SL_PCT_MIN", 
    "SL_PCT_MAX",
    "CORRECTION_PCT",
    "BIG_CANDLE_MULTIPLIER",
    "MAX_TRADES_PER_DAY",
    "PREFERRED_DAYS",
    "PREFERRED_HOUR_START",
    "PREFERRED_HOUR_END",
]


def _to_datetime_utc(timestamp_ms: Any) -> datetime | None:
    """Convert millisecond timestamp to UTC datetime."""
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _is_preferred_window(timestamp_ms: Any) -> bool:
    """Check if timestamp falls within preferred trading window (Mon-Fri 6am-12pm, Sun 8am-10am UTC-4)."""
    dt = _to_datetime_utc(timestamp_ms)
    if dt is None:
        return False
    # Convert to UTC-4
    dt_utc4 = dt.replace(tzinfo=None) - timedelta(hours=4)
    day_name = dt_utc4.strftime('%A')
    hour = dt_utc4.hour
    
    if day_name == 'Sunday':
        return 8 <= hour < 10
    elif day_name in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']:
        return 6 <= hour < 11
    else:
        return False


def _extract_parameters_from_main(pattern_keys: list[str], class_name: str = "ReversalScalper") -> dict[str, Any]:
    """
    Extract parameter values from main.py by parsing self.PARAM = value assignments.
    
    Args:
        pattern_keys: List of parameter names to search for
        class_name: Class name to scope the search (default: ReversalScalper)
    
    Returns:
        Dict of {param_name: value}
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
    
    # Find the end of __init__ method (next method or end of class)
    init_start = code.find("def __init__", class_start)
    if init_start == -1:
        return params
    
    # Look for next method definition after __init__
    next_method = code.find("\n    def ", init_start + 1)
    if next_method == -1:
        next_method = code.find("\nclass ", init_start + 1)
    
    # Extract the __init__ block
    if next_method != -1:
        init_block = code[init_start:next_method]
    else:
        init_block = code[init_start:init_start + 5000]  # Fallback: read 5000 chars
    
    for key in pattern_keys:
        # Match: self.KEY = value (with various spacing)
        match = re.search(rf"self\.{key}\s*=\s*([^#\n]+)", init_block)
        if not match:
            continue
        
        raw_value = match.group(1).strip()
        try:
            # Safely evaluate the value (handles numbers, strings, lists)
            params[key] = ast.literal_eval(raw_value)
        except Exception:
            # Fallback: keep as string
            params[key] = raw_value
    
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


def _build_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build comprehensive trade statistics including fee impact and position sizing.
    """
    positives = 0
    negatives = 0
    neutral = 0
    pnl_sum = 0.0
    fee_sum = 0.0
    hour_counter: Counter[int] = Counter()
    day_counter: Counter[str] = Counter()
    strategy_counter: Counter[str] = Counter()
    
    # Position sizing analysis
    win_quantities = []
    loss_quantities = []
    
    for trade in trades:
        pnl = float(trade.get("realized_pnl") or 0.0)
        fee = float(trade.get("fee") or 0.0)
        qty = float(trade.get("executed_quantity") or 0.0)
        
        pnl_sum += pnl
        fee_sum += fee
        
        if pnl > 0:
            positives += 1
            win_quantities.append(qty)
        elif pnl < 0:
            negatives += 1
            loss_quantities.append(qty)
        else:
            neutral += 1
        
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
        "avg_win_pnl": round(pnl_sum / positives, 4) if positives > 0 else 0,
        "avg_loss_pnl": round(pnl_sum / negatives, 4) if negatives > 0 else 0,
        "position_size_analysis": {
            "avg_quantity_win": round(sum(win_quantities) / len(win_quantities), 2) if win_quantities else 0,
            "avg_quantity_loss": round(sum(loss_quantities) / len(loss_quantities), 2) if loss_quantities else 0,
            "max_quantity_loss": max(loss_quantities) if loss_quantities else 0,
            "quantity_pnl_correlation": "Negative: larger positions correlate with larger losses" if loss_quantities and win_quantities and (sum(loss_quantities)/len(loss_quantities)) > (sum(win_quantities)/len(win_quantities)) else "Neutral",
        },
        "hours_enter_distribution": dict(sorted(hour_counter.items())),
        "days_enter_distribution": dict(day_counter),
        "strategy_used_distribution": dict(strategy_counter),
    }


def _enrich_trades_with_context(trades: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    """
    Enrich trade sample with derived context fields for LLM analysis.
    
    Adds:
    - hour_utc4, day_utc4: Time in user's timezone
    - in_preferred_window: Whether trade was in preferred time
    - is_large_loss: Whether loss > $10
    - fee_impact_pct: Fee as % of PnL
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
        
        t["is_large_loss"] = pnl < -10.0
        t["fee_impact_pct"] = round(abs(fee / pnl) * 100, 2) if pnl != 0 else 0
        
        enriched.append(t)
    
    return enriched


def _build_llm_prompt(
    symbol_filter: str,
    trades: list[dict[str, Any]],
    stats: dict[str, Any],
    regime_params: dict[str, Any],
    risk_params: dict[str, Any],
    process_signal_context: str,
    main_strategy_context: str,
    full_main_source: str,
) -> str:
    """
    Build comprehensive LLM prompt with all critical context.
    """
    enriched_trades = _enrich_trades_with_context(trades)
    
    instructions = [
        "=== ROLE ===",
        "You are a quantitative trading performance analyst specializing in crypto futures scalping.",
        "Analyze this trading performance report based on REAL executed trades and strategy code from main.py.",
        "",
        "=== STRATEGY CONTEXT ===",
        "Strategy: Price Action Reversal Scalper (hard-coded logic, NO LLM/ML in execution)",
        "Asset: NEAR/USDC perpetual futures on Orderly Network (low-volume DEX)",
        "Timeframe: 5-minute candles",
        "",
        "=== RISK PARAMETERS (from main.py) ===",
        json.dumps(risk_params, indent=2),
        "",
        "=== REGIME FILTER PARAMETERS (from main.py) ===",
        json.dumps(regime_params, indent=2),
        "",
        "=== OPTIMIZATION PRIORITIES (in order) ===",
        "1) PREVENT LARGE LOSSES: Block trades that could lose >$10, even if it means missing some wins",
        "2) MAINTAIN WIN RATE: Keep win rate >50% in RANGE regime conditions",
        "3) IMPROVE NET PnL: Account for fees (avg $0.45/trade) - small wins get eaten",
        "4) PRESERVE LATENCY: Keep execution <100ms - no heavy ML, prefer hard-coded logic",
        "5) RESPECT USER PREFERENCES: Only trade Mon-Fri 6am-12pm, Sun 8am-10am UTC-4 unless strong signal",
        "",
        "=== REQUIRED ANALYSIS POINTS ===",
        "1) POSITIVE TRADES: What conditions led to wins? (regime, OBI, time, pattern, position size)",
        "2) NEGATIVE TRADES: What conditions led to losses? Could regime filter have blocked them?",
        "3) HOUR ANALYSIS: Are losses clustered outside 6am-12pm (Mon-Fri) or 8-10am (Sun) UTC-4? Should time filter be adjusted?",
        "4) DAY ANALYSIS: Are certain days (e.g., Monday) higher risk? Any pattern?",
        "5) STRATEGY PATTERN: Is the 2-candle reversal pattern detecting valid setups? Missed opportunities?",
        "6) FEE IMPACT: Avg win=$3.61, avg fee=$0.45 = 12.5% fee drag. Propose minimum profit threshold?",
        "7) POSITION SIZING: Do larger quantities correlate with larger losses? Suggest dynamic sizing?",
        "8) REGIME SHIFT TRAP: Did the plan degrade because market structure changed, not just because of discipline or execution drift? Evaluate whether results suggest the strategy is fighting the last regime.",
        "9) REGIME PARAMETERS: Adjust ONLY if trade data clearly justifies improvement",
        "",
        "=== OUTPUT FORMAT ===",
        "Return ONLY valid JSON (no markdown, no extra text) with this EXACT schema:",
        "",
        '{',
        '  "summary": "string - one sentence overview",',
        '  "positive_trades_analysis": "string - key conditions for wins",',
        '  "negative_trades_analysis": "string - key conditions for losses",',
        '  "hour_enter_analysis": "string - time-based patterns",',
        '  "days_enter_analysis": "string - day-based patterns",',
        '  "strategy_used_analysis": "string - pattern detection effectiveness",',
        '  "fee_impact_analysis": "string - fee drag assessment + recommendation",',
        '  "position_size_analysis": "string - sizing vs PnL correlation",',
        '  "regime_shift_analysis": "string - whether volatility/liquidity regime changes likely degraded the plan",',
        '  "comparison_past_vs_current_strategy": "string - would current code have prevented losses?",',
        '  "strategy_improvements": ["string - actionable improvement 1", "improvement 2", ...],',
        '  "code_change_proposals": [',
        '    {',
        '      "target": "exact variable/function name in main.py (e.g., ReversalScalper.SLOPE_THRESHOLD_1H)",',
        '      "current_behavior": "description of current logic",',
        '      "proposed_change": "exact code snippet or parameter change",',
        '      "reason": "data-driven justification referencing trade IDs or stats",',
        '      "expected_impact": "quantified if possible (e.g., +15% valid signals)"',
        '    }',
        '  ],',
        '  "regime_filter_parameter_recommendations": [',
        '    {',
        '      "parameter": "REGIME_WINDOW_1H",',
        '      "decision": "adjust|keep",',
        '      "reason": "data-driven explanation",',
        '      "suggested_value": 30',
        '    }',
        '  ],',
        '  "final_verdict": "improve|adapt|keep"',
        '}',
        "",
        "=== CRITICAL RULES ===",
        "- Reference EXACT variable/function names from main.py (e.g., ReversalScalper._detect_regime, SLOPE_THRESHOLD_1H)",
        "- Propose changes ONLY if trade data shows clear improvement potential",
        "- If data is insufficient for a parameter, KEEP it and explain why",
        "- Prioritize HARD-CODED logic over ML/LLM additions (latency requirement)",
        "- For code_change_proposals: include the exact line of code to change",
        "- Consider fee impact: a $3 win with $0.45 fee = $2.55 net - is it worth the risk?",
        "- Large losses (>$10) are priority #1 to prevent",
        "",
        "=== DATA ===",
    ]
    
    payload = {
        "symbol_filter": symbol_filter,
        "risk_parameters_from_main": risk_params,
        "regime_filter_parameters_from_main": regime_params,
        "trade_stats": stats,
        "strategy_context_from_process_signal": process_signal_context,
        "strategy_context_from_main": main_strategy_context,
        "strategy_main_full_source": full_main_source,
        "trades_sample": enriched_trades,
    }
    
    return "\n".join(instructions) + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


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
    lines.append(f"\n**Generated:** {ts}\n")

    lines.append("## Trade Statistics")
    lines.append(f"- Total trades: {stats.get('total_trades', 0)}")
    lines.append(f"- Positive: {stats.get('positive_trades', 0)}")
    lines.append(f"- Negative: {stats.get('negative_trades', 0)}")
    lines.append(f"- Neutral: {stats.get('neutral_trades', 0)}")
    lines.append(f"- Win rate: {stats.get('win_rate_pct', 0)}%")
    lines.append(f"- PnL total: {stats.get('pnl_total', 0)}")
    lines.append(f"- Fee total: {stats.get('fee_total', 0)}")
    lines.append(f"- Net PnL after fees: {stats.get('net_pnl_after_fees', 0)}")

    lines.append("\n## Parameters (from main.py)")
    lines.append("### Risk Parameters")
    for k, v in params.get("risk_parameters", {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("### Regime Filter Parameters")
    for k, v in params.get("regime_filter", {}).items():
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
        ("strategy_used_analysis", "Strategy Pattern Analysis"),
        ("fee_impact_analysis", "Fee Impact Analysis"),
        ("position_size_analysis", "Position Size Analysis"),
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
    
    Returns dict with analysis results, saves to Markdown file.
    """
    trades = get_trades(symbol_filter=symbol_filter)
    if not trades:
        return {
            "ok": False,
            "error": f"No trades found for symbol filter: {symbol_filter}",
        }
    
    # Build statistics
    stats = _build_trade_stats(trades)
    
    # Extract parameters from main.py
    regime_params = _extract_parameters_from_main(REGIME_FILTER_PARAMETER_KEYS)
    risk_params = _extract_parameters_from_main(RISK_PARAMETER_KEYS)
    
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
            "parameters_extracted": {"regime": regime_params, "risk": risk_params},
        }
    
    # Parse and validate JSON response
    try:
        llm_parsed = json.loads(llm_response)
    except json.JSONDecodeError as e:
        logger.warning(f"LLM response not valid JSON: {e}")
        llm_parsed = {"raw_response": llm_response, "parse_error": str(e)}
    
    # Build output
    output = {
        "ok": True,
        "symbol_filter": symbol_filter,
        "trade_stats": stats,
        "parameters_extracted": {
            "regime_filter": regime_params,
            "risk_parameters": risk_params,
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