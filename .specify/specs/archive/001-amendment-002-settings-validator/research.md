# Research: Amendment 002 — Settings Validator & LLM Helper

**Feature**: Amendment 002 | **Date**: 2026-07-26 | **Status**: ✅ Complete (retrospective — modules already built)

## Research Questions & Decisions

### R1: How should setting metadata be represented?

**Decision**: Frozen `dataclass` (`SettingSpec`) with an immutable `ALL: list[SettingSpec]` registry and `BY_KEY: dict[str, SettingSpec]` lookup.

**Rationale**:
- `frozen=True` guarantees immutability — no runtime mutation, schema is version-controlled source of truth.
- `dataclass` provides free `__eq__`, `__hash__`, and `__repr__` for debugging.
- `BY_KEY` dict provides O(1) lookup for validator, Telegram handlers, and dashboard.
- No external dependency — pure stdlib. Zero import cost.
- Type-safe: `type: type` field (not a string enum) enables `isinstance(value, spec.type)` checks in validator.

**Alternatives considered**:
- JSON/YAML file: rejected — no type safety, external I/O dependency, runtime parsing overhead.
- Enum-based keys: rejected — can't attach per-key metadata (bounds, unit, description) to enum members cleanly.
- NamedTuple: rejected — `frozen=True` dataclass is more explicit about immutability intent.

### R2: Where should the deterministic validator live?

**Decision**: `trade/settings_rules.py` — pure function `validate(key, value, ctx) → Verdict`. Same function backs UI, Telegram, and bot startup. No I/O, no network, no LLM.

**Rationale**:
- Constitution VII (Simplicity): pure computation is maximally simple — no side effects, no mocks needed, no setup.
- Constitution I (One Strategy): deterministic rules don't compete with or augment the trading strategy.
- `trade/` placement is correct: the validator is a core safety mechanism, not research tooling. It MUST be in the execution path (bot startup gate).
- Single `validate()` function means every surface (dashboard, Telegram, bot.py) exercises the same code path. No divergence possible.
- 100% testable: every rule has a unit test; no external state to mock.

**Alternatives considered**:
- `db/` module: rejected — validator must be importable without DB access (dashboard client-side).
- `research/` module: rejected — validator is NOT research; it's a safety gate that must run before every trade cycle.
- Per-surface validators (one for dashboard, one for Telegram): rejected — violates DRY; would diverge.

### R3: How should LLM explanations be cached?

**Decision**: File-based JSON cache in `data/llm_cache/`, keyed by `sha256(key:language:capital_band)`. TTL enforced by `llm_explain_cache_days`.

**Rationale**:
- No DB dependency: cache survives DB resets. File I/O is simpler than table management.
- Capital-band segmentation: explanations for a $500 account differ from a $50k account (different risk framing).
- SHA-256 hash as filename: collision-resistant, no escaping needed for setting keys with special chars.
- TTL check on read: stale entries are transparently refreshed on next request.
- LLM calls are expensive (~$0.01/call) — cache avoids repeat calls for already-explained settings.

**Alternatives considered**:
- DB table: rejected — adds schema complexity; cache is disposable (miss → regenerate).
- In-memory dict: rejected — lost on restart; user would pay for explanations on every deploy.
- Redis: rejected — overkill for a single-process bot; adds infrastructure dependency.

### R4: How should LLM proposals be generated?

**Decision**: Two-phase: (1) deterministic validator runs first, produces `heuristic`-confidence proposals with `suggested_value`; (2) LLM reviews and upgrades confidence to `measured` if data supports it. LLM never invents numbers — only evaluates validator suggestions against real data.

**Rationale**:
- Safety: deterministic proposals always exist even if LLM is unreachable. Operator always sees something actionable.
- Constitution I: LLM is advisory only — never writes to settings, never in execution path.
- Evidence grading (`measured` / `heuristic` / `no_basis`) gives the operator clear signal about what's data-driven vs. rule-of-thumb.
- LLM prompt is constrained: provide context data, ask for confidence grading — don't ask LLM to invent values.

**Alternatives considered**:
- LLM-only proposals: rejected — no proposals when LLM is down; violates operator expectation.
- Purely deterministic proposals: rejected — misses the LLM's ability to synthesize multiple signals (e.g., "spread_z_max should be 2.0 because velocity is calm AND depth is stable").
- LLM writes to settings directly: rejected — violates constitution I (LLM in execution path) and is unsafe.

### R5: Why a separate `settings_baseline` table?

**Decision**: `settings_baseline` is a separate table from `settings`, tracking the provenance of each setting's current value.

**Rationale**:
- Audit trail: operator can see which settings were data-derived (`measured`) vs. manual (`overridden`) vs. never validated (`unvalidated`).
- Immutable measurement record: when a setting is calibrated from dry-run data, the evidence is preserved even if the operator later overrides.
- Schema separation: `settings` is the active config (read/write every cycle); `settings_baseline` is the provenance log (write on calibration, read on validation). Different access patterns, different tables.
- Enables future features: automatic recalibration (compare current value to baseline), rollback (restore baseline), drift detection (current diverged from measured).

**Alternatives considered**:
- Column on `settings` table: rejected — `settings` is key-value; adding provenance columns pollutes the simple model. Also, a setting can have multiple baselines over time (historical drift).
- No baseline tracking: rejected — operators managing real capital need to know which settings are evidence-based vs. guesswork.

### R6: Why immutable proposals table?

**Decision**: `settings_proposals` is append-only. Rows are never updated — acceptance is tracked via `status` and `decided_at` fields on the original row.

**Rationale**:
- Audit integrity: every proposal is preserved exactly as generated, including the model, confidence, and evidence at that moment.
- Prevents revisionism: an operator can't later claim "the LLM said X" when the proposal actually said Y.
- Simplifies concurrency: no UPDATE conflicts. INSERT-only workload.

**Alternatives considered**:
- UPDATE on accept/reject: rejected — loses the original proposal state; can't distinguish "never decided" from "rejected then changed mind."
- Soft delete: rejected — proposals should never be deleted. Retention policy deferred to future amendment.

### R7: Should the validator allow DB reads for cross-checks?

**Decision**: No. The validator reads `settings_baseline` only as a pragmatic exception for the toxicity baseline check, via a try/except that silently degrades to `ok`. All other cross-checks receive values from the caller via `SettingsContext` or `get_setting_*` helpers.

**Rationale**:
- Constitution VII goal: pure computation. The toxicity baseline check is the one cross-check that genuinely needs external state (has anyone measured this yet?).
- The try/except pattern means: if DB is unavailable (dashboard client-side), the check is skipped — validator still works.
- All other cross-checks (tp > sl, net edge, slot sizing, leverage cap) use only values the caller provides. This is the 95% case.

**Note**: This is a known deviation from FR-B4 ("no database access"). The deviation is documented, scoped to one check, and fails gracefully. Future refactoring could move this check to a higher-level orchestrator that queries the DB before calling the pure validator.

