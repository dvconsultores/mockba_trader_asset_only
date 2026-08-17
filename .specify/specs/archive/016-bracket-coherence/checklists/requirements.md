# Checklist: 016 requirements quality

- [x] Requirement traces to a measured incident (BICO 2026-08-16: −$1.57 across 3 entries, scan ATR 1.13 vs live 2.85, `se` 5.7% > 3% floor — all numbers from the live DB)
- [x] Clarify decisions recorded with rationale (Q1 skip-not-clamp, Q2 spot-only, Q3 HF impact)
- [x] HF directive respected: guard triggers only in the band the universe cap already excludes; every skip recorded (Constitution VIII)
- [x] No quality gate loosened; no threshold/stop value changed
- [x] Boundary semantics pinned (strict `>`; equality passes — ALICE-class at exactly 3.0 stays tradeable)
- [x] Counterfactual computed (day with guard ≈ −$0.13 vs −$1.70 actual)
- [x] Acceptance criteria testable (AC1–AC3 map 1:1 to the three tests; AC4 = DB value + docs)
- [x] Companion setting justified by its own incident line item (double-up −$0.62)
- [x] No migration / schema impact
- [x] Deploy interaction noted: ships in the same release window as 015; venue currently off = safe DB-push window
