"""
MockbaV4 — Shared market-conditions check (feature 005).

One source of truth for the per-venue market-health verdict, consumed by the
automatic gate (bot.py, observed mode) and the manual Telegram report
(telegram.py, live mode). Both modes produce the IDENTICAL structured report
contract (see .specify/specs/005-market-conditions-gate/contracts/market-report.md).

Non-divergence (hard rule, AC1): every threshold / regime / depth / sizing /
freshness call goes through the live functions in trade.universe, trade.regime,
trade.pnl and db.db_ops — never reimplemented here. Observed mode issues zero
new market-data calls (AC9).
"""

from __future__ import annotations
import time
from statistics import median

from db.db_ops import (
    get_setting_float, get_setting_int, get_universe_scan_age,
    get_venue_equity, get_tradeable_universe,
)
import trade.universe as universe
import trade.regime as regime
import trade.pnl as pnl


class _LiveDataUnavailable(Exception):
    """Whole-exchange snapshot failed in live mode — never judge on partial data."""


# ═════════════════════════════════════════════════════════════════════════════
# Pure verdict core (data-model.md §2) — first hit wins
# ═════════════════════════════════════════════════════════════════════════════

def _evaluate(venue, mode, scan_fresh, scan_age_hours, asset_facts, regime_mix, settings):
    """Pure verdict core. Deterministic, no I/O, no formatting.

    Returns (verdict, reasons). Rules evaluated in order (data-model.md §2):
    stale scan ⇒ FAIL; fail_share >= threshold ⇒ FAIL; any fail ⇒ WARN;
    trend/unknown shares downgrade PASS→WARN only; else PASS.
    """
    total = len(asset_facts)
    if total == 0:
        # Fail closed on an empty universe (Constitution IV) — no data, no PASS.
        return "FAIL", ["no_assets"]

    if not scan_fresh:
        return "FAIL", ["scan_stale"]

    fail_share = sum(1 for a in asset_facts.values() if not a["passes_liquidity"]) / total
    if fail_share >= settings.get("market_gate_fail_share", 0.5):
        return "FAIL", [f"liquidity_fail_share={fail_share:.2f}"]
    if fail_share > 0:
        return "WARN", [f"liquidity_partial={fail_share:.2f}"]

    trend_share = (regime_mix.get("TREND_UP", 0) + regime_mix.get("TREND_DOWN", 0)) / total
    unknown_share = regime_mix.get("UNKNOWN", 0) / total
    if trend_share >= settings.get("market_gate_trend_share", 0.6):
        return "WARN", [f"regime_trending={trend_share:.2f}"]
    if unknown_share >= settings.get("market_gate_unknown_share", 0.5):
        return "WARN", [f"regime_unknown={unknown_share:.2f}"]
    return "PASS", []


# ═════════════════════════════════════════════════════════════════════════════
# Per-asset liquidity facts
# ═════════════════════════════════════════════════════════════════════════════

def _asset_facts_from_scan(venue, universe_rows, slot):
    """Per-asset volume/depth/spread checks from stored universe rows.

    Missing data fails closed: None volume/depth/spread ⇒ the corresponding
    *_ok is False (Constitution IV). Shared baseline for both modes.
    """
    min_volume = get_setting_float("universe_min_volume_usd", 5_000_000)
    depth_mult = get_setting_float("universe_depth_slot_multiple", 3.0)
    spread_ratio = get_setting_float("universe_spread_ratio_max", 0.10)
    tp_min = get_setting_float("tp_min_pct", 0.8)
    out = {}
    for u in universe_rows:
        qv = u.get("quote_volume_24h")
        db = u.get("depth_bid_top10")
        da = u.get("depth_ask_top10")
        sp = u.get("spread_pct")
        volume_ok = qv is not None and qv >= min_volume
        depth_ok = (db is not None and da is not None
                    and db >= depth_mult * slot and da >= depth_mult * slot)
        spread_ok = sp is not None and sp <= spread_ratio * tp_min
        out[u["asset"]] = {
            "passes_liquidity": volume_ok and depth_ok and spread_ok,
            "volume_ok": volume_ok,
            "depth_ok": depth_ok,
            "spread_ok": spread_ok,
            "live_spread_degraded": None,
            "regime": "UNKNOWN",
        }
    return out


