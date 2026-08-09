# Plan: Market Conditions Check & Auto-Gate

**Feature**: 005-market-conditions-gate | **Date**: 2026-08-09 | **Spec**: `specs/005-market-conditions-gate/spec.md`
**Status**: Draft — awaiting implementation authorization
**Branch**: `005-market-conditions-gate` (per spec)

**Input**: Feature spec (Draft, Q1–Q10 resolved, 13 acceptance criteria) — this
plan does not re-litigate resolved decisions; it verifies them against the code
and fixes the design.

## Summary

One shared, structured per-venue market-health check (`trade/market_check.py`)
powering three things: (1) an **observed**-mode automatic gate in `bot.py` that
suspends **new entries only** after `market_gate_bad_streak` consecutive bad
evaluations and resumes after `market_gate_good_streak` consecutive good ones
(opt-in via `market_gate_enabled`, default false), (2) a **live**-mode manual
`/market` command + `/list` button in `telegram.py`, and (3) a compact Telegram
renderer. Both modes share one verdict contract (PASS/WARN/FAIL + reasons +
per-asset liquidity + regime mix + scan freshness) and reuse the live
threshold/depth/regime/sizing functions exclusively (non-divergence, enforced
by a patch test mirroring `tests/test_amendment003.py`). No executor, scalper,
or DB-schema changes; the PUMP #65 fee anomaly is explicitly out of scope.

## Technical Context

**Language/Version**: Python 3.11 (repo venv; `./venv/bin/python`)

**Primary Dependencies**: `requests`, stdlib `sqlite3`, `python-telegram-bot`
(telebot) — **no new dependencies**

**Storage**: SQLite `data/trading.db` — **no schema change / no migration**; new
settings defaults live in `get_setting_*` fallbacks (established Amendment 003
pattern). Gate state and observations are in-memory only.

**Testing**: pytest — `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp`

**Target Platform**: Linux server (bot.py + telegram.py, supervised by forever.py)

**Project Type**: trading bot, single-repo Python (no framework)

**Performance Goals**: gate evaluation adds no per-cycle latency beyond one
pure computation every `market_gate_interval_min`; observed mode issues **zero
new market-data calls**; live mode is bounded by a token bucket (≤ 1 depth call
per universe member).

**Constraints**: Constitution VII line budget (see note below); Telegram 4096
char limit; settings read fresh each cycle; minimum modification (disabled
default ⇒ byte-for-byte behavior unchanged).

**Scale/Scope**: 2 venues (binance, orderly) × ≤ ~50 universe members; 13
acceptance criteria.

## Constitution Check

*GATE: evaluated before Phase 0 research (above) and re-checked after Phase 1
design (below).*

| Principle | Compliance | How |
|---|---|---|
| **I** One Strategy (mean reversion) | ✅ | The gate is a venue-level "should this venue trade at all right now" filter; it adds no strategy logic, no ML/LLM, and never overrides the mean-reversion entry rule — per-asset regime/dip/pump logic is untouched. |
| **II** Reward Exceeds Risk (NON-NEGOTIABLE) | ✅ | Untouched. No change to `tp_min_pct`/`sl_min_pct`, net-edge validation, or startup gates. |
| **III** No Position Without a Stop (NON-NEGOTIABLE) | ✅ | Gate blocks **new entries only**. Exit management (`spot_manage`/`futures_manage`) runs **before** the gate check in the per-asset loop; open positions always run to TP/SL/time-stop/regime exit. |
| **IV** Unknown State = No Trading (NON-NEGOTIABLE) | ✅ | Fail closed end to end: stale/unrefreshed scan ⇒ verdict **FAIL**; missing stored data ⇒ per-asset check fails; no live-spread observation ⇒ `live_spread_degraded=None` (never good) and a venue with zero observations can never be PASS (no resume on indeterminate data); UNKNOWN regime assets never count toward a good verdict; `unknown_share >= market_gate_unknown_share` downgrades PASS→WARN. |
| **V** Real Fills Only | ✅ | Untouched. The gate reads no PnL and writes no trades. |
| **VI** Restart Safety | ✅ | Gate state is in-memory per spec (Q2); restart starts unsuspended and re-establishes within `bad_streak × interval`. Position/order reconciliation (`_reconcile_startup`) is untouched. |
| **VII** Simplicity Is a Constraint | ✅ (justified) | One new non-hot-path module; bot.py grows ~35–45 lines via existing patterns; verdict logic + state machine live outside the hot path. See line-budget note. |
| **VIII** The Bot Trades | ✅ | Opt-in (default false); default thresholds are lenient — a healthy RANGE venue evaluates **PASS** (AC11); WARN never suspends (neutral hold); skipped-entry reasons stay DEBUG + recorded in `signals` (existing path). |

