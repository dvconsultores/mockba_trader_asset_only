# MockbaV4 — Reversal Trading Bot

A crypto reversal-trading bot for Binance spot (CEX) and Orderly perps (DEX),
built on the **3MS principle** (3 Market Structure): trade only validated
trend reversals at key support/resistance areas, enter on the retest, risk a
fixed ~1% of equity per trade, and demand reward:risk ≥ 1:2.5.

Founded 2026-08-16 (spec `001-reversal-trading`), replacing the scalper era
after live evidence showed its payoff profile was structurally unwinnable
(46% win rate against a ~75% breakeven requirement). The reversal profile
breaks even at 28.6%.

## How it works

```
every 30 min, per asset (operator table, NEAR first: NEAR SOL ARB GRAM INJ)
  1. fetch 1d / 4h / 1h candles  — Binance public REST (CEX),
                                   Orderly native klines (DEX, Binance fallback)
  2. deterministic structure engine (trade/structure.py)
       swing pivots → trend → key zones → 3MS state machine → retest detection
  3. if a CONFIRMED candidate is retesting the broken neckline:
       DeepSeek v4-pro judge (reasoner, thinking on) verifies every criterion
       and prices the trade: entry / stop / target / R:R / confidence
  4. every evaluation recorded in `signals` (structure packet + AI verdict +
       full reasoning); valid signals notified via Telegram
```

**The AI is a judge, not an oracle**: the deterministic engine owns all
structure math (unit-tested against the book's valid *and* misleading
patterns); the judge verifies a checklist and fails closed on any doubt.

### The 3MS reversal (uptrend → downtrend; mirrored for long)

1. Price at a key S/R **area** (≥2 prior touches) — compulsory.
2. Market-structure change, in order: failure to make a higher high (first),
   break of the last low of the uptrend (the neckline), two lower highs with
   the second not above the old last low — compulsory.
3. A reversal trigger candle (engulfing / pin bar / double top-bottom).

Entry on the neckline **retest** (never chase the break), stop beyond the
structural point, target at the next key zone.

## Modes

- `trade_mode = observe` (current): signals + Telegram only, **no orders**.
  Signal hit-rate is measured before any capital is risked.
- `trade_mode = live` (Phase 2, separately authorized): retest entries with
  exchange-native brackets, risk-% sizing, one position per asset,
  `max_concurrent_positions` (default 2), ≤10 trades/month, daily-loss and
  consecutive-loss kill switches. Shorts on Orderly perps only.

## Layout

```
bot.py                        main loop (cycle → engine → judge → record)
trade/structure.py            deterministic 3MS engine
trading_bot/reversal_judge.py DeepSeek client + verdict validation
trading_bot/executor.py       exchange clients (orders, fills, fees, klines)
db/schema_v3.sql, db_ops.py   fresh schema; six tables the dashboard reads
dashboard/, dashboard-ui/     web UI (unchanged across the refactor)
tests/                        engine fixtures from the book, judge, executor
.specify/specs/               001-reversal-trading (founding spec); archive/
```

## Configuration

Settings live in the `settings` table (seeded on first run): `trade_mode`,
`cycle_seconds`, `rr_min`, `risk_pct`, `judge_model` (`deepseek-v4-pro`),
`judge_effort`, `max_concurrent_positions`, `max_trades_per_month`, engine
tolerances, venue toggles, fee settings. The asset list is the
`asset_universe` table — edit rows to change the watchlist.

Secrets in `.env` (never committed): `BINANCE_API_KEY/SECRET_KEY`,
`ORDERLY_*`, `DEEPSEEK_API_KEY`, `API_TOKEN`/`TELEGRAM_CHAT_ID`.

## Run

```
pip install -r requirements.txt
python bot.py            # bot (observe mode by default)
cd dashboard && uvicorn main:app   # dashboard API
```

Deploy: GitHub Actions → Docker Hub → Watchtower. DB travels via
`fetch-db.sh` / `push-db.sh` (bot stopped during push).

## Tests

```
python -m pytest tests/ -q
```

Structure-engine fixtures encode the book's diagrams directly, including the
two misleading patterns that must be rejected.
