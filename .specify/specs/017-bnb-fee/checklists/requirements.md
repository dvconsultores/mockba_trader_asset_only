# Checklist: 017 requirements quality

- [x] Requirement traces to a measured cost line (fee hurdle = 20–35% of a typical TP move in the 132-entry study; discount adds 0.05%/trade expectancy)
- [x] Clarify decisions recorded with rationale (Q1 ticker valuation + fallback, Q2 reserve excluded from equity, Q3 warn-never-block, Q4 coupled rate change + operator sequencing, Q5 mid-test change ruled: gate tracks reality)
- [x] HF directive respected: cost gate loosens, frequency can only rise; nothing new skipped
- [x] Constitution V failure mode identified BEFORE it happened live (BNB commission valued at traded asset's price ≈ 1000× error; sellable corruption)
- [x] All three fill-parsing sites enumerated (entry, market sell, order-fills lookup) — partial coverage would corrupt exactly one exit path
- [x] Failure modes have detectors, not assumptions (startup reserve < $2; runtime commission-asset mismatch)
- [x] Acceptance criteria testable (AC1–AC6 map to named tests; AC7 = DB values + suite)
- [x] No migration / schema impact; futures untouched
- [x] Deploy sequencing pinned: Binance toggle + BNB reserve BEFORE db push + restart
- [x] 016 lesson applied: test fixture pins production settings