**Post-design re-check**: the design above satisfies III (entry-block placed
after exit management and after observation recording), IV (freshness gate,
missing-data-fails-closed, no-resume-on-indeterminate), VIII (no near-zero-trade
defaults, lenient shares, WARN=hold). No gate violation.

## Project Structure

### Documentation (this feature)

```text
.specify/specs/005-market-conditions-gate/
├── spec.md              # authoritative feature spec (Draft, Q1–Q10 resolved)
├── plan.md              # this file
├── research.md          # verified signatures / call sites / line budgets
├── data-model.md        # report contract, state machine, settings
├── contracts/
│   └── market-report.md # shared verdict contract
├── quickstart.md        # validation scenarios
└── tasks.md             # Phase 2 — NOT created by this plan
```

### Source Code (repository root)

```text
trade/market_check.py          # NEW — shared check (live + observed), pure
                               #   _evaluate verdict core, update_gate_state,
                               #   format_report; ~250 lines
bot.py                        # +~35–45 lines — gate block (mode-log pattern),
                               #   observation recording, per-venue state dicts,
                               #   entry-block line
telegram.py                   # +~40 lines — /market handler, /list button,
                               #   callback routing
trade/settings_schema.py      # +7 SettingSpecs (new "gate" group)
tests/test_market_check.py    # NEW — ~14 tests (AC coverage)
docs/CURRENT_STATE.md         # +market check / gate / /market section
docs/CHANGELOG.md             # +feat: entry
```

**Structure Decision**: single-project repo; the check is a new leaf module in
`trade/` (no cross-module import from bot/telegram — consumers import it).
`trade/main.py` and `trade/signal_agent/` do not exist in this repo (deleted),
so the "never import from them" constraint is trivially satisfied.

## Research Summary

See `research.md` for the full code-verified inventory. Key findings:

- **Signatures verified** for every function the check must reuse
  (`compute_thresholds`, `detect_regime`, `_fetch_depth`,
  `_fetch_binance_book_ticker`/`_24hr`/`_exchange_info`, `run_scans_if_due`,
  `force_rescan`, `is_universe_stale`, `_TokenBucket`, `compute_slot_size`,
  `get_setting*`, `get_venue_equity`, `get_tradeable_universe`) — all live in
  the codebase with no wrapper needed.
- **Observed-mode fuel already exists**: the per-asset loop calls
  `detect_regime(asset, venue)` (always) and `_get_obi_and_spread(asset, venue)`
  (line 362) every cycle — the gate records those results, adding zero network
  calls.
- **Natural integration point**: the periodic mode-log block at the top of the
  `bot.py` loop (`if time.time() - _last_mode_log > 300:`) is the exact pattern
  the gate block mirrors.
- **`compute_thresholds` is not part of the gate verdict formulas** (the spec
  pins `spread_ok` to `universe_spread_ratio_max × tp_min_pct`, identical to the
  scanner's `_hard_filters_pass`). To satisfy AC1 (the check calls
  `compute_thresholds`), the check derives the venue's *effective* thresholds
  from the stored median ATR and carries them in a **non-gating diagnostic**
  `thresholds` field — the call site the patch test observes.
