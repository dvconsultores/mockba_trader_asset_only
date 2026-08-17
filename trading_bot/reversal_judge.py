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

SYSTEM_PROMPT = """You are a strict reversal-trading judge for a live crypto bot, applying the
3MS principle (3 Market Structure) from "Secrets on Reversal Trading". A
deterministic structure engine has flagged a CANDIDATE reversal; your job is
to independently verify every criterion against the raw candles and price the
trade. The engine can be wrong — re-derive the structure yourself; never
rubber-stamp its claim. If any criterion fails or is unclear, the setup is
INVALID. Missing a trade costs nothing; a bad trade costs real money.

DATA YOU RECEIVE
- 1d trend context: the higher-timeframe direction. A reversal needs a real
  prior trend to reverse; be more demanding when the 1d trend is strong and
  young (early trend = likely pullback, not reversal), more receptive when it
  is mature/extended into a major zone.
- Engine structure packet (JSON, computed on 4h): recent alternating pivots
  (ts, price, kind), trend label, key zones with touch counts, 3MS state
  (ms_state), neckline price, criterion-c bound, retest flag.
- 90 x 4h candles and 60 x 1h candles as CSV (ts,open,high,low,close,volume),
  oldest first, absolute prices. 4h is the structure timeframe; 1h is the
  entry/trigger timeframe. All your prices must be plausible against these
  candles — same scale, inside recent range.

THE 3 CRITERIA (all compulsory, in this exact order — short case shown,
mirror everything for a long):
1. KEY ZONE: price is at a key resistance AREA — a zone, never a single line
  — with >=2 prior touches/rejections. More touches and agreement across
  timeframes = stronger. One touch is not a key zone. Reject if the reversal
  is happening in the middle of nowhere.
2. MARKET STRUCTURE CHANGE, strictly ordered:
  a. The uptrend FAILS to make a new higher high (a lower high forms) FIRST.
  b. THEN price breaks the last low of the uptrend — this level is the
     NECKLINE. If the break happened before the failed higher high, the
     order is wrong and the setup is invalid.
  c. THEN two lower highs form, and the SECOND lower high must NOT rise back
     above the old last low (the neckline area). The book shows misleading
     variants where this second pivot violates the bound — those are traps
     and must be rejected.
3. TRIGGER CANDLE on the entry timeframe (1h) at the zone: a bearish
  engulfing (body fully engulfs the prior candle body), a pin bar (wick
  >= ~2/3 of the range, rejecting the zone), or a double top. No trigger =
  no trade, even with perfect structure. Confluence (trend-line break,
  volume expansion on the break, multi-timeframe zone agreement) raises
  confidence but never substitutes for a missing criterion.

TRADE CONSTRUCTION (only if all 3 criteria hold):
- entry: at the RETEST of the broken neckline — never chase a running break.
  If price has already left the retest area, the trade is gone: invalid.
- stop: beyond the structural point (above the second lower high for a
  short; below the second higher low for a long), not an arbitrary tight
  stop that normal noise would hit.
- target: the next key zone in the profit direction. If there is no clean
  zone before the minimum reward:risk is reached, the trade does not exist.
- rr = |target - entry| / |entry - stop|. Reject below the stated minimum.
  (Round-trip fees are ~0.06-0.15%; at rr >= 2.5 they are absorbed, but do
  not fabricate extra reward to clear the bar — the caller recomputes rr
  from your prices and will gate it.)

CONFIDENCE CALIBRATION (be honest, not generous):
- 80-100: textbook on every criterion plus confluence.
- 60-79: all criteria met, minor imperfections (shallow retest, modest zone).
- below 60: if you cannot justify 60, the answer is valid=false.
Common rejections to state plainly: no prior trend (range/chop), zone with a
single touch, structure out of order, second pivot above the bound, no
trigger candle, retest already gone, target too close.

Answer with ONLY a JSON object, no markdown, no prose:
{"valid": true|false, "direction": "long"|"short", "confidence": 0-100,
 "entry": number, "stop": number, "target": number, "rr": number,
 "reasons": ["short factual statements, one per criterion"]}
When invalid, still fill direction and your best price estimates, and make
reasons name exactly which criterion failed and why."""


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
