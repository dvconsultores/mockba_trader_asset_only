# Tasks: Amendment 001 — Adaptive Thresholds & Toxicity Observability (Remaining Work)

**Plan**: `.specify/plan.md` (Amendment 001 section)
**Spec**: `.specify/specs/amendment-001-adaptive-toxicity.md`
**Status**: Partial — spot scalper complete ✅, futures scalper pending 🔧

**Already completed**: schema v2, migration 002, `regime.py` (`get_atr_pct`), `toxicity.py`, `spot_scalper.py` rewrite.

---

## Phase 1: Setup (Complete)

All schema, migration, and foundational modules are deployed. No new setup tasks.

---

## Phase 2: User Story 4 — Futures scalper adopts the same adaptive + toxicity logic (Priority: P2)

**Goal**: Rewrite `trading_bot/futures_scalper.py` to match the spot scalper's Amendment 001 pipeline: adaptive thresholds via ATR, OBI logged but not gated, toxicity checks observe-only. Retain futures-specific behavior: bracket orders with SL, leverage, long+short, regime-gated direction matrix, venue="orderly".

**Independent Test**: Run both scalpers side-by-side with the same asset. Verify they compute identical adaptive thresholds and toxicity verdicts for the same price data. The only differences should be venue-specific (symbol derivation, order structure, fee rate, long+short support).

**Reference implementation**: `trading_bot/spot_scalper.py` (already rewritten with all Amendment 001 logic).

### 2.1 Imports and dependencies

- [X] T001 [US4] Update imports in `trading_bot/futures_scalper.py`: add `get_atr_pct` from `trade.regime`, add `evaluate as tox_eval` and `record_observation` from `trade.toxicity`, add `get_db_connection` from `db.db_ops`; remove `save_signal` and `can_trade_venue` (no longer used)

### 2.2 Detection functions — rename and add extreme_pct

- [X] T002 [US4] Rename `_is_price_dip` → `_is_dip` and `_is_price_pump` → `_is_pump` in `trading_bot/futures_scalper.py` (same logic, shorter names, match spot scalper convention)
- [X] T003 [US4] Add `_extreme_pct(a, p)` function in `trading_bot/futures_scalper.py` — computes signed extreme percentage (negative for dips, positive for pumps), identical to spot scalper

### 2.3 Entry logic — full rewrite of `scalp_cycle`

- [X] T004 [US4] Rewrite `scalp_cycle` in `trading_bot/futures_scalper.py` with the Amendment 001 pipeline:
  1. Regime gate (RANGE→long+short, TREND_UP→long only, TREND_DOWN→short only)
  2. Kill switch check (`is_entry_blocked`)
  3. Slot limit check
  4. Update price memory
  5. Adaptive threshold computation (`adaptive_enabled`, `get_atr_pct`, k/min fallback)
  6. `tp_eff > sl_eff` gate
  7. Dip/pump detection with adaptive thresholds
  8. Toxicity evaluation (`tox_eval` + `record_observation`)
  9. Direction from dip/pump (no OBI gate)
  10. Toxicity enforcement check (if `tox_enforced` → block)
  11. Cooldown check
  12. Spacing check
  13. Quantity computation + `place_entry` with leverage
  14. `_log` with 24-column INSERT, wire `signal_id`

### 2.4 Replace `_log_signal` with 24-column `_log`

- [X] T005 [US4] Replace `_log_signal(asset, venue, regime, obi, extreme_pct, action, reason)` with 24-column `_log(a, v, r, d, p, ex, th, at, ob, vl, dr, tx, act, rsn)` in `trading_bot/futures_scalper.py` — INSERT into `signals` table matching spot scalper's schema, return `cur.lastrowid` as `signal_id`

### 2.5 `_save_open` — add `signal_id` parameter

- [X] T006 [US4] Add `signal_id` parameter to `_save_open` in `trading_bot/futures_scalper.py`: new signature `_save_open(asset, venue, side, fill, signal_price, tp_pct, sl_pct, pos_id, signal_id)`. Include `"signal_id": signal_id` in the `save_position` dict. Use `fill.filled_qty` (Orderly does not deduct base fees from qty).

### 2.6 `_close_position` — add `signal_id` parameter

- [X] T007 [US4] Add `signal_id` parameter to `_close_position` in `trading_bot/futures_scalper.py`: new signature `_close_position(asset, venue, side, entry, exit, signal, qty, fee_rate, pos_id, signal_id, reason)`. Pass `signal_id` through to `record_closed_trade`.

### 2.7 `manage_open_positions` — pass `signal_id` through

