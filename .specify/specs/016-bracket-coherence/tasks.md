# Tasks: Bracket Coherence Guard (016)

**Prerequisites**: spec ✅ (Q1–Q3), plan ✅, checklist ✅, constitution v1.1.0 ✅
*(Written retroactively at operator request; all tasks were executed 2026-08-16.)*

- [X] T001 Guard in `trading_bot/spot_scalper.py` (now lines 266–274), directly after the Constitution II cost gate: `mlp = get_setting_float("max_loss_per_position_pct", 3.0)`; `if se > mlp:` → `_log(..., "skipped", "sl_exceeds_crash_floor")` + return. Strict `>` (clarify Q1); direction `None` in the row, matching the cost-gate shape.
- [X] T002 Companion setting: `upsert_setting("max_slots_cex", "1")` in the local DB (backup taken first: `data/trading.db.bak-20260816-*`). Global `max_concurrent_positions=4` untouched.
- [X] T003 `tests/test_bracket_coherence.py` — `test_incoherent_bracket_skipped` (BICO case, ATR 2.85: no order + reason row), `test_equality_passes` (ATR 1.5 ⇒ se == floor ⇒ trades), `test_normal_atr_unaffected` (ATR 0.7). Fixture seeds the live production settings — see converge deviation 1.
- [X] T004 Full regression: **120 passed** (117 + 3 new).
- [X] T005 Docs: CHANGELOG `fix:` (2026-08-16) + CURRENT_STATE `## 0.` feature-016 section.
- [X] T006 Retroactive speckit completion (this document set) + analyze + converge.

⛔ **Not tasks**: clamping `se`; touching futures; changing `universe_max_atr_pct`, thresholds, or any other setting; deploying (operator action: commit → push → Watchtower → `push-db.sh` → re-enable venue).
