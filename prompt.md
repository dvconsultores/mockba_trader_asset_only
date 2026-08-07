# Spec — Best-Asset Scanner and Capital View

Replaces the static NEAR configuration with a scanner that selects **one asset per venue**, and replaces the Assets view with a Capital view holding one figure per venue.

Two parts: **Part A** is the specification content, **Part B** is the Spec Kit driver that walks it through the workflow.

---

# PART A — Specification

## Scope

**In scope:**

1. A scanner that ranks the tradeable universe per venue and selects the single best asset for each.
2. Both trade modes — the selection governs signals exactly as it governs orders.
3. The Assets view becomes the Capital view: one capital figure for CEX, one for DEX.
4. The settings the above requires.

**Out of scope:** strategy logic, entry rules, exit management, regime detection. Those are unchanged. This spec changes *which asset* is traded and *how capital is declared*, nothing about *how* a trade is decided once an asset is active.

## Core model

```
CEX (Binance spot)   → scanner picks 1 asset → trade or signal, per cex mode
DEX (Orderly perps)  → scanner picks 1 asset → trade or signal, per dex mode
```

Each venue has an independent active asset and an independent mode (`false` / `signal` / `auto`). The CEX can be in signal mode on one asset while the DEX runs auto on another.

`max_slots` still permits several concurrent positions **in that one asset**, separated by minimum entry spacing — a grid on the best-conditioned asset, not a single position.

**The stakes of selection rise with concentration.** With one asset per venue, a bad pick is the entire venue's exposure. That is why the ranking criterion below is strategy fit, not liquidity.

## The scanner

Runs once per `scan_interval_hours` (default 24), and at startup if the stored scan is older than that. Never inside the trading cycle.

### Stage 1 — Candidates (2 whole-exchange calls)

- `GET /api/v3/ticker/bookTicker` — best bid/ask for every symbol, one call. Spread for the entire exchange at negligible weight.
- `GET /api/v3/ticker/24hr` — volume for every symbol, one call.

Filter to the venue's quote asset. Exclude leveraged tokens, stablecoin pairs, and anything not flagged trading in exchange info.

### Stage 2 — Hard filters

Filter
Rule

Volume
`quote_volume_24h >= scan_min_volume_usd`

Spread
`spread_pct <= tp_pct × scan_spread_ratio_max` (default 0.10)

Rank band
volume rank between `scan_rank_min` and `scan_rank_max` (defaults 15, 90)

Fundable
`min_notional × 1.5` covered by the venue's current slot size

**Why a band rather than a maximum.** Ranking by depth alone returns BTC, ETH, SOL, XRP, BNB — excellent execution and the smallest mean-reversion inefficiency on the exchange, because every professional desk already arbitrages them. The illiquid tail has ample inefficiency and a spread that eats all of it. The target is liquid enough that spread and slippage are a small fraction of TP, and no more liquid than that.

The band is a **hypothesis**, expressed as a setting so it can be tuned from evidence.

### Stage 3 — Depth (survivors only)

Top-10 depth on **both** sides ≥ `scan_depth_slot_multiple` × current slot size (default 3). Reject asymmetric books.

Stage 2 should leave roughly 30–60 symbols. Rate-limit with a token bucket; on exhaustion, abort cleanly and keep the previous selection rather than storing a partial scan.

### Stage 4 — Strategy replay (the ranking key)

For each survivor, fetch `scan_replay_days` (default 7) of 5m candles and replay the live entry rule:

```
for each candle:
    update the rolling peak/trough with the live window
    compute the entry threshold using the SAME function the scalper uses
    if the extreme is reached:
        look forward up to max_hold_minutes
        record whether price reached the TP level in that window
```

Produces per symbol: `signals_count`, `recovery_rate`, `median_minutes_to_tp`, `atr_pct_median`.

**This must call the live threshold function, not a copy.** A replay that diverges ranks assets for a strategy you are not running, and every test still passes. Put the threshold computation in one module that both the scalper and the scanner import.

**Recovery rate is a relative ranking signal, not a predicted win rate.** It ignores spread, fees and slippage and assumes fills at candle prices. Never label it as expected win rate in the UI, logs, or notifications.

### Stage 5 — Select