- [X] T008 [US4] Update `manage_open_positions` in `trading_bot/futures_scalper.py`: read `signal_id` from position dict (`pos_dict.get("signal_id")`) and pass it to `_close_position` on TP fill, SL fill, and time stop. All other logic (TP/SL fill detection, time stop, regime exit) stays unchanged.

### 2.8 Cooldown and spacing — update venue keys

- [X] T009 [US4] Update `_cooldown_ok` in `trading_bot/futures_scalper.py` to use venue-prefixed key `f"orderly:{asset}:{side}"` (already correct — verify). Update `_spacing_ok` to use `venue="orderly"` (already correct — verify). No functional changes needed; confirm existing implementations match spot scalper patterns.

---

## Phase 3: User Story 1,2,3 Verification — Futures scalper cross-check (Priority: P1)

**Goal**: Verify that the futures scalper rewrite correctly implements US1 (adaptive thresholds), US2 (OBI/toxicity logging), and US3 (toxicity enforcement) by cross-referencing with the spot scalper.

**Independent Test**: Compare threshold computation and toxicity verdicts between spot and futures scalpers given identical market data inputs.

**Note**: These are verification tasks, not implementation — the implementation is done via US4 (Phase 2). US1-US3 are already validated for spot; these tasks confirm the futures port is correct.

- [ ] T010 [P] [US1] Verify adaptive threshold computation in `trading_bot/futures_scalper.py` matches spot scalper: same `dk/dm`, `pk/pm`, `tk/tm`, `sk/sm` settings keys, same `max(k×atr, min)` formula, same fallback to `*_min_pct` when ATR unavailable
- [ ] T011 [P] [US2] Verify toxicity pipeline in `trading_bot/futures_scalper.py`: `tox_eval` called with `venue="orderly"`, `record_observation` called every cycle, all `tox_*_enforce=false` by default, `signals` row records all verdicts
- [ ] T012 [P] [US3] Verify toxicity enforcement logic in `trading_bot/futures_scalper.py`: `tox_enforced` gates entry only when enforcement flags are ON and corresponding verdict is 1, NULL verdicts (warmup) do not block

---

## Phase 4: `bot.py` Compatibility

**Goal**: Ensure `bot.py` correctly drives the rewritten futures scalper and no stale settings cause confusion.

- [ ] T013 Update settings refresh list in `bot.py` (around line 98–103): remove superseded keys `dip_pct`, `pump_pct`, `obi_buy_threshold`, `obi_sell_threshold`; add Amendment 001 keys: `adaptive_enabled`, `dip_k`, `dip_min_pct`, `pump_k`, `pump_min_pct`, `tp_k`, `tp_min_pct`, `sl_k`, `sl_min_pct`, `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`, `max_extreme_velocity_pct`, `spread_z_max`, `depth_ratio_min`, `obi_z_max`, `cooldown_sec`, `min_entry_spacing_pct`
- [ ] T014 Verify `scalp_cycle` and `manage_open_positions` call signatures in `bot.py` are compatible with rewritten `trading_bot/futures_scalper.py`: `futures_cycle(asset, orderly, regime, obi, price)` and `futures_manage(asset, orderly, regime)` — confirm no signature changes needed

---

## Phase 5: Polish & Integration Validation

**Goal**: Final cross-checks and consistency validation.

- [X] T015 Run syntax check on `trading_bot/futures_scalper.py` to confirm no import errors or syntax issues
- [ ] T016 Verify `record_closed_trade` in `trade/pnl.py` accepts `signal_id` keyword argument (already supported per schema — confirm no changes needed)
- [ ] T017 [P] Verify `save_position` in `db/db_ops.py` accepts `signal_id` key in the position dict (already supported per migration 002 — confirm)
- [ ] T018 Dry-run validation: run `bot.py` with `dry_run=true` for at least one cycle per asset to confirm no runtime errors in the futures scalper pipeline

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: Complete — skip
- **Phase 2 (US4 — Futures rewrite)**: No dependencies on incomplete work. US4 is the implementation phase.
- **Phase 3 (US1-US3 Verification)**: Depends on Phase 2 completion — verifies the rewrite.
- **Phase 4 (bot.py)**: Can run in parallel with Phase 3. Depends on Phase 2 (needs futures scalper to exist).
- **Phase 5 (Polish)**: Depends on Phases 2-4 completion.

### Within Phase 2 (US4)

Tasks must execute in order:
```
T001 (imports)
  → T002, T003 (detection functions, can run in parallel)
    → T004 (scalp_cycle rewrite — depends on T001-T003)
      → T005 (_log function)
      → T006 (_save_open)
      → T007 (_close_position)
      → T008 (manage_open_positions)
      → T009 (cooldown/spacing verify)
```

