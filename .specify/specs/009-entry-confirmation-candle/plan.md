# Plan: Entry Confirmation Candle

**Feature**: 009-entry-confirmation-candle | **Date**: 2026-08-15 | **Spec**: `specs/009-entry-confirmation-candle/spec.md`
**Status**: Implemented 2026-08-15 — all tasks complete, 88/88 tests green
**Branch**: `main` (repo convention — the repo tracks only main, no feature branches)

**Input**: Feature spec (Clarified, Q1–Q6 resolved, 14 acceptance criteria) and
Constitution **v1.1.0** (Principle II amended 2026-08-15). This plan does not
re-litigate resolved decisions; it pins the three mechanisms the spec left to
planning: **how the helper reaches the candle cache**, **exactly where the check
sits in the entry chain**, and **how `entry_confirmed` reaches the `signals`
INSERT without breaking bot.py's positional `_log` callers**.

## Summary

One entry-timing filter with an observe/enforce switch. `trade/regime.py` gains
`last_closed_return_up(asset, venue)` — a pure read over the **existing 5m
`_candle_cache`** that `get_atr_pct` already fills — returning whether the last
**closed** bar's return was positive (`close > open`; flat ⇒ `False`), or `None`
when indeterminate. Both scalpers evaluate it once per entry attempt after the
toxicity check and before the cooldown check, always recording the verdict on
the `signals` row (new `entry_confirmed` column) and blocking only when
`entry_confirm_candle=true`. Default `false` ⇒ **zero behaviour change** on
deployment; the column starts accumulating the A/B data immediately. No new
network call in the live configuration, no new thread, no new dependency.

## Technical Context

**Language/Version**: Python 3.11 (repo venv; `./venv/bin/python`)

**Primary Dependencies**: stdlib only — **no new dependencies**. The helper
reuses `trade/regime.py`'s existing `requests`-based `_fetch_ohlcv` path
indirectly through `get_atr_pct`.

**Storage**: SQLite `data/trading.db` — **one additive migration**
(`db/migrations/009_entry_confirmation.sql`, `ALTER TABLE signals ADD COLUMN
entry_confirmed INTEGER`) plus the matching column in `db/schema_v2.sql` for
fresh databases. This dual update is the established convention (migration 005
added `signals.tp_price`/`sl_price` and `schema_v2.sql` lines 175–176 carry
them). Idempotent on re-run: `_run_migrations` (`db/db_ops.py` line 41) wraps
each script in `try/except: pass`, so the duplicate-column error on the second
run is swallowed — same mechanism migration 008 relies on.

**Testing**: pytest — `./venv/bin/python -m pytest tests/test_entry_confirmation.py --basetemp=.pytest_tmp -q`
(plus the full `tests/` suite for regressions; 76 tests green before this feature)

**Target Platform**: Linux server (bot.py + telegram.py under forever.py, Docker
image via GitHub Actions → Watchtower; DB synced with `push-db.sh`)

**Project Type**: trading bot, single-repo Python (no framework)

**Performance Goals**: zero additional market-data requests per asset per cycle
in the live configuration (`adaptive_enabled=true`); one dict lookup and one
float comparison per entry attempt.

**Constraints**: Constitution VII line budget (already exceeded — see the
Constitution Check); settings read fresh each cycle; minimum modification; no
import from `trade/main.py`/`trade/signal_agent/`; `dry_run` untouched;
structured single-line logs, skips at DEBUG.

**Scale/Scope**: 2 venues × ≤ ~20 universe members; 14 acceptance criteria;
6 source files + 2 docs + 1 new test file.

## Constitution Check