- **Slot size**: `compute_slot_size(venue, equity, 0.0)` is numerically
  identical to the scanner's private `_slot_size_for` (venue slot, no floor) —
  the AC1-mandated function with a documented input.
- **Freshness**: `run_scans_if_due` pauses under the consecutive-losses kill
  switch (fail closed) and `universe_max_age_hours > universe_scan_interval_hours`
  is validator-enforced, so `is_universe_stale ⇒ run_scans_if_due` always
  triggers a scan when the operator hasn't paused it.
- **No `settings_rules.py` change needed**: `hard_min` in the schema already
  enforces `interval/streak >= 1` (Amendment 002 validator passes unchanged).
- **Line budgets**: hot-path total is already 2,148 (justified historically in
  `.specify/plan.md`); `market_check.py` is a non-hot-path module (same standing
  as `trade/universe.py`).

## Data Model / Contracts

- **Report contract** (both modes identical): see `contracts/market-report.md`
  and `data-model.md` §1 — `venue, mode, timestamp, scan_fresh, scan_age_hours,
  verdict, reasons, regime_mix, assets{...}, thresholds` (diagnostic).
- **Verdict rules** (single source of truth): `data-model.md` §2 — freshness
  gate ⇒ FAIL; `fail_share` thresholds ⇒ FAIL/WARN; trend/unknown shares
  downgrade PASS→WARN only.
- **Gate state machine**: `data-model.md` §3 — `update_gate_state(state,
  verdict, settings)` pure function; PASS/FAIL/WARN transitions; WARN = neutral
  hold; in-memory per venue.
- **Settings**: `data-model.md` §4 — seven `market_gate_*` keys, new `"gate"`
  group, defaults documented, no migration.
- **Observations**: `data-model.md` §5 — rolling per-asset deque recorded at the
  two existing call sites.

## Detailed Design

### Part 1 — `trade/market_check.py` (NEW, shared core)

| Function | Role |
|---|---|
| `_evaluate(venue, mode, scan_fresh, scan_age_hours, asset_facts, regime_mix, settings)` | **Pure** verdict core — rules in `data-model.md` §2; no I/O; the testable heart (AC4). |
| `_asset_facts_from_scan(venue, universe, slot)` | Per-asset `volume_ok`/`depth_ok`/`spread_ok` from stored universe rows (shared by both modes; no I/O). |
| `_asset_facts_live(venue, universe, slot, bucket)` | Live whole-exchange snapshot + per-survivor depth, token-bucket rate-limited, reusing `_fetch_binance_*` + `_fetch_depth`; DEX via Binance proxy exactly as the scanner. |
| `_asset_facts_observed(venue, universe, slot, observations, window_sec)` | Consumes the rolling observation deques (latest regime, latest non-None spread per asset). **No network calls.** |
| `check_venue_live(venue)` | Freshness gate → live facts → `_evaluate` → report (`mode="live"`). |
| `check_venue_observed(venue, observations, equity=None)` | Freshness gate → observed facts → `_evaluate` → report (`mode="observed"`). |
| `update_gate_state(state, verdict, settings)` | Pure debounce state machine → `(new_state, transition)`. |
| `format_report(report)` | Compact per-venue text for the Telegram renderer (no emoji; static labels translated by telegram.py). |
| `_ensure_fresh_scan(venue, runner=None)` | If `is_universe_stale(venue)`: call `runner` (defaults to `run_scans_if_due(venues=(venue,))`); re-check; return fresh/age. Fail closed when still stale. |

- **Non-divergence (hard rule):** every market-data / threshold / regime /
  sizing call goes through the listed `trade.universe` / `trade.regime` /
  `trade.pnl` / `db.db_ops` functions. No reimplemented formulas.
- **Freshness first**: `_ensure_fresh_scan` runs before facts are gathered in
  both modes — the check never judges on stale data (spec Q5, AC3).