T005-T009 all depend on T004 (the main `scalp_cycle` must exist before helpers are finalized), but T005-T008 can be written in parallel since they are independent functions.

### Parallel Opportunities

- **Phase 3** tasks T010, T011, T012 can all run in parallel (independent verification checks)
- **Phase 4** task T013 and T014 can run in parallel
- **Phases 3 and 4** can run in parallel after Phase 2 completes
- **Phase 5** T016 and T017 can run in parallel

---

## Implementation Strategy

### MVP Scope (Minimum to complete Amendment 001)

Phase 2 only (US4 — futures scalper rewrite). This delivers the core remaining work: a futures scalper with adaptive thresholds, OBI demotion, and toxicity observability. Phases 3-5 are verification and polish.

### Incremental Delivery

1. **T001-T003**: Imports + detection functions (foundational, no behavioral change yet)
2. **T004**: Main `scalp_cycle` rewrite — the core change
3. **T005-T009**: Supporting functions updated to match new pipeline
4. **T013-T014**: bot.py settings refresh update (can happen in parallel with T005-T009)
5. **T010-T012**: Cross-check verification
6. **T015-T018**: Final syntax and dry-run validation

### Task Summary

| Phase | Tasks | Count |
|---|---|---|
| Phase 2 (US4 — Futures rewrite) | T001–T009 | 9 |
| Phase 3 (US1-US3 verification) | T010–T012 | 3 |
| Phase 4 (bot.py compat) | T013–T014 | 2 |
| Phase 5 (Polish) | T015–T018 | 4 |
| **Total** | | **18** |

### Suggested MVP Scope

T001–T009 (9 tasks): Complete futures scalper rewrite. This alone finishes Amendment 001's core remaining work.

---

# Tasks: Amendment 002 — Settings Validator & LLM Helper

**Plan**: `.specify/plan.md` (Amendment 002 section)
**Spec**: `.specify/specs/001-amendment-002-settings-validator/spec.md`
**Status**: Partial — schema, rules, LLM module, and migration complete ✅; bot.py, telegram.py, dashboard integration pending 🔧

**Already completed**:
- `trade/settings_schema.py` — 51 `SettingSpec` entries with `BY_KEY` lookup
- `trade/settings_rules.py` — `validate()` (9 cross-check rules) + `validate_all()`
- `research/settings_llm.py` — `explain()` (cached) + `propose()` (with audit trail)
- `db/migrations/003_amendment_002.sql` — `settings_baseline` + `settings_proposals` tables
- Migration applied to production DB

---

## Phase 1: Setup (Already Complete)

All foundational modules are deployed. No new setup tasks. The validator, schema, and LLM helper are importable and tested.

---

## Phase 2: User Story 1+2 — Startup Validation Gate (Priority: P1) 🎯 MVP

**Goal**: Replace the hardcoded `validate_startup()` in `bot.py` with a call to `validate_all()` from `trade/settings_rules.py`. On any error-level verdict, log all errors, send a single Telegram alert, and refuse to enter the trading loop. Warnings are logged and sent to Telegram but do not block. Periodic re-validation runs every 5 minutes in the main loop. If a setting change introduces an error mid-session, trading halts.

**Stories covered**:
- **US1**: Operator validates all settings before the bot trades — every setting checked against deterministic rules
- **US2**: System prevents startup with invalid configuration — bot refuses to trade until all errors resolved

**Independent Test**: Populate the settings table with `tp_min_pct = 0.3, sl_min_pct = 0.5` (violates tp > sl). Start the bot. Confirm it logs each error, sends a Telegram message listing all errors, and does NOT proceed to exchange queries. Fix the setting. Restart. Confirm it passes validation and enters the trading loop.

### Implementation

