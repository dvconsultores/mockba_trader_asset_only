# Analyze: 001 consistency check (Phase 1)

**Date**: 2026-08-16, after implement, before converge.

| Check | Result |
|---|---|
| Spec What → plan → tasks | ✅ engine → M1/T002 · judge → M2/T003 · loop → T004 · removal → M5/T005 · fresh DB → M4/T006 |
| Clarifications surface in code | ✅ Q1: only 1d/4h/1h fetched · Q2: engine deterministic, judge fail-closed, reasoning persisted · Q3: `_last_judged` keyed to the closed 4h candle · Q4: per-venue klines with fallback (operator mid-implementation directive honored) · Q5: schema v3 keeps the six dashboard tables · Q6: no order path exists in observe mode — grep confirms no `place_entry` caller in bot.py · Q9: concurrency settings seeded for Phase 2 |
| Book fidelity | ✅ criterion order enforced (`ORDER_VIOLATION`), criterion-3 bound enforced (`BOUND_VIOLATION`); both misleading diagrams (pp. 35/37) are failing fixtures; zones are areas with tolerance, not lines; R:R recomputed from prices, never trusted from the model |
| UI contract | ✅ dashboard imports nothing from removed modules; both dashboard tests green; `SELECT *` on signals still valid (columns added, none removed) |
| Fail-closed inventory | ✅ judge: no key / API error / malformed after retry / incoherent prices → None → `skipped` row; candles unavailable → warn + skip; spot shorts → `short_not_possible_on_spot` |
| Smoke on live data | ✅ fresh DB seeded (20 settings, 5 assets × 2 venues); NEAR/SOL/GRAM evaluated against real Binance candles; state rows recorded; no judge call without a candidate |
| Hidden couplings | ⚠️ `upsert_setting` writes `updated_at` — schema v3 keeps the column (caught during implement) · ⚠️ Orderly kline auth is IP-locked to the server — the fallback path is what runs locally; native path first exercised on deploy (watch item) |

**Verdict**: consistent — proceed to converge.
