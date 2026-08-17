# Converge: 001 Reversal Trading Bot — Phase 1

**Date**: 2026-08-16 | **Status**: Phase 1 implemented — **46/46 tests green**

## Acceptance — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 Engine fixtures | ✅ | 12 tests: valid short + mirrored long CONFIRMED with retest; book misleading variants (pp. 35/37) and criterion-order violations rejected; pivots alternate; zones cluster |
| 2 Judge fail-closed | ✅ | 8 tests: parse, fenced JSON, R:R recomputed-and-gated, incoherent prices → None, malformed retry → None, API error → None, missing key → no call |
| 3 Bot loop | ✅ | Live smoke: 3 assets evaluated on real candles, transitions recorded, judge correctly not called (no CONFIRMED+retest present); no order path exists |
| 4 Fresh DB + UI | ✅ | v3 created, 20 settings + 5 assets × 2 venues seeded; both dashboard test suites green, dashboard imports verified |
| 5 Suite + docs | ✅ | 46 green; README/CHANGELOG/CURRENT_STATE rewritten; scalper docs and specs 000–017 archived |

## Deviations

1. **Mid-implementation operator directives absorbed**: per-venue klines
   (Orderly native + fallback) and the concurrency policy (Q9: one position
   per asset, `max_concurrent_positions=2`, best confidence×RR first) were
   added to spec/plan/loop during the build.
2. **BNB startup-reserve check dropped with the old bot.py** — meaningless in
   observe mode (no fees paid); returns in Phase 2 with the execution path.
   Executor fee accounting and its 8 tests kept intact.
3. **Orderly native klines untested locally** (auth IP-locked to server —
   correct security). First live exercise happens on deploy; the Binance
   fallback keeps analysis running regardless. Watch item on first server logs.

## Operator deploy sequence

1. Add `DEEPSEEK_API_KEY=...` to the **server** `.env`.
2. Commit → push `main` → CI builds → Watchtower pulls.
3. Stop docker → `push-db.sh` (ships the fresh v3 DB) → start.
4. Healthy startup: `[STARTUP] reversal bot (spec 001) — trade_mode=observe`
   → `[LOOP] entering main loop`; dashboard shows the 5-asset universe and
   signals accumulating as `observe` state rows.

## Measurement window (before Phase 2)

~2–4 weeks of observe mode. Gate for authorizing execution: enough confirmed
signals to judge quality (target ≥5), with hit-rate vs subsequent price
consistent with the R:R ≥ 2.5 / WR ≥ 40% expectancy the method promises.

## Queue

Phase 2 execution spec · Orderly manual dry-run checklist · judge A/B
(v4-flash) · backtest harness (spec 002) · constitution refresh.
