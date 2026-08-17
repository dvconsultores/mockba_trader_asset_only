# Feature Specification: Amendment 003 — Dynamic Asset Universe and the Capital View

**Feature Branch**: `003-dynamic-asset-universe-capital`

**Created**: 2026-08-04

**Status**: Draft

**Input**: Replace static per-asset configuration with a daily scan that selects which assets are tradeable, and replace the Assets view with a Capital view where capital is set per venue rather than per asset. Applies after Amendments 001, 002 and 004 (the per-asset capital work shipped as `002-multi-asset-capital`, migration `004_multi_asset.sql`).

---

## Rationale (recorded for the constitution)

**Static asset lists guess.** Hardcoding NEAR, or any five names, assumes conditions that were true when the list was written. Liquidity, spread and volatility all drift.

**But depth alone selects the wrong assets.** Ranking Binance spot by depth returns BTC, ETH, SOL, XRP, BNB — the most arbitraged, most efficiently priced instruments on the exchange. Execution there is excellent and the mean-reversion inefficiency is smallest, because every professional desk is already arbitraging it. Conversely the illiquid tail has plenty of inefficiency and a spread that consumes all of it.

The target is therefore a band, not a maximum: **liquid enough that spread and slippage are a small fraction of TP, and no more liquid than that.** In practice this is roughly ranks 20–80 by volume rather than the top 10.

**The ranking metric must measure the strategy, not the market.** Depth, spread and volatility describe conditions. They do not answer "would my entry rule have worked here?" That question is directly measurable by replaying the rule against recent candles, and the answer is a better ranking key than any statistical proxy.

**Per-asset capital does not survive a dynamic universe.** If the tradeable set changes daily, capital cannot be attached to an asset that may not be in tomorrow's set. Capital becomes a per-venue pool; slot size derives from the pool, per Amendment 001's equity-percentage model.

---

## Change 1 — `trade/universe.py`, the daily scanner

Runs once per `universe_scan_interval_hours` (default 24), and on startup if the stored scan is older than that. **Never runs inside the trading cycle** — it runs in a dedicated background thread owned by `bot.py`.

### Stage 1 — Candidate set (2 market-data calls, whole exchange)

- `GET /api/v3/ticker/bookTicker` — best bid and ask for every symbol, one call. Yields spread for the entire exchange at negligible weight cost.
- `GET /api/v3/ticker/24hr` — volume for every symbol, one call.
- Plus one `exchangeInfo` call for status / `min_notional` (configuration, not market data).

Filter to quote asset `USDT` (CEX) or the venue's quote (DEX), exclude leveraged tokens, stablecoin pairs, and anything flagged non-trading in exchange info.

### Stage 2 — Hard filters (pass/fail, no ranking)

| Filter | Rule |
|---|---|
| Volume | `quote_volume_24h >= universe_min_volume_usd` |
| Spread | `spread_pct <= tp_min_pct × universe_spread_ratio_max` (default 0.10 — spread ≤ 10% of TP) |
| Volume rank band | rank between `universe_rank_min` and `universe_rank_max` (defaults 15 and 90) |
| Symbol filters | `min_notional × 1.5` fundable at the venue's current slot size |

The rank band is the mechanism that implements the "liquid but not perfectly efficient" target. It is a setting so it can be tuned from evidence rather than argued about.

### Stage 3 — Depth check (per-symbol, survivors only)

For each survivor, fetch the order book and require top-10 depth on **both** sides ≥ `universe_depth_slot_multiple` × current slot size (default 3). Reject asymmetric books where one side fails.

Stage 2 should reduce the exchange to roughly 30–60 symbols, so this stage is bounded. Rate-limit with a token bucket and abort the scan cleanly if the budget is exhausted, keeping the previous universe rather than storing a partial one.

### Stage 4 — Strategy replay (the ranking key)

For each remaining symbol, fetch `universe_replay_days` (default 7) of 5m candles and replay the actual entry rule:

```
for each candle:
    update rolling peak/trough with the same window the live bot uses
    compute dip_needed from ATR, exactly as spot_scalper does
    if dip >= dip_needed:
        look forward up to max_hold_minutes
        record whether price reached tp_effective before the window closed
```

Produce per symbol:

- `signals_count` — how many entries the rule would have generated
- `recovery_rate` — fraction that reached TP within the hold window
- `median_minutes_to_tp`
- `atr_pct_median`

This is not a proxy for mean reversion. It is the strategy's own historical hit rate on that asset, which is the quantity that determines whether trading it is profitable.

**It must reuse the live threshold functions, not reimplement them.** A replay that diverges from live logic produces a ranking for a strategy you are not running. Enforced with a shared function (`trade/universe.compute_thresholds`, consumed by both scalpers and the replay) and a test that patches the shared function and observes both call sites.

### Stage 5 — Rank and store

