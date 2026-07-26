# Feature Specification: Amendment 001 — Adaptive Thresholds & Toxicity Observability

**Feature Branch**: `amendment-001-adaptive-toxicity`

**Created**: 2026-07-26

**Status**: Draft (partial implementation — spot scalper complete, futures scalper pending)

**Input**: Real-world testing of the original mean-reversion bot revealed three problems with the entry gate design: (1) OBI thresholds are useless in practice — NEAR OBI sits at 1.03–1.13, so a threshold of 1.0 never triggers and a relaxed 1.20 always passes; OBI does nothing. (2) A fixed `dip_pct` of 0.4% ignores volatility — too strict in quiet markets, too loose in volatile ones. (3) Dip magnitude alone cannot distinguish mean reversion from a dump — a 0.4% dip over 10 minutes vs. 0.4% in one candle are opposite trades. This amendment addresses all three.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Bot uses volatility-adaptive thresholds for entries (Priority: P1)

The bot computes ATR (Average True Range) as a percentage of price per asset and per venue. Instead of a fixed `dip_pct`, entry thresholds scale with volatility: `dip_threshold = max(dip_k × atr_pct, dip_min_pct)`. The same adaptive formula applies to pump_threshold, tp_pct, and sl_pct. In quiet markets (low ATR), thresholds tighten to the configured minimums. In volatile markets (high ATR), thresholds widen proportionally so the bot doesn't enter every noise-driven wiggle.

**Why this priority**: This is the core fix. Without adaptive thresholds, the bot either never trades (threshold too strict) or trades every noise spike (threshold too loose). A single fixed percentage cannot work across the full volatility spectrum.

**Independent Test**: Feed the bot synthetic price data at two volatility levels (ATR = 0.2% vs ATR = 1.5%). Verify that in the low-volatility scenario, thresholds floor at their `*_min_pct` values, and in the high-volatility scenario, thresholds scale up to `*_k × ATR`. Confirm entries only fire when the price extreme exceeds the computed adaptive threshold.

**Acceptance Scenarios**:

1. **Given** `adaptive_enabled = true`, ATR = 0.2%, `dip_k = 0.5`, `dip_min_pct = 0.15`, **When** the effective dip threshold is computed, **Then** it equals `max(0.5 × 0.2, 0.15) = 0.15%` (floor at min).
2. **Given** `adaptive_enabled = true`, ATR = 2.0%, `dip_k = 0.5`, `dip_min_pct = 0.15`, **When** the effective dip threshold is computed, **Then** it equals `max(0.5 × 2.0, 0.15) = 1.0%` (scales with volatility).
3. **Given** `adaptive_enabled = false` or ATR unavailable, **When** the effective threshold is computed, **Then** it falls back to the configured `*_min_pct` value as a static threshold.
4. **Given** effective TP threshold (`tp_k × ATR`) ≤ effective SL threshold (`sl_k × ATR`), **When** the cycle runs, **Then** the entry is skipped with reason "tp_eff <= sl_eff" — the constitution's "reward must exceed risk" principle is preserved in the adaptive regime.

---

### User Story 2 — Bot logs OBI and toxicity metrics without gating entries (Priority: P1)

OBI (Order Book Imbalance) is computed every cycle and logged to the `signals` table along with its z-score. Four toxicity checks — velocity, spread, depth, and OBI — run every cycle and produce individual verdicts (1 = would block, 0 = would pass, NULL = warmup). All four checks are observe-only by default (enforcement OFF). The `signals` table records every toxicity verdict whether or not it affected the entry decision. This turns the bot into a data-collection instrument: after running observe-only for days/weeks, an operator can review the `signals` table to determine which toxicity checks would have caught bad trades without blocking good ones.

**Why this priority**: This is the second core fix. OBI was a mandatory AND gate that did nothing. By demoting it to a logged metric and adding three more observability dimensions (spread, depth, velocity), the bot generates the data needed to calibrate future enforcement. This is the minimum viable change that fixes the "OBI does nothing" problem while building the foundation for evidence-based gating.

**Independent Test**: Run the bot with all `tox_*_enforce = false`. Verify that the `signals` table contains non-NULL values for `obi_z`, `spread_z`, `depth_ratio`, `velocity_pct`, and all four `tox_*` verdict columns for every cycle, and that `tox_enforced = 0` on all rows. Verify that entries proceed regardless of toxicity verdicts.

