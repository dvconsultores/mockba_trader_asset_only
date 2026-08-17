# Analyze: 015 consistency check (spec ↔ plan ↔ tasks ↔ constitution)

**Date**: 2026-08-16 | run before implement, per the operator's full-cycle request

| Check | Result |
|---|---|
| Every spec Part maps to plan mechanism and ≥1 task | ✅ Part 1 → M1 → T001/T002 · Part 2 → M2 → T003–T005 · Part 3 → M1 → T001 |
| Every AC maps to a test or construction argument | ✅ AC1–AC9 → T008; AC2 by construction (cache write moved behind the success branch) + reviewed in T004 |
| Clarify decisions surface in tasks | ✅ Q1 → T006/T007 · Q2 → T001 (entry-fill valuation) · Q3 → T005 (per-asset increment removed) · Q4 → T003 (constant, documented) |
| Constitution IV quote vs implementation | ✅ "5 consecutive… disable AND notify" — T003 implements both halves; the old code had neither correctly |
| Contradiction scan | ✅ none found; one tension noted: Constitution VIII vs fail-closed skips — resolved by recording `equity_unavailable` in `signals` (strictness stays measurable, VIII's own mechanism) |
| Callers not in tasks | ✅ scanner `_equity_for` verified already None-tolerant; market_check reads cache only; dashboard grep-verified independent |
| Hidden coupling risk | ⚠️ `compute_slot_size` day-cache: the first cycle after deploy computes the slot from the NEW (larger, correct) equity — intended, matches 012; noted so the operator isn't surprised by $20 slots appearing immediately |
| Out-of-scope leakage | ✅ no task touches 013/014 territory |

**Verdict**: consistent — proceed to implement.
