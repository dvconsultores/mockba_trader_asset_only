# Research: Multi-Asset Trading with Per-Asset Capital

**Feature**: 002-multi-asset-capital | **Date**: 2026-07-27

## Research Tasks & Decisions

### 1. Per-Asset Storage Mechanism

**Question**: How to store per-asset `capital_dex`, `capital_cex`, `active_dex`, `active_cex` fields alongside the existing key-value `settings` table?

**Options Considered**:

| Option | Pros | Cons |
|--------|------|------|
| A) Key convention in `settings` table: `asset:NEAR:capital_dex=5000` | No new table, backward-compatible | Awkward to query all assets, no type safety, hard to enforce uniqueness |
| B) JSON column in single `settings` row | Single read, no schema change | Not queryable by SQL, no constraints, migration fragility |
| C) New `asset_configs` relational table | Queryable, constrainable, clean migration, idiomatic SQLite | New table, new CRUD functions in `db_ops.py` |
| D) Inline JSON in existing `assets` setting | Minimal change | No per-asset struct, breaks current comma-separated format |

**Decision**: **Option C — New `asset_configs` table**

**Rationale**:
- The `settings` table is already used for single-value key-pair configuration. Mixing structured multi-column data into it would abuse the key-value pattern.
- SQLite handles small relational tables efficiently (the bot already has `open_positions`, `closed_trades`, `signals`).
- Type constraints (REAL, INTEGER/BOOLEAN) prevent invalid data at the database level.
- A UNIQUE constraint on `symbol` prevents duplicates.
- Querying all assets for the loop is a single `SELECT * FROM asset_configs`.
- The migration from comma-separated `assets` string is straightforward.

**Alternatives Considered**: Option A was viable but would require `LIKE 'asset:%'` prefix queries and manual type coercion — fragile and slower.

---

### 2. Migration Strategy

**Question**: How to safely migrate from legacy global settings to per-asset `asset_configs` without data loss?

**Legacy keys to detect & remove**:
- `assets` (comma-separated string) → data source for migration
- `dex_slot_pct` → compute `capital_dex = equity * slot_pct / 100`
- `cex_slot_pct` → compute `capital_cex = equity * slot_pct / 100`
- `auto_trade_binance` → `active_cex` for primary asset
- `auto_trade_orderly` → `active_dex` for primary asset
- `capital` (if exists) → no direct mapping, discard

**Decision**: **One-time startup migration in `db_ops.py` `initialize_database_tables()`**

**Rationale**:
- Migration runs once on first startup after upgrade. Detected by presence of legacy keys.
- After migration, legacy keys are deleted (not just ignored) to prevent re-migration.
- Equity is queried from exchanges at migration time to convert percentage→absolute.
- If equity query fails, migration logs a warning and retries next startup — does NOT block startup (the bot can still trade with existing per-asset data if migration already ran).
- Non-primary assets receive zero capital + inactive flags (operator configures them later).
- Historical data (`open_positions`, `closed_trades`, `signals`) is untouched — only `settings` keys are modified and `asset_configs` rows inserted.

**Migration idempotency**: The migration checks `SELECT COUNT(*) FROM asset_configs` — if rows exist, migration is skipped regardless of legacy key presence.

---

### 3. Bot Loop Architecture

**Question**: How to restructure the main loop to iterate over (asset, venue) pairs instead of the current nested `for asset in assets: if cex_active: ... if dex_active: ...` pattern?

**Decision**: **Flat pair iteration with per-pair exception isolation**

**Rationale**:
- Current pattern: `for asset in assets: if cex_active: [spot cycle]; if dex_active: [futures cycle]`
- New pattern:
  ```python
  pairs = get_active_pairs()  # returns list of (asset, venue, capital, config)
  for asset, venue, capital, config in pairs:
      try:
          # Per-pair cycle: manage exits, evaluate entry
      except Exception as e:
          logger.error(f"[ERROR] {venue}:{asset} cycle failed: {e}")
          # Continue to next pair — do NOT abort the loop
  ```
- Each pair's cycle is an independent try/except block (FR-020).
- `get_active_pairs()` filters to pairs where `active_<venue>=true` AND `capital_<venue> > 0`.
- If zero active pairs for a venue, skip that exchange's equity query entirely (avoid wasted API calls).

**Per-pair state isolation**:
- `spot_scalper.py` and `futures_scalper.py` already use module-level dicts keyed by asset string: `_price_memory[asset]`, `_last_entry[f"{venue}:{asset}:{side}"]`. These already provide per-asset isolation.
- The key change is scoping `_last_entry` to include venue explicitly (already done in current code: `f"binance:{asset}:{side}"`).
- PnL tracking: `is_entry_blocked(venue, equity)` currently takes venue only. Must be extended to accept `asset` parameter for per-pair kill switch scope (FR-008).

---

### 4. Position Sizing with Per-Asset Capital

**Question**: How to compute position size when each asset has its own absolute USD capital allocation?

**Current**: `compute_slot_size(venue, equity)` reads `{venue}_slot_pct` as a percentage of total equity and returns one slot.

**Decision**: **Replace percentage-based sizing with absolute-USD sizing**

**New signature**: `compute_slot_size(asset: str, venue: str, capital: float, equity: float, min_notional: float) -> float`