**Acceptance Scenarios**:

1. **Given** all `tox_*_enforce = false`, a dip meeting the adaptive threshold, and toxicity checks returning `tox_any = 1`, **When** the cycle runs, **Then** the entry is still opened (toxicity is observe-only) and the `signals` row records `tox_any = 1`, `tox_enforced = 0`.
2. **Given** all `tox_*_enforce = false`, a dip meeting the adaptive threshold, and OBI at any value, **When** the cycle runs, **Then** OBI does NOT gate the entry. The `signals` row records the OBI value and OBI z-score but makes no entry decision based on them.
3. **Given** fewer than 5 observations in the toxicity window (warmup), **When** toxicity checks run, **Then** z-scores and depth_ratio return NULL (not 0) and the `signals` row reflects NULL for those fields.

---

### User Story 3 — Bot enforces toxicity checks when explicitly enabled (Priority: P2)

An operator who has reviewed observe-only data can selectively enable one or more toxicity checks: `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`. When any enabled check triggers (verdict = 1), the entry is blocked. The `signals` row records `tox_enforced = 1` and the reason reflects which check(s) fired. Enforcement is per-check — enabling velocity enforcement does not enable spread enforcement.

**Why this priority**: Enforcement is the long-term goal, but it must be data-driven. The observe-only foundation (Story 2) must run first. This story is P2 because it depends on calibration data that doesn't exist yet.

**Independent Test**: Set `tox_spread_enforce = true` and `spread_z_max = 1.0` (artificially low to guarantee triggering). Run against live data. Verify entries are blocked when spread z-score exceeds the threshold. Set `tox_spread_enforce = false`. Verify entries resume. Test each enforcement flag independently.

**Acceptance Scenarios**:

1. **Given** `tox_spread_enforce = true`, `spread_z_max = 1.0`, and spread z-score = 1.5, **When** the cycle runs, **Then** the entry is blocked, `tox_enforced = 1`, and reason is "toxicity".
2. **Given** `tox_spread_enforce = true` but `tox_velocity_enforce = false`, and only velocity triggers (spread does not), **When** the cycle runs, **Then** the entry proceeds — only enabled checks gate.
3. **Given** warmup (NULL verdicts) and `tox_spread_enforce = true`, **When** the cycle runs, **Then** a NULL verdict does NOT block the entry — only a definitive `tox_spread = 1` gates.

---

### User Story 4 — Futures scalper adopts the same adaptive + toxicity logic (Priority: P2)

The `futures_scalper.py` module (Orderly DEX) is rewritten to match the spot scalper's Amendment 001 logic: adaptive thresholds via ATR, OBI logged but not gated, toxicity checks observe-only. The futures scalper retains its existing DEX-specific behavior (bracket orders with SL, leverage, long+short) but adopts the same entry evaluation pipeline.

**Why this priority**: The spot scalper rewrite is complete. Futures must match to maintain a single, consistent strategy across venues (constitution principle I). However, the spot scalper can validate the approach independently first.

**Independent Test**: Run both scalpers side-by-side with the same asset. Verify they compute identical adaptive thresholds and toxicity verdicts for the same price data. The only differences should be venue-specific (symbol derivation, order structure).

**Acceptance Scenarios**:

1. **Given** the same asset and same ATR_pct, **When** both spot and futures scalpers compute the effective dip threshold, **Then** they produce the same value.
2. **Given** a long entry signal on futures, **When** the entry is placed, **Then** it uses a bracket order with adaptive TP and SL computed from the fill price.
3. **Given** `tox_velocity_enforce = true` on futures, **When** velocity exceeds `max_extreme_velocity_pct`, **Then** the futures entry is blocked with the same logic as spot.

---

### Edge Cases

