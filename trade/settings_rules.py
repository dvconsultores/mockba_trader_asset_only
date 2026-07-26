"""
MockbaV4 — Deterministic settings validator (Amendment 002).

Pure function. No network, no LLM, no database writes.
Same function backs UI inline warnings, Telegram /list, and bot startup.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from trade.settings_schema import SettingSpec, BY_KEY
from db.db_ops import get_setting_float, get_setting_int, get_setting_bool, get_setting


@dataclass
class Verdict:
    level: str           # "ok" | "warn" | "error"
    message: str
    suggested_value: Any | None = None


@dataclass
class SettingsContext:
    """Facts needed for cross-setting validation. Caller provides these."""
    venue: str = ""               # "binance" | "orderly" | "" (both)
    equity: float = 0.0           # current venue equity
    min_notional: float = 0.0     # symbol min notional


def _coerce(value: Any, spec: SettingSpec) -> Any:
    """Coerce a string value to the SettingSpec type. Returns None on failure."""
    if spec.type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return None
    try:
        if spec.type is float:
            return float(value)
        if spec.type is int:
            return int(float(value))
        return str(value)
    except (ValueError, TypeError):
        return None


def validate(key: str, proposed_value: Any, ctx: SettingsContext | None = None) -> Verdict:
    """
    Validate a single setting value. Returns Verdict with level, message, and suggested_value.

    ctx is required for cross-setting checks. If None, only type/range checks run.
    """
    spec = BY_KEY.get(key)
    if spec is None:
        return Verdict("error", f"Unknown setting: {key}")

    if ctx is None:
        ctx = SettingsContext()

    # ── Type coercion ──────────────────────────────────────────────────────
    value = _coerce(proposed_value, spec)
    if value is None:
        return Verdict("error", f"Invalid type for {key}: expected {spec.type.__name__}, got {type(proposed_value).__name__}")

    # ── Hard range ─────────────────────────────────────────────────────────
    if spec.hard_min is not None and isinstance(value, (int, float)) and value < spec.hard_min:
        return Verdict("error", f"{key} = {value} below minimum {spec.hard_min}", spec.hard_min)
    if spec.hard_max is not None and isinstance(value, (int, float)) and value > spec.hard_max:
        return Verdict("error", f"{key} = {value} above maximum {spec.hard_max}", spec.hard_max)

    # ── Soft range ─────────────────────────────────────────────────────────
    if spec.soft_min is not None and isinstance(value, (int, float)) and value < spec.soft_min:
        return Verdict("warn", f"{key} = {value} below recommended {spec.soft_min}", spec.soft_min)
    if spec.soft_max is not None and isinstance(value, (int, float)) and value > spec.soft_max:
        return Verdict("warn", f"{key} = {value} above recommended {spec.soft_max}", spec.soft_max)

    # ── Cross-setting checks ───────────────────────────────────────────────
    # Only run if ctx has the needed data and the key participates in cross-checks

    # tp_min_pct <= sl_min_pct
    if key == "tp_min_pct":
        sl = get_setting_float("sl_min_pct", 0.5)
        if isinstance(value, (int, float)) and value <= sl:
            sug = round(sl * 1.5, 2)
            return Verdict("error", f"tp_min_pct ({value}) must exceed sl_min_pct ({sl}). Breakeven WR would be {(sl+0.06)/(value+sl)*100:.0f}%", sug)
    if key == "sl_min_pct":
        tp = get_setting_float("tp_min_pct", 0.8)
        if isinstance(value, (int, float)) and tp <= value:
            sug = round(tp * 0.66, 2)
            return Verdict("error", f"sl_min_pct ({value}) must be below tp_min_pct ({tp})", sug)

    # tp_k <= sl_k
    if key == "tp_k":
        slk = get_setting_float("sl_k", 0.6)
        if isinstance(value, (int, float)) and value <= slk:
            sug = round(slk * 1.5, 2)
            return Verdict("warn", f"tp_k ({value}) <= sl_k ({slk}) — TP may not clear SL during volatile periods", sug)

    # Net edge: tp_min - fee - slippage < min_net_edge
    if key in ("tp_min_pct", "assumed_slippage_pct", "min_net_edge_pct"):
        tp = get_setting_float("tp_min_pct", 0.8)
        fee_key = "dex_round_trip_fee_pct" if ctx.venue == "orderly" else "cex_round_trip_fee_pct"
        fee = get_setting_float(fee_key, 0.06)
        slip = get_setting_float("assumed_slippage_pct", 0.03)
        min_edge = get_setting_float("min_net_edge_pct", 0.30)
        net = tp - fee - slip
        if net < min_edge:
            sug = round(min_edge + fee + slip + 0.05, 2)
            return Verdict("error", f"Net edge {net:.2f}% below minimum {min_edge}% (tp={tp}, fee={fee}, slip={slip})", sug)

    # Slot % × equity < min_notional × 1.5
    if key in ("dex_slot_pct", "cex_slot_pct") and ctx.equity > 0 and ctx.min_notional > 0:
        slot = (value / 100) * ctx.equity if isinstance(value, (int, float)) else 0
        floor = ctx.min_notional * 1.5
        if 0 < slot < floor:
            needed_pct = round((floor / ctx.equity) * 100, 1)
            return Verdict("error", f"Slot ${slot:.0f} below min_notional floor ${floor:.0f} — need ≥{needed_pct}% or more equity", needed_pct)

    # max_slots × slot_pct > 100
    if key in ("max_slots", "dex_slot_pct", "cex_slot_pct"):
        slots = get_setting_int("max_slots", 9)
        slot_pct = get_setting_float(f"{ctx.venue}_slot_pct", 15) if ctx.venue else 15
        if slots * slot_pct > 100:
            sug = int(100 / slot_pct) if slot_pct > 0 else slots
            return Verdict("error", f"max_slots ({slots}) × slot_pct ({slot_pct}%) = {slots*slot_pct}% > 100% of equity", sug)

    # leverage > max_leverage
    if key == "leverage":
        max_lev = get_setting_int("max_leverage", 3)
        if isinstance(value, int) and value > max_lev:
            return Verdict("error", f"leverage ({value}x) exceeds max_leverage ({max_lev}x)", max_lev)

    # dip_k so low adaptive mode is always floor-bound
    if key == "dip_k":
        adaptive = get_setting_bool("adaptive_enabled", True)
        if adaptive and isinstance(value, (int, float)) and value < 0.3:
            return Verdict("warn", f"dip_k ({value}) is very low — adaptive thresholds will be floor-bound most of the time", None)

    # Toxicity enforce + unvalidated baseline
    if key.startswith("tox_") and key.endswith("_enforce"):
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            base_key = key.replace("_enforce", "")
            try:
                from db.db_ops import get_db_connection
                with get_db_connection() as conn:
                    row = conn.execute("SELECT status FROM settings_baseline WHERE key = ?", (base_key,)).fetchone()
                    if row and row["status"] == "unvalidated":
                        return Verdict("warn", f"{key}=true but {base_key} baseline is unvalidated — no evidence this threshold is correct", None)
            except Exception:
                pass

    return Verdict("ok", "")


def validate_all(ctx: SettingsContext | None = None) -> dict[str, Verdict]:
    """Run validate() on every setting. Returns {key: Verdict}."""
    from db.db_ops import get_all_settings
    current = get_all_settings()
    results = {}
    for spec in BY_KEY.values():
        val = current.get(spec.key, None)
        if val is not None:
            results[spec.key] = validate(spec.key, val, ctx)
    return results
