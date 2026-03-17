import json
import os
import re
import sys
import ast
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
import requests

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.get_trades import get_trades


REGIME_FILTER_PARAMETER_KEYS = [
    "REGIME_WINDOW_5M",
    "REGIME_WINDOW_1H",
    "SLOPE_THRESHOLD_5M",
    "SLOPE_THRESHOLD_1H",
    "VOLUME_THRESHOLD",
]


def _to_datetime_utc(timestamp_ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _extract_process_signal_context(max_chars: int = 3200) -> str:
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


def _extract_regime_filter_parameters_from_main() -> dict[str, Any]:
    main_path = Path(__file__).resolve().parent / "main.py"
    params: dict[str, Any] = {}

    try:
        code = main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read main.py for regime parameters: %s", exc)
        return params

    for key in REGIME_FILTER_PARAMETER_KEYS:
        match = re.search(rf"self\.{key}\s*=\s*([^#\n]+)", code)
        if not match:
            continue

        raw_value = match.group(1).strip()
        try:
            params[key] = ast.literal_eval(raw_value)
        except Exception:
            params[key] = raw_value

    return params


def _read_full_main_source() -> str:
    main_path = Path(__file__).resolve().parent / "main.py"
    try:
        return main_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read full main.py source: %s", exc)
        return "main.py full source unavailable"


def _build_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    positives = 0
    negatives = 0
    neutral = 0
    pnl_sum = 0.0
    hour_counter: Counter[int] = Counter()
    day_counter: Counter[str] = Counter()
    strategy_counter: Counter[str] = Counter()

    for trade in trades:
        pnl = float(trade.get("realized_pnl") or 0.0)
        pnl_sum += pnl
        if pnl > 0:
            positives += 1
        elif pnl < 0:
            negatives += 1
        else:
            neutral += 1

        dt = _to_datetime_utc(trade.get("executed_timestamp"))
        if dt is not None:
            hour_counter[dt.hour] += 1
            day_counter[dt.strftime("%A")] += 1

        strategy_value = (
            trade.get("strategy")
            or trade.get("indicator")
            or trade.get("source")
            or "process_signal_reversal_scalper"
        )
        strategy_counter[str(strategy_value)] += 1

    return {
        "total_trades": len(trades),
        "positive_trades": positives,
        "negative_trades": negatives,
        "neutral_trades": neutral,
        "pnl_total": round(pnl_sum, 8),
        "hours_enter_distribution": dict(sorted(hour_counter.items())),
        "days_enter_distribution": dict(day_counter),
        "strategy_used_distribution": dict(strategy_counter),
    }


def _build_llm_prompt(
    symbol_filter: str,
    trades: list[dict[str, Any]],
    stats: dict[str, Any],
    regime_filter_parameters: dict[str, Any],
    process_signal_context: str,
    main_strategy_context: str,
    full_main_source: str,
) -> str:
    trades_sample = trades[:120]

    instructions = [
        "Analyze this trading performance report based on real executed trades and strategy code from main.py.",
        "The strategy to evaluate is Price Action Reversal.",
        "Read the full main.py source from strategy_main_full_source and search for any additional strategy improvements across all functions/classes.",
        "You must compare historical outcomes (past trades) against the current strategy implementation and parameters in main.py.",
        "You must propose concrete code-level changes and parameter adjustments only when justified by trade data.",
        "Return only valid JSON (no markdown, no extra text) with this schema:",
        '{"summary":"string","positive_trades_analysis":"string","negative_trades_analysis":"string","hour_enter_analysis":"string","days_enter_analysis":"string","strategy_used_analysis":"string","comparison_past_vs_current_strategy":"string","strategy_improvements":"array of strings","code_change_proposals":[{"target":"string","current_behavior":"string","proposed_change":"string","reason":"string","expected_impact":"string"}],"regime_filter_parameter_recommendations":[{"parameter":"string","decision":"adjust|keep","reason":"string","suggested_value":"number|string"}],"final_verdict":"improve|adapt|keep"}',
        "Required analysis points:",
        "1) positive trades",
        "2) negative trades",
        "3) hour enter",
        "4) days enter",
        "5) strategy used",
        "6) output strategy improve",
        "7) adjust or keep regime filter parameters",
        "If data is insufficient for a parameter change, keep it and explain why.",
        "For each code_change_proposals item, reference variables/functions from main.py (for example process_signal, ReversalScalper methods, or regime parameters).",
    ]

    payload = {
        "symbol_filter": symbol_filter,
        "trade_stats": stats,
        "regime_filter_parameters_from_main": regime_filter_parameters,
        "strategy_context_from_process_signal": process_signal_context,
        "strategy_context_from_main": main_strategy_context,
        "strategy_main_full_source": full_main_source,
        "trades_sample": trades_sample,
    }

    return "\n".join(instructions) + "\n\nDATA:\n" + json.dumps(payload, ensure_ascii=False)


def _call_llm(prompt: str) -> str:
    api_key = (
        os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("DEEP_SEEK_API_KEY")
        or os.getenv("DEEPSEEK_KEY")
        or os.getenv("DEEPSEEK_API")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_API_KEY")
    )
    model = os.getenv("LLM_MODEL", "deepseek-chat")
    api_url = os.getenv("LLM_API_URL", "https://api.deepseek.com/chat/completions")

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
                "content": "You are a quantitative trading performance analyst. Return strict JSON only.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    }

    response = requests.post(api_url, headers=headers, json=body, timeout=90)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def analyze_trade_performance(symbol_filter: str = "NEAR_USDC") -> dict[str, Any]:
    trades = get_trades(symbol_filter=symbol_filter)
    if not trades:
        return {
            "ok": False,
            "error": f"No trades found for symbol filter: {symbol_filter}",
        }

    stats = _build_trade_stats(trades)
    regime_filter_parameters = _extract_regime_filter_parameters_from_main()
    process_signal_context = _extract_process_signal_context()
    main_strategy_context = _extract_main_strategy_context()
    full_main_source = _read_full_main_source()
    prompt = _build_llm_prompt(
        symbol_filter,
        trades,
        stats,
        regime_filter_parameters,
        process_signal_context,
        main_strategy_context,
        full_main_source,
    )

    llm_response = _call_llm(prompt)

    output = {
        "ok": True,
        "symbol_filter": symbol_filter,
        "trade_stats": stats,
        "regime_filter_parameters": regime_filter_parameters,
        "process_signal_context_excerpt": process_signal_context,
        "main_strategy_context_excerpt": main_strategy_context,
        "llm_prompt": prompt,
        "llm_response": llm_response,
    }

    output_path = PROJECT_ROOT / "data" / "performance_llm_analysis.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Performance analysis saved to %s", output_path)

    return output


# if __name__ == "__main__":
#     symbol = os.getenv("PERFORMANCE_SYMBOL_FILTER", "NEAR_USDC")
#     result = analyze_trade_performance(symbol_filter=symbol)
#     print(json.dumps(result, ensure_ascii=False, indent=2))