- [ ] T019 [US1] Replace `validate_startup()` in `bot.py` with a call to `validate_all()` from `trade.settings_rules`. The new function must: (a) import and call `validate_all()`, (b) separate verdicts into errors and warnings, (c) log every error at ERROR level and every warning at WARNING level, (d) send a single Telegram alert via `send_bot_message` listing all errors if any exist, (e) return `False` on any error, `True` otherwise. Handle `ImportError` if `settings_schema` cannot be loaded — log and refuse to trade.
- [ ] T020 [US2] Add Telegram alert on validation-blocking errors during startup in `bot.py`. Use `trading_bot.send_bot_message.send_bot_message()` to send a message to `TELEGRAM_CHAT_ID` with the format: "❌ Startup validation FAILED — trading disabled\n" followed by each error on its own line. Warnings get a separate "⚠️ Warnings:" section. Ensure the message is sent before the bot enters the sleep/exit path.
- [ ] T021 [US2] Update periodic re-validation in `bot.py` main loop (the 5-minute `VALIDATION_INTERVAL` check) to use the new `validate_all()`-based function instead of the old `validate_startup()`. On error: set `trading_enabled = 0` via `upsert_setting`, log the event, send Telegram alert. The existing `_last_validation` timestamp pattern is preserved.
- [ ] T022 [US1] Update settings refresh list in `bot.py` (the `current = {k: get_setting(k) ...}` block around lines 98–115). Remove stale legacy keys: `dip_pct`, `pump_pct`, `obi_buy_threshold`, `obi_sell_threshold`. Add Amendment 001+002 keys: `adaptive_enabled`, `dip_k`, `dip_min_pct`, `pump_k`, `pump_min_pct`, `tp_k`, `tp_min_pct`, `sl_k`, `sl_min_pct`, `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`, `max_extreme_velocity_pct`, `spread_z_max`, `depth_ratio_min`, `obi_z_max`, `cooldown_sec`, `min_entry_spacing_pct`, `llm_helper_enabled`, `llm_language`. The list is used only for change-detection logging — preserve the existing pattern.

**Checkpoint**: Bot refuses to start with invalid config, sends clear Telegram alerts, re-validates on schedule. US1 and US2 are independently testable.

---

## Phase 3: User Story 3 — Telegram `/explain` Handler (Priority: P2)

**Goal**: Add `/explain <key>` command to the Telegram bot. Returns a 2–3 sentence plain-language description of the setting, sourced from the LLM cache (first call) or generated fresh and cached. Falls back to the static `short` description from the schema if the LLM is disabled or unreachable. The `/list` menu gains "Explain" and "Propose" action buttons.

**Independent Test**: Send `/explain tp_min_pct` via Telegram. Confirm response is 2–3 sentences describing take-profit. Send again — confirm instant (cached). Set `llm_helper_enabled = false` — confirm fallback to static description. Send `/explain nonexistent` — confirm "Unknown setting" response.

### Implementation

- [ ] T023 [US3] Add `/explain` command handler in `telegram.py`. Register `@bot.message_handler(commands=['explain'])`. Extract the setting key from the message text (strip `/explain`, trim whitespace). If no key provided, prompt user: "Usage: /explain <setting_key>. Use /list to see all settings." Call `explain(key, language, band)` from `research.settings_llm`. Send the result via `send_text_message_chunked`. Handle: unknown key (respond with valid keys list), LLM disabled (fallback message), import error (log and respond with error).
- [ ] T024 [P] [US3] Add "Explain" and "Propose" action buttons to `/list` inline keyboard in `telegram.py`. In the `command_list` function (around line 145), add two new `InlineKeyboardButton` rows below the existing "List All Settings" row: one for "💡 Explain Setting" (`callback_data="ExplainPrompt"`) and one for "🤖 Propose Changes" (`callback_data="ProposeStart"`). Follow the existing pattern — use `translate()` for labels and `InlineKeyboardMarkup().row(...)`.
- [ ] T025 [US3] Add callback handler for explain setting selection flow in `telegram.py`. In the `callback_handler` function, handle `ExplainPrompt`: present an inline keyboard of setting groups (from `trade.settings_schema.GROUPS`). On group selection, show settings in that group as buttons. On setting selection, call `explain()` and display the result with a "Back" button. Add `"ExplainPrompt"` and `"ExplainGroup:*"` and `"ExplainKey:*"` to the `options` dict or as explicit `elif` branches in the callback handler. Follow existing callback patterns (answer callback, edit reply markup, send messages).

**Checkpoint**: `/explain` works for any valid setting key, caches responses, falls back gracefully. US3 independently testable.

---

## Phase 4: User Story 4 — Telegram `/propose` Handler (Priority: P2)

**Goal**: Add `/propose` command. The LLM reviews current settings and recent performance context, generates proposals with confidence grades (`measured` | `heuristic` | `no_basis`), writes them to `settings_proposals`, and presents them with accept/reject buttons. Proposals are advisory — the operator always decides. Rate-limited to `llm_max_calls_per_hour`. Only one in-flight proposal at a time.

**Independent Test**: Send `/propose` with the bot running. Confirm response lists proposals with current→proposed values, reasons, and confidence badges. Check `settings_proposals` table — confirm rows exist. Confirm `settings` table is UNCHANGED. Send `/propose` again immediately — confirm rate-limit message. Accept a proposal — confirm setting updates and proposal marked accepted.

### Implementation

