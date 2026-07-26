# Feature Specification: Amendment 002 — Settings Validator & LLM Helper

**Feature Branch**: `amendment-002-settings-validator`

**Created**: 2026-07-26

**Status**: Draft

**Input**: Amendment 001 added 22 settings (adaptive thresholds, toxicity) with no documentation. This amendment adds: (1) a static settings schema as the single source of truth for all setting metadata, (2) a deterministic, offline validator that catches invalid configurations before the bot starts or after any setting change, (3) an LLM-powered explainer and proposer that runs exclusively in the `research/` path (never in the trading execution path, per constitution principle I), (4) database tables for baseline tracking and proposal audit trail, (5) Telegram and dashboard surfacing of validation and LLM features, and (6) new LLM-related settings.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Operator validates all settings before the bot trades (Priority: P1)

An operator changes settings via Telegram, the dashboard, or directly in the database. Before the bot enters its next cycle, every setting is validated against a deterministic rule set: type matching, range bounds, cross-setting dependencies (e.g., `tp_pct > sl_pct`), and constitutional constraints (e.g., net edge ≥ `min_net_edge_pct`). Invalid settings produce clear error messages stating what is wrong and what value would fix it. The bot refuses to trade until all errors are resolved. Warnings (e.g., toxicity baseline unvalidated) are logged but do not block trading.

**Why this priority**: Without validation, a mistyped setting can cause the bot to trade with no stop-loss, violate the constitution's "reward must exceed risk" principle, or crash mid-cycle. The validator is the safety net for every setting change — no LLM, no network, no ambiguity.

**Independent Test**: Feed the validator a dictionary of settings with a deliberate error (e.g., `tp_pct = 0.3, sl_pct = 0.5`). Confirm the validator returns `error` with message "tp_pct must be greater than sl_pct" and `suggested_value` for `sl_pct` of 0.25. Feed a valid dictionary. Confirm all checks pass with `ok`. Verify no network call was made during validation.

**Acceptance Scenarios**:

1. **Given** `tp_pct = 0.3` and `sl_pct = 0.5` (reward does not exceed risk), **When** `validate()` runs, **Then** it returns verdict `error` with message indicating tp must exceed sl and a suggested sl value below tp.
2. **Given** `leverage = 15` on a venue with `max_leverage = 10`, **When** `validate()` runs, **Then** it returns verdict `error` with message "leverage 15 exceeds max 10 for venue X" and `suggested_value = 10`.
3. **Given** `tox_window = 3` (below minimum of 5 for z-score), **When** `validate()` runs, **Then** it returns verdict `warn` — warmup will never complete, but trading is not blocked.
4. **Given** all settings valid but `toxicity_baseline` is unvalidated (NULL in `settings_baseline`), **When** `validate()` runs, **Then** it returns verdict `warn` with message "toxicity baseline has not been measured; enforcement may use defaults."
5. **Given** a setting with type `float` receives value `"yes"`, **When** `validate()` runs, **Then** it returns verdict `error` with message "expected float, got str."

---

### User Story 2 — System prevents startup with invalid configuration (Priority: P1)

On bot startup, the full settings table is loaded and validated. If any setting fails with `error` verdict, the bot logs every error, sends a Telegram alert, and refuses to enter the trading loop. It does not open positions, place orders, or query the exchange. The operator must fix all errors before the bot will start trading. Warnings are logged and sent to Telegram but do not block startup.

**Why this priority**: This is the startup gate. A bot that starts with invalid settings can cause immediate financial damage (wrong leverage, wrong position size, no stop). The startup validation is the last line of defense before real money is at risk.

**Independent Test**: Populate the settings table with `tp_pct = 0.5, sl_pct = 0.5` (equal, violates tp > sl). Start the bot. Confirm it logs the error, sends a Telegram message, and does NOT proceed to `executor.py`. Fix the setting. Restart. Confirm it passes validation and enters the trading loop.

**Acceptance Scenarios**:

1. **Given** the settings table contains at least one error-level violation, **When** the bot starts, **Then** it logs all errors, sends a single Telegram message listing each error, and exits without calling any exchange API.
2. **Given** the settings table passes all error checks but has warnings, **When** the bot starts, **Then** it logs the warnings, sends a Telegram summary, and proceeds to the trading loop.
3. **Given** a setting change is made mid-session (via Telegram or dashboard), **When** the change is saved, **Then** the full validation re-runs before the next cycle. If an error is introduced, trading halts.
4. **Given** the `settings_schema` module cannot be loaded (corrupted file), **When** the bot starts, **Then** it logs the error and refuses to trade — it never proceeds without a schema.

---

### User Story 3 — Operator gets plain-language explanations of settings via Telegram (Priority: P2)

An operator sends `/explain sl_pct` in Telegram. The bot responds with a 2–3 sentence description in the operator's preferred language: what the setting controls, what range is normal, and what happens if it's too high or too low. The explanation is generated once (by LLM) and cached by `(setting_key, capital_band, language)` so repeat queries are instant and cost nothing. Explanations are never generated in the trading execution path.

**Why this priority**: Amendment 001 added 22 settings with no documentation. An operator encountering `tox_depth_enforce` or `velocity_window` in the settings table has no way to know what they do without reading source code. Explanations make the bot self-documenting. This is P2 because the validator (P1) prevents misconfiguration even without explanations — explanations improve the operator experience but are not safety-critical.

**Independent Test**: Send `/explain tp_pct` via Telegram. Confirm the response is 2–3 sentences describing take-profit percentage, its relationship to sl_pct, and typical range. Send `/explain tp_pct` again. Confirm the second response is instant (cached). Change the language setting to `es` and send `/explain tp_pct`. Confirm the response is in Spanish.

**Acceptance Scenarios**:

1. **Given** the LLM helper is enabled and `tp_pct` has a cached explanation in English, **When** `/explain tp_pct` is sent, **Then** the cached explanation is returned with no LLM call.
2. **Given** no cached explanation exists for `tox_depth_enforce` in English, **When** `/explain tox_depth_enforce` is sent, **Then** an LLM call generates the explanation, it is cached, and the response is returned.
3. **Given** `llm_helper_enabled = false`, **When** `/explain` is sent, **Then** the bot responds "LLM helper is disabled. Enable llm_helper_enabled to use this feature."
4. **Given** an invalid setting key (e.g., `/explain nonexistent`), **When** sent, **Then** the bot responds "Unknown setting: nonexistent. Use /list to see all settings."

---

### User Story 4 — Operator receives LLM-generated setting proposals with evidence grading (Priority: P2)

An operator sends `/propose` in Telegram. The LLM reviews the current settings, recent trading performance (win rate, PnL, skipped-entry reasons, toxicity verdicts), and market conditions. It returns a list of proposals, each with: current value, proposed value, a 1–2 sentence reason, supporting evidence from recent data, and a confidence grade (`measured` | `heuristic` | `no_basis`). Proposals are written to the `settings_proposals` table — never directly to the settings table. The operator reviews proposals and decides which to apply manually. The LLM is never in the execution path.

**Why this priority**: After days of observe-only toxicity data collection (Amendment 001), an operator has data but no guidance on how to calibrate thresholds. The proposer bridges the gap between raw data and actionable settings changes. This is P2 because proposals are advisory only — the operator always makes the final decision, and the validator (P1) ensures no harmful proposal can be applied accidentally.

**Independent Test**: Run the bot in dry-run for 50 cycles with toxicity observe-only. Send `/propose`. Confirm the response lists at least one proposal with `current_value`, `proposed_value`, `reason`, `evidence`, and `confidence`. Check the `settings_proposals` table — confirm a row exists for each proposal. Confirm the `settings` table is unchanged.

**Acceptance Scenarios**:

