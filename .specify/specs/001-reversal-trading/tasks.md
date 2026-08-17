# Tasks: 001 Reversal Trading Bot

## Phase 1 — observe mode (this implementation)

- [X] T001 `db/schema_v3.sql` — fresh six-table schema (UI-compatible) + `record_signal`/asset-list helpers in `db_ops.py`; drop migrations
- [X] T002 `trade/structure.py` — pivots, trend, zones, 3MS state machine, retest (M1)
- [X] T003 `trading_bot/reversal_judge.py` — DeepSeek client + prompt + verdict validation (M2)
- [X] T004 `bot.py` — rewrite: 30-min loop, kline fetch, engine → judge → signal → Telegram, equity cache
- [X] T005 Remove scalper code: spot_scalper, futures_scalper, regime, toxicity, universe, market_check + their tests; prune settings schema/rules
- [X] T006 Fresh DB: backup old, create v3, seed settings + NEAR/SOL/ARB/GRAM/INJ
- [X] T007 Tests: test_structure.py (incl. book misleading fixtures), test_reversal_judge.py; suite green
- [X] T008 Docs: README rewrite, fresh CHANGELOG/CURRENT_STATE, archive old specs & docs
- [X] T009 analyze.md + converge.md after suite green

## Phase 2 — execution (separate authorization)

- [ ] Spot entry on retest + exchange OCO from judge prices; risk-% sizing (M3)
- [ ] Orderly perps long/short after manual dry-run checklist (010-era procedure re-validated)
- [ ] Kill switches re-armed for live mode; monthly trade cap enforcement
- [ ] Judge A/B hook (v4-flash secondary) if operator enables

⛔ Not tasks: touching dashboard/, changing UI queries, deleting exchange-side history, backtesting harness (spec 002 candidate).
