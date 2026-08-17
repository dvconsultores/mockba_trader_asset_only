# MockbaV4 — Current State

> Founded 2026-08-16 — reversal trading bot (spec 001).
> Scalper-era state: `docs/archive/CURRENT_STATE-scalper-era.md`.

## Mode

**Observe** (`trade_mode=observe`): the bot analyzes, judges, records, and
notifies — it places no orders. Phase 2 (live execution) requires separate
authorization once observe-mode signal hit-rate is measured.

## Pipeline

30-min cycles → 1d/4h/1h candles per venue → deterministic 3MS engine →
DeepSeek v4-pro judge on CONFIRMED-and-retesting candidates (≤1 judge call
per asset per 4h candle) → `signals` row with structure packet, verdict,
and full reasoning → Telegram on valid signals.

## Configuration snapshot (seeded defaults)

| Setting | Value | Note |
|---|---|---|
| trade_mode | observe | Phase 2 flips to live |
| cycle_seconds | 1800 | 2 scans/hour |
| rr_min | 2.5 | breakeven WR 28.6% |
| position_size_pct | 45 | % of venue capital per position (live phase; 2 slots = 90%, 10% buffer) |
| dex_leverage | 3 | Orderly notional = margin × leverage |
| max_concurrent_positions | 2 | one per asset (spec Q9) |
| max_trades_per_month | 10 | book discipline |
| judge_model / effort | deepseek-v4-pro / high | thinking enabled |
| auto_trade_* | false | armed only in Phase 2 |

Assets (`asset_universe`, operator-owned, rank order): NEAR, SOL, ARB, GRAM,
INJ — both venues; Orderly analysis uses native klines, falls back to
Binance data on transient failures. Assets with no Orderly perp at all
(GRAM — verified 2026-08-17) are auto-blacklisted on the orderly venue at
startup (`sync_orderly_listings`); their Binance analysis continues.

## Pending / queue

- Server deploy of the rewrite (operator: push → Watchtower → push-db with
  the fresh DB → start) and `DEEPSEEK_API_KEY` added to the server `.env`.
- Observe-mode evidence window (~2–4 weeks): signal count, judge hit-rate
  against subsequent price, per-asset behavior.
- Phase 2 spec work: execution (retest entry + structural brackets +
  capital-based sizing per spec Q10), Orderly manual dry-run checklist
  before DEX capital,
  kill switches re-armed, monthly trade cap.
- Candidates: judge A/B (v4-flash), backtesting harness (spec 002),
  constitution refresh for the reversal era.