*GATE: evaluated before design and re-checked after (below). Constitution
**v1.1.0**, amended 2026-08-15.*

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy (mean reversion) | ✅ | No new signal source. The rule is a **sign test on the last completed 5m return** (clarify Q1), not candlestick pattern detection — no multi-bar shapes, no pattern taxonomy, no lookup table. It serves the second half of Principle I's own question: displacement answers "at an extreme", this answers "reverting yet". No ML/LLM in the hot path. |
| **II** Reward Must Exceed Cost (NON-NEGOTIABLE, v1.1.0) | ✅ | Untouched. The cost gate (`te > venue_fee + slippage + min_net_edge`, `spot_scalper.py` line 249 / `futures_scalper.py` line 146) is evaluated **before** this filter and is not modified. This feature changes entry *timing*, never the TP/SL pair. |
| **III** No Leveraged Position Without a Confirmed Stop (NON-NEGOTIABLE) | ✅ | Entry-side only. Bracket construction, SL verification and the emergency-close path in `executor.py` are untouched. A blocked entry places no order at all. |
| **IV** Unknown State = No Trading (NON-NEGOTIABLE) | ✅ | `None` (cache miss, fetch failure, <2 bars) is **never** treated as confirmation: in enforce mode it skips the entry (fail closed); in observe mode it is recorded as `NULL` and blocks nothing (mode is a no-op by definition). |
| **V** Real Fills Only | ✅ | No PnL, fee, or fill path touched. `entry_confirmed` is signal metadata, not a trade number. |
| **VI** Restart Safety | ✅ | Stateless. `_candle_cache` is a rebuildable read-through cache; no new persistent state; reconciliation untouched. |
| **VII** Simplicity Is a Constraint | ⚠️ **Justified overrun** | The 1,500-line hot-path budget is **already exceeded at 2,442 lines** before this feature (`bot.py` 776, `executor.py` 623, `spot_scalper.py` 310, `regime.py` 263, `pnl.py` 262, `futures_scalper.py` 208). 009 adds **≈25 lines** (helper ~13, two call sites ~5 each, two `_log` signatures/INSERTs ~2). Per VII's "explicit justification" clause: the overrun is pre-existing and not caused here; this feature adds no module, no class, no import from `trade/main.py`/`trade/signal_agent/` (neither exists), and reuses the existing cache rather than adding a parallel fetch path. **Recommendation recorded for converge**: a dedicated spec should either re-baseline the VII budget against reality or split `executor.py`. |
| **VIII** The Bot Trades | ✅ **by default**, ⚠️ **when enforced** | Default `false` ⇒ zero frequency change. Enforcement keeps 36% of entries (~11/day → ~4/day) — a large cut that VIII takes seriously. Mitigations: (a) opt-in only, on the operator's own recorded data; (b) throughput is not slot-limited — 42-min average hold ⇒ ~34 trades/day/slot theoretical against 6.5 actual, i.e. **19% of slot capacity**, with zero `max_slots_cex`/`max_concurrent_positions` skips in the last three days; (c) daily return on slot capital rises **+0.19%/day → +1.59%/day** on the study sample; (d) every block is recorded in `signals` with reason `entry_not_confirmed`, so strictness stays measurable. Frequency recovery (`max_concurrent_positions`, `max_active_pairs`, `universe_size`) is a follow-up spec, out of scope here. |

**Post-design re-check**: the design below satisfies IV (`None` fails closed in
the only mode that can block), III/V (no order or PnL path touched), I (sign
test, not pattern detection), and VIII (default off + measurability). The VII
overrun is pre-existing, quantified, and justified above. No new gate violation.

## Project Structure

### Documentation (this feature)

```text
.specify/specs/009-entry-confirmation-candle/
├── spec.md              # authoritative spec (Clarified, Q1–Q6 resolved)
├── plan.md              # this file
└── tasks.md             # Phase 2 — NOT created by this plan
```

*(No `research.md`/`data-model.md`/`contracts/` for this feature: the spec's
Clarifications already carry the decisions, the data model is one nullable
column, and there is no cross-module contract — the helper has a single
signature consumed by two call sites. The 008 layout is not copied where it
would be empty ceremony.)*

### Source Code (repository root)