1. **Given** 100+ cycles of observe-only toxicity data showing spread z-scores consistently below 1.0, **When** `/propose` is sent, **Then** the LLM may propose lowering `spread_z_max` from the default to a data-driven value with confidence `measured`.
2. **Given** fewer than 10 cycles of data (cold start), **When** `/propose` is sent, **Then** all proposals carry confidence `no_basis` and the reason explicitly states that insufficient data exists.
3. **Given** `llm_helper_enabled = false`, **When** `/propose` is sent, **Then** the bot responds that the LLM helper is disabled.
4. **Given** the LLM call times out (exceeds `llm_timeout_sec`), **When** `/propose` is sent, **Then** the bot responds with a timeout message and does not write partial proposals to the database.
5. **Given** the hourly LLM call limit (`llm_max_calls_per_hour`) has been reached, **When** `/propose` is sent, **Then** the bot responds with the remaining wait time and does not make an LLM call.

---

### User Story 5 — Dashboard shows inline validation errors as the operator types (Priority: P3)

The dashboard settings panel runs the deterministic validator on every keystroke (debounced at 500ms). When a value is invalid, the field turns red and a tooltip shows the error message. When a cross-setting dependency is violated (e.g., tp ≤ sl), both fields highlight with the dependency error. Validation is purely client-side via the deterministic rules — the dashboard never calls an LLM or waits for a server round-trip for validation feedback.

**Why this priority**: Immediate feedback reduces operator errors before they reach the bot. However, the Telegram-based validator (Story 1) already catches all errors before trading, so inline dashboard validation is a convenience improvement, not a safety requirement.

**Independent Test**: Open the dashboard settings panel. Type `0.3` in the `tp_pct` field and `0.5` in the `sl_pct` field. Confirm both fields highlight red and show "tp_pct must be greater than sl_pct." Change `sl_pct` to `0.2`. Confirm the error clears. Type `abc` in a numeric field. Confirm it highlights red immediately.

**Acceptance Scenarios**:

1. **Given** the operator types a value outside the hard range for a setting, **When** the 500ms debounce fires, **Then** the field highlights red with the range error message.
2. **Given** two interdependent settings (tp and sl) are both individually valid but tp ≤ sl, **When** either value changes, **Then** both fields highlight with the cross-dependency error.
3. **Given** no LLM connectivity, **When** the operator types in the dashboard, **Then** inline validation still works — it depends only on the deterministic rules.
4. **Given** a warning-level validation (not an error), **When** detected, **Then** the field highlights amber (not red) and the tooltip indicates this is a warning, not a blocker.

---

### User Story 6 — Every proposed setting change is auditable (Priority: P3)

When the LLM generates a proposal (Story 4), a row is written to `settings_proposals` with: the setting key, current value, proposed value, reason, evidence summary, confidence grade, timestamp, and whether the proposal was accepted or rejected. When an operator manually changes a setting via Telegram or dashboard, the `settings_baseline` table is checked: if the previous value was `measured` (derived from data), the change is flagged. The audit trail enables post-hoc review of every configuration decision.

**Why this priority**: Audit trails matter for operators managing real capital, but the bot can trade correctly without them. This is P3 because it supports governance and review workflows rather than core trading safety.

**Independent Test**: Send `/propose`. Check `settings_proposals` for new rows. Manually change a setting via Telegram `/set`. Verify the `settings_baseline` row for that setting reflects the new value and the `updated_at` timestamp is current. Query the proposals table to confirm every proposal is immutable once written.

**Acceptance Scenarios**:

1. **Given** a proposal is generated with confidence `measured`, **When** the operator accepts it, **Then** the `settings_proposals` row is updated with `accepted = true` and `accepted_at` timestamp.
2. **Given** a setting was originally marked `measured` in `settings_baseline`, **When** the operator manually changes it, **Then** the baseline status changes to `overridden` and the previous measured value is preserved.
3. **Given** a proposal is generated, **When** 30 days pass without acceptance or rejection, **Then** the proposal remains in the table — no automatic expiration (retention policy deferred to future amendment).

---

### Edge Cases

