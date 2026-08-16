# Tasks: Entry Confirmation Candle (009)

**Input**: Design documents from `/specs/009-entry-confirmation-candle/`

**Prerequisites**: spec.md ✅ (Clarified, Q1–Q6), plan.md ✅, constitution.md ✅ (**v1.1.0**, Principle II amended 2026-08-15)

**Tests**: NEW `tests/test_entry_confirmation.py` (REQUIRED — plan.md Testing Strategy: 12 tests covering AC1–AC11 and AC14). Fixture pattern per `tests/test_spot_exit_hardening.py` (`db.db_ops.DB_PATH` → `tmp_path` + `initialize_database_tables()`; autouse module-state reset; network isolated via `mock.patch`).

**Organization**: dependency order — database (column must exist before anything writes it) → settings registry → helper → scalper call sites → tests → docs → full-suite regression. `bot.py` is **not** touched (plan M3: trailing default parameter keeps its 14-positional `_log` callers valid).

**Branch**: work directly on `main` (repo convention — no feature branches).

## Format: `[ID] [P?] Description`

- **[P]**: can run in parallel (different files, no dependencies)
- Each task: exact file paths + line anchors + definition of done / verification command

## Path Conventions

- Helper: `trade/regime.py` (`_candle_cache` line 208, `_candle_cache_key` 211, `get_atr_pct` def 215, `_compute_atr_pct` def 244 — append after it; `get_setting_int` already imported line 15)
- Spot call site: `trading_bot/spot_scalper.py` (import line 19, `scalp_cycle` def 215, toxicity skip 267, `_cooldown_ok` skip 269 — insert between; `signal_only` return 274, `entered` log 289, `_log` def 299)
- Futures call site: `trading_bot/futures_scalper.py` (import line 20, `scalp_cycle` def 115, toxicity skip 164, `_cooldown_ok` skip 166, `signal_only` 171, `entered` 183, `_log` def 199)
- Settings registry: `trade/settings_schema.py` (`entry` group 47–60; insert after `adaptive_enabled` line 59; `SettingSpec` dataclass line 13)
- Database: `db/migrations/009_entry_confirmation.sql` (NEW), `db/schema_v2.sql` (`signals` table line 148, `sl_price` line 176)
- Tests: `tests/test_entry_confirmation.py` (NEW)
- Docs: `docs/CURRENT_STATE.md` (`## 0.` convention), `docs/CHANGELOG.md`
- Feature tests: `./venv/bin/python -m pytest tests/test_entry_confirmation.py --basetemp=.pytest_tmp -q`
- Full regression: `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`

---

## Phase 1: Database — additive column (AC11)

**Purpose**: `signals.entry_confirmed` must exist before any code writes it. Dual update (migration + schema file) is the migration-005 convention — `schema_v2.sql` lines 175–176 carry `tp_price`/`sl_price`, which migration 005 introduced.

- [X] T001 Create `db/migrations/009_entry_confirmation.sql`:
  ```sql
  -- Migration 009: entry confirmation verdict on signals.
  -- 1 = confirmed, 0 = not confirmed, NULL = indeterminate / not evaluated.
  ALTER TABLE signals ADD COLUMN entry_confirmed INTEGER;
  ```
  Idempotent by the same mechanism migration 008 uses: `_run_migrations` (`db/db_ops.py` line 41) wraps each script in `try/except: pass`, so the duplicate-column error on re-run is swallowed. Do NOT add a backfill — pre-existing rows stay `NULL`.
  - *Verify*: `./venv/bin/python -c "from db.db_ops import initialize_database_tables, get_db_connection; initialize_database_tables(); initialize_database_tables();
    c=[r[1] for r in next(iter([get_db_connection().__enter__()])).execute('PRAGMA table_info(signals)')]; print('entry_confirmed' in c, c.count('entry_confirmed'))"` → `True 1`

- [X] T002 [P] Add `entry_confirmed  INTEGER` to the `signals` table in `db/schema_v2.sql` (table at line 148, after `sl_price` line 176) so fresh databases match migrated ones. Nullable, no default, no constraint.
  - *Verify*: fresh temp DB via `initialize_database_tables()` has the column (covered by T010 `test_migration_idempotent`).

