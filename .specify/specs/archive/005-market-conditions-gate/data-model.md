# Data Model: Market Conditions Check & Auto-Gate

**Feature**: 005-market-conditions-gate | **Date**: 2026-08-09

No DB schema change. This feature introduces: (1) one in-memory per-venue
market-health report (the shared verdict contract), (2) an in-memory per-venue
debounced gate state machine, (3) seven new `settings` rows (defaults live in
`get_setting_*` fallbacks — no migration). The full report contract is
specified in `contracts/market-report.md`; this document is the canonical
reference for the state machine and the settings.

## 1. Report dict contract (both modes, identical shape)

Produced by `check_venue_live(venue)` and `check_venue_observed(venue,
observations, equity=None)`. Fields, types and defaults:

```python
{
    "venue": "binance" | "orderly",
    "mode": "live" | "observed",
    "timestamp": float,              # time.time() at evaluation
    "scan_fresh": bool,              # stored scan age <= universe_max_age_hours
    "scan_age_hours": float | None,  # None when no scan stored
    "verdict": "PASS" | "WARN" | "FAIL",
    "reasons": [str, ...],           # one-line machine reasons, e.g. "liquidity_fail_share=0.55"
    "regime_mix": {
        "RANGE": int, "TREND_UP": int, "TREND_DOWN": int, "UNKNOWN": int,
    },
    "assets": {
        "<ASSET>": {
            "passes_liquidity": bool,
            "volume_ok": bool,               # stored quote_volume_24h >= universe_min_volume_usd
            "depth_ok": bool,                # stored depth both sides >= universe_depth_slot_multiple × slot
            "spread_ok": bool,               # stored spread_pct <= universe_spread_ratio_max × tp_min_pct
            "live_spread_degraded": bool | None,  # None = no live observation in window
            "regime": "RANGE" | "TREND_UP" | "TREND_DOWN" | "UNKNOWN",
        }, ...
    },
    "thresholds": {                  # NON-GATING diagnostic via compute_thresholds
        "atr_pct_median": float | None,      # median stored ATR across universe
        "dip_needed_pct": float | None,
        "tp_effective_pct": float | None,
        "sl_effective_pct": float | None,
    },
}
```

- `assets` keys are the venue universe members only (`get_tradeable_universe`).
- Missing data fails closed: `None` volume/depth/spread ⇒ the corresponding
  `*_ok` is `False` (Constitution IV — missing data never passes).
- `live_spread_degraded` is `True` when a live observation exists and
  `live_spread > universe_spread_degradation_multiple × scan_spread`; `False`
  when observed and not degraded; `None` when no observation exists in the
  window (live mode always has one).

## 2. Verdict rules (single source of truth — `_evaluate`)

Evaluated in order; first hit wins. Both modes use these **identical** rules.

| # | Condition | Verdict | Reason key |
|---|---|---|---|
| 1 | `scan_fresh == False` (stale scan, refresh failed or paused) | **FAIL** | `scan_stale` |
| 2 | `fail_share >= market_gate_fail_share` where `fail_share = assets with passes_liquidity == False / total` | **FAIL** | `liquidity_fail_share=<n>` |
| 3 | `fail_share > 0` (any asset failing, below the FAIL share) | **WARN** (floor) | `liquidity_partial=<n>` |
| 4 | `trend_share = (TREND_UP + TREND_DOWN)/total >= market_gate_trend_share` | downgrade PASS→WARN | `regime_trending=<n>` |
| 5 | `unknown_share = UNKNOWN/total >= market_gate_unknown_share` | downgrade PASS→WARN | `regime_unknown=<n>` |
| 6 | otherwise | **PASS** | — |

Rules 4–5 only **downgrade** (never upgrade, never FAIL). UNKNOWN regime assets
never count toward a "good" verdict (Constitution IV). The aggregation shares
are settings, never constants (spec: "No hardcoded numbers").

## 3. Gate state machine (per venue, in memory)

Pure function `update_gate_state(state, verdict, settings)` in
`trade/market_check.py`; bot.py holds `_gate_state: dict[str, dict]` and the
last-eval timestamp per venue.

