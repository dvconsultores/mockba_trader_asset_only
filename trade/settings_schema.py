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
    SettingSpec("sl_k_spot", float, "trading", None, 0.1, 5.0, 0.3, 2.0,
                "Spot-only SL ATR multiplier — overrides sl_k for spot (wider room: spot cannot be liquidated)"),
    SettingSpec("sl_min_pct_spot", float, "trading", "%", 0.1, 10.0, 0.3, 3.0,
                "Spot-only SL floor %% — overrides sl_min_pct for spot (must stay below tp_min_pct)"),
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
    SettingSpec("max_loss_per_position_pct", float, "exit", "%", 0.1, 20.0, 0.5, 5.0,
                "Crash-guard floor — market-sell a spot position when live price falls below entry × (1 − pct/100)", ("sl_min_pct_spot",)),

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

    # ── Capital view (Amendment 003) ───────────────────────────────────────
    # Declared pools — display/validation only, never used for sizing.
    SettingSpec("capital_cex_usdt", float, "risk", "$", 0, None, None, None,
                "Declared capital pool for Binance spot (USDT) — for display/validation, never sizing"),
    SettingSpec("capital_dex_usdc", float, "risk", "$", 0, None, None, None,
                "Declared capital pool for Orderly perps (USDC) — for display/validation, never sizing"),
    SettingSpec("cex_slot_pct", float, "risk", "%", 0.1, 100, 2, 50,
                "% of CEX live equity per slot (slot size = slot_pct × equity)"),
    SettingSpec("dex_slot_pct", float, "risk", "%", 0.1, 100, 2, 50,
                "% of DEX live equity per slot (slot size = slot_pct × equity)"),
    SettingSpec("max_slots_cex", int, "risk", "slots", 1, 100, 1, 20,
                "Maximum concurrent open slots on Binance spot"),
    SettingSpec("max_slots_dex", int, "risk", "slots", 1, 100, 1, 20,
                "Maximum concurrent open slots on Orderly perps"),

    # ── Universe scanner (Amendment 003) ───────────────────────────────────
    SettingSpec("universe_scan_interval_hours", int, "universe", "hours", 1, 720, 6, 168,
                "How often the daily universe scan runs"),
    SettingSpec("universe_max_age_hours", int, "universe", "hours", 1, 720, 12, 168,
                "Max age of a stored scan; older blocks new entries on that venue", ("universe_scan_interval_hours",)),
    SettingSpec("universe_size", int, "universe", "assets", 1, 200, 5, 50,
                "Top-N assets stored per venue after ranking"),
    SettingSpec("universe_min_volume_usd", float, "universe", "$", 0, None, 1e6, 1e8,
                "Minimum 24h quote volume (USD) for a candidate"),
    SettingSpec("universe_spread_ratio_max", float, "universe", None, 0.01, 1.0, 0.05, 0.25,
                "Max spread as a fraction of tp_min_pct (spread <= tp × this)", ("tp_min_pct",)),
    SettingSpec("universe_rank_min", int, "universe", "rank", 1, 500, 5, 100,
                "Lowest volume-rank included (1 = most volume)", ("universe_rank_max",)),
    SettingSpec("universe_rank_max", int, "universe", "rank", 1, 2000, 30, 500,
                "Highest volume-rank included", ("universe_rank_min",)),
    SettingSpec("universe_depth_slot_multiple", float, "universe", "x", 0.1, 20, 1, 10,
                "Top-10 depth required on both sides as a multiple of slot size"),
    SettingSpec("universe_replay_days", int, "universe", "days", 1, 90, 3, 30,
                "Days of 5m candles to replay the entry rule over"),
    SettingSpec("universe_min_signals", int, "universe", "signals", 1, 1000, 10, 200,
                "Minimum replay entries for a symbol to be ranked (too few = meaningless)"),
    SettingSpec("universe_min_recovery_rate", str, "universe", None, None, None, None, None,
                "'auto' = implied breakeven win rate from current settings; a literal rate (0-1) overrides"),
    SettingSpec("universe_spread_degradation_multiple", float, "universe", "x", 1.0, 50, 1.5, 10,
                "Skip entries when live spread exceeds scan-time spread by this multiple"),
    SettingSpec("universe_rotate_inactive_hours", float, "universe", "hours", 0, 168, 6, 24,
                "Rotate universe members with no actionable signal within N hours out of the next scan (0=off)"),
    SettingSpec("universe_max_atr_pct", float, "universe", "%", 0.1, 20.0, 0.5, 5.0,
                "Max replay median ATR% for a spot universe candidate — crash-prone names above the cap never enter the universe"),

    # ── Market gate (feature 005) ─────────────────────────────────────────
    # Opt-in venue-level gate: suspends NEW entries only after bad_streak
    # consecutive FAIL verdicts; resumes after good_streak consecutive PASS.
    # Hard minima enforce interval/streak >= 1 in the schema — no
    # settings_rules.py cross-check is necessary (Amendment 002 validator).
    SettingSpec("market_gate_enabled", bool, "gate", None, None, None, None, None,
                "Opt-in master switch for the market-conditions gate (default off — zero behavior change)"),
    SettingSpec("market_gate_interval_min", int, "gate", "min", 1, 1440, 2, 60,
                "Market-gate evaluation cadence (minutes)"),
    SettingSpec("market_gate_bad_streak", int, "gate", None, 1, 100, 1, 20,
                "Consecutive FAIL evaluations before the gate suspends new entries"),
    SettingSpec("market_gate_good_streak", int, "gate", None, 1, 100, 1, 20,
                "Consecutive PASS evaluations before the gate resumes new entries"),
    SettingSpec("market_gate_fail_share", float, "gate", None, 0.0, 1.0, 0.25, 0.75,
                "Universe share failing liquidity that FAILs the gate verdict"),
    SettingSpec("market_gate_trend_share", float, "gate", None, 0.0, 1.0, 0.3, 0.8,
                "Universe share in TREND_UP/DOWN that downgrades PASS to WARN"),
    SettingSpec("market_gate_unknown_share", float, "gate", None, 0.0, 1.0, 0.2, 0.7,
                "Universe share UNKNOWN that downgrades PASS to WARN"),
    SettingSpec("market_gate_warn_liquidity_share", float, "gate", None, 0.0, 1.0, 0.1, 0.5,
                "WARN liquidity_partial suspends only when fail_share is at/above this (a lone bad asset is mild)"),
    SettingSpec("market_gate_regime_escalates", bool, "gate", None, None, None, None, None,
                "Regime-trending/unknown WARNs escalate to suspension when true (default false — liquidity-only suspension; the broad-market filter owns macro trends)"),
    SettingSpec("market_filter_enabled", bool, "gate", None, None, None, None, None,
                "Broad-market gate: block entries while the majors' average 24h change is a downtrend"),
    SettingSpec("market_filter_assets", str, "gate", None, None, None, None, None,
                "Comma-separated majors whose average 24h change defines the broad trend"),
    SettingSpec("market_filter_max_downtrend_pct", float, "gate", "%", -20.0, 5.0, -3.0, -0.5,
                "Block entries when the majors' average 24h change is below this"),
    SettingSpec("market_filter_cache_min", int, "gate", "min", 1, 60, 2, 15,
                "Cache duration for the broad-market check (limits API calls)"),

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
