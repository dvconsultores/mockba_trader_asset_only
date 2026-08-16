# Checklist: 015 requirements quality

- [x] Every requirement traces to a measured defect (audit items 3, 4, 12 — all observed in code, two reproduced in DB data: cached equity $9.88 vs ~$50 account)
- [x] Every clarify question resolved with a recorded rationale (Q1–Q4)
- [x] NON-NEGOTIABLE principle identified and quoted (Constitution IV, incl. the notify clause)
- [x] Fail-closed direction stated for every unknown-state branch (None ⇒ skip/streak, never 0)
- [x] No requirement loosens a quality gate (HF directive respected — AC3/AC4 restore intended capacity)
- [x] All `get_equity` call sites enumerated with per-site handling (plan inventory, 6/6)
- [x] Acceptance criteria are independently testable (AC1–AC9 map 1:1 to tests; AC2 by construction + review)
- [x] Out-of-scope list prevents scope creep into 013/014 territory
- [x] No migration / schema impact confirmed
- [x] Deploy risk considered (in-memory streak resets on restart — safe direction, noted in plan VI)