def _asset_facts_observed(venue, universe_rows, slot, observations, window_sec):
    """Consumes the rolling per-asset observation deques (data-model.md §5).

    Latest regime + latest non-None spread within ts >= now - window_sec.
    Zero network calls (AC9). No observation in the window ⇒
    live_spread_degraded None (indeterminate, never good) and regime UNKNOWN.
    """
    deg_mult = get_setting_float("universe_spread_degradation_multiple", 3.0)
    now = time.time()
    facts = _asset_facts_from_scan(venue, universe_rows, slot)
    for u in universe_rows:
        asset = u["asset"]
        obs = observations.get(asset)
        regime_seen = None
        spread_seen = None
        if obs:
            for o in obs:  # deque iterates oldest → newest; latest wins
                if o.get("ts", 0) < now - window_sec:
                    continue
                if o.get("regime") is not None:
                    regime_seen = o["regime"]
                if o.get("spread") is not None:
                    spread_seen = o["spread"]
        scan_spread = u.get("spread_pct")
        if spread_seen is not None and scan_spread is not None:
            degraded = spread_seen > scan_spread * deg_mult
        else:
            degraded = None
        facts[asset].update({
            "live_spread_degraded": degraded,
            "regime": regime_seen or "UNKNOWN",
        })
    return facts


def _asset_facts_live(venue, universe_rows, slot, bucket):
    """Live whole-exchange snapshot: bookTicker + 24hr + exchangeInfo, then
    per-survivor depth through the scanner's token bucket. Reuses only the
    live universe functions (AC1). Whole-exchange failure ⇒ fail closed."""
    min_volume = get_setting_float("universe_min_volume_usd", 5_000_000)
    depth_mult = get_setting_float("universe_depth_slot_multiple", 3.0)
    spread_ratio = get_setting_float("universe_spread_ratio_max", 0.10)
    tp_min = get_setting_float("tp_min_pct", 0.8)
    deg_mult = get_setting_float("universe_spread_degradation_multiple", 3.0)
    try:
        book = {b["symbol"]: b for b in universe._fetch_binance_book_ticker()}
        vol = universe._fetch_binance_24hr()
        universe._fetch_binance_exchange_info()
    except Exception:
        raise _LiveDataUnavailable(venue)

    facts = _asset_facts_from_scan(venue, universe_rows, slot)
    for u in universe_rows:
        asset = u["asset"]
        b = book.get(f"{asset}USDT")
        qv = vol.get(f"{asset}USDT")
        volume_ok = qv is not None and qv >= min_volume
        spread = ((b["ask"] - b["bid"]) / b["bid"] * 100) if b and b.get("bid") else None
        spread_ok = spread is not None and spread <= spread_ratio * tp_min
        d = universe._fetch_depth(venue, u["symbol"]) if bucket.take(1) else None
        depth_ok = (d is not None and d["bid"] >= depth_mult * slot
                    and d["ask"] >= depth_mult * slot)
        scan_spread = u.get("spread_pct")
        if spread is not None and scan_spread is not None:
            degraded = spread > scan_spread * deg_mult
        else:
            degraded = None
        facts[asset].update({
            "volume_ok": volume_ok,
            "depth_ok": depth_ok,
            "spread_ok": spread_ok,
            "passes_liquidity": volume_ok and depth_ok and spread_ok,
            "live_spread_degraded": degraded,
            "regime": regime.detect_regime(asset, venue),
        })
    return facts


def _regime_mix(asset_facts):
    counts = {"RANGE": 0, "TREND_UP": 0, "TREND_DOWN": 0, "UNKNOWN": 0}
    for a in asset_facts.values():
        r = a.get("regime")
        counts[r if r in counts else "UNKNOWN"] += 1
    return counts


# ═════════════════════════════════════════════════════════════════════════════
# Freshness gate (AC3) — never judge on stale data
# ═════════════════════════════════════════════════════════════════════════════

def _scan_age_hours(venue):
    age = get_universe_scan_age(venue)
    return None if age is None else (time.time() - age) / 3600.0