Reject any symbol with `recovery_rate` below `scan_min_recovery_rate` (default `auto` — the breakeven win rate implied by current settings, computed not hardcoded) or `signals_count` below `scan_min_signals` (default 20).

Rank survivors by `recovery_rate`, tiebreak by `atr_pct_median` descending.

**Rank 1 becomes the venue's active asset.** Store the full ranked list — it is the fallback chain in Stage 6, and it is what makes the selection auditable.

If nothing qualifies: the venue has **no active asset**, opens nothing, signals nothing, and logs why. Do not loosen filters to fill the slot.

## Per-cycle guards

**Live spread degradation.** Compare current spread against scan-time spread, using the order book snapshot already fetched. Beyond `scan_spread_degradation_multiple` (default 3), the active asset is suspended and the bot **promotes the next qualifying asset from the stored ranked list**. Log the switch. This is why the list is stored rather than just the winner.

**Selection change never forces an exit.** When the scan or a fallback changes the active asset while a position is open on the old one: `manage_open_positions()` continues normally to TP, SL, time stop, or regime exit. Only new entries move to the new asset. A rescan must never produce a batch of market sells.

**Consequence to state plainly:** a venue can hold positions in asset X while opening new positions in asset Y. `max_slots` counts **total open positions on that venue**, across assets, not per asset.

**Stale scan.** Older than `scan_max_age_hours` (default 36) → no new entries and no signals on that venue; existing positions still managed. Unknown state fails closed.

## Modes

**Signal mode is a faithful preview of auto mode.** Same active asset, same filters, same guards, same slot size, same `signals` rows. The only difference is the final step: notify instead of place.

Concretely:

- The active asset is the same in both modes. Signal mode never signals an asset auto mode would not trade.
- A suspended or stale selection suppresses signals exactly as it suppresses entries.
- A blacklisted asset produces neither.
- Notifications carry the slot size auto mode would have used, plus the asset's rank, spread, and recovery rate.
- Venue equity unreadable → signal suppressed. A signal without a size is not actionable.

**Structural requirement:** one late mode branch. Every filter, guard, and sizing computation runs in shared code before it. Duplicated filter logic across a signal path and an auto path will drift, and signal mode stops predicting auto mode.

## Capital view (replaces Assets view)

### Model

Capital is declared **per venue**, not per asset — an asset-scoped figure cannot survive a selection that changes daily.

```
capital_cex_usdt      # declared allocation, Binance spot
capital_dex_usdc      # declared allocation, Orderly
cex_slot_pct          # % of CEX equity per slot
dex_slot_pct          # % of DEX equity per slot
max_slots_cex
max_slots_dex
```

Slot size is a percentage of **live exchange equity**, floored at `min_notional × 1.5`, recomputed once daily, compounding realized PnL only.

`capital_*` is the operator's declared allocation, displayed and validated. **Live equity is what sizes positions.** Where they diverge, the exchange wins and the discrepancy is surfaced.

### UI

Two panels, one per venue:

- Declared capital (editable) beside live exchange equity, with a divergence warning
- Slot percentage → resulting slot size in currency, max slots, total deployable
- Mode selector: off / signal / auto
- Venue fee rate and the resulting net edge at current settings
- Inline deterministic validation: min-notional floor breach, `max_slots × slot_pct > 100`, insufficient equity

Below each panel, a read-only **Active Asset** card: the selected symbol, its rank, spread, depth, recovery rate, signal count, and scan age — plus the next three in the ranked list as visible fallbacks.

Operator controls: a **blacklist** toggle per asset. Assets are not manually selected; that is the scanner's job. A human can veto, not appoint.

### Telegram

- `/capital` — both venues: declared vs live equity, slot size, deployed, free, mode
- `/asset [cex|dex]` — active asset with metrics and scan age, plus the fallback list
- `/blacklist add|remove <asset>`
- `/rescan [cex|dex]` — force a scan now
- Remove every per-asset capital and asset-selection command. Report any that reference removed settings.

## Data

