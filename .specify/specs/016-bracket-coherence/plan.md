# Plan: Bracket Coherence Guard

**Feature**: 016-bracket-coherence | **Date**: 2026-08-16 | **Spec**: `specs/016-bracket-coherence/spec.md`
**Status**: Implemented 2026-08-16 — 120/120 tests green *(plan written retroactively at operator request; documents what was built)*
**Branch**: `main`

## Summary

One guard in `spot_scalper.scalp_cycle`, placed directly after the Constitution
II cost gate: if the effective stop `se` exceeds `max_loss_per_position_pct`,
skip with reason `sl_exceeds_crash_floor` (strict `>` — equality is coherent).
Companion DB setting `max_slots_cex` 2 → 1. Total: ~9 lines of guard + comment,
one setting, three tests.

## Constitution Check

| Principle | Compliance |
|---|---|
| **II** (v1.1.0) | ✅ Strengthened in spirit: the cost gate prices `te` against fees; this prices `se` against the disaster floor. An entry whose stop cannot fit under the floor carries a loss the entry economics never accounted for. |
| I | ✅ No new signal source — a pure consistency test between two existing risk numbers. |
| III / IV / V / VI | ✅ Entry-side only; no order, fill, or state path touched. |
| VII | ✅ +9 lines (pre-existing overrun, spec 014). |
| **VIII** | ✅ Fires only when live ATR > `mlp / sl_k_spot` (= 1.5% at current settings) — the band `universe_max_atr_pct` already intends to exclude, so no *intended* trade is lost; every skip recorded with its reason. |

## Pinned mechanisms

- **M1 — Placement** ([spot_scalper.py:266-274](../../..//trading_bot/spot_scalper.py)):
  after the cost gate (both need `te`/`se` from `compute_thresholds`), before
  cooldown/spacing — so a skip is attributed to coherence, not misread as a
  cooldown. Runs before `direction` is known, matching the cost gate's shape
  (direction `None` in the log row, like `tp_eff below cost+edge`).
- **M2 — Skip, never clamp** (clarify Q1): clamping would re-tighten stops on
  exactly the highest-ATR names — the configuration the 113-entry study
  measured as the worst. The guard treats "stop can't fit under the floor" as
  "asset untradeable right now", the live-time analogue of the scan-time
  universe cap.
- **M3 — Companion `max_slots_cex=1`**: DB-only; blocks the double-up pattern
  (BICO re-entered 4 min after a stop-out). The global 4-slot cap and spacing
  rule are untouched, so venue-level capacity is unchanged.

## Incident evidence (why this exact guard)

BICO 2026-08-16: scan-time ATR median 1.13 (passed the 1.5 universe cap,
ranked #3), live ATR 2.04→2.85 during an alt dump ⇒ `se` = 4.1–5.7% vs the 3%
crash floor. Three entries, −$1.57 = 92% of the day's −$1.70. With the guard,
all three are skipped and the day closes ≈ flat (−$0.13). The guard is the
*live* enforcement of the constraint the universe cap only checks at scan time.

## Testing

`tests/test_bracket_coherence.py` — 3 tests: the BICO case (ATR 2.85 ⇒ skip,
reason recorded, no order), the equality boundary (ATR 1.5 ⇒ `se` 3.0 == floor
⇒ trades), and the normal case (ATR 0.7 ⇒ unaffected). **The fixture seeds the
live production settings** (`sl_k_spot=2.0` etc.) — see converge deviation 1.

## Out of scope

Futures coherence (crash floor is a spot mechanism; DEX off); any change to
`universe_max_atr_pct` semantics; clamping variants; the 5-day test protocol
(operational, recorded in the session log and converge).
