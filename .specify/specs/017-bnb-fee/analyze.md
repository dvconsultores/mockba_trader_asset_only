# Analyze: 017 consistency check (spec ↔ plan ↔ tasks ↔ constitution)

**Date**: 2026-08-16 — run after implement, before converge.

| Check | Result |
|---|---|
| Spec What → plan mechanism → task | ✅ conversion → M1 → T002 · sellable → M2 → T003 · detectors → M4 (T004) + M1's warning (T002) · setting/validator → M5/M6 → T001/T005/T007 |
| Clarify decisions surface in code | ✅ Q1: `get_price("BNB")` + `notional × rate/200` fallback in `_fee_to_usdt` · Q2: `get_equity` untouched (grep-verified) · Q3: both detectors warn-only, no trading block anywhere · Q4: DB carries 0.15+true, operator sequence in converge · Q5: annotation noted in CURRENT_STATE |
| AC ↔ test mapping | ✅ AC1 → `test_entry_bnb_commission_full_sellable` · AC2 → base-asset regression tests (2) · AC3 → ticker-failure fallback test · AC4 → mismatch caplog test (+ negative test) · AC5 → startup check is 5 lines in bot.py, exercised manually (see deviation 1) · AC6 → 3 validator tests · AC7 → DB values verified + 133 green |
| Constitution v1.1.0 | ✅ II: gate now prices the actual 0.15 cost; V: BNB fees recorded from actual fills at BNB's own price; VIII: gate loosens ⇒ frequency can only rise; VII: +~35 lines (pre-existing overrun, spec 014) |
| Contradiction scan | ✅ candidate examined: does excluding the BNB reserve from equity contradict 015's "equity = everything at the venue"? No — 015 defines equity as *deployable* capital (USDT + positions); the reserve is consumable, not deployable, and including it would inflate the daily-loss-limit base by ~5% |
| All fill paths covered | ✅ grep for `commission` in executor: three sites, all route through `_fee_to_usdt`; Orderly paths untouched (`total_fee` is already USDC) |
| Hidden coupling | ⚠️ noted: `_fee_to_usdt`'s fallback reads `cex_round_trip_fee_pct` — if the operator later reverts the discount but not the setting, the estimate degrades with it. Acceptable: the validator cross-check (M5) warns on exactly that mismatch |
| Hygiene fix separation | ✅ payoff-validator fix is its own CHANGELOG entry, tested in the 017 test file for convenience but independent of every BNB mechanism |

**Deviation candidate**: AC5 (startup reserve warning) has no automated test —
it lives in `bot.py run()` before the main loop, which no test harness drives.
Verified by inspection (5 lines, warn-only, all calls null-safe). Promoted to
converge as deviation 2.

**Verdict**: consistent — proceed to converge.
