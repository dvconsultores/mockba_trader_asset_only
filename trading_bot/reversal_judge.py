"""
MockbaV4 — DeepSeek reversal judge (spec 001).

The deterministic engine (trade/structure.py) produces a candidate; this
module asks DeepSeek v4-pro (reasoner, thinking enabled, effort high) to
verify each 3MS criterion and price the trade. The AI is a judge with a
checklist, never an oracle: a malformed or unverifiable answer fails closed.

Raw `requests` on purpose — matches the codebase's exchange clients, no SDK
dependency. `DEEPSEEK_API_KEY` lives in .env (gitignored), like all keys.
"""

from __future__ import annotations
import json
import os
import requests

from dotenv import load_dotenv
load_dotenv()

from db.db_ops import get_setting
from logs.log_config import apolo_trader_logger as logger

API_URL = "https://api.deepseek.com/chat/completions"
REQUIRED = {"valid", "direction", "confidence", "entry", "stop", "target", "rr", "reasons"}

SYSTEM_PROMPT = """You are a strict reversal-trading judge applying the 3MS principle
(3 Market Structure). You receive a candidate produced by a deterministic
structure engine plus multi-timeframe candles. Verify EVERY criterion; if any
fails or is unclear, the setup is invalid. Criteria:
1. Price is at a key support/resistance AREA (>=2 prior touches/rejections).
2. Market structure change, in order: (a) failure to make a higher high (for
   short; lower low for long) occurring FIRST, (b) break of the last low/high
   of the prior trend (the neckline), (c) two lower highs (higher lows), the
   second NOT beyond the last low/high of the prior trend.
3. A reversal trigger candle (engulfing / pin bar / double top-bottom) at the
   zone, confirmed on the entry timeframe.
Trade construction: entry on the RETEST of the broken neckline (never chase);
stop beyond the structural point; target at the next key zone; reject if
reward:risk < the stated minimum. Missing a trade is acceptable; a bad trade
is not.
Answer with ONLY a JSON object, no markdown, no prose:
{"valid": true|false, "direction": "long"|"short", "confidence": 0-100,
 "entry": number, "stop": number, "target": number, "rr": number,
 "reasons": ["short factual statements, one per criterion"]}"""


def _fmt_candles(candles: list[dict], n: int) -> str:
    rows = [
        f"{int(c['ts'])},{c['open']:.6g},{c['high']:.6g},{c['low']:.6g},{c['close']:.6g},{c.get('volume', 0):.4g}"
        for c in candles[-n:]
    ]
    return "ts,open,high,low,close,volume\n" + "\n".join(rows)


def build_prompt(asset: str, structure_packet: str, tf_1d_trend: str,
                 candles_4h: list[dict], candles_1h: list[dict],
                 rr_min: float) -> str:
    return (
        f"Asset: {asset}\n"
        f"1d trend context: {tf_1d_trend}\n"
        f"Minimum acceptable reward:risk: {rr_min}\n\n"
        f"Engine candidate (4h structure packet, JSON):\n{structure_packet}\n\n"
        f"4h candles (oldest first):\n{_fmt_candles(candles_4h, 90)}\n\n"
        f"1h candles (oldest first):\n{_fmt_candles(candles_1h, 60)}\n\n"
        "Verify the candidate against every criterion and answer with the JSON object only."
    )


def _validate(verdict: dict, rr_min: float) -> dict | None:
    if not REQUIRED.issubset(verdict):
        return None
    try:
        v = {
            "valid": bool(verdict["valid"]),
            "direction": str(verdict["direction"]),
            "confidence": float(verdict["confidence"]),
            "entry": float(verdict["entry"]),
            "stop": float(verdict["stop"]),
            "target": float(verdict["target"]),
            "rr": float(verdict["rr"]),
            "reasons": [str(r) for r in verdict["reasons"]],
        }
    except (TypeError, ValueError):
        return None
    if v["direction"] not in ("long", "short"):
        return None
    if v["valid"]:
        # a "valid" verdict must be internally coherent
        if v["direction"] == "long" and not (v["stop"] < v["entry"] < v["target"]):
            return None
        if v["direction"] == "short" and not (v["target"] < v["entry"] < v["stop"]):
            return None
        risk = abs(v["entry"] - v["stop"])
        if risk <= 0:
            return None
        computed_rr = abs(v["target"] - v["entry"]) / risk
        v["rr"] = computed_rr                     # trust math over the model's arithmetic
        if computed_rr < rr_min:
            v["valid"] = False
            v["reasons"].append(f"rr {computed_rr:.2f} below minimum {rr_min}")
    return v


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def judge(asset: str, structure_packet: str, tf_1d_trend: str,
          candles_4h: list[dict], candles_1h: list[dict],
          rr_min: float = 2.5) -> tuple[dict | None, str, str]:
    """Ask DeepSeek to verify a candidate.

    Returns (verdict|None, reasoning_content, model). None = fail closed
    (API error, malformed output after retry, or incoherent prices)."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    model = get_setting("judge_model") or "deepseek-v4-pro"
    if not api_key:
        logger.warning("[JUDGE] DEEPSEEK_API_KEY missing — judge unavailable")
        return None, "", model
    prompt = build_prompt(asset, structure_packet, tf_1d_trend, candles_4h, candles_1h, rr_min)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "reasoning_effort": get_setting("judge_effort") or "high",
        "thinking": {"type": "enabled"},
    }
    reasoning = ""
    for attempt in range(2):
        try:
            r = requests.post(
                API_URL, json=body, timeout=180,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
            )
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            reasoning = msg.get("reasoning_content") or reasoning
            parsed = _extract_json(msg.get("content") or "")
            if parsed is not None:
                verdict = _validate(parsed, rr_min)
                if verdict is not None:
                    return verdict, reasoning, model
            logger.warning(f"[JUDGE] {asset}: malformed verdict (attempt {attempt + 1})")
        except Exception as e:
            logger.warning(f"[JUDGE] {asset}: API error (attempt {attempt + 1}): {e}")
    return None, reasoning, model
