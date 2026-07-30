"""
MockbaV4 — Settings schema (Amendment 002).

Static metadata for every setting. Single source of truth for UI, validator, and Telegram.
Version-controlled. No LLM, no database reads.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: type                  # bool | int | float | str
    group: str                  # trading | entry | exit | risk | toxicity | llm | mode
    unit: str | None            # "%" | "sec" | "x" | None
    hard_min: float | None
    hard_max: float | None
    soft_min: float | None
    soft_max: float | None
    short: str                  # one-line description
    depends_on: tuple[str, ...] = ()  # keys whose values participate in cross-checks


# ═══════════════════════════════════════════════════════════════════════════════
ALL: list[SettingSpec] = [
    # ── Trading ────────────────────────────────────────────────────────────
    SettingSpec("tp_min_pct", float, "trading", "%", 0.1, 10.0, 0.3, 3.0,
                "Minimum take-profit percentage (floor when ATR is low)"),
    SettingSpec("sl_min_pct", float, "trading", "%", 0.1, 10.0, 0.3, 3.0,
                "Minimum stop-loss percentage (floor when ATR is low)"),
    SettingSpec("tp_k", float, "trading", None, 0.1, 5.0, 0.5, 2.0,
                "TP ATR multiplier — tp_effective = max(k × ATR%, min_pct)"),
    SettingSpec("sl_k", float, "trading", None, 0.1, 5.0, 0.3, 2.0,
                "SL ATR multiplier — sl_effective = max(k × ATR%, min_pct)"),
    SettingSpec("leverage", int, "trading", "x", 1, 10, 1, 5,
                "DEX futures leverage"),
    SettingSpec("max_leverage", int, "trading", "x", 1, 10, 2, 5,
                "Hard cap on leverage"),

    # ── Entry ──────────────────────────────────────────────────────────────
    SettingSpec("dip_min_pct", float, "entry", "%", 0.05, 5.0, 0.10, 1.0,
                "Minimum dip % below rolling peak to trigger buy/long"),
    SettingSpec("pump_min_pct", float, "entry", "%", 0.05, 5.0, 0.10, 1.0,
                "Minimum pump % above rolling trough to trigger sell/short"),
    SettingSpec("dip_k", float, "entry", None, 0.1, 5.0, 0.3, 2.0,
                "Dip ATR multiplier — dip_needed = max(k × ATR%, min_pct)"),
    SettingSpec("pump_k", float, "entry", None, 0.1, 5.0, 0.3, 2.0,
                "Pump ATR multiplier — pump_needed = max(k × ATR%, min_pct)"),
    SettingSpec("cooldown_sec", int, "entry", "sec", 10, 3600, 30, 600,
                "Minimum seconds between entries (same asset, same direction)"),
    SettingSpec("min_entry_spacing_pct", float, "entry", "%", 0.05, 5.0, 0.10, 2.0,
                "Minimum % distance from any open position's entry price"),
    SettingSpec("adaptive_enabled", bool, "entry", None, None, None, None, None,
                "Scale dip/pump/TP/SL thresholds with ATR volatility"),
    # max_slots removed (Amendment 004) — replaced by max_concurrent_positions in risk group

    # ── Exit ───────────────────────────────────────────────────────────────
    SettingSpec("max_hold_minutes_spot", int, "exit", "min", 5, 1440, 30, 480,
                "Time stop: close spot position after N minutes"),
    SettingSpec("max_hold_minutes_futures", int, "exit", "min", 5, 1440, 60, 720,
                "Time stop: close futures position after N minutes"),

    # ── Risk ───────────────────────────────────────────────────────────────
    SettingSpec("daily_loss_limit", float, "risk", "$", 0, None, None, None,
                "Absolute daily loss limit (0 = use percentage instead)"),
    SettingSpec("daily_loss_limit_pct", float, "risk", "%", 0, 100, 0, 20,
                "Stop trading if daily PnL drops below this % of equity"),
    SettingSpec("max_consecutive_losses", int, "risk", None, 0, 50, 0, 10,
                "Stop trading after N consecutive losses (0 = off)"),
    # dex_slot_pct and cex_slot_pct removed (Amendment 004) — replaced by per-asset capital_dex/capital_cex in asset_configs table
    # max_concurrent_positions: replaces old max_slots, now global across all pairs
    SettingSpec("dex_round_trip_fee_pct", float, "risk", "%", 0, 5.0, 0.03, 1.0,
                "Orderly DEX round-trip fee % for net-edge calculation"),
    SettingSpec("cex_round_trip_fee_pct", float, "risk", "%", 0, 5.0, 0.10, 1.0,
                "Binance CEX round-trip fee % for net-edge calculation"),
    SettingSpec("assumed_slippage_pct", float, "risk", "%", 0, 5.0, 0.01, 1.0,
                "Assumed slippage % for net-edge calculation"),
    SettingSpec("min_net_edge_pct", float, "risk", "%", 0.01, 5.0, 0.10, 1.0,
                "Refuse to trade if net edge (TP − fees − slippage) below this"),

    # ── Regime ─────────────────────────────────────────────────────────────
    SettingSpec("regime_cache_sec", int, "risk", "sec", 30, 3600, 120, 600,
                "How long to cache regime classification per asset"),
    SettingSpec("slope_threshold", float, "risk", None, 0.0001, 0.01, 0.0005, 0.005,
                "Linear regression slope threshold for trend detection"),

    # ── Volatility ─────────────────────────────────────────────────────────
    SettingSpec("atr_period", int, "risk", "candles", 5, 50, 10, 30,
                "Number of 5m candles for ATR calculation"),
    SettingSpec("atr_interval", str, "risk", None, None, None, None, None,
                "Candle interval for ATR (fixed at 5m)"),
    SettingSpec("candle_cache_sec", int, "risk", "sec", 30, 600, 60, 300,
                "How long to cache 5m OHLCV candles"),

    # ── Toxicity ───────────────────────────────────────────────────────────
    SettingSpec("tox_window", int, "toxicity", "samples", 20, 1000, 60, 300,
                "Rolling window size for toxicity z-score calculation"),
    SettingSpec("velocity_window", int, "toxicity", "cycles", 1, 20, 2, 10,
                "Number of cycles over which extreme_pct is accumulated"),
    SettingSpec("tox_velocity_enforce", bool, "toxicity", None, None, None, None, None,
                "Block entries when velocity check trips"),
    SettingSpec("tox_spread_enforce", bool, "toxicity", None, None, None, None, None,
                "Block entries when spread check trips"),
    SettingSpec("tox_depth_enforce", bool, "toxicity", None, None, None, None, None,
                "Block entries when depth check trips"),
    SettingSpec("tox_obi_enforce", bool, "toxicity", None, None, None, None, None,
                "Block entries when OBI check trips"),
    SettingSpec("max_extreme_velocity_pct", float, "toxicity", "%/cycle", 0.01, 5.0, 0.05, 1.0,
                "Velocity threshold — extreme_pct accumulated per cycle"),
    SettingSpec("spread_z_max", float, "toxicity", "z", 1.0, 5.0, 1.5, 3.5,
                "Spread z-score threshold"),
    SettingSpec("depth_ratio_min", float, "toxicity", None, 0.1, 2.0, 0.3, 1.0,
                "Minimum depth ratio (current / rolling mean)"),
    SettingSpec("obi_z_max", float, "toxicity", "z", 1.0, 5.0, 1.5, 3.5,
                "OBI z-score threshold"),

    # ── LLM ────────────────────────────────────────────────────────────────
    SettingSpec("llm_helper_enabled", bool, "llm", None, None, None, None, None,
                "Enable LLM-powered setting explanations and proposals"),
    SettingSpec("llm_language", str, "llm", None, None, None, None, None,
                "Language for LLM explanations (en, es)"),
    SettingSpec("llm_model", str, "llm", None, None, None, None, None,
                "DeepSeek model identifier"),
    SettingSpec("llm_timeout_sec", int, "llm", "sec", 5, 120, 10, 60,
                "LLM API call timeout"),
    SettingSpec("llm_explain_cache_days", int, "llm", "days", 1, 365, 7, 90,
                "How long to cache LLM explanations"),
    SettingSpec("llm_max_calls_per_hour", int, "llm", "calls/hr", 1, 60, 5, 30,
                "Rate limit for LLM API calls"),

    # ── Multi-Asset (Amendment 004) ────────────────────────────────────────
    SettingSpec("global_daily_loss_limit", float, "risk", "$", 0, None, None, None,
                "Stop ALL trading if total daily PnL across all pairs drops below this (0=off)"),
    SettingSpec("global_daily_loss_limit_pct", float, "risk", "%", 0, 100, 0, 20,
                "Stop ALL trading if total daily PnL% drops below this (0=off)"),
    SettingSpec("max_active_pairs", int, "risk", "pairs", 1, 50, 2, 12,
                "Maximum concurrently active (asset, venue) pairs (default 6)"),
    SettingSpec("max_concurrent_positions", int, "risk", "positions", 1, 50, 2, 20,
                "Maximum open positions across all pairs (default 9, replaces max_slots)"),

    # ── Mode ───────────────────────────────────────────────────────────────
    SettingSpec("trading_enabled", bool, "mode", None, None, None, None, None,
                "Global trading on/off (kill switch sets this to false)"),
    # auto_trade_binance and auto_trade_orderly removed (Amendment 004) — replaced by per-asset active_cex/active_dex in asset_configs table
    SettingSpec("dry_run", bool, "mode", None, None, None, None, None,
                "Simulate orders — no real money is used"),
]

# Index by key for fast lookup
BY_KEY: dict[str, SettingSpec] = {s.key: s for s in ALL}
GROUPS: list[str] = sorted(set(s.group for s in ALL))