```
CREATE TABLE IF NOT EXISTS asset_scan (
    venue                TEXT    NOT NULL,   -- 'binance' | 'orderly'
    asset                TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    rank                 INTEGER NOT NULL,   -- 1 = active
    scanned_at           REAL    NOT NULL,
    quote_volume_24h     REAL,
    volume_rank          INTEGER,
    spread_pct           REAL,
    depth_bid_top10      REAL,
    depth_ask_top10      REAL,
    atr_pct_median       REAL,
    signals_count        INTEGER,
    recovery_rate        REAL,
    median_minutes_to_tp REAL,
    suspended            INTEGER NOT NULL DEFAULT 0,  -- spread degradation
    blacklisted          INTEGER NOT NULL DEFAULT 0,  -- operator veto
    PRIMARY KEY (venue, asset)
);

CREATE INDEX IF NOT EXISTS idx_scan_rank ON asset_scan(venue, rank);
```

Replaced wholesale each scan. **`blacklisted` must survive** — carry it forward by asset, or the operator's veto is erased nightly. `suspended` resets each scan.

The active asset is `rank = 1 AND NOT suspended AND NOT blacklisted`, else the next qualifying rank.

## Settings

```
# scanner
scan_interval_hours               24
scan_max_age_hours                36
scan_min_volume_usd               5000000
scan_spread_ratio_max             0.10
scan_rank_min                     15
scan_rank_max                     90
scan_depth_slot_multiple          3
scan_replay_days                  7
scan_min_signals                  20
scan_min_recovery_rate            auto
scan_spread_degradation_multiple  3
scan_fallback_depth               3     # how far down the list to fall back

# capital, per venue
capital_cex_usdt                  0
capital_dex_usdc                  0
cex_slot_pct                      10
dex_slot_pct                      10
max_slots_cex                     1
max_slots_dex                     1

# fees, per venue — DEX ownership means these genuinely differ
fee_round_trip_pct_cex            0.20
fee_round_trip_pct_dex            0.06
```

**Removed:** every per-asset capital setting, every static asset-list setting, and any global `fee_round_trip_pct` (now per venue).

All seed as **unvalidated**. The rank band and the spread ratio in particular are hypotheses the dry-run is designed to test.

### Validation rules

Check
Level

`scan_rank_min >= scan_rank_max`
error

`scan_max_age_hours < scan_interval_hours`
error — guarantees permanent staleness

`max_slots_<venue> × <venue>_slot_pct > 100`
error

`slot_pct × equity < min_notional × 1.5`
error — venue cannot fund a slot

`fee_round_trip_pct_<venue>` leaves net edge below minimum at current `tp_pct`
error

`scan_spread_ratio_max > 0.25`
warn — spread would exceed a quarter of TP

`scan_depth_slot_multiple × slot_size` exceeds typical depth
warn — scan will find nothing

Every net-edge calculation uses the **venue's own** fee rate: startup validation, per-cycle validation, and `scan_min_recovery_rate = auto`.

## Acceptance criteria

**Scanner**

1. Stage 1 uses exactly two whole-exchange calls; per-symbol calls only after Stage 2.
2. A symbol failing any hard filter never reaches the depth stage.
3. Rate-limit exhaustion preserves the previous selection — no partial write.
4. The replay calls the same threshold function as the live scalper, verified by patching it and observing both call sites.
5. Replay against a synthetic series with known recovery behavior yields the expected `recovery_rate`.
6. `signals_count` below the minimum excludes a symbol regardless of recovery rate.
7. `scan_min_recovery_rate = auto` resolves to the breakeven rate from current settings and moves when `tp_pct` moves.
8. No qualifying symbol → venue has no active asset, opens and signals nothing, logs the reason.
9. Blacklist survives a rescan; `suspended` does not.

**Per-cycle**
10. Live spread beyond the degradation multiple suspends the active asset and promotes the next qualifying rank, using no additional API call.
11. An open position on a deselected asset is managed to its normal exit; no forced close.
12. `max_slots` counts total open positions per venue across assets, verified with positions in two assets simultaneously.
13. A scan older than `scan_max_age_hours` blocks entries and signals but not exit management.

**Modes**
14. Signal and auto select the same active asset given identical input.
15. Given one market fixture, both modes produce identical `signals` rows — the only difference is whether an order was placed.
16. A suspended or stale selection suppresses notifications, with one venue-quiet message on the transition, not per cycle.
17. Venue equity unreadable → signal suppressed, reason recorded.
18. One venue in signal and one in auto operate independently in the same cycle.

