"""
MockbaV4 — Toxicity checks (Amendment 001, observe-only by default).

Four checks: velocity, spread, depth, OBI.
All z-scored per asset per venue against their own history.
NULL during warmup (tox_window samples not yet collected).
"""

from __future__ import annotations
import time
from collections import deque
from typing import Optional

from db.db_ops import get_setting_float, get_setting_bool


# ── Per-asset-per-venue series ────────────────────────────────────────────────

_history: dict[str, dict[str, deque]] = {}  # key → {spread, depth, obi, extreme}

_FIELDS = ("spread", "depth", "obi", "extreme")


def _key(asset: str, venue: str) -> str:
    return f"{venue}:{asset}"


def _ensure_series(key: str):
    if key not in _history:
        window = get_setting_float("tox_window", 120)
        _history[key] = {f: deque(maxlen=int(window)) for f in _FIELDS}


def record_observation(asset: str, venue: str, spread_pct: float,
                       depth_top10: float, obi: float, extreme_pct: float):
    """Feed one cycle's data into the rolling history."""
    k = _key(asset, venue)
    _ensure_series(k)
    _history[k]["spread"].append(spread_pct)
    _history[k]["depth"].append(depth_top10)
    _history[k]["obi"].append(obi)
    _history[k]["extreme"].append(abs(extreme_pct) if extreme_pct else 0.0)


def _z_score(series: deque, value: float) -> float | None:
    """Z-score of value against the series. Returns None if warmup."""
    if len(series) < 5:
        return None
    mean = sum(series) / len(series)
    variance = sum((x - mean) ** 2 for x in series) / len(series)
    if variance == 0:
        return 0.0
    return (value - mean) / (variance ** 0.5)


def _velocity(asset: str, venue: str, extreme_pct: float) -> float:
    """Extreme_pct accumulated over velocity_window cycles."""
    k = _key(asset, venue)
    _ensure_series(k)
    window = int(get_setting_float("velocity_window", 3))
    series = _history[k]["extreme"]
    if len(series) < window:
        return 0.0
    recent = list(series)[-window:]
    return sum(recent) / window


def evaluate(asset: str, venue: str, spread_pct: float, depth_top10: float,
             obi: float, extreme_pct: float) -> dict:
    """
    Run all four toxicity checks. Returns a dict of verdicts.
    Verdict: 1 = would block, 0 = would pass, None = warmup.
    """
    k = _key(asset, venue)
    _ensure_series(k)
    series = _history[k]

    result = {
        "spread_z": _z_score(series["spread"], spread_pct),
        "depth_ratio": depth_top10 / (sum(series["depth"]) / len(series["depth"])) if len(series["depth"]) >= 5 else None,
        "obi_z": _z_score(series["obi"], obi),
        "velocity_pct": _velocity(asset, venue, extreme_pct),
    }

    # Spread check
    if result["spread_z"] is None:
        result["tox_spread"] = None
    else:
        result["tox_spread"] = 1 if result["spread_z"] > get_setting_float("spread_z_max", 2.5) else 0

    # Depth check
    if result["depth_ratio"] is None:
        result["tox_depth"] = None
    else:
        result["tox_depth"] = 1 if result["depth_ratio"] < get_setting_float("depth_ratio_min", 0.5) else 0

    # OBI check
    if result["obi_z"] is None:
        result["tox_obi"] = None
    else:
        result["tox_obi"] = 1 if abs(result["obi_z"]) > get_setting_float("obi_z_max", 2.5) else 0

    # Velocity check
    max_vel = get_setting_float("max_extreme_velocity_pct", 0.25)
    result["tox_velocity"] = 1 if result["velocity_pct"] > max_vel else 0

    # Aggregate
    verdicts = [result.get(f"tox_{f}") for f in ("velocity", "spread", "depth", "obi")]
    result["tox_any"] = 1 if any(v == 1 for v in verdicts) else 0

    # Enforced?
    enforce = any(
        get_setting_bool(f"tox_{f}_enforce", False) and result.get(f"tox_{f}") == 1
        for f in ("velocity", "spread", "depth", "obi")
    )
    result["tox_enforced"] = 1 if enforce else 0

    return result
