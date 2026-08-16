# Tasks: Frequency Recovery (012)

**Prerequisites**: spec.md ✅ (clarified — operator capital plan, Q1/Q2), plan.md ✅, constitution v1.1.0 ✅

Settings-only feature — tasks are DB operations plus verification; no source files.

- [X] T001 Back up the local DB before each change (`data/trading.db.bak-20260816-103446`, `-...` for the $100 revision).
- [X] T002 `upsert_setting("max_concurrent_positions", "4")` — was 2.
- [X] T003 `upsert_setting("cex_slot_pct", "20")` — was 40 (interim 25 superseded same-day by clarify Q1).
- [X] T004 `upsert_setting("capital_cex_usdt", "100")` — was 50; declared pool matches funded capital.
- [X] T005 Validate all three via `trade.settings_rules.validate` → `ok` / `ok` / `ok`.
- [X] T006 Full test suite (tmp-DB isolated, so unaffected by design) re-run green: **107 passed**.
- [X] T007 Docs — CURRENT_STATE feature-012 section + CHANGELOG `ops:` entry (updated to the $100/20% plan); audit item #12 (`get_equity` USDT-only) recorded in both.
- [X] T008 Deploy note recorded: slot-size day-cache means the new percentage applies at the deploy restart; DB push follows the image (established order).

⛔ **Not tasks**: touching thresholds/cooldowns/filters (quality gates frozen per the HF directive); fixing audit #12 (belongs to 015); changing `dex_slot_pct` (DEX off).
