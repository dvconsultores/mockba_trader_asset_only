# Specification Quality Checklist: Spot Exit Hardening (006)

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-12
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) — *Pass with note: the spec names affected files/functions per the user-mandated 005 convention (explicitly requested), but contains no code, algorithm design, or framework/API choices beyond what already exists.*
- [x] Focused on user value and business needs — worst-case per-position loss is capped; week-wiping gaps are prevented; trade frequency preserved.
- [x] Written for non-technical stakeholders — *Pass with note: 005-convention file references retained per explicit user request; narrative (What/Why) is stakeholder-readable.*
- [x] All mandatory sections completed — What, Why, Resolved decisions, Layout (affected files), Scope, Constraints (constitution compliance), Assumptions, Acceptance criteria.

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain — none were introduced; all defaults were specified by the user or have reasonable documented defaults.
- [x] Requirements are testable and unambiguous — each Part maps to testable acceptance criteria (AC1–AC12).
- [x] Success criteria are measurable — ACs use concrete thresholds (1.5%, 3.0%, BICO 2.1% vs PUMP 0.6%/MMT 0.87%), ordering guarantees, and observable behaviors.
- [x] Success criteria are technology-agnostic (no implementation details) — *Pass with note: ACs reference behavior (floor breach → cancel + market-sell, exit_reason) rather than code structure; file names appear only as affected-file scope, per 005 convention.*
- [x] All acceptance scenarios are defined — 12 acceptance criteria covering cap, guard, ordering, fail-closed, real-fill, validation, docs.
- [x] Edge cases are identified — missing live price (None) → no action; position already closed by exchange fill → no phantom double-close; guard vs SL interplay; ATR-source selection pinned in planning.
- [x] Scope is clearly bounded — In scope / Out of scope sections; futures path, executor, entry logic, DB schema explicitly excluded.
- [x] Dependencies and assumptions identified — Assumptions section (local settings management, ATR source pinned in planning, floor wider than SL, spot-only).

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria — each Part 1/2/3 behavior maps to ≥1 AC.
- [x] User scenarios cover primary flows — crash-through-stop scenario, high-ATR universe exclusion, normal-exit invariance, unknown-price no-action.
- [x] Feature meets measurable outcomes defined in Success Criteria — worst-case loss capped at ~3% (vs observed −44.35%); BICO-class names removed without touching PUMP/MMT.
- [x] No implementation details leak into specification — no code, no new modules, no API design; only existing-function placement per 005 convention.

## Notes

- Items marked incomplete require spec updates before `/speckit.clarify` or `/speckit.plan`.
- **Open item for planning (not a spec gap)**: the exact ATR measure for the universe cap (24h high–low range vs replay `atr_pct_median`) is explicitly pinned during the plan phase; the 1.5 default is calibrated against `atr_pct_median` (see Assumptions + Layout §1).
- All items pass; spec is ready for `/speckit.clarify` or `/speckit.plan`.
