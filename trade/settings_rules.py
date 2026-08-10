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
    # Per-venue pools (Amendment 003)
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

    # Spot-only SL overrides must stay below the TP (mirror the shared checks)
    if key == "sl_min_pct_spot":
        tp = get_setting_float("tp_min_pct", 0.8)
        if isinstance(value, (int, float)) and tp <= value:
            sug = round(tp * 0.66, 2)
            return Verdict("error", f"sl_min_pct_spot ({value}) must be below tp_min_pct ({tp})", sug)
    if key == "sl_k_spot":
        tkp = get_setting_float("tp_k", 1.0)
        if isinstance(value, (int, float)) and tkp <= value:
            sug = round(tkp * 0.8, 2)
            return Verdict("warn", f"sl_k_spot ({value}) >= tp_k ({tkp}) — TP may not clear SL during volatile periods", sug)

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

    # Per-venue fee rate (Amendment 003): the venue's own fee must clear the
    # net-edge gate at tp_min_pct. DEX and CEX use their own rates, so the
    # same tp/slip can pass on DEX and fail on CEX.
    if key in ("dex_round_trip_fee_pct", "cex_round_trip_fee_pct"):
        tp = get_setting_float("tp_min_pct", 0.8)
        slip = get_setting_float("assumed_slippage_pct", 0.03)
        min_edge = get_setting_float("min_net_edge_pct", 0.30)
        net = tp - value - slip
        if net < min_edge:
            sug = round(max(0.0, tp - slip - min_edge - 0.05), 3)
            return Verdict("error", f"Net edge {net:.2f}% below minimum {min_edge}% for {key} (tp={tp}, fee={value}, slip={slip})", sug)

    # max_active_pairs vs actual universe size (Amendment 003)
    if key == "max_active_pairs":
        if isinstance(value, int):
            try:
                from db.db_ops import get_db_connection
                with get_db_connection() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) AS c FROM asset_universe WHERE blacklisted = 0"
                    ).fetchone()
                actual = int(row["c"]) if row else 0
                if actual > value:
                    return Verdict("warn", f"{actual} universe assets exceed max_active_pairs ({value})", actual)
            except Exception:
                pass

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

    # ── Universe cross-checks (Amendment 003) ─────────────────────────────
    if key == "universe_rank_min":
        rmax = get_setting_int("universe_rank_max", 90)
        if isinstance(value, int) and value >= rmax:
            return Verdict("error", f"universe_rank_min ({value}) must be below universe_rank_max ({rmax}) — band is empty", rmax - 1)
    if key == "universe_rank_max":
        rmin = get_setting_int("universe_rank_min", 15)
        if isinstance(value, int) and value <= rmin:
            return Verdict("error", f"universe_rank_max ({value}) must exceed universe_rank_min ({rmin}) — band is empty", rmin + 1)

    if key == "universe_spread_ratio_max":
        if isinstance(value, (int, float)) and value > 0.25:
            return Verdict("warn", f"universe_spread_ratio_max ({value}) — spread would exceed a quarter of TP", 0.25)

    if key == "universe_max_age_hours":
        interval = get_setting_int("universe_scan_interval_hours", 24)
        if isinstance(value, int) and value < interval:
            return Verdict("error", f"universe_max_age_hours ({value}) < universe_scan_interval_hours ({interval}) — universe is permanently stale", interval + 1)
    if key == "universe_scan_interval_hours":
        max_age = get_setting_int("universe_max_age_hours", 36)
        if isinstance(value, int) and value > max_age:
            return Verdict("error", f"universe_scan_interval_hours ({value}) > universe_max_age_hours ({max_age}) — universe is permanently stale", max_age - 1)

    # max_slots × slot_pct > 100 → error (per venue)
    if key == "max_slots_cex":
        pct = get_setting_float("cex_slot_pct", 10.0)
        if isinstance(value, int) and value * pct > 100:
            sug = max(1, int(100 / pct))
            return Verdict("error", f"max_slots_cex ({value}) × cex_slot_pct ({pct}%) = {value*pct:.0f}% of equity — exceeds 100%", sug)
    if key == "max_slots_dex":
        pct = get_setting_float("dex_slot_pct", 10.0)
        if isinstance(value, int) and value * pct > 100:
            sug = max(1, int(100 / pct))
            return Verdict("error", f"max_slots_dex ({value}) × dex_slot_pct ({pct}%) = {value*pct:.0f}% of equity — exceeds 100%", sug)
    if key == "cex_slot_pct":
        slots = get_setting_int("max_slots_cex", 9)
        if isinstance(value, (int, float)) and slots * value > 100:
            sug = round(100 / slots, 2)
            return Verdict("error", f"max_slots_cex ({slots}) × cex_slot_pct ({value}%) = {slots*value:.0f}% of equity — exceeds 100%", sug)
    if key == "dex_slot_pct":
        slots = get_setting_int("max_slots_dex", 9)
        if isinstance(value, (int, float)) and slots * value > 100:
            sug = round(100 / slots, 2)
            return Verdict("error", f"max_slots_dex ({slots}) × dex_slot_pct ({value}%) = {slots*value:.0f}% of equity — exceeds 100%", sug)

    # universe_size vs stored universe (warn — universe will be short)
    if key == "universe_size":
        if isinstance(value, int):
            try:
                from db.db_ops import get_db_connection
                with get_db_connection() as conn:
                    rows = conn.execute(
                        "SELECT venue, COUNT(*) AS c FROM asset_universe GROUP BY venue"
                    ).fetchall()
                short = [f"{r['venue']}={r['c']}" for r in rows if r["c"] < value]
                if short:
                    return Verdict("warn", f"universe_size ({value}) exceeds stored universe count ({', '.join(short)}) — universe will be short", None)
            except Exception:
                pass

    # depth multiple × slot size vs stored depth (warn — universe will be empty)
    if key in ("universe_depth_slot_multiple", "cex_slot_pct", "dex_slot_pct"):
        try:
            from db.db_ops import get_db_connection
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT venue, AVG(depth_bid_top10) AS db FROM asset_universe "
                    "WHERE depth_bid_top10 IS NOT NULL GROUP BY venue LIMIT 1"
                ).fetchone()
                if not row or not row["db"]:
                    pass
                else:
                    mult = get_setting_float("universe_depth_slot_multiple", 3.0)
                    eqv = 0.0
                    try:
                        eqrow = conn.execute(
                            "SELECT equity FROM venue_state WHERE venue = ?", (row["venue"],)
                        ).fetchone()
                        eqv = float(eqrow["equity"]) if eqrow else 0.0
                    except Exception:
                        eqv = 0.0
                    if eqv > 0:
                        vpct = get_setting_float(
                            "cex_slot_pct" if row["venue"] == "binance" else "dex_slot_pct", 10.0
                        )
                        slot = eqv * vpct / 100
                        if mult * slot > float(row["db"]):
                            return Verdict("warn",
                                f"depth requirement ({mult}× slot ${slot:.0f} ≈ ${mult*slot:.0f}) "
                                f"exceeds stored median depth ${float(row['db']):.0f} — universe will be empty", None)
        except Exception:
            pass

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


