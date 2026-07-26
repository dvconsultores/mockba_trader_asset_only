"""
MockbaV4 — LLM settings helper (Amendment 002, research/ only).

Never imported by trading_bot/ or bot.py (constitution principle I).
Produces text for humans and proposals humans must approve.
"""

from __future__ import annotations
import os, json, time, hashlib
from dataclasses import dataclass, field
from typing import Any

import requests

from trade.settings_schema import BY_KEY, SettingSpec
from trade.settings_rules import validate, SettingsContext
from db.db_ops import (
    get_setting, get_setting_float, get_setting_bool, get_setting_int,
    get_all_settings, upsert_setting, get_db_connection,
)

API_KEY = os.getenv("DEEP_SEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
API_URL = "https://api.deepseek.com/v1/chat/completions"

# ── Rate limiter ──────────────────────────────────────────────────────────────

_call_times: list[float] = []


def _rate_limit_ok() -> bool:
    max_calls = get_setting_int("llm_max_calls_per_hour", 20)
    now = time.time()
    global _call_times
    _call_times = [t for t in _call_times if now - t < 3600]
    return len(_call_times) < max_calls


def _record_call():
    _call_times.append(time.time())


# ── Cache (file-based, disposable) ────────────────────────────────────────────

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "llm_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(key: str, language: str, capital_band: str) -> str:
    h = hashlib.sha256(f"{key}:{language}:{capital_band}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.json")


def _band(equity: float) -> str:
    if equity < 100:
        return "under_100"
    if equity < 1000:
        return "100_to_1k"
    if equity < 10000:
        return "1k_to_10k"
    return "above_10k"


# ═══════════════════════════════════════════════════════════════════════════════
# explain() — cached, two-sentence description
# ═══════════════════════════════════════════════════════════════════════════════

def explain(key: str, language: str = "en", capital_band: str = "100_to_1k") -> str:
    """Return a 2-3 sentence explanation of a setting. Cached."""
    path = _cache_path(key, language, capital_band)
    if os.path.exists(path):
        cache_days = get_setting_int("llm_explain_cache_days", 30)
        age = time.time() - os.path.getmtime(path)
        if age < cache_days * 86400:
            with open(path) as f:
                return json.load(f).get("text", "")

    spec = BY_KEY.get(key)
    if spec is None:
        return f"Unknown setting: {key}"

    ctx = SettingsContext()
    verdict = validate(key, get_setting(key) or "", ctx)

    if not _rate_limit_ok():
        return f"[rate limited] {spec.short}. Current: {get_setting(key) or '?'}. Verdict: {verdict.level}."

    prompt = (
        f"Explain this trading bot setting in {language} in 2-3 plain sentences. "
        f"Setting: {key} — {spec.short}. "
        f"Type: {spec.type.__name__}, unit: {spec.unit or 'none'}. "
        f"Recommended range: {spec.soft_min}–{spec.soft_max}. "
        f"Current value: {get_setting(key) or '?'}. "
        f"Validation verdict: {verdict.level} — {verdict.message}. "
        f"Capital band: {capital_band}. "
        f"Cover: what it does, what raising/lowering does, what it interacts with. "
        f"No numeric recommendations — the validator handles those."
    )

    try:
        r = requests.post(API_URL, headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }, json={
            "model": get_setting("llm_model") or "deepseek-chat",
            "temperature": 0.1, "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=get_setting_int("llm_timeout_sec", 30))
        _record_call()
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        text = f"[LLM unavailable: {e}] {spec.short}. Validator says: {verdict.level}."

    with open(path, "w") as f:
        json.dump({"text": text, "key": key, "language": language, "band": capital_band, "ts": time.time()}, f)
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# propose() — advisory, never writes to settings
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Proposal:
    key: str
    current_value: Any
    proposed_value: Any
    reason: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "no_basis"


def propose(context_summary: str = "") -> list[Proposal]:
    """
    Generate setting proposals from current state + measured context.
    Writes to settings_proposals table (NEVER to settings).
    Returns list of Proposal objects.
    """
    current = get_all_settings()
    proposals = []

    # First: deterministic validator suggestions
    ctx = SettingsContext()
    for spec in BY_KEY.values():
        val = current.get(spec.key, "")
        verdict = validate(spec.key, val, ctx)
        if verdict.level in ("error", "warn") and verdict.suggested_value is not None:
            proposals.append(Proposal(
                key=spec.key,
                current_value=val,
                proposed_value=str(verdict.suggested_value),
                reason=verdict.message,
                evidence=["deterministic_validator"],
                confidence="heuristic",
            ))

    # If LLM disabled or unavailable, return deterministic-only proposals
    if not get_setting_bool("llm_helper_enabled", True) or not API_KEY:
        _save_proposals(proposals, "deterministic")
        return proposals

    if not _rate_limit_ok():
        _save_proposals(proposals, "deterministic")
        return proposals

    # If no measured context, mark all as no_basis and return
    if not context_summary:
        for p in proposals:
            p.confidence = "no_basis"
            p.evidence = ["No measured data available — run dry-run first"]
        _save_proposals(proposals, "deterministic")
        return proposals

    # LLM-enhanced proposals
    prompt = (
        "You are a trading bot calibration assistant. Review these settings and propose changes.\n\n"
        f"=== CURRENT SETTINGS ===\n{json.dumps({k: v for k, v in sorted(current.items())}, indent=2)}\n\n"
        f"=== MEASURED CONTEXT ===\n{context_summary}\n\n"
        "=== DETERMINISTIC SUGGESTIONS ===\n"
        + "\n".join(f"- {p.key}: {p.current_value} → {p.proposed_value} ({p.reason})" for p in proposals)
        + "\n\nFor each proposal, determine confidence: 'measured' if grounded in the context data, "
        "'heuristic' if based on rules of thumb, 'no_basis' if no data supports it.\n"
        "Reply with JSON array: [{\"key\":\"...\", \"confidence\":\"...\", \"reason\":\"...\", \"evidence\":[...]}]\n"
        "Only include settings where you have a specific recommendation. Do not invent numbers."
    )

    try:
        r = requests.post(API_URL, headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }, json={
            "model": get_setting("llm_model") or "deepseek-chat",
            "temperature": 0.1, "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }, timeout=get_setting_int("llm_timeout_sec", 30))
        _record_call()
        r.raise_for_status()
        llm_result = json.loads(r.json()["choices"][0]["message"]["content"])
        if isinstance(llm_result, dict):
            llm_result = llm_result.get("proposals", llm_result.get("recommendations", []))
        if isinstance(llm_result, list):
            for item in llm_result:
                if isinstance(item, dict) and item.get("key") in BY_KEY:
                    for p in proposals:
                        if p.key == item["key"]:
                            p.confidence = item.get("confidence", "no_basis")
                            p.reason = item.get("reason", p.reason)
                            p.evidence = item.get("evidence", p.evidence)
    except Exception:
        pass  # LLM failure → keep deterministic proposals

    _save_proposals(proposals, "telegram" if context_summary else "deterministic")
    return proposals


def _save_proposals(proposals: list[Proposal], source: str):
    model = get_setting("llm_model") or "none"
    now = time.time()
    with get_db_connection() as conn:
        for p in proposals:
            conn.execute("""
                INSERT INTO settings_proposals (created_at, source, key, current_value, proposed_value,
                    reason, evidence, confidence, status, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (now, source, p.key, str(p.current_value), str(p.proposed_value),
                  p.reason, json.dumps(p.evidence), p.confidence, model))
        conn.commit()
