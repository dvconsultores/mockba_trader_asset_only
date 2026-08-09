# Tasks: Market Conditions Check & Auto-Gate (005)

**Input**: Design documents from `/specs/005-market-conditions-gate/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/market-report.md ✅, quickstart.md ✅, constitution.md ✅

**Tests**: New `tests/test_market_check.py` (REQUIRED — spec lists tests in scope; mirrors `tests/test_amendment003.py`).

**Organization**: Tasks are grouped by implementation part in dependency order: shared core (Part 1) → settings → auto gate (Part 2) → Telegram (Part 3) → tests → docs. The pure core MUST complete before its two consumers; tests come after their subjects; docs last. (spec.md has no user stories — it is organized by parts, so no story labels, matching `003`/`004` repo format.)

**Branch**: work directly on `develop` (repo convention — no feature branches).

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- Include exact file paths in descriptions

## Path Conventions

- Shared check: `trade/market_check.py` (new)
- Trading bot: `bot.py`
- Telegram bot: `telegram.py`
- Settings: `trade/settings_schema.py`; `trade/settings_rules.py` (NO change)
- Tests: `tests/test_market_check.py` (new)
- Docs: `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`
- Run tests: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`

---

## Phase 1: Shared Core — `trade/market_check.py` (+ settings) — blocks both consumers

**Purpose**: one source of truth verdict core + both modes, per `contracts/market-report.md`. Never imports from `bot.py`/`telegram.py`/`trade/main.py`/`trade/signal_agent/` (Constitution VII). No network calls except live mode (token-bucket bounded). Reuses ONLY the verified shared functions from research.md §1 (non-divergence, AC1).

- [X] T001 Create `trade/market_check.py` — pure `_evaluate(venue, mode, scan_fresh, scan_age_hours, asset_facts, regime_mix, settings)` verdict core implementing `data-model.md` §2 rules in order: `scan_fresh == False` → **FAIL** `scan_stale`; `fail_share >= market_gate_fail_share` → **FAIL** `liquidity_fail_share=<n>`; `fail_share > 0` → **WARN** `liquidity_partial=<n>` (floor); `trend_share >= market_gate_trend_share` → PASS→WARN `regime_trending=<n>`; `unknown_share >= market_gate_unknown_share` → PASS→WARN `regime_unknown=<n>`; else **PASS**. Deterministic, no I/O, no formatting. Carries the non-gating `thresholds` diagnostic via `trade.universe.compute_thresholds` from the median stored ATR (AC1 call site — never gates).
  - *Verify*: synthetic fact matrix produces the verdict per data-model §2; no module-level side effects; no thresholds/regime logic reimplemented.

- [X] T002 [P] Register the 7 `market_gate_*` settings in `trade/settings_schema.py` — new `"gate"` group, one `SettingSpec` each per `data-model.md` §4: `market_gate_enabled` (bool), `market_gate_interval_min` (int, hard 1–1440, soft 2–60), `market_gate_bad_streak` (int, hard 1–100, soft 1–20), `market_gate_good_streak` (int, hard 1–100, soft 1–20), `market_gate_fail_share` (float, hard 0.0–1.0, soft 0.25–0.75), `market_gate_trend_share` (float, hard 0.0–1.0, soft 0.3–0.8), `market_gate_unknown_share` (float, hard 0.0–1.0, soft 0.2–0.7). Append to `ALL` only (`BY_KEY`/`GROUPS` auto-derive). Do NOT edit `trade/settings_rules.py` — schema `hard_min` enforces `interval/streak >= 1`; confirm the Amendment 002 validator (`trade.settings_rules.validate_all`) still passes unchanged.
  - *Verify*: quickstart Scenario 1 — 7 keys in `BY_KEY`; `validate('market_gate_interval_min', 0)` → error; `validate('market_gate_bad_streak', 1)` → ok; `validate_all()` passes with no `settings_rules.py` change.