```python
state = {"suspended": bool, "bad_streak": int, "good_streak": int}
# returns (new_state, transition)
# transition: None | {"type": "suspend"} | {"type": "resume"}
```

| Input verdict | Transition rule |
|---|---|
| `PASS` | `good_streak += 1`, `bad_streak = 0`. If `suspended` and `good_streak >= market_gate_good_streak` → `suspended = False`, transition `resume`. |
| `FAIL` | `bad_streak += 1`, `good_streak = 0`. If not `suspended` and `bad_streak >= market_gate_bad_streak` → `suspended = True`, transition `suspend`. |
| `WARN` | `bad_streak = 0`, `good_streak = 0` (neutral hold — no transition, never suspends, never resumes). |

Design notes:
- **WARN = hold.** A WARN is "not good" (must not resume, Constitution IV) and
  "not bad enough to suspend" (must not over-trigger, Constitution VIII).
  Resetting both streaks prevents both flapping and premature resume.
- **In-memory per spec** (Q2: "per-venue debounce streaks in memory"). On
  restart the gate starts unsuspended and re-establishes state within
  `bad_streak × market_gate_interval_min` — documented, acceptable (suspension
  is an entry throttle, not a position-risk control; Constitution VI concerns
  positions/orders, which are unaffected).
- **One transition notification** exactly at the transition, via
  `send_message` (same mechanism as `_notify_entry`); a structured `[GATE]`
  INFO log line at each transition.

## 4. New settings (`trade/settings_schema.py`, new group `"gate"`)

| Key | Type | Default | Hard range | Soft range | Purpose |
|---|---|---|---|---|---|
| `market_gate_enabled` | bool | `false` | — | — | Opt-in master switch (zero behavior change until true) |
| `market_gate_interval_min` | int | 5 | 1–1440 | 2–60 | Gate evaluation cadence (min) |
| `market_gate_bad_streak` | int | 2 | 1–100 | 1–20 | Consecutive FAIL evals before suspend |
| `market_gate_good_streak` | int | 2 | 1–100 | 1–20 | Consecutive PASS evals before resume |
| `market_gate_fail_share` | float | 0.5 | 0.0–1.0 | 0.25–0.75 | Universe fraction failing liquidity ⇒ FAIL |
| `market_gate_trend_share` | float | 0.6 | 0.0–1.0 | 0.3–0.8 | Universe fraction in TREND_UP/DOWN ⇒ WARN downgrade |
| `market_gate_unknown_share` | float | 0.5 | 0.0–1.0 | 0.2–0.7 | Universe fraction UNKNOWN ⇒ WARN downgrade |

- Defaults are intentionally lenient (Constitution VIII): a healthy RANGE venue
  evaluates PASS (AC11).
- Hard minima (`interval >= 1`, streaks `>= 1`) are enforced by the schema's
  `hard_min`, so **no `settings_rules.py` change is strictly necessary**
  (Amendment 002 validator passes unchanged; AC12 covered by `validate()`
  range checks in tests).
- All settings are read fresh each cycle; Telegram/UI changes take effect
  without restart.

## 5. Observations (observed mode only, in memory)

```python
_gate_observations: dict[str, dict[str, deque]]  # venue -> asset -> deque(maxlen=40)
# entry: {"ts": float, "regime": str | None, "spread": float | None, "obi": float | None}
```

- Appended at the two existing call sites in bot.py (zero extra API load):
  - after `regime = detect_regime(asset, venue)` → `{"ts": ..., "regime": regime}`;
  - after `obi, spread = _get_obi_and_spread(asset, venue)` → `{"ts": ..., "spread": spread, "obi": obi}`.
- The evaluator filters entries with `ts >= now - market_gate_interval_min*60`
  and takes the **latest** regime and the **latest non-None** spread per asset.
- No DB table; this is process-local rolling data (matches "rolling window, no
  meaningful extra API load" in the spec).

## 6. In-memory state owned by bot.py

| Dict | Key | Value |
|---|---|---|
| `_gate_state` | venue | `{"suspended": bool, "bad_streak": int, "good_streak": int}` |
| `_last_gate_eval` | venue | last evaluation `time.time()` |
| `_gate_observations` | venue → asset | rolling observation deque |