- **Schema module missing or corrupted**: Bot refuses to start. Logs the exact import error. No fallback to "assume valid" — unknown settings are unsafe.
- **Setting exists in DB but not in schema**: Validator flags it as `warn` — "unknown setting X, may be deprecated." Does not block trading (it may be from a future version or a removed setting).
- **Setting exists in schema but not in DB**: Validator returns `error` — required setting missing, default applied. The bot must have every schema-defined setting present.
- **LLM API unreachable**: `/explain` falls back to a static short description from the schema. `/propose` returns "LLM unavailable, try again later." Neither blocks trading.
- **LLM returns malformed response**: `/explain` falls back to the static description. `/propose` logs the raw response for debugging and returns "LLM response could not be parsed." No partial data is written.
- **`llm_language` set to an unsupported language**: Falls back to English with a warning logged.
- **Explain cache grows unbounded**: Cache entries expire after `llm_explain_cache_days`. Default 90 days.
- **Concurrent `/propose` calls**: Rate-limited to one in-flight LLM call at a time. Subsequent calls receive "A proposal is already being generated."
- **Validator called with empty context (no DB)**: Returns `error` for every setting — "no value provided." The validator requires values, not just keys.

## Requirements *(mandatory)*

### Functional Requirements

#### FR-A: Settings Schema (`trade/settings_schema.py`)

- **FR-A1**: The schema MUST define a setting metadata record for every setting with fields: unique key, data type (one of: decimal, integer, boolean, text, structured), category group, display unit (if applicable), hard minimum and maximum (violations are errors), soft minimum and maximum (violations are warnings), one-line description, list of dependent setting keys for cross-validation, and a sensible default value.
- **FR-A2**: The schema MUST provide a lookup from every setting key to its metadata record. This is the single source of truth for all setting metadata.
- **FR-A3**: The module MUST be version-controlled and never modified at runtime. It MUST contain entries for all Amendment 001 settings (adaptive, toxicity) plus the 6 new LLM settings from this amendment.
- **FR-A4**: Every `cross_checks` entry MUST reference a key that also exists in `SETTINGS`. Self-referencing is allowed (for rules that need context beyond the single value).

#### FR-B: Deterministic Validator (`trade/settings_rules.py`)

- **FR-B1**: The validator MUST accept a setting key, its value, and a context of related values, and return a verdict with: a level (`ok`, `warn`, or `error`), a human-readable message, and an optional suggested corrected value.
- **FR-B2**: Type/range validation MUST check: value matches the declared data type, value is within hard bounds (error if violated), value is within soft bounds (warn if violated).
- **FR-B3**: Cross-check rules MUST include at minimum:
  - `tp_pct > sl_pct` (error if violated)
  - Net edge: `tp_pct - round_trip_fee_pct - assumed_slippage_pct >= min_net_edge_pct` (error if violated)
  - Slot sizing: `base_order_size_usd <= max_order_usd` (error if violated)
  - Leverage cap: `leverage <= max_leverage` per venue (error if violated)
  - Liquidation distance: `(entry - liq_price) / entry > sl_pct` (warn if violated)
  - Toxicity warmup: `tox_window >= 5` (warn if < 5)
  - Toxicity baseline unvalidated: warn if `toxicity_baseline` is NULL in `settings_baseline`
  - Adaptive inactivity: warn if `adaptive_enabled = true` but all `*_k` multipliers are at their defaults (suggests adaptive is on but not tuned)
- **FR-B4**: The validator MUST be a pure computation — no I/O, no network, no LLM, no database access. All needed data is provided by the caller.
- **FR-B5**: A batch validation operation MUST run single-key validation on every setting and return all verdicts.
- **FR-B6**: Every rule MUST be independently testable with a unit test. No rule may depend on external state.

#### FR-C: LLM Explainer & Proposer (`research/settings_llm.py`)