def _ensure_fresh_scan(venue, runner=None):
    """Refresh a stale/due stored scan before evaluating (both modes).

    runner defaults to run_scans_if_due (which pauses under the consecutive-
    losses kill switch). Returns (scan_fresh, scan_age_hours); still stale
    after refresh ⇒ fail closed (Constitution IV).
    """
    if universe.is_universe_stale(venue):
        if runner is None:
            universe.run_scans_if_due(venues=(venue,))
        else:
            runner()
    return not universe.is_universe_stale(venue), _scan_age_hours(venue)


def _diagnostic_thresholds(universe_rows):
    """NON-GATING diagnostic: effective thresholds from the stored median ATR,
    via the live compute_thresholds (the AC1-observed call site). Never gates."""
    atrs = [u.get("atr_pct_median") for u in universe_rows
            if u.get("atr_pct_median") is not None]
    atr_median = median(atrs) if atrs else None
    if atr_median is None:
        return {"atr_pct_median": None, "dip_needed_pct": None,
                "tp_effective_pct": None, "sl_effective_pct": None}
    dn, _pn, te, se = universe.compute_thresholds(
        atr_median,
        get_setting_float("dip_k", 0.5), get_setting_float("dip_min_pct", 0.15),
        get_setting_float("pump_k", 0.5), get_setting_float("pump_min_pct", 0.15),
        get_setting_float("tp_k", 1.0), get_setting_float("tp_min_pct", 0.8),
        get_setting_float("sl_k", 0.6), get_setting_float("sl_min_pct", 0.5),
    )
    return {"atr_pct_median": atr_median, "dip_needed_pct": dn,
            "tp_effective_pct": te, "sl_effective_pct": se}


def _cached_equity(venue):
    st = get_venue_equity(venue)
    return float(st["equity"]) if st else 0.0


def _gate_share_settings():
    """Verdict aggregation shares — settings, never constants (AC11)."""
    return {
        "market_gate_fail_share": get_setting_float("market_gate_fail_share", 0.5),
        "market_gate_trend_share": get_setting_float("market_gate_trend_share", 0.6),
        "market_gate_unknown_share": get_setting_float("market_gate_unknown_share", 0.5),
    }


