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


# Stop more than 2.5× the TP ⇒ breakeven WR > ~71% — beyond anything the
# measured confirmed-arm WR (75%) leaves margin for.
PAYOFF_WARN_RATIO = 2.5


def _payoff_warn(tp: Any, sl: Any, tp_name: str, sl_name: str) -> Verdict | None:
    """Constitution II v1.1.0: payoff < 1 is allowed; only extreme ratios warn."""
    if not isinstance(tp, (int, float)) or not isinstance(sl, (int, float)):
        return None
    if tp <= 0 or sl <= 0:
        return None
    if sl / tp > PAYOFF_WARN_RATIO:
        bwr = sl / (tp + sl) * 100
        return Verdict("warn",
            f"{sl_name} ({sl}) is more than {PAYOFF_WARN_RATIO}x {tp_name} ({tp}) — "
            f"breakeven win rate {bwr:.0f}%; needs measured-WR evidence (Constitution II v1.1.0)",
            round(tp * PAYOFF_WARN_RATIO, 2))
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

    # ── Cross-setting checks ───────────────────────────────────────────────
    # Only run if ctx has the needed data and the key participates in cross-checks

    # Payoff-ratio sanity (Constitution II v1.1.0). The pre-amendment rule
    # required tp > sl; v1.1.0 prices reward against COST (net-edge checks
    # below) and explicitly allows payoff < 1 with an asymmetric hit rate —
    # the ratified wide-stop configuration (sl floor 1.5 vs tp floor 0.8) is
    # valid. Only an extreme ratio still warns: stop > 2.5× the TP means a
    # breakeven win rate above ~71%, which needs measured-WR evidence.
    if key == "tp_min_pct":
        for sl_key in ("sl_min_pct", "sl_min_pct_spot"):
            sl = get_setting_float(sl_key, 0.0)
            v = _payoff_warn(value, sl, "tp_min_pct", sl_key)
            if v:
                return v
    if key in ("sl_min_pct", "sl_min_pct_spot"):
        tp = get_setting_float("tp_min_pct", 0.8)
        v = _payoff_warn(tp, value, "tp_min_pct", key)
        if v:
            return v
    if key == "tp_k":
        for sl_key in ("sl_k", "sl_k_spot"):
            slk = get_setting_float(sl_key, 0.0)
            v = _payoff_warn(value, slk, "tp_k", sl_key)
            if v:
                return v

    # Crash-guard floor must not sit strictly inside the spot SL floor — an
    # inside floor would pre-empt and cancel the working spot stop (Constitution
    # III). Equality is allowed: the guard and the static floor trigger at the
    # same level, so the guard stays a pure gap-catcher (AC10, clarified Q4).
    if key == "max_loss_per_position_pct":
        sl_spot = get_setting_float("sl_min_pct_spot", get_setting_float("sl_min_pct", 0.5))
        if isinstance(value, (int, float)) and value < sl_spot:
            sug = round(sl_spot + 0.1, 2)
            return Verdict("error",
                f"max_loss_per_position_pct ({value}) is strictly inside the spot SL floor ({sl_spot}) — "
                f"the guard would pre-empt and cancel the spot stop", sug)

    if key in ("sl_k", "sl_k_spot"):
        tkp = get_setting_float("tp_k", 1.0)
        v = _payoff_warn(tkp, value, "tp_k", key)
        if v:
            return v

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

    # BNB fee-discount coherence (feature 017): the configured CEX round-trip
    # rate must match how fees are actually paid — 0.15 with the discount,
    # 0.20 without. A mismatch mis-prices the Constitution II cost gate.
    if key in ("cex_fee_bnb", "cex_round_trip_fee_pct"):
        bnb_on = value if key == "cex_fee_bnb" else get_setting_bool("cex_fee_bnb", False)
        rate = value if key == "cex_round_trip_fee_pct" else get_setting_float("cex_round_trip_fee_pct", 0.20)
        if isinstance(rate, (int, float)):
            if bnb_on and rate > 0.16:
                return Verdict("warn",
                    f"cex_fee_bnb=true but cex_round_trip_fee_pct ({rate}) is the full rate — "
                    f"cost gate over-prices entries; set 0.15", 0.15)
            if not bnb_on and rate < 0.19:
                return Verdict("warn",
                    f"cex_fee_bnb=false but cex_round_trip_fee_pct ({rate}) assumes the BNB discount — "
                    f"cost gate under-prices entries; set 0.20", 0.20)

    # max_active_pairs vs actual universe size (Amendment 003).
    # PER VENUE: bot.py truncates each venue's list to max_active_pairs
    # separately, so only a single venue exceeding the cap drops assets.
    # (The old check summed both venues against the per-venue cap and warned
    # even when nothing was being dropped — 2026-08-16 fix.)
    if key == "max_active_pairs":
        if isinstance(value, int):
            try:
                from db.db_ops import get_db_connection
                with get_db_connection() as conn:
                    rows = conn.execute(
                        "SELECT venue, COUNT(*) AS c FROM asset_universe "
                        "WHERE blacklisted = 0 GROUP BY venue"
                    ).fetchall()
                over = [f"{r['venue']}={r['c']}" for r in rows if int(r["c"]) > value]
                if over:
                    worst = max(int(r["c"]) for r in rows)
                    return Verdict("warn",
                        f"universe exceeds max_active_pairs ({value}) on {', '.join(over)} — excess assets are never traded",
                        worst)
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

    # universe_max_atr_pct so low it would empty the stored spot universe
    # (Constitution VIII guardrail — mirrors the depth-multiple empty-universe warn).
    if key == "universe_max_atr_pct":
        if isinstance(value, (int, float)):
            try:
                from db.db_ops import get_db_connection
                with get_db_connection() as conn:
                    row = conn.execute(
                        "SELECT MIN(atr_pct_median) AS mn FROM asset_universe "
                        "WHERE venue = 'binance' AND atr_pct_median IS NOT NULL"
                    ).fetchone()
                mn = float(row["mn"]) if row and row["mn"] is not None else None
                if mn is not None and value < mn:
                    return Verdict("warn",
                        f"universe_max_atr_pct ({value}) is below the lowest stored binance ATR ({mn:.2f}) — universe will be empty",
                        round(mn, 2))
            except Exception:
                pass

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

    # ── Soft range ─────────────────────────────────────────────────────────
    # Runs after the cross-checks so a hard error is never masked by a soft
    # warning (errors take precedence over recommendations).
    if spec.soft_min is not None and isinstance(value, (int, float)) and value < spec.soft_min:
        return Verdict("warn", f"{key} = {value} below recommended {spec.soft_min}", spec.soft_min)
    if spec.soft_max is not None and isinstance(value, (int, float)) and value > spec.soft_max:
        return Verdict("warn", f"{key} = {value} above recommended {spec.soft_max}", spec.soft_max)

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
