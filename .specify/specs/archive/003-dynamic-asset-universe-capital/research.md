# Research: Dynamic Asset Universe & Capital View

**Feature**: 003-dynamic-asset-universe-capital | **Date**: 2026-08-04

## 1. Why depth alone selects the wrong assets

Ranking Binance spot by depth returns BTC, ETH, SOL, XRP, BNB — the most
arbitraged, most efficiently priced instruments on the exchange. Mean-reversion
inefficiency is smallest there because every professional desk already arbitrages
it. The illiquid tail has plenty of inefficiency but a spread that consumes all
of it. **Conclusion:** the tradeable set is a band, not a maximum — roughly
volume ranks 20–80 rather than the top 10. Implemented as the configurable
`universe_rank_min`/`universe_rank_max` band (defaults 15–90), a hypothesis to be
validated by rank-decile evidence from the first dry-run.

## 2. Ranking metric: measure the strategy, not the market

Depth, spread and volatility describe market conditions; they do not answer
"would my entry rule have worked here?". That question is directly measurable by
replaying the rule against recent candles. **Conclusion:** the ranking key is the
strategy's own replay `recovery_rate`. To prevent divergence between the replay
and live logic, the adaptive threshold computation is extracted into one shared
function (`trade.universe.compute_thresholds`) that both live scalpers and the
replay call — enforced by a patch test (`test_replay_uses_shared_threshold_function`).

**Caveat:** recovery rate ignores spread, fees and slippage and assumes fills at
the candle price. It is a relative ranking signal, not an expectancy estimate;
the UI and docs must not present it as a predicted win rate.

## 3. Fee asymmetry

Measured DEX round-trip is 0.06% (25 Orderly trades, see `docs/CALIBRATION.md`);
CEX is ~0.20% at standard taker rates. At a 0.8% TP, that is fees consuming ~7.5%
of gross edge on DEX versus ~25% on CEX. **Conclusion:** fees are per-venue
(`dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`) and every net-edge
calculation — startup validation, per-cycle validation, and the replay's minimum
recovery rate (`universe_min_recovery_rate='auto'` → `(sl+fee)/(tp+sl)`) — uses
the venue's own rate. The CEX figure is assumed and must be replaced once
measured.

## 4. Per-asset capital cannot survive a dynamic universe

If the tradeable set changes daily, capital cannot attach to an asset that may
not be in tomorrow's set. **Conclusion:** capital becomes a per-venue pool.
Slot size = `{venue}_slot_pct × live equity` (exchange wins on any disagreement
with the declared `capital_*` pool; sizing never reads the declared pool).

## 5. DEX data proxying and rate limiting

Orderly public market data is restricted; the codebase proxies Orderly data
through Binance in `bot.py` (`_get_obi_and_spread`, `_get_live_price_orderly`),
while `trade/regime.py` `_fetch_orderly_ohlcv` calls Orderly's own kline endpoint
with Binance as the fallback venue for regime/ATR. The DEX scan uses the Binance
whole-exchange snapshot filtered to the Orderly perp listing (small, near-static). Stage 3 depth calls are bounded by a token bucket
(default budget 1200 calls/scan) — exhaustion aborts the scan and preserves the
previous universe (no partial write).

## 6. Staleness and fail-closed

A stale universe is unknown state → Constitution IV requires fail-closed: a scan
older than `universe_max_age_hours` blocks new entries on that venue while exits
continue to be managed. Universe churn must never force an exit — the loop splits
exit management (always) from entry evaluation (universe-gated).

## Open questions (dry-run)

1. Is the 15–90 rank band right? (realized expectancy by volume-rank decile)
2. How large is the predicted-vs-realized recovery gap? (calibration number)
3. Do trades cluster in stable universe members or newcomers? (churn)
4. What is the measured CEX round-trip fee?