**Capital**
19. Slot size derives from live exchange equity, never from `capital_*`.
20. Divergence beyond tolerance raises a UI warning and does not change sizing.
21. Net-edge validation uses the venue's own fee rate — identical settings pass on DEX and fail on CEX when fees are the deciding factor.
22. No per-asset capital setting remains reachable from UI, Telegram, or the bot.

**UI**
23. The Capital view renders both venues with inline deterministic validation and no LLM call.
24. The Active Asset card is read-only apart from the blacklist toggle.
25. Scan age is shown; a stale scan is visually distinct.

---

# PART B — Spec Kit Driver

Paste one stage at a time.

## Stage 0 — Preflight

```
Read docs/CURRENT_STATE.md, ARCHITECTURE.md, and the spec document
"Best-Asset Scanner and Capital View" (Part A).

Report before writing anything:

1. Where the current code hardcodes NEAR or reads a static asset
   list, file by file.
2. Where trade mode (false/signal/auto) is branched on today: at
   what point in the cycle, and which filters run before versus
   after that branch. This spec requires ALL filters before it.
3. Where per-asset capital settings are read from, including
   Telegram commands and UI endpoints.
4. Whether threshold computation currently lives in one place or is
   inlined in the scalpers.
5. Any conflict between Part A and what the code actually does.

Do not run a speckit command yet.
```

**Check:** item 4 determines the size of this change. Inlined thresholds must be extracted to a shared module before the scanner can reuse them.

## Stage 1 — `/speckit.specify`

```
/speckit.specify

Feature: best-asset scanner and Capital view.

Source: the specification document "Best-Asset Scanner and Capital
View" (Part A). Read it in full and use it as the specification
input. Where it states a decision, that decision is final — do not
reinterpret.

One-line scope: replace the static NEAR configuration with a scanner
that selects ONE asset per venue by measured strategy fit, apply
that selection identically in signal and auto modes, and replace the
Assets view with a Capital view holding one capital figure per venue.

Not in scope: entry rules, exit management, regime detection,
position sizing formula. Unchanged.

Constitution: no principle is overridden. If you find a conflict,
stop and report rather than resolving.
```

**Check:** the spec must contain the rank-band rationale (liquid but not perfectly efficient), the replay-as-ranking-key decision, and the signal/auto parity rule. If asset selection reduces to "rank by liquidity," regenerate.

## Stage 2 — `/speckit.clarify`

Pre-answered:

Question
Answer

Scan in-process or separate job?
In-process, on a timer, outside the trading cycle. No new process.

Scan fails entirely?
Keep the previous selection. Older than `scan_max_age_hours` → block entries and signals. Never partial-write.

Different filters for DEX?
Same filters, same values. Only the fee rate differs. A DEX with few qualifying symbols is correct, not a bug.

Manual asset selection?
No. Blacklist only — the scanner appoints, the human vetoes.

Declared capital vs live equity?
Exchange wins for sizing; divergence shown as a warning.

Insufficient candle history?
Excluded, counted, reported. Never defaulted.

Signal mode without readable equity?
Suppress. A signal without a size is not actionable.

Suppressed signal recorded?
Yes — `signals` row, `action='skipped'`, reason. Telegram not notified of skips.

Position open on a deselected asset?
Managed to its normal exit. Never force-closed.

`max_slots` scope?
Total open positions per venue, across assets.

**One question to answer yourself, before Stage 3:**

> In signal mode you execute manually, so the position never enters `open_positions` via the bot. Does the bot reconcile it from the exchange and then manage exits — time stop, regime exit, TP monitoring — or emit entry signals only? Reconciliation already exists for restart safety, so adoption is nearly free; without it, signal mode gives entries with no exit discipline. But it means the bot acting on a position you opened.

## Stage 3 — `/speckit.plan`

