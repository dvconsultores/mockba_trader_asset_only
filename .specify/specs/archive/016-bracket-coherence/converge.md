# Converge: Bracket Coherence Guard (016)

**Date**: 2026-08-16 | **Status**: Implemented — 6/6 tasks, **120/120 tests green** (117 + 3)
**Cycle**: specify → clarify (Q1–Q3) → plan → checklist → tasks → analyze → implement → converge ✅ *(plan/checklist/tasks/analyze completed retroactively at operator request; verified against the code as built)*

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 Incoherent bracket skipped; equality passes | ✅ | `test_incoherent_bracket_skipped` (ATR 2.85 ⇒ no order), `test_equality_passes` (se 3.0 == floor ⇒ trades) |
| 2 Skip recorded with reason | ✅ | Same test asserts the `sl_exceeds_crash_floor` signals row (Constitution VIII) |
| 3 Normal entries unaffected; suite green | ✅ | `test_normal_atr_unaffected`; full suite 120 |
| 4 `max_slots_cex=1` + docs | ✅ | DB value set (backup taken); CHANGELOG + CURRENT_STATE §0 |

## Deviations

1. **Test fixture had to pin production settings.** The first run of
   `test_incoherent_bracket_skipped` FAILED — not because the guard was wrong,
   but because a fresh test DB uses code defaults (`sl_k=0.6` ⇒ se 1.71 < 3),
   so the "incoherent" bracket was coherent under defaults. The fixture now
   seeds the live configuration (`sl_k_spot=2.0`, `sl_min_pct_spot=1.5`,
   `tp_k=1.2`, `mlp=3.0`). **Lesson worth keeping**: any test of a guard that
   *interacts* with tunables must pin the production values, or it tests a
   configuration nobody runs. Pre-existing suites mostly dodge this by testing
   mechanisms rather than thresholds — future threshold-dependent tests should
   follow this fixture's pattern.
2. **Compact-first, ceremony-second.** As with 011, the code shipped hours
   before the full document set. The operator's completion request is now
   standing precedent: every spec ends with the full set, even when written
   retroactively and labeled as such.

## Counterfactual (the number that justifies the feature)

Replaying 2026-08-16 with the guard: BICO's three entries (live ATR 2.04–2.85 ⇒
se 4.1–5.7% > 3%) are all skipped ⇒ day PnL ≈ **−$0.13 instead of −$1.70**.

## Remaining work

- **Operator deploy** (the venue-off window is ideal): commit → push `main` →
  Watchtower → `push-db.sh` (carries `max_slots_cex=1`) → re-enable
  `auto_trade_binance`. Until re-enabled, the 4 open positions are OCO-protected
  only (crash guard / time stop / DB recording suspended).
- **5-day stability test** (operator decision, 2026-08-16): one frozen
  configuration, no mid-test changes barring safety defects. Day-5 gates:
  enforce 009 if the A/B holds · revisit `tp_k` (confirmed-arm grid hinted
  1.5× ≥ 1.2×) · then spec 013.
- **Watch item**: `sl_exceeds_crash_floor` skip count. Zero over 5 days means
  the universe cap is holding and the guard is pure insurance; a large count
  means scan-time ATR is chronically stale and 013's live-data work should
  absorb a universe refresh.
- Queue unchanged: 013 loop latency · 014 VII re-baseline · audit items 5, 8–11
  · futures `fee_entry` / dangling-TP (010 leftovers).
