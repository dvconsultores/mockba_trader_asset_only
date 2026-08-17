# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).
Scalper-era history: `docs/archive/CHANGELOG-scalper-era.md`.

## 2026-08-16

- `feat!:` **Founding rewrite — reversal trading bot (spec 001).** The scalper
    era ends on live evidence (24 trades, 46% WR vs ~75% breakeven). New core:
    deterministic 3MS structure engine (`trade/structure.py`, unit-tested
    against the book's valid and misleading patterns), DeepSeek v4-pro judge
    (`trading_bot/reversal_judge.py`, thinking on, fail-closed, reasoning
    logged), 30-min cycle over the operator asset table (NEAR, SOL, ARB, GRAM,
    INJ), per-venue candles (Binance public REST; Orderly native klines with
    fallback), observe mode (signals + Telegram, no orders). Fresh schema v3 —
    six tables kept name/shape-compatible so the dashboard runs unchanged;
    signals gains structure-packet and AI-verdict columns. Removed: scalper
    modules (spot/futures scalpers, regime, toxicity, universe scanner, market
    gate, settings validator), their tests and settings; migrations; old DB
    (operator decision — exchange history is the archive). Kept: executor
    (orders/fills/BNB fee accounting), Telegram, dashboard, deploy pipeline,
    kill-switch groundwork for Phase 2. Specs 000–017 and scalper docs
    archived. Suite: 46 green.