```text
trade/regime.py                # +last_closed_return_up (~13 lines) after
                               #   get_atr_pct (def line 215) / _compute_atr_pct
                               #   (line 244). _candle_cache (line 208),
                               #   _candle_cache_key (211) reused as-is.
trading_bot/spot_scalper.py    # import +last_closed_return_up (line 19);
                               #   confirmation block after the toxicity check
                               #   (line 267) and before _cooldown_ok (line 269);
                               #   _log (def 299) +ec param & INSERT column
trading_bot/futures_scalper.py # same, with direction symmetry (long/short):
                               #   import line 20, block after line 164,
                               #   before line 166; _log def 199
trade/settings_schema.py       # +1 SettingSpec ("entry_confirm_candle", bool,
                               #   group "entry") after adaptive_enabled (line 59)
db/migrations/009_entry_confirmation.sql   # NEW — ALTER TABLE signals ADD COLUMN
db/schema_v2.sql               # signals table (line 148): +entry_confirmed
                               #   (fresh-DB parity, migration-005 convention)
tests/test_entry_confirmation.py           # NEW — AC1–AC8, AC10, AC11
docs/CURRENT_STATE.md          # new "## 0." feature-009 section
docs/CHANGELOG.md              # +feat: entry (2026-08-15)
```

**Structure Decision**: single-project repo, no new module. The helper lives in
`trade/regime.py` because that module already owns the 5m candle cache — putting
it anywhere else would either duplicate the cache or create a new import edge.

## Research Summary

All line numbers read from source at plan time:

- **`trade/regime.py`**: `_candle_cache` dict at **line 208**
  (`key -> (ts, candles)`); `_candle_cache_key(asset, venue, interval)` at
  **211**; `get_atr_pct` def at **215** — it checks the cache (222–224), fetches
  via `_fetch_ohlcv(fetch_venue, symbol_atr, "5m", period + 5)` and writes the
  cache at **233**, and on exception falls back to the stale cache (237–240).
  **Key finding**: `get_atr_pct` is the single owner of cache population, so the
  new helper never needs its own fetch/error branch — it delegates.
- **Candle shape**: `_fetch_ohlcv` (**line 61**) returns dicts with
  `open/high/low/close/volume` (float), oldest → newest. Binance returns the
  **in-progress** bar last when no `endTime` is passed (which is the case at
  **line 232**), so the last *closed* bar is `candles[-2]` — confirmed against
  the live API during the 009 study.
- **`trading_bot/spot_scalper.py`**: `scalp_cycle` def **215**; cost gate
  (Constitution II v1.1) **249**; `tox_eval` **257**; `tbl` **259**; `direction`
  **261**; toxicity skip **267**; `_cooldown_ok` skip **269** — the insertion
  point is **between 267 and 269**; `_log` def **299** (16 params, INSERT of 26
  columns). `from trade.regime import get_atr_pct` at **19**.
- **`trading_bot/futures_scalper.py`**: same shape — `scalp_cycle` **115**, cost
  gate **146**, `tox_eval` **154**, `tbl` **156**, `direction` **158**
  (long/short), toxicity skip **164**, `_cooldown_ok` **166**, `_log` def
  **199**. Import at **20**.
- **`_log` callers outside the scalpers**: `bot.py::_record_gate_skip` and
  `_record_global_block` call `log(...)` with **14 positional args** and no
  keywords. A trailing parameter with a default is therefore safe — **no bot.py
  change needed** (verified: both call sites stop at `reason`).
- **`trade/settings_schema.py`**: `SettingSpec` dataclass **line 13**
  (`key, type, group, unit, hard_min, hard_max, soft_min, soft_max, short,
  depends_on=()`); the `entry` group runs **47–60**; `adaptive_enabled`
  (**59**) is the bool-with-no-ranges template. `trade/settings_rules.py`
  needs **no change** — a bool with no ranges passes the generic Amendment 002
  validator, same as `adaptive_enabled`/`market_gate_enabled`.