- [X] T003 Create `_asset_facts_from_scan(venue, universe, slot)` in `trade/market_check.py` — per-asset liquidity fields from stored universe rows (no I/O): `volume_ok` (quote_volume_24h >= `universe_min_volume_usd`), `depth_ok` (stored depth both sides >= `universe_depth_slot_multiple` × slot), `spread_ok` (stored spread_pct <= `universe_spread_ratio_max` × `tp_min_pct`). `None`/missing volume/depth/spread ⇒ `*_ok = False` (fail closed, Constitution IV).
  - *Verify*: synthetic rows with NULL fields → all `*_ok=False`; no DB or network calls.

- [X] T004 Create `_asset_facts_observed(venue, universe, slot, observations, window_sec)` in `trade/market_check.py` — consumes the rolling observation deques (`data-model.md` §5): latest regime + latest non-`None` spread per asset within `ts >= now - window_sec`; `live_spread_degraded` = live > scan_spread × `universe_spread_degradation_multiple`, `None` when no observation in window (indeterminate, never good). **Zero network calls** (AC9).
  - *Verify*: spy on `_fetch_binance_*`/`_fetch_depth` → none invoked (AC9).

- [X] T005 Create `_asset_facts_live(venue, universe, slot, bucket)` in `trade/market_check.py` — live whole-exchange snapshot: `_fetch_binance_book_ticker()` + `_fetch_binance_24hr()` + `_fetch_binance_exchange_info()`, then `_fetch_depth(venue, symbol)` per universe member through `_TokenBucket(capacity=max(1, len(universe)), refill_per_sec=60.0)` (scanner's own class/rate). Whole-exchange call failure ⇒ wrapper verdict **FAIL** `data_unavailable` (never partial); per-asset depth `None` ⇒ `depth_ok=False`. Reuses scanner functions only (AC1).
  - *Verify*: patched `_fetch_*` spies invoked; bucket refusal leaves `depth_ok=False`; no reimplemented fetch/depth logic.

- [X] T006 Create `_ensure_fresh_scan(venue, runner=None)` in `trade/market_check.py` — if `trade.universe.is_universe_stale(venue)`: invoke runner (default `run_scans_if_due(venues=(venue,))`; `force_rescan(venue)` for the manual path), re-check; return `(scan_fresh, scan_age_hours)` via `get_universe_scan_age`; still stale → fail closed (AC3, Constitution IV — never judges on stale data).
  - *Verify*: stale scan + patched refresh → not FAIL; patched no-op refresh → FAIL `scan_stale`.

- [X] T007 Create `check_venue_live(venue)` and `check_venue_observed(venue, observations, equity=None)` in `trade/market_check.py` — freshness gate first (`_ensure_fresh_scan`), then mode facts (`_asset_facts_live` / `_asset_facts_observed`), then `_evaluate`; slot via `trade.pnl.compute_slot_size(venue, equity, 0.0)` (observed equity from `get_venue_equity(venue)`, fallback `0.0`). Both return the IDENTICAL structured report dict per `contracts/market-report.md` (venue, mode, timestamp, scan_fresh, scan_age_hours, verdict, reasons, regime_mix, assets{...}, thresholds) — AC2.
  - *Verify*: both wrappers return dicts with identical keys/types for equivalent inputs; only `mode` and fact-freshness differ.

- [X] T008 Create `update_gate_state(state, verdict, settings)` in `trade/market_check.py` — pure debounce state machine per `data-model.md` §3: `PASS` → `good_streak += 1`, `bad_streak = 0`, resume when `suspended` and `good_streak >= market_gate_good_streak`; `FAIL` → `bad_streak += 1`, `good_streak = 0`, suspend when not suspended and `bad_streak >= market_gate_bad_streak`; `WARN` → both streaks = 0 (neutral hold — never suspends, never resumes, prevents flapping). Returns `(new_state, transition)` with `None | {"type": "suspend"} | {"type": "resume"}` (AC6).
  - *Verify*: streak sequences suspend only at `bad_streak`, resume only at `good_streak`; WARN holds and resets both streaks.

- [X] T009 Create `format_report(report)` in `trade/market_check.py` — compact per-venue text per `contracts/market-report.md` renderer contract: `Verdict:` token verbatim (PASS/WARN/FAIL not translated), scan age/freshness, regime-mix counts, liquidity pass count (`n/m assets pass`), threshold diagnostics line; no emoji in structural text; fits ≤ `TELEGRAM_MAX_MESSAGE_LEN = 4096` per venue (AC10). Static labels left for `telegram.py` `translate()`.
  - *Verify*: output contains the verdict token + counts; length ≤ 4096 for a large synthetic report.

**Checkpoint**: shared core done — both consumers (`bot.py`, `telegram.py`) and the tests can build on `trade/market_check.py`.

---

## Phase 2: Automatic Gate — `bot.py` (observed mode)

**Purpose**: periodic evaluation every `market_gate_interval_min` in the existing main loop (mirrors the periodic mode-log block at `bot.py` ~line 230), per-venue debounce, entries-only blocking, ONE transition notification. No thread/process. Zero behavior change when disabled (AC5).

- [X] T010 Add module-level in-memory gate state in `bot.py` — `_gate_state: dict[str, dict]` (per venue `{"suspended", "bad_streak", "good_streak"}`), `_last_gate_eval: dict[str, float]`, `_gate_observations: dict[str, dict[str, deque]]` (deque maxlen=40, `data-model.md` §5), `_gate_disabled_logged = False`. No DB writes, no thread.
  - *Verify*: bot starts with empty state; no new imports beyond stdlib `collections.deque`.

- [X] T011 Record observations at the two existing call sites in `bot.py` — after `regime = detect_regime(asset, venue)` (line ~323) append `{"ts": ..., "regime": regime}`; after `obi, spread = _get_obi_and_spread(asset, venue)` (line ~362) append `{"ts": ..., "spread": spread, "obi": obi}` to `_gate_observations[venue][asset]`. Zero extra API load (AC9); regime observations always flow (even when entries blocked).
  - *Verify*: no new network calls; deques accumulate per cycle; gate disabled ⇒ no behavioral change.

- [X] T012 Add the periodic gate block in `bot.py` directly after the periodic mode-log block (`if time.time() - _last_mode_log > 300:` at ~line 230) — same `time.time() - _last_gate_eval.get(venue, 0.0) > interval` pattern; skip the whole block when `get_setting_bool("market_gate_enabled", False)` is false (single startup "disabled" INFO log, then silent); skip venues whose `_normalize_venue_mode(get_setting("auto_trade_orderly"|"auto_trade_binance")) == "False"`; read `market_gate_interval_min` fresh each evaluation; call `check_venue_observed(venue, _gate_observations[venue], equity=...)` (equity from the already-cached `get_venue_equity`, fallback `0.0`); feed `update_gate_state`; persist `_gate_state[venue]` / `_last_gate_eval[venue]`. Settings read fresh each cycle (Telegram/UI changes take effect without restart) (AC5, AC11).
  - *Verify*: disabled ⇒ no state writes, no blocking, no notifications (AC5); enabled ⇒ per-venue cadence respected; quickstart Scenario 3 setup (`market_gate_interval_min=1`) works.

- [X] T013 Add transition notifications + `[GATE]` logs in `bot.py` — when `update_gate_state` returns a transition: `suspend` → exactly ONE `send_message(...)` (via `from trading_bot.send_bot_message import send_message`, same mechanism `_notify_entry` uses at `bot.py` ~line 528) e.g. `"[GATE] DEX (orderly) suspended — poor market conditions"`; `resume` → exactly ONE `"[GATE] DEX (orderly) recovered — market conditions normal"`. No per-check messages (AC8). Structured single-line INFO logs `[GATE] venue=... verdict=... reason=... action=suspend|resume|hold`, no emoji.
  - *Verify*: patch `send_message` → exactly one call on suspend, one on resume, zero on intermediate evals (AC8).

- [X] T014 Add the entries-only blocking guard in `bot.py` per-asset loop — placed AFTER observation recording and AFTER exit management (`spot_manage`/`futures_manage` run first at ~line 330), AFTER the spread-degradation guard (~line 380), just before `if obi is not None and price is not None:` (~line 383): `if _gate_state.get(venue, {}).get("suspended"): logger.debug(f"[SKIP] {venue}:{asset} market gate suspended"); continue`. Blocks NEW entries only — exits already ran earlier in the same iteration (Constitution III, AC7). Placing it after `_get_obi_and_spread` keeps spread observations flowing during suspension (no resume deadlock, research.md §3). Additive layer — does NOT replace the stale-universe guard, per-asset regime gating, spread-degradation guard, or kill switches.
  - *Verify*: suspended venue → no `spot_cycle`/`futures_cycle` call, but `spot_manage`/`futures_manage` still run (AC7); resume possible because observations keep flowing.

**Checkpoint**: automatic gate functional end-to-end — quickstart Scenario 3 reproducible in dry-run.

---

## Phase 3: Manual Telegram Report — `telegram.py` (live mode)

**Purpose**: `/market` command + `/list` button running the shared check in live mode; the operator's escape hatch / override view (same verdict shape, operator decides). Independent of Phase 2 (different file).

- [X] T015 Add `command_market(m)` in `telegram.py` — `@bot.message_handler(commands=['market'])`; private-chat guard + `TELEGRAM_CHAT_ID` authorization (same as `command_list`, ~line 57); run `check_venue_live("binance")` and `check_venue_live("orderly")` (fresh snapshot; `_ensure_fresh_scan` first); render each via `format_report(report)`, apply `translate()` to static labels (verdict tokens verbatim); concatenate and chunk at `TELEGRAM_MAX_MESSAGE_LEN = 4096` (AC10).
  - *Verify*: `/market` in the authorized chat returns compact per-venue verdicts ≤ 4096 chars; unauthorized chat → `"🔍 Not authorized"`; quickstart Scenario 2.

- [X] T016 Add the "Market check" button in `telegram.py` — second `InlineKeyboardButton` with `callback_data="market"` in the `command_list` markup (~line 66); route `"market": command_market` in the `_dispatch_callback` `options` dict (~line 82–85) so the existing `func(call.message)` dispatch calls it (AC10).
  - *Verify*: pressing the button in `/list` renders the same report as `/market`; existing "Open Mini App" button unchanged.

**Checkpoint**: manual report works — quickstart Scenario 2 passes.

---

## Phase 4: Tests — `tests/test_market_check.py`

**Purpose**: cover the 13 acceptance criteria; mirror `tests/test_amendment003.py` (tmp-DB fixture via `db.db_ops.DB_PATH` monkeypatch; network isolation via `mock.patch.object` on `trade.universe`/`trade.regime`).

- [X] T017 Create `tests/test_market_check.py` — core verdict tests (AC1–AC4, AC9, AC11): `test_check_uses_shared_functions` (spy on `compute_thresholds`/`detect_regime`/`compute_slot_size`/`_fetch_depth`; observe call sites in both modes — AC1); `test_live_and_observed_same_contract` (identical synthetic universe/facts → shared keys/values equal, `mode` differs — AC2); `test_stale_scan_triggers_refresh` + `test_stale_unrefreshed_fails` (stale `scanned_at`; patch `run_scans_if_due` to refresh ⇒ not FAIL; patch to leave stale ⇒ FAIL `scan_stale` — AC3); `test_stale_while_kill_switch_paused_fails` (patch `run_scans_if_due` to a no-op as under the consecutive-losses kill-switch pause ⇒ FAIL `scan_stale`, never PASS on stale data — AC3/Constitution IV); `test_verdict_correctness` (synthetic per-asset matrix across volume/depth/spread/degradation/regime shares/scan age ⇒ expected PASS/WARN/FAIL + reasons — AC4); `test_observed_mode_no_api_load` (spy `_fetch_binance_book_ticker`/`_24hr`/`_exchange_info`/`_fetch_depth`; none called — AC9); `test_not_near_zero_trade` (default settings + healthy RANGE universe ⇒ PASS; deliberately poor universe ⇒ FAIL — AC11).
  - *Verify*: `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q` — all pass.

- [X] T018 Extend `tests/test_market_check.py` — gate/state/renderer/settings tests (AC5–AC8, AC10, AC12): `test_disabled_default_no_behavior_change` (enabled unset/false ⇒ gate block skipped, empty `_gate_state`, no notifications/blocking — AC5); `test_debounce_transitions` (feed verdict sequences to `update_gate_state`; suspend only after `bad_streak`, resume only after `good_streak`; WARN holds and resets both streaks — AC6); `test_entries_only_never_exits` (gate's only loop effect is the entry-block boolean; exits run before the guard — structural + ordering — AC7); `test_transition_notifications_once` (patch `send_message`; exactly one on suspend, one on resume, zero intermediate — AC8); `test_manual_report_compact` (`format_report` ≤ 4096, contains verdict tokens + counts — AC10); `test_settings_validation` (7 `market_gate_*` keys; valid/invalid values; hard-range violations rejected with clear messages; `trade.settings_rules.validate_all` passes — AC12); `test_observations_flow_during_suspension` (while suspended, `_get_obi_and_spread` still runs and `_gate_observations` deques keep accumulating — resume-deadlock regression; AC6/AC7).
  - *Verify*: full suite green — `./venv/bin/python -m pytest tests/test_market_check.py --basetemp=.pytest_tmp -q`; quickstart Scenario 5.

**Checkpoint**: AC1–AC12 verified by unit tests; AC13 (docs) verified in Phase 5.

---

## Phase 5: Docs (AC13)

- [X] T019 [P] Update `docs/CURRENT_STATE.md` — describe `trade/market_check.py` (two modes, one verdict contract, freshness-first, non-divergence rule), the automatic gate (7 `market_gate_*` settings table with defaults/ranges from `data-model.md` §4, per-venue debounce state machine, entries-only blocking, transition notifications, in-memory state), and the `/market` command + `/list` button (live snapshot, 4096 limit, `TELEGRAM_CHAT_ID` auth).
  - *Verify*: a reader can reproduce the gate's behavior and the `/market` report from the doc alone (AC13).

- [X] T020 [P] Add a `feat:` entry to `docs/CHANGELOG.md` — dated 2026-08-09, "Market conditions check & auto-gate (005)": the three parts, the seven settings, `tests/test_market_check.py`; per the `how-to-work-with-specs.md` convention (AC13).
  - *Verify*: entry at the top of the changelog, `feat:` prefix, references feature 005.

**Checkpoint**: AC13 satisfied — feature complete. Run quickstart.md Scenarios 1–5 before convergence.

---

## ⛔ Out of Scope — MUST NOT Be Implemented

Explicitly excluded by spec.md (Q8 + Scope) and plan.md (Out of Scope). Do NOT create tasks or code for these:

- **PUMP closed-trade #65 fee anomaly** (43% fee, −44% PnL at entry≈exit) — a SEPARATE execution/fill investigation; follow-up only. No changes to `trading_bot/executor.py`, `trading_bot/spot_scalper.py`, `trading_bot/futures_scalper.py`.
- **No DB schema change / migration** — `db/schema_v2.sql`, `db/migrations/`, `db/db_ops.py` remain untouched; new settings defaults live in `get_setting_*` fallbacks.
- **No new process/thread/external service** for the gate — it lives in the existing main loop.
- **No change to existing per-asset guards** — regime gating, spread-degradation guard, stale-universe block, kill switches (daily-loss, consecutive-loss), and `dry_run` behavior all stay as-is.
- **`trade/settings_rules.py`** — NO change needed (schema `hard_min` covers `streak/interval >= 1`); verify the Amendment 002 validator still passes but do NOT edit.
- **No imports from `trade/main.py` / `trade/signal_agent/`** — these do not exist in this repo (Constitution VII).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Shared Core + Settings)**: no dependencies; T002 [P] runs in parallel (different file)
- **Phase 2 (Gate, bot.py)**: depends on Phase 1 (T001–T009) — consumes `check_venue_observed`/`update_gate_state`
- **Phase 3 (Telegram)**: depends on Phase 1 (T001–T009) — consumes `check_venue_live`/`format_report`; independent of Phase 2 (can run in parallel — different file)
- **Phase 4 (Tests)**: depends on Phase 1 + Phase 2/3 subjects (T001–T016)
- **Phase 5 (Docs)**: depends on all implementation phases

### Part Dependencies

- **Part 1 (core)**: none — MUST be complete first (blocks everything)
- **Part 2 (gate)**: Part 1 + settings (T002) — parallelizable with Part 3 (different files)
- **Part 3 (Telegram)**: Part 1 — parallelizable with Part 2 (different files)
- **Tests**: after all subjects (T001–T016)
- **Docs**: last

### Within Each Phase

- Core before wrappers: `_evaluate` (T001) → facts (`_asset_facts_*`, T003–T005) → freshness (T006) → wrappers (T007) → state machine (T008) → renderer (T009)
- Settings (T002) parallel with the core — different file
- bot.py tasks are sequential (same file): state (T010) → observations (T011) → gate block (T012) → notifications (T013) → entry guard (T014)
- Tests after their subjects; docs last

### Parallel Opportunities

- T002 (settings) ∥ T001/T003–T009 (core) — different files
- Phase 2 (`bot.py`) ∥ Phase 3 (`telegram.py`) — different files, both depend only on Phase 1
- T019 ∥ T020 (docs) — different files
- All gate/check behavior is testable without a live exchange (patch the network layer)

---

## Parallel Examples

```bash
# Part 1 + settings in parallel (different files)
Task: "T001, T003–T009 — trade/market_check.py"
Task: "T002 — trade/settings_schema.py"

# Gate + Telegram in parallel after Phase 1 (different files)
Task: "T010–T014 — automatic gate in bot.py"
Task: "T015–T016 — /market command + /list button in telegram.py"
```

---

## Implementation Strategy

### MVP First (Shared Core + Manual Report)

1. Phase 1 (shared core + settings) — T001–T009
2. Phase 3 (Telegram) — T015–T016
3. **STOP and VALIDATE**: `/market` live report works end-to-end (operator escape hatch available; gate stays disabled by default — zero behavior change)
4. Deploy/demo if ready

### Incremental Delivery

1. Phase 1 core → Phase 3 manual report (MVP: informational only)
2. Add Phase 2 gate (opt-in) → dry-run quickstart Scenario 3 → Phase 4 tests → Phase 5 docs
3. Enable `market_gate_enabled` only after observing real verdicts match operator judgment (escape hatch validates the gate)

### Parallel Team Strategy

1. Person A: Phase 1 core (T001, T003–T009)
2. Person B: T002 settings (parallel, quick)
3. Once Phase 1 done: Person A → Phase 2 (`bot.py`), Person B → Phase 3 (`telegram.py`)
4. Together: Phase 4 tests → Phase 5 docs

---

## Notes

- [P] tasks = different files, no dependencies
- No user stories in spec.md — organized by the spec's three parts + settings/tests/docs (matches `003`/`004` repo format)
- Verify tests fail before implementing (fixed test list in plan.md Testing Strategy)
- Commit after each task or logical group; work directly on `develop`
- Do NOT touch executor/scalpers/DB schema/PUMP #65 (see Out of Scope)
- Settings defaults live in `get_setting_*` fallbacks — no migration
- After implementation: run quickstart.md Scenarios 1–5