- **Live mode calls** (manual button, token-bucket bounded): whole-exchange
  `_fetch_binance_book_ticker()` + `_fetch_binance_24hr()` +
  `_fetch_binance_exchange_info()`, then `_fetch_depth(venue, symbol)` per
  universe member through a `_TokenBucket(capacity=max(1, len(universe)),
  refill_per_sec=60.0)` — the scanner's own class and refill rate. If any
  whole-exchange call fails ⇒ verdict FAIL (`data_unavailable`), never partial.
- **Observed mode**: no `_fetch_*` calls at all (AC9); `detect_regime` results
  come from the observations (the per-cycle calls already happened).
- **`thresholds` diagnostic**: `compute_thresholds(atr_median, dip_k,
  dip_min_pct, pump_k, pump_min_pct, tp_k, tp_min_pct, sl_k, sl_min_pct)` with
  `atr_median` = median of stored `atr_pct_median` across the universe — used
  for reporting only; never gates (AC1 patch test observes this call site).
- **Never imports** from `trade/main.py` or `trade/signal_agent/` (do not exist
  in this repo) — Constitution VII.

### Part 2 — `bot.py` automatic gate

**New module-level state** (in memory): `_gate_state: dict[str, dict]`,
`_last_gate_eval: dict[str, float]`, `_gate_observations: dict[str,
dict[str, deque]]`.

**1. Observation recording** (two lines at existing call sites, zero extra API):
- after `regime = detect_regime(asset, venue)` → append `{"ts", "regime"}`;
- after `obi, spread = _get_obi_and_spread(asset, venue)` → append
  `{"ts", "spread", "obi"}`.

**2. Periodic gate block** — placed directly after the existing periodic
mode-log block, same pattern (no thread, no process):

```python
# ── Periodic market-gate evaluation (every market_gate_interval_min) ──
if not get_setting_bool("market_gate_enabled", False):
    _gate_disabled_logged_once or logger.info("[GATE] disabled ...")  # once at startup
else:
    interval = get_setting_int("market_gate_interval_min", 5) * 60
    for venue, mode in (("binance", cex_mode), ("orderly", dex_mode)):
        if mode == "False":
            continue                      # venue not trading — skip
        if time.time() - _last_gate_eval.get(venue, 0.0) < interval:
            continue
        report = check_venue_observed(venue, _gate_observations[venue],
                                      equity=_cached_equity(venue))
        state = _gate_state.get(venue, _IDLE_STATE)
        new_state, transition = update_gate_state(state, report["verdict"], settings)
        if transition == "suspend":
            send_message(...)              # ONE debounced notification
            logger.info("[GATE] venue=... verdict=FAIL reason=... action=suspend")
        elif transition == "resume":
            send_message(...)              # ONE debounced notification
            logger.info("[GATE] venue=... verdict=PASS reason=... action=resume")
        else:
            logger.info("[GATE] venue=... verdict=... reason=... action=hold")
        _gate_state[venue] = new_state
        _last_gate_eval[venue] = time.time()
```

- Settings (interval, streaks, shares) are read **fresh** each evaluation.
- Disabled ⇒ the whole block is skipped; no per-cycle logs, no notifications,
  no entry blocking (AC5 — byte-for-byte unchanged).
- The venue's `_cached_equity` comes from `get_venue_equity(venue)` (already
  cached each cycle) — no API call; falls back to `0.0`.

**3. Entry blocking (entries only)** — one guard in the per-asset loop, placed
**after** observation recording and **after** exit management, just before the
scalper call:

```python
obi, spread = _get_obi_and_spread(asset, venue)
record_observation(...)                 # NEW (reuses existing call)
price = ...
... existing spread-degradation guard ...
if _gate_state.get(venue, {}).get("suspended"):   # NEW — blocks entries only
    logger.debug(f"[SKIP] {venue}:{asset} market gate suspended")
    continue
if obi is not None and price is not None:
    result = spot_cycle(...) / futures_cycle(...)
```