- **`db/db_ops.py::_run_migrations`** (**line 41**): iterates
  `sorted(os.listdir("db/migrations"))` and runs each script inside
  `try/except: pass` — the swallow *is* the idempotency mechanism for
  `ALTER TABLE` scripts (migration 008 relies on it for `DROP COLUMN`).
- **`db/schema_v2.sql`**: `signals` table at **line 148**; it already carries
  `tp_price`/`sl_price` (**175–176**) which were introduced by migration 005 —
  confirming the "migration **and** schema file" convention for additive columns.
- **Test fixture pattern**: `tests/test_spot_exit_hardening.py` (and
  `test_amendment003.py`) use a `db` fixture that monkeypatches
  `db.db_ops.DB_PATH` to a `tmp_path` file and calls
  `initialize_database_tables()` — which runs the migrations, so the new column
  exists in test DBs automatically.

## Pinned Mechanisms

### M1 — Helper reaches the cache through `get_atr_pct`, never its own fetch

```python
def last_closed_return_up(asset: str, venue: str) -> bool | None:
    key = _candle_cache_key(asset, venue, "5m")
    cached = _candle_cache.get(key)
    if cached is None or (time.time() - cached[0]) >= get_setting_int("candle_cache_sec", 60):
        get_atr_pct(asset, venue)          # sole cache owner; no duplicate fetch/error path
        cached = _candle_cache.get(key)
    if cached is None or len(cached[1]) < 2:
        return None                        # Constitution IV — indeterminate, never confirmed
    bar = cached[1][-2]                    # [-1] is the in-progress bar
    return bar["close"] > bar["open"]      # flat ⇒ False (clarify Q1)
```

**Why delegate rather than fetch**: `get_atr_pct` already owns fetch, cache
write, and the stale-cache fallback on API failure. Duplicating that would
create a second error-handling path that could diverge. Cost: in the live
configuration (`adaptive_enabled=true`, the current DB value) `get_atr_pct` has
already run for this asset earlier in the same `scalp_cycle` (line 231/134), so
the cache is warm and the delegation is a **dict hit — zero requests**. With
`adaptive_enabled=false` the delegation costs at most one 5m fetch per asset per
`candle_cache_sec`. **This refines spec AC9**: "zero additional API calls" holds
for the live configuration; the `adaptive_enabled=false` path is bounded by the
same cache TTL and is stated in the docs.

### M2 — Placement: after toxicity, before cooldown (clarify Q3)

Inserted between the `tbl` skip and the `_cooldown_ok` skip
(spot 267→269, futures 164→166). At that point `direction`, `tp_price` and
`sl_price` are all populated, so an `entry_not_confirmed` row is exactly as
informative as the neighbouring `toxicity`/`cooldown` rows, and the cheaper
checks (threshold, toxicity) still shed load first.

```python
    conf = last_closed_return_up(asset, venue)
    ec = None if conf is None else int(conf if direction == "long" else not conf)
    if get_setting_bool("entry_confirm_candle", False) and ec != 1:
        _log(..., "skipped", "entry_not_confirmed", tp=tp_price, sl=sl_price, ec=ec); return None
```

`ec` is the **direction-adjusted** verdict (clarify Q4: futures `short` is
confirmed by a **down** bar), so a single integer carries the recorded state and
the enforcement decision. Spot has no `short` branch but uses the identical
expression — `direction` is always `"long"` there, so the code is literally the
same in both files.

### M3 — `entry_confirmed` reaches the INSERT via a trailing default parameter

Both `_log` signatures gain `ec=None` **after** `sl=0.0`, and the INSERT gains
the `entry_confirmed` column + one placeholder. Verified safe: the only
non-scalper callers (`bot.py::_record_gate_skip`, `_record_global_block`) pass
14 positional arguments and never reach `tp`/`sl`/`ec`, so they keep writing
`NULL` — which is the correct value for a gate/global skip that never evaluated
confirmation. **No bot.py change in this feature.**