**Checkpoint**: column exists on both fresh and migrated databases.

---

## Phase 2: Settings registry (AC10)

**Purpose**: register the switch so the Amendment 002 validator, the UI and Telegram see it. Default lives in the `get_setting_bool(..., False)` fallback — **no DB row required**.

- [X] T003 Insert into `trade/settings_schema.py` `ALL`, after `adaptive_enabled` (line 59–60), closing the `entry` group:
  ```python
      SettingSpec("entry_confirm_candle", bool, "entry", None, None, None, None, None,
                  "Require the last closed 5m return to confirm the reversal before entering (default false — observe-only: the verdict is recorded in signals.entry_confirmed but never blocks)"),
  ```
  Bool with no ranges / no `depends_on` — the exact `adaptive_enabled` shape. `BY_KEY`/`GROUPS` auto-derive. Do NOT touch `trade/settings_rules.py` (no cross-check needed; the generic validator accepts a bool with no ranges).
  - *Verify*: `./venv/bin/python -c "
    from trade.settings_schema import BY_KEY
    from trade.settings_rules import validate
    from db.db_ops import get_setting_bool
    s=BY_KEY['entry_confirm_candle']; print(s.type.__name__, s.group)
    print(validate('entry_confirm_candle', True).level, validate('entry_confirm_candle', False).level)
    print('default', get_setting_bool('entry_confirm_candle', False))"` → `bool entry` / `ok ok` / `default False`

**Checkpoint**: setting registered and validated.

---

## Phase 3: Helper — `trade/regime.py` (AC1, AC2, AC9)

**Purpose**: one pure read over the existing 5m cache. Depends on nothing; must precede the call sites.

- [X] T004 Append `last_closed_return_up(asset, venue) -> bool | None` to `trade/regime.py` after `_compute_atr_pct` (def line 244), exactly per plan.md M1:
  - cache miss **or** entry older than `candle_cache_sec` ⇒ call `get_atr_pct(asset, venue)` to populate (**sole cache owner — do NOT add a second fetch or error path**), then re-read `_candle_cache`.
  - `None` when the cache is still empty or has `< 2` bars (Constitution IV).
  - read `cached[1][-2]` — the last **closed** bar; `[-1]` is the in-progress one.
  - return `bar["close"] > bar["open"]` — a **flat bar returns `False`** (clarify Q1).
  - `get_setting_int` is already imported (line 15); add no new imports.
  - Add one line to the module docstring noting the helper shares the ATR candle cache.
  - *Verify*: `./venv/bin/python -c "
    import time, trade.regime as r
    r._candle_cache[r._candle_cache_key('X','binance','5m')]=(time.time(),[{'open':1,'close':2,'high':2,'low':1,'volume':0},{'open':2,'close':3,'high':3,'low':2,'volume':0},{'open':9,'close':1,'high':9,'low':1,'volume':0}])
    print(r.last_closed_return_up('X','binance'))"` → `True` (reads `[-2]`, ignores the falling in-progress bar)

**Checkpoint**: helper correct on the `[-2]` rule and the flat-bar rule.

---

## Phase 4: Scalper call sites (AC3–AC8)

**Purpose**: evaluate once per entry attempt, record always, block only when enforcing. Depends on T001 (column), T003 (setting), T004 (helper). Two files → [P].