- **ATR unavailable (new asset, no candles)**: Falls back to `*_min_pct` static thresholds. Logs a warning. Does not block trading.
- **ATR = 0 (stablecoin or pegged asset)**: Falls back to `*_min_pct`. The `tp_eff <= sl_eff` check must still pass.
- **All four toxicity enforce flags ON simultaneously**: Any single check triggering blocks the entry. The bot does not require all four to agree.
- **Toxicity window misconfiguration**: If `tox_window` is set to < 5, z-scores will always be NULL (warmup never completes). This is a configuration error, not a crash. The bot logs a warning and proceeds with NULL verdicts.
- **Settings changed mid-cycle**: Adaptive thresholds and toxicity enforcement flags are read fresh each cycle. A change takes effect on the next cycle without restart.
- **Signal table column count mismatch**: The migration script (002) is idempotent. Running it against a schema that already has the columns is harmless.
- **OBI value extreme (flash crash, API anomaly)**: OBI is logged but not gated, so an anomalous OBI reading cannot block a legitimate entry. The OBI z-score check would flag it in the `signals` table for later review.

## Requirements *(mandatory)*

### Functional Requirements

#### FR-A: Adaptive Thresholds

- **FR-A1**: The bot MUST compute ATR as a percentage of price (`atr_pct`) per asset per venue using `atr_period` candles (default 14).
- **FR-A2**: When `adaptive_enabled = true` and ATR is available, the effective threshold for each parameter (dip, pump, TP, SL) MUST be `max(k × atr_pct, min_pct)` where `k` and `min_pct` are per-parameter settings.
- **FR-A3**: When `adaptive_enabled = false` or ATR is unavailable, the bot MUST fall back to the `*_min_pct` value as a static threshold.
- **FR-A4**: The bot MUST enforce `tp_eff > sl_eff` at the start of every entry evaluation cycle. If the adaptive computation violates this (e.g., ATR so low that both floor at their mins and tp_min ≤ sl_min), the cycle MUST be skipped.
- **FR-A5**: ATR MUST be cached per asset per venue for `candle_cache_sec` (default 60) to avoid excessive API calls.

#### FR-B: OBI Demotion

- **FR-B1**: OBI MUST be computed and logged to the `signals` table on every cycle but MUST NOT gate entry decisions.
- **FR-B2**: The settings `obi_buy_threshold` and `obi_sell_threshold` MUST be removed from the settings table.
- **FR-B3**: The `signals` table MUST record the raw OBI value and its z-score against the per-asset-per-venue OBI history.

#### FR-C: Toxicity Observability

- **FR-C1**: Four toxicity checks MUST run every cycle: velocity (cumulative extreme over `velocity_window` cycles), spread (z-score of current spread vs. history), depth (ratio of current depth to historical mean), and OBI (z-score of current OBI vs. history).
- **FR-C2**: Each check MUST produce a verdict: 1 (would block), 0 (would pass), or NULL (warmup — fewer than 5 observations in the window).
- **FR-C3**: All toxicity verdicts, z-scores, ratios, and the aggregate `tox_any` flag MUST be written to the `signals` table on every cycle.
- **FR-C4**: Toxicity enforcement MUST be OFF by default for all four checks. `tox_enforced` MUST be 0 unless at least one enforcement flag is ON and its corresponding verdict is 1.

#### FR-D: Toxicity Enforcement (Optional)

- **FR-D1**: Each toxicity check MUST have an independent enforcement flag: `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`.
- **FR-D2**: When any enabled check returns verdict = 1, the entry MUST be blocked and `tox_enforced` MUST be set to 1 in the `signals` row.
- **FR-D3**: A NULL verdict (warmup) MUST NOT block an entry, even if the corresponding enforcement flag is ON.
- **FR-D4**: Enforcement flags MUST default to false and MUST be changeable via settings without restart.

#### FR-E: Schema Expansion

- **FR-E1**: The `signals` table MUST be expanded to 24 columns including: direction, threshold_pct, atr_pct, velocity_pct, obi_z, spread_pct, spread_z, depth_top10, depth_ratio, tox_velocity, tox_spread, tox_depth, tox_obi, tox_any, tox_enforced, and position_id.
- **FR-E2**: A `signal_id` foreign key MUST link `closed_trades` rows back to their originating `signals` row for post-trade toxicity analysis.
- **FR-E3**: A `signal_id` column MUST be added to `open_positions` for the same traceability.
- **FR-E4**: The migration MUST be idempotent — safe to run multiple times against the same database.

#### FR-F: Settings Changes

