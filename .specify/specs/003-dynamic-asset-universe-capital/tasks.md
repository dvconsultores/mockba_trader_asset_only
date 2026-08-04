# Tasks: Dynamic Asset Universe & Capital View

**Input**: Design documents from `/specs/003-dynamic-asset-universe-capital/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/api.md ✅, quickstart.md ✅, checklists/requirements.md ✅

**Tests**: `tests/test_amendment003.py` (17 unit tests) — implemented.

**Organization**: Tasks are grouped by pipeline stage and interface. Implementation
is complete; tasks marked `[X]`. Remaining calibration tasks are marked `[ ]`.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths in descriptions

## Path Conventions

- **Scanner**: `trade/universe.py`
- **Trading bot**: `bot.py`, `trading_bot/`, `trade/`
- **Dashboard API**: `dashboard/main.py`
- **Dashboard UI**: `dashboard-ui/src/`
- **Telegram bot**: `telegram.py`
- **Database**: `db/schema_v2.sql`, `db/migrations/`

---

## Phase 1: Data Layer

- [X] T001 Create migration `db/migrations/006_amendment_003.sql` — `asset_universe`
      + `venue_state` tables, universe/capital settings seeds, defensive
      `fee_round_trip_pct` delete, `settings_baseline` seeds (unvalidated)
- [X] T002 [P] Add `asset_universe` + `venue_state` DDL to `db/schema_v2.sql`
      (idempotent)
- [X] T003 Add universe CRUD to `db/db_ops.py`: `replace_universe` (blacklist
      carry-forward), `get_universe`, `get_universe_row`, `get_universe_scan_age`,
      `set_blacklist`, `get_tradeable_universe`
- [X] T004 Add `venue_state` CRUD to `db/db_ops.py`: `set_venue_equity`,
      `get_venue_equity`; and `get_capital_pool(venue)`

## Phase 2: Scanner (`trade/universe.py`)

- [X] T005 Shared threshold function `compute_thresholds(...)` — extracted from
      `spot_scalper.py`/`futures_scalper.py`, used by both scalpers AND the replay
- [X] T006 Stage 1 `_fetch_candidates(venue)` — bookTicker + 24hr (2
      whole-exchange calls) + exchangeInfo config; quote filter, leveraged-token /
      stablecoin / non-trading exclusion; DEX via Orderly listing + Binance proxy
- [X] T007 Stage 2 `_hard_filters_pass(...)` — volume, spread vs `tp_min ×
      spread_ratio_max`, volume-rank band, `min_notional × 1.5` fundability
- [X] T008 Stage 3 `_depth_check(...)` + `_TokenBucket` — top-10 depth both sides
      ≥ `multiple × slot`; `ScanBudgetExhausted` aborts preserving previous universe
- [X] T009 Stage 4 `replay_symbol(...)` — rolling peak/trough (window 40, warmup
      10), ATR from `atr_period`, `compute_thresholds`, forward TP look within
      `max_hold_minutes`; yields signals_count / recovery_rate /
      median_minutes_to_tp / atr_pct_median
- [X] T010 Stage 5 `select_ranked(...)` — reject `signals_count <
      universe_min_signals` and `recovery_rate < min_recovery_rate`; rank by
      recovery then ATR; truncate to `universe_size`; DEX short-store without
      loosening filters
- [X] T011 `min_recovery_rate(venue)` — `'auto'` resolves to per-venue breakeven
      `(sl+fee)/(tp+sl)`; literal overrides
- [X] T012 Orchestration: `scan_venue`, `run_scans_if_due`, `is_universe_stale`

## Phase 3: Trading Loop Guards (`bot.py`)

- [X] T013 Scanner thread — runs on startup if stale, then every
      `universe_scan_interval_hours`; never inside the trading cycle
- [X] T014 Universe-driven iteration — `get_tradeable_universe(venue)` unioned
      with open-position assets; exits always run first (churn never forces an exit)
- [X] T015 Stale-universe guard — `universe_max_age_hours` blocks new entries,
      exits continue
- [X] T016 Live spread degradation guard — `_get_obi_and_spread` (same OBI call,
      no extra API) vs scan-time spread × degradation multiple
- [X] T017 `set_venue_equity` cache write per cycle; startup validation runs
      `validate_capital_pools`
- [X] T018 `trade/pnl.py` `compute_slot_size` — `{venue}_slot_pct × live equity`,
      min-notional floor, per-day cache; per-asset capital branch removed
- [X] T019 [P] Scalpers — import shared `compute_thresholds`; per-venue
      `max_slots_cex` / `max_slots_dex`

## Phase 4: Validation (Amendment 002 surface)

- [X] T020 [P] `trade/settings_schema.py` — 16 new SettingSpecs
      (`universe_*`, `capital_*`, `max_slots_*`)
- [X] T021 [P] `trade/settings_rules.py` — cross-checks (rank band empty → error,
      staleness guarantee → error, `max_slots × slot_pct > 100` → error,
      per-venue fee net-edge → error, spread_ratio > 0.25 → warn, short universe /
      depth requirement → warn) + `validate_capital_pools`

## Phase 5: Interfaces

- [X] T022 [P] `telegram.py` — `/capital`, `/universe [cex|dex]`,
      `/blacklist add|remove`; remove per-asset add/toggle/remove handlers;
      repoint signal asset picker to the universe
- [X] T023 [P] `dashboard/main.py` — `GET /api/capital`, `GET /api/universe/{venue}`,
      `PUT /api/universe/{venue}/{asset}/blacklist`; remove `/api/assets*`
- [X] T024 [P] `dashboard-ui` — `CapitalManager.tsx` (venue panels + universe
      panels), App tab `capital`, `validation.ts` new keys; delete `AssetManager.tsx`

## Phase 6: Tests & Docs

- [X] T025 `tests/test_amendment003.py` — 17 tests (shared-threshold binding,
      replay recovery, breakeven auto, hard filters, min-signals exclusion,
      DEX short-store, ranking, blacklist carry-forward, budget exhaustion,
      live-equity sizing, per-venue net edge, cross-checks)
- [X] T026 Update `docs/CALIBRATION.md` (recovery gap, rank band, per-venue fees)
      and `docs/CURRENT_STATE.md`
- [X] T027 Apply migrations to the live DB (`initialize_database_tables`) and
      verify tables/settings

## Phase 7: Dry-Run Calibration (REMAINING — requires live data)

- [ ] T028 Dry-run: universe churn (entered/left daily; trade clustering in stable
      members vs newcomers)
- [ ] T029 Dry-run: predicted-vs-realized recovery rate per asset; record the gap
      in `docs/CALIBRATION.md`
- [ ] T030 Dry-run: realized expectancy by volume-rank decile (tests the 15–90 band)
- [ ] T031 Dry-run: per-venue net expectancy side by side (fee advantage in outcomes)
- [ ] T032 Measure the actual CEX round-trip fee and replace the 0.20% assumption

---

## Phase 8: Convergence

- [X] T033 Repoint `bot.py` restart reconciliation and startup validation away from
      legacy per-asset capital: `_reconcile_startup` now derives (asset, venue)
      pairs from the dynamic universe (`get_tradeable_universe` unioned with
      open-position assets) so Constitution VI restart safety covers universe-only
      assets; the `validate_all_assets`/`get_active_pairs` per-asset-capital
      reads were dropped from `validate_startup`; `validate_asset_capital`,
      `validate_asset_overallocation`, `validate_all_assets` and the
      asset_configs-based `max_active_pairs` cross-check were retired from
      `trade/settings_rules.py` (AC16 — `max_active_pairs` now checks the
      universe count)
- [X] T034 Universe-replay OHLCV for DEX is fetched via the Binance proxy
      (`{ASSET}USDT`) in `trade/universe.py` `scan_venue`, matching
      `trade/regime.py`'s proxy pattern and the plan's DEX-data decision; the
      direct Orderly kline call is no longer used by the replay