- Exit management (`spot_manage`/`futures_manage`) already ran earlier in the
  same iteration — Constitution III holds by construction (AC7).
- Placing the guard after observation recording prevents the resume deadlock
  (research.md §3): spread observations keep flowing during a suspension.
- The gate does **not** replace the stale-universe guard, per-asset regime
  gating, spread-degradation guard, or kill switches — it is an additional
  venue-level layer, checked alongside them.
- Notification text: plain, structured, no emoji, e.g.
  `[GATE] DEX (orderly) suspended — poor market conditions` /
  `[GATE] DEX (orderly) recovered — market conditions normal`, via
  `send_message` (the same mechanism `_notify_entry` uses).

### Part 3 — `telegram.py` `/market` command + `/list` button

- **New handler** `command_market(m)`:
  - private-chat + `TELEGRAM_CHAT_ID` auth (same guard as `command_list`);
  - runs `check_venue_live("binance")` and `check_venue_live("orderly")`
    (fresh snapshot; `_ensure_fresh_scan` first — telegram.py calls
    `run_scans_if_due(venues=(venue,))` with `equity_fn=None`, which uses the
    cached `venue_state` equity);
  - renders via `format_report(report)` per venue, applies `translate()` to the
    static labels, concatenates, and chunks at `TELEGRAM_MAX_MESSAGE_LEN = 4096`;
    verdict tokens (PASS/WARN/FAIL) are rendered verbatim.
- **`/list` button**: add a second `InlineKeyboardButton` (callback_data
  `"market"`) to the existing Mini App markup; route through `_dispatch_callback`
  by adding `"market": command_market` to the `options` dict (it already calls
  `func(call.message)`, and `command_market(m)` accepts a message).
- Synchronous execution matches every existing handler; the live check is
  bounded (token bucket) so polling stalls are brief and operator-initiated.
- **Escape hatch**: the manual report is the operator's override view against
  gate false positives (e.g., before a manual Sunday restart) — same verdict
  shape, operator decides.

### Part 4 — Settings, docs, tests

- `trade/settings_schema.py`: +7 `SettingSpec`s in a new `"gate"` group
  (table in `data-model.md` §4). `GROUPS`/`BY_KEY` derive automatically.
- `trade/settings_rules.py`: **no change** (hard ranges in the schema enforce
  `interval >= 1`, `streak >= 1`; no cross-check is strictly necessary).