- [ ] T026 [US4] Add `/propose` command handler in `telegram.py`. Register `@bot.message_handler(commands=['propose'])`. Build a context summary string from recent signals/trades (last 50 signals from `signals` table, last 20 closed trades from `closed_trades` table — query inline as done in `execute_trade_performance`). Call `propose(context_summary)` from `research.settings_llm`. Format results as a message listing each proposal with: key, current→proposed value, reason, evidence summary, confidence badge (`🟢 measured` / `🟡 heuristic` / `⚪ no_basis`). Add inline accept/reject buttons per proposal with callback data `proposal_accept:<id>` and `proposal_reject:<id>`. Handle: LLM disabled, rate limited, timeout, empty proposals. Use `send_text_message_chunked` for text, `bot.send_message` with `reply_markup` for proposals with buttons.
- [ ] T027 [US4] Add proposal accept/reject inline button handlers in `telegram.py`. In `callback_handler`, handle `proposal_accept:<id>` and `proposal_reject:<id>` callbacks. On accept: read the proposal row from `settings_proposals` by id, apply the proposed value via `upsert_setting(key, value)`, update the proposal row with `status='accepted'` and `decided_at` timestamp. On reject: update proposal row with `status='rejected'` and `decided_at`. Edit the original message to show the updated status (strikethrough or ✅/❌ indicator). Follow existing DB access pattern (inline `import sqlite3` with `_get_db_rw`-style connection or use `db.db_ops` functions).

**Checkpoint**: `/propose` generates advisory proposals with evidence grading, accept/reject workflow complete. US4 independently testable.

---

## Phase 5: User Story 5 — Dashboard Inline Validation (Priority: P3)

**Goal**: The dashboard settings panel runs client-side validation on every keystroke (debounced 500ms). Invalid fields highlight red (error) or amber (warning) with a tooltip showing the message. Cross-dependency errors (e.g., tp ≤ sl) highlight all affected fields simultaneously. Validation is purely client-side — no network call, no LLM.

**Independent Test**: Open dashboard settings panel. Type `0.3` in `tp_min_pct` and `0.5` in `sl_min_pct`. Confirm both fields highlight red with "tp must exceed sl" message. Change `sl_min_pct` to `0.2`. Confirm errors clear. Type `abc` in a numeric field. Confirm red highlight immediately.

### Implementation

- [ ] T028 [P] [US5] Add `/api/validate` endpoint in `dashboard/main.py`. Add a GET endpoint at `/api/validate` that imports and calls `validate_all()` from `trade.settings_rules`. Return JSON: `{ok: true, verdicts: {key: {level, message, suggested_value}}}`. No authentication required (read-only, deterministic, exposes no secrets). Handle import errors gracefully with a 500 response. Ensure `trade/` is importable from the dashboard container — if not, add `sys.path` adjustment at module top matching the existing pattern.
- [ ] T029 [P] [US5] Create client-side validation module in `dashboard-ui/src/validation.ts`. Export a `validateSetting(key: string, value: string, allSettings: Record<string, string>)` function that returns `{level: "ok"|"warn"|"error", message: string}`. Implement: (a) type coercion matching `_coerce()` in `trade/settings_rules.py` (bool, int, float, str), (b) hard/soft range checks using thresholds from a local `SETTING_SPECS` constant mirroring the Python `SettingSpec` entries for dashboard-visible settings, (c) cross-setting checks: `tp_min_pct > sl_min_pct`, `leverage <= max_leverage`, `tp_min_pct - dex_round_trip_fee_pct - assumed_slippage_pct >= min_net_edge_pct`, `slots × slot_pct <= 100`. Export a `validateAll(settings: Record<string, string>)` function returning `Record<string, Verdict>`. This module is pure TypeScript — no imports from the Python backend, no network calls.
- [ ] T030 [US5] Integrate inline validation into `dashboard-ui/src/MiniSettings.tsx`. Modify the `NumberField` component to accept an optional `verdict` prop (`{level, message}`). When `level === "error"`, render the border red (`border-red-500`). When `level === "warn"`, render the border amber (`border-amber-500`). Add a `title` attribute with the message for native tooltip. In `MiniSettingsComponent`, add a `useMemo` that calls `validateAll(settings)` from `validation.ts` whenever `settings` changes. Pass the per-key verdict to each `NumberField` and `ComboList`. For cross-dependency errors, derive which keys are affected and pass the verdict to all of them. Preserve all existing behavior (auto-save, editable state, preset options) — validation is purely additive visual feedback.

**Checkpoint**: Dashboard fields show real-time validation feedback with no server round-trip. US5 independently testable.

---