- **FR-C1**: The module MUST NOT be imported by `trading_bot/`, `bot.py`, `executor.py`, `spot_scalper.py`, `futures_scalper.py`, or any module in the trading execution path. Comply with constitution principle I.
- **FR-C2**: The explain operation MUST return a 2–3 sentence plain-language description of the setting. It MUST check a cache keyed by setting key, capital band, and language before calling the LLM. Cache entries expire after the configured cache duration.
- **FR-C3**: The explain operation MUST fall back to the static description from the schema if the LLM is disabled or unreachable.
- **FR-C4**: The propose operation MUST return a list of proposals, each with: setting key, current value, proposed value, reason (1–2 sentences), evidence summary, and confidence grade (`measured` | `heuristic` | `no_basis`).
- **FR-C5**: The propose operation MUST write results to the proposals table. It MUST NEVER write to the settings table.
- **FR-C6**: LLM calls MUST be rate-limited to `llm_max_calls_per_hour`. Calls MUST time out after `llm_timeout_sec`.
- **FR-C7**: The LLM model selection, API endpoint, and credentials MUST be configurable via settings and environment variables (never hardcoded).

#### FR-D: Database Changes (Migration 003)

- **FR-D1**: A settings baseline table MUST be created with fields: setting key (primary key), baseline value, baseline status (one of: `measured`, `unvalidated`, `overridden`), measured timestamp, and last-updated timestamp.
- **FR-D2**: A settings proposals table MUST be created with fields: unique proposal identifier, setting key, current value at proposal time, proposed value, reason, evidence summary, confidence grade, acceptance flag (default: not accepted), proposal timestamp, and acceptance timestamp.
- **FR-D3**: The migration MUST be idempotent — safe to run against a database that already has these tables.
- **FR-D4**: Existing settings with known-good values (constitution-mandated defaults) MUST be inserted into `settings_baseline` with `baseline_status = 'unvalidated'` on migration.

#### FR-E: Telegram Integration

- **FR-E1**: `/list` MUST gain `Explain` and `Propose` action buttons (inline keyboard) alongside existing actions.
- **FR-E2**: `/explain <key>` MUST invoke the LLM explain operation and return the result. If `<key>` is omitted, prompt the operator to select from the settings list.
- **FR-E3**: `/propose` MUST invoke the LLM propose operation and return a formatted list of proposals with accept/reject buttons.
- **FR-E4**: Telegram handlers for `/explain` and `/propose` MUST NOT block the trading loop. They run asynchronously.

#### FR-F: Dashboard Integration

- **FR-F1**: The settings panel MUST run client-side validation on every keystroke, debounced at 500ms.
- **FR-F2**: Validation rules executed client-side MUST match the deterministic rules from the server-side validator — no LLM dependency, no network call for validation feedback.
- **FR-F3**: Invalid fields MUST highlight red (error) or amber (warning) with a tooltip showing the validation message.
- **FR-F4**: Cross-dependency errors (e.g., tp ≤ sl) MUST highlight all affected fields simultaneously.

#### FR-G: New LLM Settings

- **FR-G1**: The following settings MUST be added to the schema and settings table with defaults:
  - `llm_helper_enabled` (boolean, default: disabled) — master switch for LLM features
  - `llm_language` (text, default: English) — language for explanations
  - `llm_model` (text, default: a lightweight model) — LLM model identifier
  - `llm_timeout_sec` (integer, default: 30, hard bounds: 5–120) — LLM call timeout in seconds
  - `llm_explain_cache_days` (integer, default: 90, hard bounds: 1–365) — explanation cache TTL in days
  - `llm_max_calls_per_hour` (integer, default: 10, hard bounds: 1–60) — rate limit

### Key Entities