```
/speckit.plan

Constitution check must address:
- Principle I: the replay is a ranking computation outside the
  trading cycle; the bot reads only a stored selection.
- Principle IV: no qualifying asset, stale scan, or unreadable
  equity each block entries AND signals.
- Principle VII: state the scanner module's line budget. No Redis,
  no queue, no new service — SQLite plus an in-process dict is the
  expected answer.
- Principle VIII: the filters cannot leave a venue permanently
  without an asset, and this is testable.

Also produce:
- The shared threshold-function contract used by BOTH the scalper
  and the replay. Name the module and the function. If thresholds
  are currently inlined, extraction is its own task.
- The mode-branch placement: ONE late point where auto places and
  signal notifies, with every filter and sizing computation in
  shared code before it. Show the call flow.
- API call budget per scan per venue with weight accounting.
- Schema for asset_scan and the migration.
- SettingSpec entries for all 18 settings with hard and soft ranges.
```

**Check, in order of consequence:**

1. One late mode branch, filters shared.
2. The shared threshold function is named and both call sites use it.

## Stage 4 — `/speckit.tasks`

```
/speckit.tasks

Build order:
  1. Extract threshold computation to a shared module (if inlined)
  2. Migration + asset_scan table
  3. SettingSpec entries + validation rules for the 18 settings
  4. Scanner stages 1-2 (whole-exchange filters)
  5. Scanner stage 3 (depth, rate-limited)
  6. Scanner stages 4-5 (replay, rank, select)
  7. Consolidate the mode branch to one late decision point
  8. Per-cycle guards: spread degradation, fallback promotion,
     staleness
  9. Per-venue fee rates through all net-edge validation
 10. Signal notification payload: slot size, asset metrics
 11. Capital view UI + Active Asset card
 12. Telegram /capital, /asset, /blacklist, /rescan
 13. Remove per-asset capital and static asset lists everywhere

Each task carries its acceptance criterion numbers from Part A.
```

**Check:** tasks 1, 7, and 13 exist separately. Folded into others, each gets half-done.

## Stage 5 — `/speckit.analyze`

```
/speckit.analyze

Check explicitly:
- No filter, guard, or sizing computation appears twice — once for
  signal, once for auto.
- Threshold computation appears in exactly one module.
- Every net-edge calculation uses the venue's own fee rate.
- No task assumes the Assets view still exists.
- scan_min_recovery_rate = 'auto' is a string in a numeric setting;
  confirm SettingSpec handles it.
- Recovery rate is never labelled a predicted win rate.

Report contradictions; do not resolve silently.
```

## Stage 6 — `/speckit.implement`

**Authorization gate.**

```
/speckit.implement

- dry_run stays true. Do not change it.
- Do not set any venue's trade mode.
- No real order. Read-only market data calls are permitted.
- Migration against a copy first; report row counts before and
  after, then run against live.
- Commit per task. Report acceptance criteria pass/fail by number.
- Stop and ask if a task cannot be completed as specified.
```

**Check after:**

1. Run the scanner manually and read the result. An unfamiliar name, or BTC/ETH, means the rank band is wrong — information, not failure. Look at ranks 2–5 too: if #1 barely beat them, the selection is noise-sensitive and the band or the replay window needs work.
2. Put one venue in signal mode for a few hours. Notifications must name the same asset auto would trade, at the same size.

## Stage 7 — `/speckit.converge`

```
/speckit.converge

Update PROGRESS.md, docs/CHANGELOG.md, and docs/CALIBRATION.md with
the first real scan: how many symbols passed each stage, the volume-
rank distribution of survivors, the recovery-rate spread between
rank 1 and rank 5. These are the first measured numbers for the
scanner settings — record them, and leave the baselines marked
unvalidated until the dry-run confirms them.
```

---

## Dry-run reporting additions

- **Selection stability** — how often the active asset changed, and whether trades cluster in stable selections or in fresh ones.
- **Recovery rate: predicted versus realized**, for the selected asset. The replay ignores spread, fees and slippage, so realized will be lower. The size of that gap is what makes the ranking trustworthy.
- **Rank-1 versus the field** — realized expectancy of the selected asset against the mean of ranks 2–5. If they are indistinguishable, the scanner is choosing at random and the ranking criterion needs rework.
- **Rank-band evidence** — the volume rank of selected assets over time, against realized expectancy. The 15–90 band is a hypothesis; this is its test.
- **Per-venue net expectancy** side by side, so the DEX fee advantage appears in outcomes rather than assumption.