## Data Model

| Object | Definition |
|---|---|
| `signals.entry_confirmed` | `INTEGER NULL` — `1` confirmed, `0` not confirmed, `NULL` indeterminate or not evaluated (gate/global skips, pre-migration rows). Mirrors the nullable `tox_*` verdict columns. |
| `entry_confirm_candle` | `SettingSpec(bool, group "entry", default false)`, no unit, no ranges, no `depends_on`. Default lives in the `get_setting_bool(..., False)` fallback — no DB row required. |

No other schema, contract, or in-memory structure changes.

## Detailed Design

### Part 1 — `trade/regime.py`

Append `last_closed_return_up` after `_compute_atr_pct` (line 244+), exactly as
in M1. Requires `get_setting_int` — **already imported** at line 15
(`from db.db_ops import get_setting_float, get_setting_int`). Module docstring
gains one line noting the helper shares the ATR candle cache.

### Part 2 — `trading_bot/spot_scalper.py`

- Line 19 import → `from trade.regime import get_atr_pct, last_closed_return_up`.
- Insert the M2 block between lines 267 and 269.
- `_log` (299): signature `..., tp=0.0, sl=0.0, ec=None`; INSERT column list
  gains `entry_confirmed`, the `VALUES` tuple gains one `?` and `ec`.
- Every existing `_log` call in this file is unchanged (they omit `ec` ⇒ `NULL`),
  **except** the new `entry_not_confirmed` call and the two success paths
  (`"signaled"` at line 275 and `"entered"` at 289) which pass `ec=ec` so the
  A/B has both arms. *This is the observe-mode data path — without it the
  column would only ever record blocked entries.*

### Part 3 — `trading_bot/futures_scalper.py`

Identical, with the direction-adjusted `ec` covering `short`. Import line 20,
block between 164 and 166, `_log` def 199, `ec=ec` on the `"signaled"` (line
172) and `"entered"` (183) calls.

### Part 4 — `trade/settings_schema.py`

Insert after `adaptive_enabled` (line 59–60), closing the `entry` group:

```python
    SettingSpec("entry_confirm_candle", bool, "entry", None, None, None, None, None,
                "Require the last closed 5m return to confirm the reversal before entering (default false — observe-only: the verdict is recorded in signals.entry_confirmed but never blocks)"),
```

### Part 5 — Database

`db/migrations/009_entry_confirmation.sql`:

```sql
-- Migration 009: entry confirmation verdict on signals.
-- 1 = confirmed, 0 = not confirmed, NULL = indeterminate / not evaluated.
ALTER TABLE signals ADD COLUMN entry_confirmed INTEGER;
```

`db/schema_v2.sql` — add `entry_confirmed  INTEGER` to the `signals` table
(after `sl_price`, line 176) for fresh-database parity.

### Part 6 — Tests

New `tests/test_entry_confirmation.py` — see Testing Strategy.

### Part 7 — Docs

`docs/CURRENT_STATE.md` gains a `## 0.` feature-009 section; `docs/CHANGELOG.md`
gains a `feat:` entry.

## Edge Cases