def _report(venue, mode, scan_fresh, scan_age_hours, verdict, reasons,
            asset_facts, universe_rows):
    return {
        "venue": venue,
        "mode": mode,
        "timestamp": time.time(),
        "scan_fresh": scan_fresh,
        "scan_age_hours": scan_age_hours,
        "verdict": verdict,
        "reasons": reasons,
        "regime_mix": _regime_mix(asset_facts),
        "assets": asset_facts,
        "thresholds": _diagnostic_thresholds(universe_rows),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Public wrappers — one verdict contract, two modes (AC2)
# ═════════════════════════════════════════════════════════════════════════════

def check_venue_live(venue):
    """Live-snapshot mode (manual /market report). Fresh whole-exchange data,
    token-bucket bounded like the scanner. Returns the report dict."""
    scan_fresh, scan_age_hours = _ensure_fresh_scan(venue)
    universe_rows = get_tradeable_universe(venue)
    slot = pnl.compute_slot_size(venue, _cached_equity(venue), 0.0)
    bucket = universe._TokenBucket(capacity=max(1, len(universe_rows)), refill_per_sec=60.0)
    try:
        asset_facts = _asset_facts_live(venue, universe_rows, slot, bucket)
    except _LiveDataUnavailable:
        asset_facts = {}
        verdict, reasons = "FAIL", ["data_unavailable"]
    else:
        verdict, reasons = _evaluate(venue, "live", scan_fresh, scan_age_hours,
                                     asset_facts, _regime_mix(asset_facts),
                                     _gate_share_settings())
    return _report(venue, "live", scan_fresh, scan_age_hours, verdict, reasons,
                   asset_facts, universe_rows)


def check_venue_observed(venue, observations, equity=None):
    """Observed mode (automatic gate). Consumes per-cycle observations only —
    zero new market-data calls (AC9). equity defaults to the cached
    venue_state value (written by bot.py each cycle)."""
    scan_fresh, scan_age_hours = _ensure_fresh_scan(venue)
    universe_rows = get_tradeable_universe(venue)
    eq = _cached_equity(venue) if equity is None else equity
    slot = pnl.compute_slot_size(venue, eq, 0.0)
    window_sec = get_setting_int("market_gate_interval_min", 5) * 60
    asset_facts = _asset_facts_observed(venue, universe_rows, slot,
                                        observations or {}, window_sec)
    verdict, reasons = _evaluate(venue, "observed", scan_fresh, scan_age_hours,
                                 asset_facts, _regime_mix(asset_facts),
                                 _gate_share_settings())
    return _report(venue, "observed", scan_fresh, scan_age_hours, verdict, reasons,
                   asset_facts, universe_rows)


# ═════════════════════════════════════════════════════════════════════════════
# Gate debounce state machine (data-model.md §3)
# ═════════════════════════════════════════════════════════════════════════════

def update_gate_state(state, verdict, settings):
    """Pure debounce state machine per venue. Returns (new_state, transition).

    PASS → good_streak+1, bad_streak=0, resume when suspended and
           good_streak >= market_gate_good_streak.
    FAIL → bad_streak+1, good_streak=0, suspend when not suspended and
           bad_streak >= market_gate_bad_streak.
    WARN → both streaks reset (neutral hold — never suspends, never resumes).
    transition: None | {"type": "suspend"} | {"type": "resume"} (AC6).
    """
    new_state = dict(state)
    new_state.setdefault("suspended", False)
    new_state.setdefault("bad_streak", 0)
    new_state.setdefault("good_streak", 0)
    transition = None
    if verdict == "PASS":
        new_state["good_streak"] += 1
        new_state["bad_streak"] = 0
        if new_state["suspended"] and new_state["good_streak"] >= settings["market_gate_good_streak"]:
            new_state["suspended"] = False
            transition = {"type": "resume"}
    elif verdict == "FAIL":
        new_state["bad_streak"] += 1
        new_state["good_streak"] = 0
        if not new_state["suspended"] and new_state["bad_streak"] >= settings["market_gate_bad_streak"]:
            new_state["suspended"] = True
            transition = {"type": "suspend"}
    elif verdict == "WARN":
        new_state["bad_streak"] = 0
        new_state["good_streak"] = 0
    return new_state, transition


# ═════════════════════════════════════════════════════════════════════════════
# Text renderer (contract: contracts/market-report.md) — no emoji in the
# structural text; localized static labels are applied by telegram.py.
# ═════════════════════════════════════════════════════════════════════════════

_DEFAULT_LABELS = {
    "market": "Market",
    "verdict": "Verdict",
    "reasons": "Reasons",
    "scan": "Scan",
    "liquidity": "Liquidity",
    "thresholds": "Thresholds (diag)",
    "regime_mix": "regime mix",
    "assets_pass": "assets pass",
}


def format_report(report, labels=None):
    """Compact per-venue block. Verdict tokens (PASS/WARN/FAIL) are rendered
    verbatim; static labels can be overridden via the `labels` dict."""
    L = dict(_DEFAULT_LABELS)
    if labels:
        L.update(labels)
    venue = report["venue"]
    label = "DEX (orderly)" if venue == "orderly" else "CEX (binance)"
    lines = [
        f"{L['market']} — {label} · {report['mode']}",
        f"{L['verdict']}: {report['verdict']}",
    ]
    if report["reasons"]:
        lines.append(f"{L['reasons']}: " + ", ".join(report["reasons"]))
    age = report["scan_age_hours"]
    age_txt = f"{age:.1f}h" if age is not None else "none"
    fresh = "fresh" if report["scan_fresh"] else "stale"
    rm = report["regime_mix"]
    lines.append(
        f"{L['scan']}: {fresh} ({age_txt}) · {L['regime_mix']}: "
        f"RANGE {rm.get('RANGE', 0)} · UP {rm.get('TREND_UP', 0)} · "
        f"DOWN {rm.get('TREND_DOWN', 0)} · UNKNOWN {rm.get('UNKNOWN', 0)}"
    )
    assets = report["assets"]
    total = len(assets)
    passing = sum(1 for a in assets.values() if a.get("passes_liquidity"))
    lines.append(f"{L['liquidity']}: {passing}/{total} {L['assets_pass']}")
    th = report.get("thresholds") or {}
    if th.get("dip_needed_pct") is not None:
        lines.append(
            f"{L['thresholds']}: dip {th['dip_needed_pct']:.2f}% · "
            f"tp {th['tp_effective_pct']:.2f}% · sl {th['sl_effective_pct']:.2f}%"
        )
    return "\n".join(lines)
