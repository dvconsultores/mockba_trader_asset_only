# Analyze: 016 consistency check (spec ↔ plan ↔ tasks ↔ constitution)

**Date**: 2026-08-16 — run as part of the retroactive completion; the implementation already existed, so this pass checked the documents against the **code as built** rather than against intent.

| Check | Result |
|---|---|
| Spec What → plan mechanism → task | ✅ guard → M1/M2 → T001 · companion setting → M3 → T002 |
| Clarify decisions surface in code | ✅ Q1: strict `>` at [spot_scalper.py:273](../../../trading_bot/spot_scalper.py) + `test_equality_passes` · Q2: no futures_scalper change (grep-verified) · Q3: skip recorded via `_log` |
| AC ↔ test mapping | ✅ AC1 → tests 1+2 · AC2 → reason-row assertion · AC3 → test 3 + full suite · AC4 → DB value + docs (T002/T005) |
| Constitution v1.1.0 | ✅ II strengthened (stop priced against the floor), VIII preserved (skip only in the already-excluded band, recorded); no NON-NEGOTIABLE touched |
| Contradiction scan | ✅ one candidate examined: does skipping high-ATR names contradict the HF directive? No — the universe cap already excludes ATR > 1.5 at scan time; the guard closes the scan-vs-live gap, it does not narrow the intended universe |
| Guard/crash-floor interaction | ✅ `se == mlp` passes and the crash guard at `mlp` still backstops it — no dead zone, no double-trigger |
| Hidden coupling | ⚠️ noted: if the operator ever raises `sl_k_spot` or lowers `max_loss_per_position_pct`, the guard's trigger band widens silently (`mlp / sl_k_spot`). Documented in CURRENT_STATE; acceptable — the guard degrades toward *more* caution, never less |
| Test-fixture finding | ⚠️ promoted to converge deviation 1: fresh test DBs use code defaults, not production settings — the first test run passed a bracket the live config would have blocked. Fixture now pins the live values |

**Verdict**: consistent with the code as built — proceed to converge.