| Edge case | Handling |
|---|---|
| `entry_confirm_candle` unset (existing DBs) | `get_setting_bool(..., False)` ⇒ observe-only; zero behaviour change (AC3). |
| Fewer than 2 cached bars | `None` ⇒ `ec=NULL`; observe records nothing, enforce skips (AC2, AC7). |
| API failure | `get_atr_pct` falls back to the stale cache; if that is empty too, `None` ⇒ fail closed in enforce mode. |
| Flat bar (`close == open`) | **Not confirmed** (`False`) — clarify Q1, fail closed. |
| `adaptive_enabled=false` | Helper's delegation populates the cache; ≤1 fetch per asset per `candle_cache_sec` (M1, documented). |
| Cache up to 60s stale | Accepted (clarify Q6); observe-mode measures live behaviour including this staleness. |
| Futures `short` | Confirmed by a **down** bar (`ec = int(not conf)`) — clarify Q4, evidence-free, unit-tested against synthetic series only. |
| `signal_only` mode | Confirmation applies identically — a signal that would not have been entered must not be advertised as one. The block sits before the `signal_only` return (spot 274, futures 171). |
| Gate / global-loss skips (`bot.py`) | 14-positional callers ⇒ `ec` defaults to `None` ⇒ `NULL`; correct (confirmation was never evaluated). |
| Migration re-run | Duplicate-column error swallowed by `_run_migrations`' `try/except` (AC11) — the mechanism migration 008 already relies on. |
| Pre-migration rows | Stay `NULL`; no backfill (AC11). |
| Operator toggles mid-run | Setting read fresh per entry attempt ⇒ effective without restart. |

## Out of Scope

- The TP/SL pair, `sl_k_spot`, thresholds, ATR, regime, toxicity, kill switches,
  market gate, broad-market filter, universe scanner, exits/stops, sizing.
- Frequency recovery (`max_concurrent_positions`, `cex_slot_pct`,
  `max_active_pairs`, `universe_size`) — follow-up spec.
- Loop latency / whole-exchange snapshot (bookTicker) — follow-up spec.
- `bot.py` (no change needed — M3), `trade/settings_rules.py` (bool, no ranges),
  `telegram.py`, `dashboard/*`, `db/db_ops.py`.
- Any new API call, thread, process, dependency, or report contract.

## Testing Strategy (14 acceptance criteria)

New `tests/test_entry_confirmation.py`, following the established fixture
pattern (`db.db_ops.DB_PATH` monkeypatched to `tmp_path` +
`initialize_database_tables()`; module state cleared in an autouse fixture;
network isolated with `mock.patch`).

| Test | Verifies | AC |
|---|---|---|
| `test_helper_reads_last_closed_bar` | Seeds `_candle_cache` directly: up bar ⇒ `True`, down ⇒ `False`, **flat ⇒ `False`**; a deliberately opposite in-progress `[-1]` bar proves `[-2]` is the one read. | AC1 |
| `test_helper_indeterminate` | Empty cache with `get_atr_pct` patched to a no-op ⇒ `None`; 1-bar cache ⇒ `None`. | AC2 |
| `test_observe_mode_never_blocks` | `entry_confirm_candle` unset, cache seeded with a **down** bar, dip conditions met ⇒ `scalp_cycle` still enters (fake exchange), no `entry_not_confirmed` row. | AC3 |
| `test_observe_mode_records` | After an entered/signaled cycle, the `signals` row carries `entry_confirmed` ∈ {0,1} matching the seeded bar direction. | AC4 |
| `test_enforce_blocks_unconfirmed` | `entry_confirm_candle=true` + down bar ⇒ returns `None`, a `skipped`/`entry_not_confirmed` row exists, and the fake exchange received **no** `place_entry` call. | AC5 |
| `test_enforce_passes_confirmed` | Same with an up bar ⇒ entry proceeds, `entry_confirmed=1`. | AC6 |
| `test_enforce_none_fails_closed` | Enforce + `last_closed_return_up` patched to `None` ⇒ skipped with `entry_not_confirmed`, `entry_confirmed` `NULL`. | AC7 |
| `test_futures_direction_symmetry` | Futures `long` needs up, `short` needs down — synthetic cache, both directions, enforce mode. Marked evidence-free (clarify Q4). | AC8 |
| `test_no_additional_api_calls` | `trade.regime._fetch_ohlcv` patched with a call counter; a warm cache ⇒ helper triggers **0** fetches. | AC9 |
| `test_setting_registered` | `BY_KEY["entry_confirm_candle"]` is bool/group `entry`; `validate(..., True/False).level == "ok"`; `get_setting_bool` default `False`. | AC10 |
| `test_migration_idempotent` | `initialize_database_tables()` twice ⇒ `entry_confirmed` present exactly once in `PRAGMA table_info(signals)`; a pre-seeded row keeps `NULL`. | AC11 |
| `test_ab_query` | The AC14 query (`GROUP BY entry_confirmed` over `signals`) runs and returns rows. | AC14 |

