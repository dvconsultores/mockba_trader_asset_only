# Feature Specification: 016 — Bracket Coherence Guard

**Feature Branch**: `016-bracket-coherence` | **Created**: 2026-08-16
**Status**: Implemented 2026-08-16 — 120/120 tests green. Full speckit set completed at operator request (plan/checklist/tasks/analyze written retroactively; they document what was built).
**Flow**: specify → clarify → plan → checklist → tasks → analyze → implement → converge ✅
**Constitution**: v1.1.0 — Principle II/VIII compliant; closes the live-ATR gap behind the 2026-08-16 BICO incident.

## What

One entry guard, **spot only**: if the effective stop distance exceeds the
crash floor (`se > max_loss_per_position_pct`), the entry is skipped and
recorded with reason `sl_exceeds_crash_floor`.

## Why — the BICO incident (2026-08-16)

`universe_max_atr_pct` (1.5) checks the **scan-time** ATR median only. BICO
passed at 1.13, then its live ATR spiked to 2.04→2.85 during an alt dump. The
adaptive stop computed 2×ATR = 4.1–5.7% — **beyond the 3% crash floor**, so the
bracket was incoherent: the crash guard was always going to fire first, at a
loss the entry economics never priced. The bot entered it three times
(−$1.57 = 92% of the day's −$1.70). With this guard, all three entries are
blocked and the day closes ≈ flat.

## Clarifications

### Session 2026-08-16

- **Q1 — Skip, not clamp.** Clamping `se` to the floor would re-tighten stops on
  exactly the volatile names the wide-stop study warned about. An asset whose
  honest stop cannot fit under the disaster floor is untradeable *right now* —
  same principle as the universe ATR cap, enforced live. Equality passes
  (`se == mlp` is coherent; ALICE at 2.79 vs 3.0 stays tradeable).
- **Q2 — Spot only.** The crash floor (006) is a spot mechanism; futures has
  mandatory exchange stops and DEX is off. Futures coherence is a future spec.
- **Q3 — HF impact ≈ nil**: only fires when live ATR > `mlp / sl_k_spot` (= 1.5 at
  current settings) — the same band the universe cap already intends to exclude.
  Every skip is recorded (Constitution VIII).

## Companion setting (same deploy)

`max_slots_cex` 2 → **1**: today the bot doubled into falling BICO 4 minutes
after its first stop-out (−$0.62). One position per asset while capital is $100;
the 4-slot global cap is untouched.

## Acceptance

1. `se > max_loss_per_position_pct` ⇒ skipped, reason `sl_exceeds_crash_floor`,
   no order; `se == mlp` passes.
2. Recorded in `signals` with the usual context (Constitution VIII).
3. Normal-ATR entries unaffected; full suite green.
4. DB: `max_slots_cex=1`. Docs: CHANGELOG + CURRENT_STATE.