# ── Per-venue capital pools (Amendment 003) ──────────────────────────────────

def validate_capital_pools(ctx: SettingsContext | None = None) -> list[Verdict]:
    """Amendment 003: per-venue capital pools vs live equity.

    Declared capital is a UI/validation quantity only — sizing never reads it.
    A divergence beyond 25% is a warning; sizing always uses live exchange
    equity. Also enforces max_slots × slot_pct <= 100 per venue.
    """
    results: list[Verdict] = []
    if ctx is None:
        ctx = SettingsContext()

    for venue, equity, pool_key, slot_key, max_slots_key in (
        ("binance", ctx.cex_equity, "capital_cex_usdt", "cex_slot_pct", "max_slots_cex"),
        ("orderly", ctx.dex_equity, "capital_dex_usdc", "dex_slot_pct", "max_slots_dex"),
    ):
        pool = get_setting_float(pool_key, 0.0)
        if equity > 0 and pool > 0:
            diff = abs(pool - equity) / equity
            if diff > 0.25:
                results.append(Verdict(
                    "warn",
                    f"{venue}: declared capital ${pool:,.0f} diverges from live equity "
                    f"${equity:,.0f} by {diff * 100:.0f}% — exchange wins, sizing unchanged",
                    None,
                ))
        pct = get_setting_float(slot_key, 10.0)
        slots = get_setting_int(max_slots_key, 9)
        if slots * pct > 100:
            results.append(Verdict(
                "error",
                f"{venue}: max_slots ({slots}) × slot_pct ({pct}%) = {slots * pct:.0f}% of equity — exceeds 100%",
                None,
            ))

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