Reject any symbol whose `recovery_rate` is below `universe_min_recovery_rate` (default `auto` — the breakeven win rate implied by current settings, computed not hardcoded) or whose `signals_count` is below `universe_min_signals` (default 20 — too few to mean anything).

Rank survivors by `recovery_rate`, tiebreak by `atr_pct_median` descending (more volatility, more signals, given the spread filter already passed).

Store the top `universe_size` (default 20) with all computed metrics and a `scanned_at` timestamp.

**Replay caveat (state in UI/docs):** recovery rate measured on historical candles ignores spread, fees and slippage, and assumes fills at the candle price. It is a *relative* ranking signal for comparing assets, not an expectancy estimate. Do not present it as a predicted win rate anywhere in the UI.

---

## Change 2 — Per-cycle guards

**Live spread check.** An asset that qualified at scan time can deteriorate hours later — a delisting announcement, a liquidity event. Each cycle, compare current spread against the scan-time spread already available from the order book snapshot fetched for OBI. If it exceeds `universe_spread_degradation_multiple` (default 3), skip that asset until the next scan and log it. No extra API call.

**Universe churn must never force an exit.** When an asset drops out of the new universe while a position is open: `manage_open_positions()` continues normally to TP, SL, time stop or regime exit. Only new entries stop. A rescan must never trigger a batch of market sells.

**Stale universe.** If the stored scan is older than `universe_max_age_hours` (default 36), no new entries are opened on that venue and a warning is logged. Existing positions are still managed. A stale universe is unknown state, and the constitution requires unknown state to fail closed.

---

## Change 3 — Venue asymmetry

**CEX (Binance spot):** hundreds of candidates. Full dynamic scan as above.

**DEX (Orderly):** a much smaller perp listing. The same pipeline runs, but `universe_rank_min` and `universe_rank_max` are effectively inert and `universe_size` will often exceed the number of qualifying symbols. Expect a short, near-static list. This is correct, not a bug — do not loosen the spread or depth filters to fill a quota.

**Fee asymmetry is a first-class input.** Measured DEX round-trip is materially lower than CEX (0.06% versus ~0.20% at standard rates), and the operator is the DEX venue owner paying reduced taker rates. At a 0.8% TP that is the difference between fees consuming roughly 25% of gross edge and roughly 7.5%.

Therefore the round-trip fee rate is **per venue** (`dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`, already per-venue since Amendment 004), and every net-edge calculation — startup validation, per-cycle effective-threshold validation, the universe replay's minimum recovery rate — uses the venue's own value. Record the actual DEX rate in `docs/CALIBRATION.md` once measured rather than assuming.

---

## Change 4 — Capital view (replaces Assets view)

### Model

Capital is a **per-venue pool**. Per-asset capital is removed — it cannot survive a universe that changes daily.

```
capital_cex_usdt        # pool for Binance spot
capital_dex_usdc        # pool for Orderly
cex_slot_pct            # % of CEX equity per slot
dex_slot_pct            # % of DEX equity per slot
max_slots_cex
max_slots_dex
```

Slot sizing follows Amendment 001: percentage of venue equity, min-notional floor, daily recompute, realized PnL only.

`capital_*` is the operator's declared allocation. Live equity is still read from the exchange each cycle; where they disagree, **the exchange wins** and the discrepancy is surfaced in the UI. Never size from a database figure.

### UI

Two panels, one per venue, each showing:

- Declared capital (editable) and live exchange equity, side by side, with a warning when they diverge beyond a tolerance
- Slot percentage, resulting slot size in currency, max slots, total deployable
- Deterministic validation from Amendment 002 inline — min-notional floor breaches, `max_slots × slot_pct > 100`, insufficient equity
- Venue enable toggle
- Fee rate for that venue, and the resulting net edge at current settings

Below, a read-only **Universe** panel per venue: the current top-N with rank, spread, depth, recovery rate, signal count, and scan age. Plus per-asset enable/disable overrides — a blacklist the operator controls, since a human may know something the scanner cannot (an upcoming delisting, a chain halt).

The Universe panel is read-only apart from the blacklist. Assets are not manually added; that is what the scanner decides.

### Telegram

- `/capital` — both venues, declared versus live equity, slot size, deployed, free.
- `/universe [cex|dex]` — current list with metrics and scan age.
- `/blacklist add|remove <asset>` — operator override.
- Existing asset add/select commands are removed. Every command that referenced per-asset capital is removed so none are left dangling.

---

## Schema

```
CREATE TABLE IF NOT EXISTS asset_universe (
    venue                TEXT    NOT NULL,      -- 'binance' | 'orderly'
    asset                TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,      -- venue-native
    rank                 INTEGER NOT NULL,
    scanned_at           REAL    NOT NULL,
    quote_volume_24h     REAL,
    spread_pct           REAL,
    depth_bid_top10      REAL,
    depth_ask_top10      REAL,
    atr_pct_median       REAL,
    signals_count        INTEGER,
    recovery_rate        REAL,
    median_minutes_to_tp REAL,
    blacklisted          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (venue, asset)
);

CREATE INDEX IF NOT EXISTS idx_universe_rank ON asset_universe(venue, rank);
```

