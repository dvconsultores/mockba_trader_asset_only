# CHANGELOG

Convention: `type: short description` (per `how-to-work-with-specs.md`).
Scalper-era history: `docs/archive/CHANGELOG-scalper-era.md`.

## 2026-08-17

- `fix:` **Orderly kline circuit breaker.** Live logs showed connect
    timeouts to api.orderly.org (10s each × 4 assets × 3 timeframes per
    cycle). After 3 consecutive kline failures the bot now skips
    Orderly-native fetches and uses the Binance fallback (spec 001 Q4)
    directly for 10 minutes, then probes again; one warning per trip
    instead of one per call. Analysis never stops. 4 new tests (53 green).
- `fix:` **Orderly listing sync at startup.** First live cycle showed GRAM
    500s from Orderly's kline endpoint — `PERP_GRAM_USDC` doesn't exist
    (verified via `/v1/public/info`: NEAR/SOL/ARB/INJ listed, GRAM/TON not).
    New `sync_orderly_listings()` runs at startup: assets with no Orderly
    perp are blacklisted on the orderly venue only (Binance analysis
    unaffected), killing the per-cycle warning spam and preventing
    untradeable DEX signals. One-way: never auto-clears operator-set
    blacklist flags; endpoint failure keeps current state. 3 new tests
    (49 green).

## 2026-08-16

- `feat:` **Capital-based sizing (spec 001 Q10, operator directive).** The
    book's 1–1.5%-of-equity risk formula is dropped: venue capital is 100%
    working capital, so Phase 2 sizing = `position_size_pct` of available
    venue capital per position (default 45%; 2 slots deploy 90%, ~10% loss
    buffer — operator amendment), Orderly notional = margin × `dex_leverage`
    (default 3x, e.g. $20 → $60). The committed capital is fully at risk; the
    structural stop bounds the loss. Phase 2 pre-order guards recorded:
    venue min-notional (Binance/Orderly) + spread sanity check; order book
    otherwise unused by design. Telegram signals tag the venue explicitly
    (`binance · CEX spot` / `orderly · DEX perp`). `risk_pct` removed from
    settings/DB. `max_trades_per_month=10` and kill switches unchanged.
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
