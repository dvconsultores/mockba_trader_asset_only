# Research: Market Conditions Check & Auto-Gate

**Feature**: 005-market-conditions-gate | **Date**: 2026-08-09

Code-verified facts from the live repo (`trade/universe.py`, `trade/regime.py`,
`trade/pnl.py`, `bot.py`, `telegram.py`, `trading_bot/send_bot_message.py`,
`db/db_ops.py`, `trade/settings_schema.py`, `trade/settings_rules.py`,
`tests/test_amendment003.py`). Every signature below was read from source, not
assumed.

## 1. Shared functions the check must reuse (exact signatures)

| Function | Signature | Notes |
|---|---|---|
| `trade.universe.compute_thresholds` | `(atr, dk, dm, pk, pm, tk, tm, sk, sm) -> (dn, pn, te, se)` | Shared adaptive thresholds; live scalpers + replay both call it. |
| `trade.regime.detect_regime` | `(asset, venue) -> "RANGE"\|"TREND_UP"\|"TREND_DOWN"\|"UNKNOWN"` | Cached `regime_cache_sec` (300); Orderly proxied via Binance; API failure → cached value or `UNKNOWN`. |
| `trade.universe._fetch_depth` | `(venue, symbol, limit=10) -> {"bid": float, "ask": float} \| None` | Orderly maps `PERP_X_USDC` → `XUSDT` (Binance proxy). Returns None on failure (fail closed). |
| `trade.universe._fetch_binance_book_ticker` | `() -> list[dict]` | Whole-exchange best bid/ask, one call. |
| `trade.universe._fetch_binance_24hr` | `() -> dict[str, float]` | Whole-exchange 24h quote volume, one call. |
| `trade.universe._fetch_binance_exchange_info` | `() -> dict[str, dict]` | status / spot_allowed / min_notional. |
| `trade.universe.run_scans_if_due` | `(venues=("binance","orderly"), equity_fn=None, notify=None) -> list[dict]` | Scans when absent/older than `universe_scan_interval_hours` or force-requested; **pauses while the consecutive-losses kill switch is active**; never raises. |
| `trade.universe.force_rescan` | `(venue)` | Requests an immediate scan on the next `run_scans_if_due` call. |
| `trade.universe.is_universe_stale` | `(venue) -> bool` | `get_universe_scan_age` older than `universe_max_age_hours` (36) or no scan. |
| `trade.universe._TokenBucket` | `(capacity, refill_per_sec)` | `.take(n=1) -> bool`; scanner uses `refill_per_sec=60.0`. |
| `trade.pnl.compute_slot_size` | `(venue, equity, min_notional, capital=0.0) -> float` | `{venue}_slot_pct × equity` floored at `min_notional × 1.5`, cached per UTC day. |
| `db.db_ops.get_setting*` | `get_setting(key) / get_setting_float(key, default) / get_setting_int / get_setting_bool` | Read fresh each cycle. |
| `db.db_ops.get_universe_scan_age` | `(venue) -> float \| None` | Newest `scanned_at`. |
| `db.db_ops.get_venue_equity` | `(venue) -> dict \| None` | Equity cache written by bot.py each cycle. |
| `db.db_ops.get_tradeable_universe` | `(venue) -> list[dict]` | Non-blacklisted rows by rank. |
| `db.db_ops.get_universe_row` | `(venue, asset) -> dict \| None` | Includes blacklisted. |
| `trading_bot.send_bot_message.send_bot_message` | `(chat_id: int, message: str) -> str` | 4096-chunk, MarkdownV2 with plain-text fallback, retry. |
| `trading_bot.send_bot_message.send_message` | `(message: str) -> str` | Wrapper → configured `TELEGRAM_CHAT_ID`. |