- `tests/test_market_check.py`: see Testing Strategy.
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md`: see Docs Update.
- No `tasks.md` (later phase). No executor/scalper/DB changes.

## Edge Cases

| Edge case | Handling |
|---|---|
| Stale/due stored scan | `_ensure_fresh_scan` triggers `run_scans_if_due` (bot.py passes its exchanges via `equity_fn`; telegram.py relies on cached equity) before evaluating. Still stale (scan failed / kill-switch pause) ⇒ verdict **FAIL** `scan_stale` (Constitution IV). |
| UNKNOWN regime | Counted in `regime_mix`; never counts toward "good"; `unknown_share >= market_gate_unknown_share` downgrades PASS→WARN. Per-asset `regime="UNKNOWN"` is informational (liquidity unaffected). |
| Exchange API failure (live mode) | Whole-exchange call fails ⇒ FAIL `data_unavailable`; per-asset depth `None` ⇒ `depth_ok=False`. Never a partial "good". |
| No live-spread observations (observed) | `live_spread_degraded=None` (indeterminate); an asset without observations can still pass on stored checks, but a venue with **zero** spread observations can never be PASS (no resume on indeterminate) — combined with recording-before-blocking, resume is never deadlocked. |
| Missing stored data (NULL volume/depth/spread) | `*_ok = False` — missing data never passes. |
| Flapping | Debounce streaks (settings); WARN is a neutral hold that resets both streaks. |
| Near-zero-trade (Constitution VIII) | Defaults are lenient: healthy RANGE venue ⇒ PASS; WARN never suspends; gate is opt-in. |
| Gate disabled (default) | Entire block skipped: no blocking, no notifications, no per-eval logs; single startup "disabled" log only. Byte-for-byte unchanged (AC5). |
| Multiple venues independently | Per-venue state dicts, per-venue evaluation cadence, independent suspend/resume; a DEX suspension never affects CEX. |
| Restart | In-memory gate state resets; re-establishes within `bad_streak × interval`; positions/orders reconciliation untouched (Constitution VI). |
| Concurrent scan (scanner thread + gate) | Gate calls `run_scans_if_due` only when stale; benign race (SQLite serializes writes; `scan_venue` writes wholesale at the end). |
| `run_scans_if_due` paused by kill switch | Stays stale ⇒ FAIL; entries already blocked by the kill switch — fail closed. |

## Out of Scope

- **PUMP closed-trade #65 fee anomaly** (43% fee, −44% PnL at entry≈exit) — a
  **separate** execution/fill investigation, recorded as a follow-up; NOT part
  of this plan (spec Q8).
- No changes to `trading_bot/executor.py`, `spot_scalper.py`, `futures_scalper.py`
  (unless strictly required — none are).
- No DB schema change / migration (defaults live in `get_setting_*` fallbacks).
- No new process/thread/external service for the gate (lives in the main loop).
- No change to existing per-asset guards (regime gating, spread degradation,
  stale-universe block, kill switches, daily/consecutive loss).
- No `dry_run` behavior change and no gate bypass of any order path.

## Testing Strategy (13 acceptance criteria)

`tests/test_market_check.py` mirrors `tests/test_amendment003.py` (tmp-DB
fixture via `db.db_ops.DB_PATH` monkeypatch; network isolation via
`mock.patch.object`). Suggested tests:

| # | Test | Verifies AC |
|---|---|---|
| 1 | `test_check_uses_shared_functions` — spy on `compute_thresholds`/`detect_regime`/`compute_slot_size`/`_fetch_depth`; run both modes; assert each call site is observed | AC1 |
| 2 | `test_live_and_observed_same_contract` — identical synthetic universe/facts through both modes; assert shared keys/values equal, `mode` differs | AC2 |
| 3 | `test_stale_scan_triggers_refresh` + `test_stale_unrefreshed_fails` — stale `scanned_at`; patch `run_scans_if_due` to refresh ⇒ not FAIL; patch to leave stale ⇒ FAIL `scan_stale` | AC3 |
| 4 | `test_verdict_correctness` — synthetic per-asset facts across the matrix (volume, depth, spread, degradation multiple, regime shares, scan age) ⇒ expected PASS/WARN/FAIL + reasons | AC4 |
| 5 | `test_disabled_default_no_behavior_change` — `market_gate_enabled` unset/false ⇒ gate block skipped (no state writes, no notifications, no blocking; structural assertions on the enable check + empty `_gate_state`) | AC5 |
| 6 | `test_debounce_transitions` — feed verdict sequences to `update_gate_state`; suspend only after `bad_streak`, resume only after `good_streak`; WARN holds and resets streaks | AC6 |
| 7 | `test_entries_only_never_exits` — gate's only loop effect is the entry-block boolean; position-management functions are never invoked from gate code (structural + ordering assertion: exits run before the guard) | AC7 |
| 8 | `test_transition_notifications_once` — patch `send_message`; assert exactly one call on suspend, one on resume, zero on intermediate evals | AC8 |
| 9 | `test_observed_mode_no_api_load` — spy `_fetch_binance_book_ticker`/`_24hr`/`_exchange_info`/`_fetch_depth`; run `check_venue_observed`; assert none called | AC9 |
| 10 | `test_manual_report_compact` — `format_report` output ≤ 4096, contains verdict tokens + counts; handler-level auth documented (structural) | AC10 |
| 11 | `test_not_near_zero_trade` — default settings + healthy RANGE universe ⇒ PASS; deliberately poor universe ⇒ FAIL | AC11 |
| 12 | `test_settings_validation` — `validate("market_gate_*", v)` for valid/invalid values; hard-range violations rejected with clear messages; Amendment 002 `validate_all` passes | AC12 |
| 13 | (docs) verified via Docs Update checklist, not a unit test | AC13 |

Run: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`

