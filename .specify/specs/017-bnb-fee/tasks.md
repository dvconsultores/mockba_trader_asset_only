# Tasks: BNB Fee Discount (017)

**Prerequisites**: spec ✅ (Q1–Q5), plan ✅, checklist ✅

- [X] T001 `trade/settings_schema.py`: add `cex_fee_bnb` (bool, risk group, default absent ⇒ false, related `cex_round_trip_fee_pct`).
- [X] T002 `trading_bot/executor.py` (BinanceSpot): `_fee_to_usdt` helper (M1) + runtime mismatch warning; import `get_setting_float`.
- [X] T003 Apply at the three sites (M2/M3): `place_entry` (sellable condition + conversion), `market_sell`, `get_order_fills`.
- [X] T004 `bot.py`: startup BNB reserve check (M4), after executor construction, warn-only.
- [X] T005 `trade/settings_rules.py`: `cex_fee_bnb` ↔ `cex_round_trip_fee_pct` cross-check (M5).
- [X] T006 `tests/test_bnb_fee.py`: helper units, place_entry integration (BNB + base regression), mismatch caplog, validator checks. Fixture pins production settings.
- [X] T007 DB (backup first): `cex_fee_bnb=true`, `cex_round_trip_fee_pct=0.15`.
- [X] T008 Full regression suite green.
- [X] T009 Docs: CHANGELOG, CURRENT_STATE §0, analyze.md, converge.md (incl. operator deploy sequence + 5-day-test annotation).

⛔ **Not tasks**: touching futures paths; auto-buying BNB; counting the reserve in equity; changing tp/sl/edge settings; deploying (operator: Binance toggle + BNB purchase → docker stop → push-db → start).