## Phase 6: User Story 6 — Audit Trail & Proposals View (Priority: P3)

**Goal**: Every LLM-generated proposal is immutable in `settings_proposals`. The dashboard displays proposal history with status, confidence, and timestamps. Operators can review past proposals and see which were accepted or rejected. No proposals are ever auto-deleted.

**Independent Test**: Generate proposals via `/propose`. Open dashboard proposals view. Confirm all proposals appear with correct timestamps, confidence badges, and status. Accept one via dashboard — confirm status updates.

### Implementation

- [ ] T031 [P] [US6] Add `/api/proposals` history endpoint in `dashboard/main.py`. Add a GET endpoint at `/api/proposals` that queries `settings_proposals` table ordered by `created_at DESC`. Support query params: `?status=pending|accepted|rejected` (filter), `?limit=50` (default 50, max 200). Return JSON: `{proposals: [{id, created_at, source, key, current_value, proposed_value, reason, evidence, confidence, status, decided_at, model}]}`. No auth required (read-only). Follow existing `_get_db()` pattern.
- [ ] T032 [P] [US6] Create `Proposals.tsx` component in `dashboard-ui/src/Proposals.tsx`. Fetch from `/api/proposals` on mount. Display as a scrollable list with cards showing: key name, current→proposed value, reason (truncated to 1 line), confidence badge (color-coded: green=`measured`, yellow=`heuristic`, gray=`no_basis`), status badge (pending/accepted/rejected), relative timestamp. Add filter tabs: All / Pending / Accepted / Rejected. Add accept/reject buttons on pending proposals (POST to a new `/api/proposals/:id/accept` or reuse `/api/miniapp` to apply the setting). Add the `Proposals` tab to `App.tsx` navigation (in the "More" menu or as a new tab). Follow existing component patterns (Tailwind CSS, Lucide icons, dark theme `#1a1528`/`#2a2240`/`#D0CFCC`).

**Checkpoint**: Complete audit trail visible in dashboard. US6 independently testable.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Goal**: Final verification that the LLM helper stays isolated from the trading path, and end-to-end validation of the full Amendment 002 pipeline.

- [ ] T033 Verify `research/settings_llm.py` is NOT imported by any trading-path module. Grep for `settings_llm` across `bot.py`, `trading_bot/executor.py`, `trading_bot/spot_scalper.py`, `trading_bot/futures_scalper.py`, `trade/pnl.py`, `trade/regime.py`. If any import is found, refactor to remove it (constitution principle I). Document the verification result in a comment at the top of `research/settings_llm.py`.
- [ ] T034 End-to-end dry-run validation: (a) Set `tp_min_pct = 0.3, sl_min_pct = 0.5` in DB, start `bot.py` — verify it logs errors, sends Telegram alert, does NOT query exchanges. (b) Fix settings, restart — verify clean startup. (c) Send `/explain tp_min_pct` via Telegram — verify response. (d) Send `/propose` — verify proposals generated. (e) Open dashboard, type invalid values — verify red/amber highlights. (f) Check `settings_proposals` table — verify rows immutable. Run with `dry_run=true` throughout.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: Complete — no tasks
- **Phase 2 (US1+US2 — Startup gate)**: No dependencies on incomplete work. These are the MVP.
- **Phase 3 (US3 — Telegram /explain)**: Independent of Phase 2. Can run in parallel.
- **Phase 4 (US4 — Telegram /propose)**: Independent of Phase 3. Can run in parallel with Phase 3 (both touch `telegram.py` — coordinate).
- **Phase 5 (US5 — Dashboard validation)**: Independent of Phases 2–4. Can run in parallel.
- **Phase 6 (US6 — Audit trail)**: Depends on Phase 4 (needs proposals to exist). Can run in parallel with Phase 5.
- **Phase 7 (Polish)**: Depends on all phases above.

### Within Phase 2 (US1+US2)

```
T019 (validate_startup rewrite)
  ├── T020 (Telegram alert on error) — can run in parallel with T021
  ├── T021 (periodic re-validation) — can run in parallel with T020
  └── T022 (settings refresh list update) — can run in parallel with T020, T021
```

T019 must complete first (establishes the new validation function). T020–T022 all extend it and can run in parallel after T019.

### Within Phase 3 (US3)

```
T023 (/explain command) ── can run in parallel with T024
T024 (list buttons) ── can run in parallel with T023
  └── T025 (explain callback flow) — depends on T024 (buttons must exist)
```

### Within Phase 4 (US4)

```
T026 (/propose command)
  └── T027 (accept/reject handlers) — depends on T026
```

### Within Phase 5 (US5)