## Docs Update

- **`docs/CURRENT_STATE.md`** — new subsection describing:
  - `trade/market_check.py` — shared check, two modes, one verdict contract,
    freshness-first behavior, non-divergence rule;
  - the automatic gate — settings, per-venue debounce state machine,
    entries-only blocking, transition notifications, in-memory state;
  - `/market` command + `/list` button — live-snapshot report, 4096 limit,
    `TELEGRAM_CHAT_ID` auth;
  - new `market_gate_*` settings table (defaults, ranges).
- **`docs/CHANGELOG.md`** — `feat:` entry (dated 2026-08-09): "Market
  conditions check & auto-gate (005)" summarizing the three parts, the seven
  settings, and the test file.

## File Manifest

| File | Action |
|---|---|
| `.specify/specs/005-market-conditions-gate/` | ✅ spec, plan, research, data-model, contracts, quickstart (this phase) |
| `trade/market_check.py` | New — shared check (live + observed), `_evaluate`, `update_gate_state`, `format_report` |
| `bot.py` | Gate block (mode-log pattern) + observation recording + per-venue state + entry-block guard |
| `telegram.py` | `/market` handler + `/list` button + callback routing |
| `trade/settings_schema.py` | 7 `SettingSpec`s, group `"gate"` |
| `trade/settings_rules.py` | No change (not strictly necessary) |
| `tests/test_market_check.py` | New — ~14 tests covering AC1–AC12 |
| `docs/CURRENT_STATE.md` | Updated |
| `docs/CHANGELOG.md` | Updated (`feat:`) |
| `db/*`, `trading_bot/*`, executor/scalpers | Unchanged |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Check diverges from live threshold/depth/regime logic | Low | High | Non-divergence is a hard rule; patch test observes every shared call site (AC1) |
| Observed-mode data starvation deadlocks resume | Medium | High | Observations recorded before the entry-block guard; WARN/zero-observation never resumes (fail closed, not deadlocked) |
| Stale scan judged as good | Low | High | Freshness gate runs `run_scans_if_due` first; still stale ⇒ FAIL (Constitution IV) |
| Gate becomes near-zero-trade config | Medium | Medium | Lenient defaults; healthy RANGE passes (AC11); WARN holds, never suspends; opt-in |
| bot.py line growth / hot-path budget | Low | Medium | ~35–45 lines via existing patterns; verdict + state machine live in non-hot-path `market_check.py` |
| Telegram report exceeds 4096 | Low | Medium | `format_report` compact + chunked; renderer test (AC10) |
| Concurrent scan race (scanner thread + gate) | Low | Low | SQLite serializes; `scan_venue` writes wholesale; benign |

## Line-Budget Note (Constitution VII)

- Hot-path modules currently total **2,148 lines** — already above the nominal
  1,500, with a documented historical justification in `.specify/plan.md`
  (executor complexity: two exchange APIs). This feature does **not** add to
  that overage meaningfully: bot.py grows ~35–45 lines (one periodic block
  mirroring the existing mode-log block, two observation-recording lines, one
  entry-guard line, state dicts).
- `trade/market_check.py` is a new **non-hot-path** module (evaluated every
  `market_gate_interval_min` and on demand; not part of the per-cycle hot path,
  not in the constitution's enumerated hot-path list — same standing as
  `trade/universe.py`). Keeping the verdict core and state machine there is the
  explicit design choice that keeps hot-path growth minimal.