- [X] T005 [P] `trading_bot/spot_scalper.py`:
  (a) line 19 → `from trade.regime import get_atr_pct, last_closed_return_up`;
  (b) insert between the toxicity skip (267) and the `_cooldown_ok` skip (269):
  ```python
      conf = last_closed_return_up(asset, venue)
      ec = None if conf is None else int(conf if direction == "long" else not conf)
      if get_setting_bool("entry_confirm_candle", False) and ec != 1:
          _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","entry_not_confirmed",tp=tp_price,sl=sl_price,ec=ec); return None
  ```
  (c) `_log` (def 299): signature gains a trailing `ec=None`; the INSERT column list gains `entry_confirmed`, the `VALUES` list one `?`, the tuple one `ec` — **append at the end of all three**, do not reorder;
  (d) pass `ec=ec` on the `"signaled"` (274) and `"entered"` (289) `_log` calls — **required for observe mode**, otherwise the column only ever records blocked entries.
  Do NOT change any other `_log` call, the cost gate (249), or anything above line 267.
  - *Verify*: T007–T011 cover it; smoke: `./venv/bin/python -c "import trading_bot.spot_scalper"` imports clean.

- [X] T006 [P] `trading_bot/futures_scalper.py` — identical to T005 with futures anchors: import line 20; block between 164 and 166; `_log` def 199; `ec=ec` on `"signaled"` (171) and `"entered"` (183). The `ec` expression is **unchanged** — `direction` is `"long"`/`"short"` here, so `int(conf if direction == "long" else not conf)` gives the Q4 symmetry (short confirmed by a **down** bar) with no extra branch.
  - *Verify*: `./venv/bin/python -c "import trading_bot.futures_scalper"` imports clean; T010 asserts the symmetry.

**Checkpoint**: both scalpers evaluate, record, and (when enabled) enforce.

---

## Phase 5: Tests (AC1–AC11, AC14)

