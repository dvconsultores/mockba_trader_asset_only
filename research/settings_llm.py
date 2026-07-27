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
# explain_all() — reads all settings, LLM analyzes and explains each
# ═══════════════════════════════════════════════════════════════════════════════

def explain_all(language: str = "en", capital_band: str = "100_to_1k") -> str:
    """Read all settings from DB, send to LLM for analysis, return a paragraph
    per setting covering what it does, its current value, and what changing it means.
    Cached for the same hour (settings-snapshot hash)."""
    current = get_all_settings()
    if not current:
        return "No settings found in database."

    # Build a snapshot hash for caching (changes when settings change)
    snapshot = json.dumps(dict(sorted(current.items())), sort_keys=True)
    cache_key = hashlib.sha256(f"explain_all:{snapshot}:{language}:{capital_band}".encode()).hexdigest()[:16]
    cache_file = os.path.join(CACHE_DIR, f"explain_all_{cache_key}.json")
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 3600:  # 1-hour cache
            with open(cache_file) as f:
                return json.load(f).get("text", "")

    # Build context with schema info + current values + validation
    ctx = SettingsContext()
    settings_summary = []
    for key, val in sorted(current.items()):
        spec = BY_KEY.get(key)
        verdict = validate(key, val, ctx)
        if spec:
            settings_summary.append(
                f"  {key} = {val}  (type: {spec.type.__name__}, unit: {spec.unit or 'none'}, "
                f"range: {spec.soft_min}–{spec.soft_max}, verdict: {verdict.level})"
            )
        else:
            settings_summary.append(f"  {key} = {val}  (verdict: {verdict.level})")

    if not _rate_limit_ok():
        # Grouped layout matching the Telegram fallback
        return _format_settings_grouped(current, "⚠️ LLM rate-limited — validator overview only")

    prompt = (
        f"Analyze these trading bot settings in {language}. "
        f"Capital band: {capital_band}.\n\n"
        f"=== ALL SETTINGS ===\n"
        + "\n".join(settings_summary)
        + "\n\nFor EACH setting, write ONE paragraph (3-4 sentences) covering:\n"
        "1. What the setting controls and why it matters.\n"
        "2. What the current value means in practice.\n"
        "3. What would happen if you raised it vs lowered it.\n"
        "4. How it interacts with other settings (if any).\n"
        "Format each paragraph with the setting key as a bold header, like:\n"
        "**setting_name**: explanation paragraph here.\n\n"
        "Do NOT give numeric recommendations. Focus on education, not calibration.\n"
        "If a setting's verdict is 'error', note that it's outside the safe range."
    )

    try:
        r = requests.post(API_URL, headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }, json={
            "model": get_setting("llm_model") or "deepseek-chat",
            "temperature": 0.1, "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=get_setting_int("llm_timeout_sec", 60))
        _record_call()
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        text = _format_settings_grouped(current, f"⚠️ LLM unavailable — validator overview")

    with open(cache_file, "w") as f:
        json.dump({"text": text, "settings_count": len(current), "ts": time.time()}, f)
    return text


def _format_settings_grouped(current: dict, header: str = "") -> str:
    """Produce a friendly, manual-style settings overview — easy to read like a book."""
    from trade.settings_rules import validate, SettingsContext
    ctx = SettingsContext()

    lines = [f"{header}"] if header else []
    lines.append("📖 Your Trading Bot Manual\n")
    lines.append("Here is what each setting does and what it means for your trading.\n")

    # Helper: only consider settings that exist in the schema (skip legacy/unknown keys)
    from trade.settings_schema import BY_KEY
    def v(k): return current.get(k, "")
    def verdict(k):
        if k not in BY_KEY:
            return type('V', (), {'level': 'skip', 'message': ''})()
        return validate(k, v(k), ctx)
    def has_error(k):
        vd = verdict(k)
        return vd.level == "error"
    def has_warn(k):
        vd = verdict(k)
        return vd.level == "warn"

    # Shorthand for common values (only schema-known settings)
    def g(k, default=""): return v(k) if k in BY_KEY else default

    # Shorthand for common values
    tp = v("tp_min_pct"); sl = v("sl_min_pct")
    lev = v("leverage"); ml = v("max_leverage")
    dip = v("dip_min_pct"); pump = v("pump_min_pct")
    adaptive = v("adaptive_enabled") in ("true", "True", "1")
    cooldown = v("cooldown_sec")
    spacing = v("min_entry_spacing_pct")
    dloss = v("daily_loss_limit_pct"); dloss_abs = v("daily_loss_limit")
    consec = v("max_consecutive_losses")
    fee_d = v("dex_round_trip_fee_pct"); fee_c = v("cex_round_trip_fee_pct")
    slip = v("assumed_slippage_pct"); edge = v("min_net_edge_pct")
    net = float(tp or 0.8) - float(fee_d or 0.06) - float(slip or 0.03)
    atr_p = v("atr_period")
    lev_val = int(g("leverage", "3") or 3)

    # ═══════════════════════════════════════════════════════
    # Chapter 1: How the bot decides to buy and sell
    # ═══════════════════════════════════════════════════════
    lines.append("━━━ 🎯 How the bot enters trades ━━━\n")

    lines.append(f"Your bot watches prices and looks for dips to buy and pumps to sell. "
                 f"Right now, a dip must be at least {dip}% below the recent peak before "
                 f"the bot considers buying. A pump must be at least {pump}% above the "
                 f"recent trough before it considers selling.")

    if adaptive:
        lines.append(f"\nAdaptive thresholds are ON. This means the bot uses market "
                     f"volatility to decide how big a dip or pump needs to be. When the "
                     f"market is calm, it uses the minimums above ({dip}% / {pump}%). "
                     f"When the market gets wild, it automatically raises those bars so "
                     f"it does not jump into every wiggle.")
    else:
        lines.append(f"\nAdaptive thresholds are OFF. The bot will always use exactly "
                     f"{dip}% for dips and {pump}% for pumps, no matter how volatile "
                     f"the market gets.")

    lines.append(f"\nAfter each entry, the bot waits at least {cooldown} seconds before "
                 f"entering the same asset again in the same direction. It also requires "
                 f"at least {spacing}% distance from any existing position's entry price, "
                 f"so it does not stack entries on top of each other.")

    # ═══════════════════════════════════════════════════════
    # Chapter 2: Profit targets and stop losses
    # ═══════════════════════════════════════════════════════
    lines.append("\n━━━ 💰 Profit targets and stop losses ━━━\n")

    if not has_error("tp_min_pct") and not has_error("sl_min_pct"):
        lines.append(f"Your take-profit is set to {tp}% and your stop-loss to {sl}%. "
                     f"This means the bot aims to make {tp}% on winning trades and limits "
                     f"losses to {sl}% on losing ones.")
    else:
        lines.append(f"⚠️ Your take-profit ({tp}%) and stop-loss ({sl}%) need attention. "
                     f"Take-profit must be higher than stop-loss, otherwise the math "
                     f"does not work in your favor.")
        if has_error("tp_min_pct"):
            lines.append(f"   Problem: {verdict('tp_min_pct').message}")
        if has_error("sl_min_pct"):
            lines.append(f"   Problem: {verdict('sl_min_pct').message}")

    lines.append(f"\nAfter accounting for exchange fees and expected slippage, your net "
                 f"edge per trade is approximately {net:.2f}% (take-profit {tp}% minus "
                 f"DEX fees {fee_d}% minus slippage {slip}%). The bot refuses to trade "
                 f"if this drops below {edge}% — this is your safety floor.")

    if lev_val > 1:
        lines.append(f"\nOn DEX (futures), you are using {lev_val}x leverage with a "
                     f"hard cap of {ml}x. Every position has a mandatory stop-loss — "
                     f"the bot never leaves a leveraged position unprotected.")

    # ═══════════════════════════════════════════════════════
    # Chapter 3: How the bot protects your capital
    # ═══════════════════════════════════════════════════════
    lines.append("\n━━━ 🛡️ Capital protection ━━━\n")

    if float(dloss or 0) > 0:
        lines.append(f"Your daily loss limit is set to {dloss}% of your equity. If your "
                     f"total losses in a single day reach that threshold, the bot stops "
                     f"trading for the rest of the day. This prevents one bad session "
                     f"from doing serious damage.")
    else:
        lines.append(f"⚠️ Your daily loss limit is set to 0, which means it is "
                     f"disabled. Consider setting it to 5% or so — it is your circuit "
                     f"breaker for bad days.")

    if float(consec or 0) > 0:
        lines.append(f"If the bot loses {consec} trades in a row, it will also stop. "
                     f"This is an extra safety net for detecting when the strategy "
                     f"is out of sync with the market.")
    else:
        lines.append(f"Consecutive loss protection is OFF. Turning it on (e.g., set "
                     f"to 4) adds another layer of safety.")

    if float(dloss_abs or 0) > 0:
        lines.append(f"There is also an absolute daily loss limit of ${dloss_abs}. "
                     f"If total dollar losses cross that line, trading stops.")

    # ═══════════════════════════════════════════════════════
    # Chapter 4: Technical details (for reference)
    # ═══════════════════════════════════════════════════════
    lines.append("\n━━━ 🔧 Technical reference ━━━\n")

    lines.append(f"⏱️  ATR uses {atr_p} five-minute candles to measure volatility.")
    lines.append(f"💰  DEX fees: {fee_d}% round-trip. CEX fees: {fee_c}% round-trip.")
    lines.append(f"📏  Assumed slippage: {slip}%. Minimum net edge required: {edge}%.")

    if float(v("max_hold_minutes_spot") or 0) > 0:
        lines.append(f"⏰  Spot positions auto-close after {v('max_hold_minutes_spot')} minutes.")
    if float(v("max_hold_minutes_futures") or 0) > 0:
        lines.append(f"⏰  Futures positions auto-close after {v('max_hold_minutes_futures')} minutes.")

    # ═══════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════
    errors = sum(1 for k in current if has_error(k))
    warns = sum(1 for k in current if has_warn(k))
    if errors > 0 or warns > 0:
        lines.append(f"\n━━━ ⚠️ Issues to fix ━━━")
        lines.append(f"There are {errors} errors and {warns} warnings in your configuration. "
                     f"Use /propose to get suggestions on what to change.")

    return "\n".join(lines)


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