**Regression**: the full suite (76 tests) must stay green — in particular
`test_spot_exit_hardening.py` (shares `scalp_cycle`) and `test_market_check.py`
(its `_record_gate_skip` path exercises the modified `_log` signature).

Run: `./venv/bin/python -m pytest tests/test_entry_confirmation.py --basetemp=.pytest_tmp -q`
then `./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp -q`.

## Docs Update

- **`docs/CURRENT_STATE.md`** — new `## 0. Entry Confirmation Candle (feature
  009, 2026-08-15)` section (following the 006/007/008 `## 0.` convention): the
  rule as a sign test on the last closed 5m return, observe/enforce modes, the
  new setting and column, the cache-sharing note (including the
  `adaptive_enabled=false` caveat from M1), the measured evidence, and the
  Constitution VIII frequency trade-off with the A/B query.
- **`docs/CHANGELOG.md`** — `feat:` entry under a `## 2026-08-15` heading, also
  recording Constitution **v1.1.0** (Principle II amendment) as context.

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/009-entry-confirmation-candle/` | spec ✅, plan (this file), tasks (next phase) |
| `trade/regime.py` | +`last_closed_return_up` (~13 lines), docstring line |
| `trading_bot/spot_scalper.py` | import, confirmation block, `_log` `ec` param + INSERT, `ec=ec` on signaled/entered |
| `trading_bot/futures_scalper.py` | same + direction symmetry |
| `trade/settings_schema.py` | +1 `SettingSpec` (`entry_confirm_candle`) |
| `db/migrations/009_entry_confirmation.sql` | NEW — additive column |
| `db/schema_v2.sql` | `signals` +`entry_confirmed` (fresh-DB parity) |
| `tests/test_entry_confirmation.py` | NEW — 12 tests |
| `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md` | feature-009 section + `feat:` entry |
| `bot.py`, `trade/settings_rules.py`, `db/db_ops.py`, `telegram.py`, `dashboard/*` | **Unchanged** |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Helper reads the in-progress bar (look-ahead / noise) | Medium | High | `[-2]` pinned in M1 with an explicit test seeding an opposite `[-1]` bar (AC1); verified against the live API during the study. |
| `_log` signature change breaks bot.py's positional callers | Low | High | Trailing default parameter; both callers verified to pass exactly 14 positional args; `test_market_check.py` exercises `_record_gate_skip` as a regression guard. |
| Enforcement cuts frequency too far (Constitution VIII) | Medium | Medium | Default off; enablement is an operator decision on recorded data; slot capacity at 19% leaves headroom; recovery levers named in a follow-up spec. |
| Futures short rule wrong (no evidence) | Medium | Low *(DEX off)* | Flagged evidence-free in spec + plan; synthetic unit test only; `auto_trade_orderly=False` means no live exposure until DEX is armed. |
| Study does not generalise (n=41 confirmed arm, 10 days, one regime) | Medium | Medium | Observe-mode is precisely the mitigation — it upgrades the sample on live data before any enforcement decision (AC14). |
| `adaptive_enabled=false` adds an unexpected fetch | Low | Low | Bounded by `candle_cache_sec`; documented in M1 and CURRENT_STATE; AC9 scoped to the live configuration. |
| Migration fails silently on a real error | Low | Medium | `_run_migrations` swallows all exceptions (pre-existing repo issue, out of scope); `test_migration_idempotent` asserts the column actually exists after init. |
| Line budget (Constitution VII) | — | — | Pre-existing 2,442-line overrun quantified and justified in the Constitution Check; +25 lines; re-baselining recommended to converge. |