- [X] T007 Create `tests/test_entry_confirmation.py` with the `db` fixture (`db.db_ops.DB_PATH` → `tmp_path`, `initialize_database_tables()`) and an autouse fixture clearing `trade.regime._candle_cache`, `spot_scalper._price_memory/_peak/_trough/_last_entry/_last_sl` and the futures equivalents. Add `test_helper_reads_last_closed_bar` (up/down/**flat ⇒ False**, plus an opposite in-progress `[-1]` bar proving `[-2]` is read — AC1) and `test_helper_indeterminate` (empty cache with `get_atr_pct` patched to a no-op ⇒ `None`; 1-bar cache ⇒ `None` — AC2).
- [X] T008 Add `test_observe_mode_never_blocks` (AC3) and `test_observe_mode_records` (AC4) — seed a **down** bar, drive `spot_scalper.scalp_cycle` with a fake exchange through a dip, assert the entry still happens and the `signals` row carries `entry_confirmed=0`.
- [X] T009 Add `test_enforce_blocks_unconfirmed` (AC5 — no `place_entry` call on the fake exchange, `skipped`/`entry_not_confirmed` row), `test_enforce_passes_confirmed` (AC6), `test_enforce_none_fails_closed` (AC7 — helper patched to `None`, row `entry_confirmed` `NULL`).
- [X] T010 Add `test_futures_direction_symmetry` (AC8 — long needs up, short needs down; synthetic cache; docstring marks it evidence-free per clarify Q4), `test_no_additional_api_calls` (AC9 — `trade.regime._fetch_ohlcv` patched with a counter; warm cache ⇒ 0 fetches), `test_setting_registered` (AC10), `test_migration_idempotent` (AC11 — double `initialize_database_tables()`, column present exactly once, pre-seeded row stays `NULL`).
- [X] T011 Add `test_ab_query` (AC14) — the `GROUP BY entry_confirmed` query over `signals` runs and returns rows.
  - *Verify* (T007–T011): `./venv/bin/python -m pytest tests/test_entry_confirmation.py --basetemp=.pytest_tmp -q` → all pass.

**Checkpoint**: AC1–AC11 + AC14 verified by unit tests.

---

## Phase 6: Docs (AC13)

- [X] T012 [P] `docs/CURRENT_STATE.md` — new `## 0. Entry Confirmation Candle (feature 009, 2026-08-15)` section at the top (006/007/008 convention): the rule as a **sign test on the last closed 5m return** (not pattern detection — Principle I), observe vs enforce, the new setting and column, cache sharing **including the `adaptive_enabled=false` caveat** (plan M1), the measured evidence (113 entries: +0.387%/trade confirmed vs −0.194% unconfirmed; stop-out 20% → 12%), and the Constitution VIII trade-off with the A/B query.
- [X] T013 [P] `docs/CHANGELOG.md` — `feat:` entry under a new `## 2026-08-15` heading, also recording Constitution **v1.1.0** (Principle II: "Reward Must Exceed Risk" → "Reward Must Exceed Cost") as context.

**Checkpoint**: docs updated — feature complete.

---

## Phase 7: Full-Suite Regression (AC12)

- [X] T014 Run `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q` from the project root. All suites must pass with 0 failures and 0 collection errors — 76 tests were green before this feature, plus the 12 new ones. Pay attention to `test_market_check.py` (its `_record_gate_skip` path exercises the modified `_log` signature — the plan M3 regression guard) and `test_spot_exit_hardening.py` (shares `scalp_cycle`).
  - *Verify*: 0 failed, 0 errors.

---

## ⛔ Out of Scope — MUST NOT Be Implemented

- **`bot.py`** — no change (plan M3: trailing default parameter keeps `_record_gate_skip` / `_record_global_block` valid with 14 positional args).
- **`trade/settings_rules.py`** — no change (bool with no ranges passes the generic Amendment 002 validator).
- **TP/SL values, `sl_k_spot`, thresholds, ATR, regime classification, toxicity, kill switches, market gate, broad-market filter, universe scanner, exits/stops, sizing** — untouched.
- **The cost gate** (`spot_scalper.py` 249 / `futures_scalper.py` 146) — Constitution II v1.1.0, untouched.
- **Frequency recovery** (`max_concurrent_positions`, `cex_slot_pct`, `max_active_pairs`, `universe_size`) — follow-up spec.
- **Loop latency / bookTicker snapshot** — follow-up spec.
- **No new API call, thread, process, dependency, or report contract.**
- **No backfill** of `signals.entry_confirmed`; **no** second fetch/error path in the helper (delegate to `get_atr_pct`).
- **Do NOT enable `entry_confirm_candle` in the DB** — enabling is an operator decision on observed data, not a code task.

---

## Dependencies & Execution Order

- **Phase 1 (DB)**: T001 → T002 [P] — column before any writer
- **Phase 2 (Settings)**: T003 — independent of Phase 1, before the call sites
- **Phase 3 (Helper)**: T004 — before the call sites
- **Phase 4 (Call sites)**: T005 ∥ T006 — depend on T001, T003, T004
- **Phase 5 (Tests)**: T007 → T008 → T009 → T010 → T011 (same file, sequential) — depend on T001–T006
- **Phase 6 (Docs)**: T012 ∥ T013 — depend on T001–T006
- **Phase 7 (Regression)**: T014 last

### Parallel Opportunities

- T001 ∥ T002 (migration file ∥ schema file)
- T005 ∥ T006 (spot ∥ futures — different files, identical shape)
- T012 ∥ T013 (different docs)

---

## Implementation Strategy

### MVP First (column + setting + helper + spot)

1. T001–T003 — column and setting exist
2. T004 — helper verified on the `[-2]` and flat-bar rules
3. T005 — spot call site (the venue with evidence and live trading)
4. **STOP and VALIDATE**: observe mode records, enforce blocks, nothing else moves
5. T006 futures → T007–T011 tests → T012–T013 docs → T014 regression

### Post-implementation (operator, not code)

- Leave `entry_confirm_candle` **unset** and let observe-mode accumulate.
- Decide enforcement on the AC14 query once the sample is materially larger than the study's n=113 (target 300+ entries), per the spec's Assumptions.

---

## Notes

- [P] tasks = different files, no dependencies
- No user stories in spec.md — organized by parts (matches the 005/006/008 repo format)
- Work directly on `main`; commit after each task or logical group
- Constitution **v1.1.0** applies — Principle II is the cost rule, not `tp > sl`
- Constitution VII: the 1,500-line hot-path budget is **already exceeded (2,442)**; this feature adds ~25 lines with explicit justification in plan.md. Re-baselining is a converge recommendation, not a task here.