`compute_thresholds` is **not** used by the gate verdict formulas (the spec pins
`spread_ok` to `universe_spread_ratio_max × tp_min_pct`, identical to the
scanner's `_hard_filters_pass`). The check still **calls** `compute_thresholds`
to derive the venue-level *effective* thresholds (from the stored median ATR)
and carries them in the report as a non-gating diagnostic field — this is the
call site the AC1 patch test observes, and it reuses logic rather than
reimplementing it.

## 2. Slot size for the depth requirement

The scanner's venue-level slot is `_slot_size_for(venue, equity) = {venue}_slot_pct
× equity` (no min-notional floor at venue level; the floor applies per-symbol in
`_hard_filters_pass`). `compute_slot_size(venue, equity, min_notional=0.0)`
returns exactly that value (`max(raw, 0.0)`). **Decision:** the check computes
the depth requirement via `compute_slot_size(venue, equity, 0.0)` — the AC1
mandated function, numerically identical to the scanner's slot, and passing
`min_notional=0.0` is documented (the gate performs no fundability check).

Equity source per mode:
- **observed**: `get_venue_equity(venue)` (already cached by bot.py each cycle — zero API).
- **live**: fresh `ex.get_equity()` when the caller has an exchange (bot.py), else the cached `venue_state` value (telegram.py has no exchange object).

## 3. What bot.py already collects per cycle (observed-mode fuel)

In the per-asset loop (`bot.py` ~lines 320–390) the bot already computes, per
asset per venue, with no new network calls:

1. `regime = detect_regime(asset, venue)` — **always** runs (before exit
   management), even when entries are blocked. → regime observations always flow.
2. `obi, spread = _get_obi_and_spread(asset, venue)` (line 362) — one depth
   call that also feeds the per-cycle spread-degradation guard. It is reached
   **after** the stale / kill-switch / UNKNOWN guards, so it stops flowing while
   a venue is stale or kill-switch-blocked.

**Deadlock hazard:** if the gate's entry-block were placed before
`_get_obi_and_spread`, spread observations would stop while a venue is
suspended and the gate could never accumulate enough "good" data to resume.
**Decision:** record observations at the two existing call sites, and place the
gate's entry-block **after** observation recording (just before the
`spot_cycle`/`futures_cycle` call), so live-spread data keeps flowing during a
suspension and resume is possible. Regime observations flow unconditionally.

## 4. Natural integration point in bot.py

The periodic **mode-log block** is at the top of the loop body:

```python
_last_mode_log = 0.0
while True:
    try:
        if time.time() - _last_mode_log > 300:
            dex_m = _normalize_venue_mode(get_setting("auto_trade_orderly"))
            cex_m = _normalize_venue_mode(get_setting("auto_trade_binance"))
            logger.info(f"[MODE] DEX={dex_m} CEX={cex_m} ...")
            _last_mode_log = time.time()
```

The gate block mirrors this exact `time.time() - last > interval` pattern, runs
in the same loop (no thread/process), reads settings fresh, and skips when
`market_gate_enabled` is false or the venue mode is `"False"`. Venue modes come
from `_normalize_venue_mode(get_setting("auto_trade_orderly"|"auto_trade_binance"))`.

## 5. Existing helpers relevant to the gate

- `is_entry_blocked_per_pair(asset, venue, equity)` — per-pair kill switches;
  the gate is an **additional venue-level** layer, checked alongside it (does
  not replace it).
- `is_universe_stale(venue)` — existing fail-closed stale-universe guard; the
  gate uses the same predicate for its own freshness gate.
- `_notify_entry(...)` — shows the notification mechanism: `from
  trading_bot.send_bot_message import send_message; send_message(msg)`. Gate
  transition notifications reuse the same `send_message`.
- `send_bot_message(chat_id, message)` — signature confirmed; `send_message`
  resolves `TELEGRAM_CHAT_ID` itself.

## 6. settings_schema.py — how new settings slot in

`SettingSpec(key, type, group, unit, hard_min, hard_max, soft_min, soft_max,
short, depends_on=())`. The `ALL` list is the single source of truth; `BY_KEY`
and `GROUPS` are derived (`GROUPS = sorted(set(...))`), so adding specs to
`ALL` is sufficient. `settings_rules.validate()` enforces hard ranges
(`hard_min`/`hard_max` → error) and runs cross-checks only for keys that opt in.
Group `"universe"` already exists; a new `"gate"` group is additive (groups are
a derived set — no other code hardcodes the group list).

**Decision:** the four Q10 settings get hard ranges that enforce the
spec's minimums (`interval >= 1`, `bad_streak >= 1`, `good_streak >= 1`) in the
schema itself, so **no `settings_rules.py` cross-check is strictly necessary**
(Amendment 002 passes as-is). Three aggregation thresholds are added as
`market_gate_fail_share` / `market_gate_trend_share` / `market_gate_unknown_share`
because the spec's "No hardcoded assets or numbers" constraint forbids
hardcoding the verdict aggregation thresholds. No DB migration: defaults live in
the `get_setting_*` fallbacks (established Amendment 003 pattern).

## 7. Test pattern to mirror (tests/test_amendment003.py)

- `db` fixture: monkeypatch `db.db_ops.DB_PATH` to a tmp SQLite, run
  `initialize_database_tables()`.
- Non-divergence: replace a shared function with a spy that records calls and
  forwards to the original (`test_replay_uses_shared_threshold_function`),
  assert the call site was observed.
- Network isolation: `mock.patch.object(trade.universe, "_fetch_candidates",
  ...)` / `_fetch_depth` / `requests.get` — the same style applies to
  `_fetch_binance_*` and `detect_regime` for `tests/test_market_check.py`.

## 8. Line budgets (Constitution VII)

Current hot-path totals (verified with `wc -l`):
`bot.py` 558 · `regime.py` 263 · `pnl.py` 249 · `executor.py` 623 ·
`spot_scalper.py` 254 · `futures_scalper.py` 201 → **2,148 lines**, already above
the nominal 1,500 with a documented historical justification in
`.specify/plan.md` (executor complexity, two exchange APIs).

`trade/market_check.py` is a **new, non-hot-path module** (evaluated every
`market_gate_interval_min` and on demand; not part of the per-cycle hot path and
not in the constitution's enumerated hot-path list — the same standing as
`trade/universe.py`, 736 lines). The design keeps bot.py's growth to a thin
~35–45 lines (gate block + observation recording + per-venue state dict) by
moving the verdict logic and the debounce state machine into `market_check.py`.
This is the minimal-change posture: bot.py grows by one small periodic block and
one entry-guard line, both alongside existing, identical patterns.