- **Setting Metadata Record**: Immutable metadata for one setting. Key, type, group, unit, hard/soft bounds, description, cross-check dependencies, default value. Defined in a version-controlled registry that is the single source of truth.
- **Verdict**: Result of validating one setting. Level (`ok` | `warn` | `error`), human-readable message, optional suggested corrected value. Always deterministic for the same inputs.
- **Proposal**: An LLM-generated suggested setting change. Contains current value, proposed value, reason, evidence summary, and confidence grade. Stored immutably in `settings_proposals`. Never applied automatically.
- **SettingsBaseline**: Tracks whether a setting's current value was empirically measured, remains unvalidated (default), or has been manually overridden. Provides context for the validator's toxicity-baseline warning.
- **SettingsProposal**: Immutable audit record of every LLM-generated proposal. Tracks acceptance/rejection. Supports post-hoc governance review.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The validator catches 100% of constitutionally prohibited configurations (tp ≤ sl, net edge < min, leverage > max) before the bot enters its first trading cycle.
- **SC-002**: Every validation rule has a corresponding automated test. The full validation test suite completes in under 2 seconds with no network access.
- **SC-003**: An operator can get a plain-language explanation for any setting within 3 seconds of sending `/explain` (cached) or within 15 seconds (first-time LLM generation).
- **SC-004**: The `/propose` command returns at least one evidence-backed proposal after 100+ cycles of observe-only data collection, with confidence `measured` for data-driven proposals.
- **SC-005**: Dashboard inline validation provides feedback within 600ms of the operator stopping typing (500ms debounce + <100ms computation).
- **SC-006**: The LLM helper module has zero dependency references from any trading execution module — verifiable by automated dependency analysis.
- **SC-007**: The `settings_proposals` table contains a complete, immutable record of every LLM proposal ever generated, with no rows ever deleted by application code.
- **SC-008**: Migration 003 runs successfully against a fresh database, against a database with Amendment 001 applied, and against a database where 003 was already applied — no errors in any scenario.

## Assumptions

- The LLM backend is accessible via a standard chat completions API. The specific provider is configured via environment variables and not hardcoded.
- The `capital_band` categorization (used in explain cache keying) follows the existing band logic in the bot — small/medium/large based on account equity relative to thresholds.
- The dashboard is the existing web application in the project. Client-side validation will replicate the deterministic rules locally, without calling the server-side validator.
- The `settings` table structure from Amendment 001 (key-value pairs read via `get_setting_float`/`get_setting_bool`) remains unchanged. New tables (`settings_baseline`, `settings_proposals`) are additive.
- The Telegram bot already supports inline keyboards and setting update commands. The new explain and propose handlers follow the same interaction pattern.
- Toxicity baseline measurement (populating `settings_baseline` with `measured` status) is a manual or future-automated process — not part of this amendment. The validator warns when it's missing but does not block.
- LLM costs are acceptable to the operator. The `llm_helper_enabled` default of `false` ensures no unintended LLM usage.

## Dependencies

- **Pre-existing**: Amendment 001 (adaptive thresholds, toxicity observability, 22 settings, `signals` table expansion), `schema_v2.sql`, `db_ops.py`, `telegram.py`, `dashboard-ui/`.
- **Co-delivered**: Migration `003_amendment_002.sql`, `trade/settings_schema.py`, `trade/settings_rules.py`, `research/settings_llm.py`.
- **External**: LLM API endpoint (configured via `.env`), accessible from the research environment but NOT required for bot operation.

## Constitution Compliance

| Principle | Status | Evidence |
|-----------|--------|----------|
| I. One Strategy | ✅ PASS | LLM is in `research/`, never imported by trading modules. Validator is a pure function — no strategy change. |
| II. Reward > Risk | ✅ PASS | Validator enforces `tp > sl` and net edge as `error` (hard block). LLM can propose changes but cannot apply them. |
| III. Stop Confirmation | ✅ PASS | No change to order execution. Leverage cap rule added to validator. |
| IV. Unknown = No Trade | ✅ PASS | Validator adds startup gate: unknown setting schema → no trade. Missing required setting → error. |
| V. Real Fills Only | ✅ PASS | No change to PnL computation or fill recording. |
| VI. Restart Safety | ✅ PASS | Startup validation runs before any exchange query. Failed validation → clean exit, no orphaned state. |
| VII. Simplicity | ✅ PASS | Validator is deterministic, no I/O, 100% testable. New modules are focused: schema (~150 lines), rules (~200 lines), LLM (~200 lines). All under line budget. |
| VIII. The Bot Trades | ✅ PASS | Warnings do not block trading. Only hard errors (constitutional violations, type mismatches, missing required settings) block. |