```
T028 (/api/validate endpoint) ── can run in parallel with T029
T029 (client validation.ts) ── can run in parallel with T028
  └── T030 (MiniSettings.tsx integration) — depends on T029
```

### Within Phase 6 (US6)

```
T031 (/api/proposals endpoint) ── can run in parallel with T032
T032 (Proposals.tsx component) ── can run in parallel with T031
```

### Parallel Opportunities Across Phases

- **Phases 2, 3, 4, 5** can all start in parallel (different files/modules)
- **Phase 3 and Phase 4** both touch `telegram.py` — coordinate to avoid merge conflicts
- **T028 and T029** are in different languages/repos — fully parallel
- **T031 and T032** are backend/frontend — fully parallel
- **T033** can run at any time (verification only)

---

## Implementation Strategy

### MVP Scope (Minimum to Complete Amendment 002)

Phase 2 only (T019–T022, 4 tasks): Replace `validate_startup()` with `validate_all()` and update the settings refresh list. This delivers the core safety improvement — the bot refuses to trade with invalid configuration — using the already-complete validator module.

### Incremental Delivery

1. **T019–T022**: Startup validation gate — bot refuses invalid config (P1, safety-critical)
2. **T023–T025**: Telegram `/explain` — self-documenting settings (P2)
3. **T026–T027**: Telegram `/propose` — data-driven suggestions (P2)
4. **T028–T030**: Dashboard inline validation — real-time feedback (P3)
5. **T031–T032**: Audit trail view — governance support (P3)
6. **T033–T034**: Verification and dry-run checkout

### Task Summary

| Phase | Tasks | Count |
|---|---|---|
| Phase 2 (US1+US2 — Startup gate) | T019–T022 | 4 |
| Phase 3 (US3 — Telegram /explain) | T023–T025 | 3 |
| Phase 4 (US4 — Telegram /propose) | T026–T027 | 2 |
| Phase 5 (US5 — Dashboard validation) | T028–T030 | 3 |
| Phase 6 (US6 — Audit trail) | T031–T032 | 2 |
| Phase 7 (Polish) | T033–T034 | 2 |
| **Total** | | **16** |

### Suggested MVP Scope

T019–T022 (4 tasks): Startup validation gate. This alone makes the bot refuse to trade with invalid settings, leveraging the already-complete validator.

---

## Phase 8: Convergence

**Generated**: 2026-07-26 | **Source**: `/speckit.converge` assessment of Amendment 002 implementation vs spec, plan, and tasks.

**Already built**: `trade/settings_schema.py` (51 SettingSpec entries), `trade/settings_rules.py` (validate + validate_all), `research/settings_llm.py` (explain + propose), `db/migrations/003_amendment_002.sql` (applied), `bot.py` (validate_startup uses validate_all), `dashboard/main.py` (/api/settings/validate endpoint). FR-C1 isolation verified — zero `settings_llm` imports in trading path.

**Remaining gaps** (17 findings → 17 tasks):

- [ ] T035 **CRITICAL** Fix `llm_helper_enabled` default seed in `db/migrations/003_amendment_002.sql` — change `'true'` to `'false'` per FR-G1 ("default: disabled") and constitution principle VII (LLM must not be active by default) (`contradicts`)

- [ ] T036 [P] [FR-A1] Add `default` field to `SettingSpec` dataclass in `trade/settings_schema.py` and populate sensible defaults for all 51 entries per FR-A1 (`missing`)

- [ ] T037 [P] [T020] Send Telegram alert on validation-blocking startup errors in `bot.py` — import `send_bot_message` from `trading_bot.send_bot_message`, on error verdicts call `send_bot_message(TELEGRAM_CHAT_ID, formatted_errors)` before refusing to trade (`missing`)

- [ ] T038 [P] [T023] Add `/explain` command handler in `telegram.py` — register `@bot.message_handler(commands=['explain'])`, extract key from message, call `explain()` from `research.settings_llm`, send result via `send_text_message_chunked`, handle unknown key / LLM disabled / import error (`missing`)

- [ ] T039 [P] [T026] Add `/propose` command handler in `telegram.py` — register `@bot.message_handler(commands=['propose'])`, build context from recent signals/trades, call `propose()` from `research.settings_llm`, format proposals with confidence badges and accept/reject buttons, handle rate-limit / timeout / LLM disabled (`missing`)

- [ ] T040 [T019] Remove redundant manual `tp`/`sl`/`net_edge` checks from `validate_startup()` in `bot.py` L56-60 (already covered by `validate_all()`); add `try`/`except ImportError` guard around `from trade.settings_rules import validate_all` — log and refuse to trade if schema unloadable per US2/AC4 (`partial`)