**Rationale**:
- Each asset's `capital_<venue>` is the maximum USD to allocate to that pair.
- Slot size = `min(capital, max_slot_usd)` where `max_slot_usd` is derived from the existing risk parameters.
- DEX compounding: profits compound into the asset's own `capital_dex` pool. This means `capital_dex` is the *current* allocation, updated periodically (daily or on significant PnL change) from that asset's realized PnL on DEX.
- CEX does not compound profits into capital (spot, no leverage — capital stays fixed).
- `max_effective_slots` becomes per-pair: how many slots can this asset's capital support at the current slot size.

---

### 5. Kill Switch Scoping

**Question**: How to scope daily loss limits and consecutive loss tracking per (asset, venue) pair while also supporting a global kill switch?

**Decision**: **Per-pair PnL tracking with global aggregate**

**Rationale**:
- `get_daily_pnl(venue)` currently sums `pnl_net` for a venue. Extend to `get_daily_pnl(asset=None, venue=None)` with optional asset filtering.
- `get_consecutive_losses(venue)` extended similarly to `get_consecutive_losses(asset=None, venue=None)`.
- In the loop, each pair checks its own limits first. If its limit is breached, skip that pair.
- After all pairs are processed, check the global `global_daily_loss_limit` by summing PnL across all pairs. If breached, disable all trading (`trading_enabled=0`).
- This is additive to the existing per-venue checks, layered on top.

---

### 6. API Rate Limiting Strategy

**Question**: Multi-asset iteration multiplies exchange API calls. Current per-cycle call budget: ~4 calls (OBI × 2 venues + price × 2 venues). With N assets: ~4N calls per cycle.

**Decision**: **Staggered batching if needed, start unbounded for ≤10 assets**

**Rationale**:
- Binance rate limits: 1200 requests/minute for order book (depth), 1200/min for ticker/price. With 10 assets and a 30-second cycle: 10 × 2 × 2 = 40 calls/cycle = 80 calls/minute. Well within limits.
- Orderly DEX: OBI and price are proxied through Binance (existing pattern in `bot.py` lines 176-182), so the same Binance limits apply to both venues.
- Exponential backoff on rate-limit errors (429) per venue — already standard HTTP client practice.
- If more than 10 assets are configured, the max active pairs setting (default 6) caps the actual API call volume.
- No websocket-per-asset model needed at this scale. The 30-second cycle is sufficient for REST polling.

**Bottom line**: No staggering/batching implementation needed for the default configuration. The `max_active_pairs` guardrail (FR-018, default 6) is the primary rate-limit safety mechanism.

---

### 7. Validation Rules for Per-Asset Capital

**Question**: What validation rules must be added to `settings_rules.py` for per-asset capital?

**Decision**: **New cross-check rules in the validator**

New rules to add:
1. `capital_dex > 0 if active_dex is true` — warn, not error (zero capital with active flag is a no-op, not dangerous)
2. `capital_cex > 0 if active_cex is true` — same
3. `sum(asset.capital_dex for asset in active_dex_assets) <= dex_equity` — error if exceeded (FR-010)
4. `sum(asset.capital_cex for asset in active_cex_assets) <= cex_equity` — error if exceeded (FR-010)
5. `active_pairs_count <= max_active_pairs` — warn if exceeded (operator can raise the limit)
6. `concurrent_positions <= max_concurrent_positions` — already covered by existing `max_slots` check

The validator already takes a `SettingsContext` with `venue` and `equity`. This must be extended to also accept the asset list for cross-asset validation.

---

### 8. DEX Compounding Model

**Question**: How does DEX profit compounding work per-asset?

**Current**: DEX compounding is implicit — `compute_slot_size` uses `equity * slot_pct / 100`, and equity grows as positions close profitably. All profits go to total equity, shared by all assets.

**Decision**: **Per-asset DEX capital tracking with periodic recomputation**

**Rationale**:
- Each asset's `capital_dex` is the current allocation. It grows as that asset's DEX positions close profitably.
- Recomputation: daily (at UTC midnight, matching the existing slot-size cache pattern). Sum of that asset's `closed_trades.pnl_net` for DEX trades since the last recomputation is added to `capital_dex`.
- Alternatively, the operator can manually adjust `capital_dex` at any time via the UI.
- CEX `capital_cex` does NOT compound — CEX is spot-only, no leverage, capital is fixed allocation.
- This is tracked as a new column: `capital_dex_base` (original allocation) + derived `capital_dex_current` = `capital_dex_base + cumulative_dex_pnl`. The UI shows current; the base is stored.

**Simplification for initial implementation**: Store only `capital_dex` and `capital_cex` as mutable values. The operator (or a future auto-rebalance feature) updates them. Compounding is implicit in growing exchange equity, and the operator periodically rebalances. This avoids adding `capital_dex_base` complexity in v1.

---

## Resolved Unknowns Summary

| # | Unknown | Resolution |
|---|---------|------------|
| 1 | Storage mechanism | New `asset_configs` table in SQLite |
| 2 | Migration strategy | One-time startup migration, idempotent, legacy key deletion |
| 3 | Loop architecture | Flat pair iteration, per-pair exception isolation |
| 4 | Position sizing | Absolute USD capital per pair, no percentage math |
| 5 | Kill switch scoping | Per-pair + global aggregate daily loss limit |
| 6 | Rate limit strategy | No batching needed for ≤10 assets; max_active_pairs is the safety cap |
| 7 | Capital validation rules | 6 new cross-check rules in settings_rules.py |
| 8 | DEX compounding | Manual rebalance by operator; capital_dex is mutable; automatic compounding deferred |
