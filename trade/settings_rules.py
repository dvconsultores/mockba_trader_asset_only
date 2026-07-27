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
    # Multi-asset (Amendment 004)
    asset_configs: list[dict] | None = None  # from get_all_asset_configs()
    dex_equity: float = 0.0       # Orderly DEX account balance
    cex_equity: float = 0.0       # Binance CEX account balance


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

    # Multi-asset: max_active_pairs vs actual active pairs
    if key == "max_active_pairs" and ctx.asset_configs is not None:
        actual = sum(
            (1 if (c.get("active_dex") or 0) and (c.get("capital_dex") or 0) > 0 else 0) +
            (1 if (c.get("active_cex") or 0) and (c.get("capital_cex") or 0) > 0 else 0)
            for c in ctx.asset_configs
        )
        if isinstance(value, int) and actual > value:
            return Verdict("warn", f"{actual} active pairs exceed max_active_pairs ({value})", actual)

    # Multi-asset: max_concurrent_positions vs max_slots (already handled above)
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


# ── Per-asset capital validation (Amendment 004) ─────────────────────────────

def validate_asset_capital(
    symbol: str,
    capital_dex: float,
    capital_cex: float,
    active_dex: bool,
    active_cex: bool,
    ctx: SettingsContext | None = None,
) -> list[Verdict]:
    """Validate a single asset's capital and flags. Returns list of verdicts (empty = OK)."""
    results: list[Verdict] = []
    if ctx is None:
        ctx = SettingsContext()

    # Active without capital
    if active_dex and capital_dex <= 0:
        results.append(Verdict("warn", f"{symbol}: active_dex=true but capital_dex=0 — DEX will be skipped"))
    if active_cex and capital_cex <= 0:
        results.append(Verdict("warn", f"{symbol}: active_cex=true but capital_cex=0 — CEX will be skipped"))

    # Negative capital
    if capital_dex < 0:
        results.append(Verdict("error", f"{symbol}: capital_dex must be >= 0", 0.0))
    if capital_cex < 0:
        results.append(Verdict("error", f"{symbol}: capital_cex must be >= 0", 0.0))

    return results


def validate_asset_overallocation(ctx: SettingsContext) -> list[Verdict]:
    """Check that sum of allocated capital per venue does not exceed equity."""
    results: list[Verdict] = []
    if ctx.asset_configs is None:
        return results

    # DEX overallocation
    dex_allocated = sum(
        c.get("capital_dex", 0) or 0
        for c in ctx.asset_configs
        if (c.get("active_dex") or 0) and (c.get("capital_dex") or 0) > 0
    )
    if ctx.dex_equity > 0 and dex_allocated > ctx.dex_equity:
        over = dex_allocated - ctx.dex_equity
        results.append(Verdict(
            "error",
            f"DEX overallocation: total ${dex_allocated:.0f} exceeds available ${ctx.dex_equity:.0f} by ${over:.0f}",
            ctx.dex_equity,
        ))

    # CEX overallocation
    cex_allocated = sum(
        c.get("capital_cex", 0) or 0
        for c in ctx.asset_configs
        if (c.get("active_cex") or 0) and (c.get("capital_cex") or 0) > 0
    )
    if ctx.cex_equity > 0 and cex_allocated > ctx.cex_equity:
        over = cex_allocated - ctx.cex_equity
        results.append(Verdict(
            "error",
            f"CEX overallocation: total ${cex_allocated:.0f} exceeds available ${ctx.cex_equity:.0f} by ${over:.0f}",
            ctx.cex_equity,
        ))

    return results


def validate_all_assets(ctx: SettingsContext | None = None) -> dict[str, list[Verdict]]:
    """Run per-asset validation on all asset_configs rows. Returns {symbol: [Verdict]}."""
    from db.db_ops import get_all_asset_configs
    if ctx is None:
        ctx = SettingsContext()
    configs = ctx.asset_configs if ctx.asset_configs is not None else get_all_asset_configs()
    results: dict[str, list[Verdict]] = {}
    for c in configs:
        verdicts = validate_asset_capital(
            c["symbol"],
            float(c.get("capital_dex", 0) or 0),
            float(c.get("capital_cex", 0) or 0),
            bool(c.get("active_dex", 0)),
            bool(c.get("active_cex", 0)),
            ctx,
        )
        if verdicts:
            results[c["symbol"]] = verdicts
    # Overallocation check
    overall = validate_asset_overallocation(ctx)
    if overall:
        results["__overallocation__"] = overall
    return results


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