- [ ] T041 [T022] Add missing keys to settings refresh list in `bot.py` (around L95): Amendment 001 toxicity keys (`tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce`, `max_extreme_velocity_pct`, `spread_z_max`, `depth_ratio_min`, `obi_z_max`) and Amendment 002 LLM keys (`llm_helper_enabled`, `llm_language`, `llm_model`, `llm_timeout_sec`, `llm_explain_cache_days`, `llm_max_calls_per_hour`). Preserve existing pattern for change-detection logging (`partial`)

- [ ] T042 [FR-G1] Update `llm_explain_cache_days` seed value in `db/migrations/003_amendment_002.sql` from `'30'` to `'90'` per FR-G1 spec default (`partial`)

- [ ] T043 [P] [T024] Add "💡 Explain Setting" (`ExplainPrompt`) and "🤖 Propose Changes" (`ProposeStart`) inline buttons to `/list` menu in `telegram.py` `command_list` function L139-143, following existing `InlineKeyboardMarkup().row(...)` pattern (`missing`)

- [ ] T044 [P] [T025] Add explain callback flow in `telegram.py` `callback_handler`: handle `ExplainPrompt` (show setting groups from `trade.settings_schema.GROUPS`), `ExplainGroup:*` (show settings in group as buttons), `ExplainKey:*` (call `explain()`, display result with Back button). Follow existing callback patterns (`missing`)

- [ ] T045 [P] [T027] Add proposal accept/reject callback handlers in `telegram.py` `callback_handler`: handle `proposal_accept:<id>` (apply value via `upsert_setting`, update `settings_proposals` status=accepted), `proposal_reject:<id>` (update status=rejected). Edit original message to show ✅/❌ (`missing`)

- [ ] T046 [P] [T029] Create `dashboard-ui/src/validation.ts` — export `validateSetting(key, value, allSettings)` and `validateAll(settings)` functions implementing: type coercion (bool/int/float/str), hard/soft range checks from a local `SETTING_SPECS` constant, cross-setting rules (tp>sl, leverage≤max, net edge, slots×slot≤100). Pure TypeScript, no network (`missing`)

- [ ] T047 [P] [T030] Integrate `validateAll()` verdicts into `dashboard-ui/src/MiniSettings.tsx`: add `useMemo` calling `validateAll(settings)` on settings change, pass per-key verdict to `NumberField` (already has `error` prop for red border), add amber `border-amber-500` for `warn` level, set `title` attribute for native tooltip. Preserve all existing auto-save/edit behavior (`missing`)

- [ ] T048 [P] [T031] Add `GET /api/proposals` endpoint in `dashboard/main.py` — query `settings_proposals` ordered by `created_at DESC`, support `?status=pending|accepted|rejected` filter and `?limit=50` param. Return JSON `{proposals: [...]}`. No auth required (read-only). Follow existing `_get_db()` pattern (`missing`)

- [ ] T049 [P] [T032] Create `dashboard-ui/src/Proposals.tsx` — fetch `/api/proposals` on mount, display card list with key, current→proposed value, reason (1-line truncated), confidence badge (green/yellow/gray), status badge, relative timestamp. Add filter tabs (All/Pending/Accepted/Rejected). Add accept/reject buttons on pending proposals (POST to apply setting). Add tab to `App.tsx`. Follow existing Tailwind/dark-theme patterns (`missing`)

- [ ] T050 [T033] Verify and document `settings_llm` isolation in `research/settings_llm.py`: add a comment block at top confirming zero imports from `bot.py`, `trading_bot/`, `executor.py`, scalpers, `trade/pnl.py`, `trade/regime.py`. Reference constitution principle I (`missing`)

- [ ] T051 [T034] End-to-end dry-run validation: (a) set tp≤sl in DB, start bot → verify errors logged + Telegram alert + no exchange queries; (b) fix settings, restart → clean startup; (c) send `/explain tp_min_pct` → verify response; (d) send `/propose` → verify proposals; (e) open dashboard, type invalid values → verify red/amber highlights; (f) check `settings_proposals` immutability. Run with `dry_run=true` (`missing`)

---

### Convergence Summary

| Gap Type | Count |
|---|---|
| missing | 13 |
| partial | 3 |
| contradicts | 1 |
| unrequested | 0 |
| **Total** | **17** |

| Severity | Count |
|---|---|
| CRITICAL | 1 |
| HIGH | 4 |
| MEDIUM | 10 |
| LOW | 2 |

**Handoff**: Run `/speckit.implement` to complete these 17 convergence tasks. A follow-up `/speckit.converge` run should find zero remaining gaps.