- **FR-F1**: New settings added: `adaptive_enabled`, `atr_period`, `candle_cache_sec`, `dip_k`, `dip_min_pct`, `pump_k`, `pump_min_pct`, `tp_k`, `tp_min_pct`, `sl_k`, `sl_min_pct`, `tox_window`, `velocity_window`, `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`, `max_extreme_velocity_pct`, `spread_z_max`, `depth_ratio_min`, `obi_z_max`.
- **FR-F2**: Superseded settings removed: `obi_buy_threshold`, `obi_sell_threshold`, `dip_pct`, `pump_pct`.
- **FR-F3**: All new settings MUST be readable via `get_setting_float` / `get_setting_bool` with sensible defaults so the bot starts correctly on a fresh database.

### Key Entities

- **Signal** (expanded): A per-cycle decision record. Captures the full state at evaluation time: asset, venue, regime, direction, price, extreme_pct, adaptive threshold used, ATR value, OBI raw and z-score, spread raw and z-score, depth raw and ratio, all four toxicity verdicts, aggregate tox_any, tox_enforced, action (entered | skipped), reason. Links to position via `position_id` and to closed trades via `signal_id`.
- **Toxicity History**: A per-asset-per-venue rolling window of spread, depth, OBI, and extreme values used to compute z-scores and ratios. In-memory only (not persisted across restarts). Warmup requires `tox_window` observations.
- **ATR**: Average True Range expressed as a percentage of price. Computed from `atr_period` OHLCV candles. Cached per asset per venue.

## Success Criteria

1. **SC-001**: After 24 hours of dry-run operation, the `signals` table contains at least one row per cycle per asset with all 24 columns populated (NULLs allowed only during warmup for z-score/ratio columns).
2. **SC-002**: With `adaptive_enabled = true`, the effective dip threshold varies across volatility regimes — higher when ATR is above 1%, floored at `dip_min_pct` when ATR is below 0.3%.
3. **SC-003**: With all `tox_*_enforce = false`, no entry is ever blocked by a toxicity verdict — `tox_enforced` is 0 on every row for at least 100 consecutive cycles.
4. **SC-004**: With `tox_spread_enforce = true` and `spread_z_max = 1.0` (trigger threshold), entries are blocked within 5 cycles of a spread spike while entries with normal spreads continue unaffected.
5. **SC-005**: The spot scalper and futures scalper produce identical adaptive threshold values and toxicity verdicts when given the same asset and market data.
6. **SC-006**: The migration script (002) runs successfully against a database with the pre-amendment schema and against a database where it has already been applied, without errors or duplicate columns.
7. **SC-007**: The bot starts correctly on a fresh database — all new settings receive their default values, and removed settings (`obi_buy_threshold`, `obi_sell_threshold`, `dip_pct`, `pump_pct`) are absent.

## Assumptions

- OBI will continue to be computed each cycle (the data is still useful for the OBI z-score toxicity check), but it is no longer a gating condition.
- The `tox_window` default of 120 cycles provides sufficient history for meaningful z-scores. At a 5-second cycle, warmup completes in ~10 minutes.
- ATR from Binance/Orderly OHLCV endpoints is reliable enough for adaptive threshold computation. No alternative volatility estimator (e.g., Parkinson, Garman-Klass) is needed for v1.
- The futures scalper rewrite will mirror the spot scalper's logic exactly; no futures-specific toxicity or adaptive features are in scope beyond what spot already implements.
- Toxicity enforcement calibration (choosing optimal thresholds for `spread_z_max`, `depth_ratio_min`, `obi_z_max`, `max_extreme_velocity_pct`) is out of scope for this amendment — it requires post-deployment data analysis.
- The `signals` table row count will grow unboundedly. A retention/archival strategy is deferred to a future amendment.

## Dependencies

- **Pre-existing**: `schema_v2.sql` (the base schema), `regime.py` (extended with `get_atr_pct()`), `db_ops.py` (settings read/write), `pnl.py` (entry blocking, slot sizing).
- **Co-delivered**: Migration `002_amendment_001.sql`, `toxicity.py` module, rewritten `spot_scalper.py`.
- **Pending**: Rewrite of `futures_scalper.py` to adopt the same adaptive + toxicity pipeline.