One row per venue per asset, replaced wholesale on each scan. `blacklisted` survives a rescan — carried forward by asset, or the operator's override is silently erased every night.

## Migration

`db/migrations/006_amendment_003.sql` (006 — the next sequential number after 004_multi_asset.sql and 005_signal_tp_sl.sql; the amendment text referenced 004 which is already taken):

- Create `asset_universe` table + index (idempotent).
- Seed universe settings with defaults.
- Delete any legacy global `fee_round_trip_pct` key (a no-op on this codebase — the per-venue keys `dex_round_trip_fee_pct` / `cex_round_trip_fee_pct` already exist since Amendment 004 and are reused).
- Seed `settings_baseline` for all new `universe_%` keys as `unvalidated`.

Before deleting any legacy per-asset capital setting, report how its value maps into the venue pools. In this codebase, `asset_configs.capital_dex/capital_cex` are not settings rows — they are read-only legacy rows that the Capital view stops reading; the venue pools are the new `capital_dex_usdc` / `capital_cex_usdt` settings.

`universe_min_recovery_rate = 'auto'` means computed from current settings as the implied breakeven win rate — a literal value overrides it. Documented in the `SettingSpec`.

## Settings added to Amendment 002's schema

Every key needs a `SettingSpec` entry with hard and soft ranges. Cross-checks for `settings_rules.py`:

| Check | Level |
|---|---|
| `universe_rank_min >= universe_rank_max` | error |
| `universe_spread_ratio_max > 0.25` | warn — spread would exceed a quarter of TP |
| `universe_size` exceeds the count of symbols passing filters | warn — universe will be short |
| `universe_depth_slot_multiple × slot_size` exceeds typical depth | warn — universe will be empty |
| `universe_max_age_hours < universe_scan_interval_hours` | error — guarantees permanent staleness |
| `fee_round_trip_pct_<venue>` such that net edge fails at `tp_min_pct` | error |

## Acceptance criteria

**Scanner**

1. Stage 1 uses exactly two whole-exchange market-data calls (bookTicker + 24hr) plus one exchange-info config call; per-symbol calls occur only after Stage 2.
2. A symbol failing any hard filter never reaches the depth stage.
3. Rate-limit exhaustion aborts the scan and preserves the previous universe — no partial write.
4. The replay uses the same threshold functions as the live scalper, verified by patching the shared function and observing both call sites.
5. Replay on a synthetic series with known recovery behavior produces the expected `recovery_rate`.
6. A symbol with `signals_count < universe_min_signals` is excluded regardless of recovery rate.
7. `universe_min_recovery_rate = 'auto'` resolves to the breakeven rate implied by current settings, and changes when `tp_min_pct` changes.
8. Blacklist survives a rescan.
9. A DEX venue with fewer qualifying symbols than `universe_size` stores what qualifies, without loosening filters.

**Per-cycle**

10. An asset whose live spread exceeds the degradation multiple is skipped for entries and logged, using no additional API call.
11. An open position on an asset that dropped out of the universe is still managed to its normal exit; no forced close.
12. A universe older than `universe_max_age_hours` blocks new entries on that venue but not exit management.

**Capital and fees**

13. Slot size derives from live exchange equity, never from `capital_*`.
14. Declared capital diverging from live equity beyond tolerance (25%) raises a UI warning and does not change sizing.
15. Net-edge validation uses the venue's own fee rate — the same settings pass on DEX and fail on CEX when the fee difference is the deciding factor.
16. No per-asset capital setting remains reachable from UI, Telegram, or the bot.

**UI**

17. The Capital view renders both venues with deterministic validation inline and requires no LLM call.
18. The Universe panel is read-only except for the blacklist toggle.
19. Scan age is displayed, and a stale universe is visually distinct.

## Dry-run reporting additions

Add to module 2.7's report:

- **Universe churn** — how many assets entered and left daily, and whether trades cluster in stable members or in newcomers.
- **Recovery rate: predicted versus realized**, per asset. The scanner's replay ignores spread, fees and slippage, so realized will be lower. The *size* of that gap is the calibration number that makes the ranking trustworthy, and it should be recorded in `docs/CALIBRATION.md`.
- **Rank band evidence** — realized expectancy by volume-rank decile, to test whether the 15–90 band is right or whether the edge actually lives somewhere else. This is the single most valuable output of the first dry-run under this amendment, because the band is currently a hypothesis.
- **Per-venue net expectancy** side by side, so the fee advantage is visible in outcomes rather than assumed.
