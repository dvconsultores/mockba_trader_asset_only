# Feature Specification: 017 — BNB Fee Discount

**Feature Branch**: `017-bnb-fee` | **Created**: 2026-08-16
**Status**: Specified → implementing 2026-08-16 (operator directive: "ok proceed with bnb but use a spec fot the task, 017 bnb fee, all the constitution steps")
**Flow**: specify → clarify → plan → checklist → tasks → analyze → implement → converge
**Constitution**: v1.1.0 — Principles II (gate prices *actual* cost) and V (fees recorded from actual fills) are the whole point of this spec.
**Note**: 017 was previously penciled in for swing-mode; swing-mode is renumbered **018**.

## What

Support paying Binance spot fees in BNB (25% discount: 0.075%/side instead of
0.1%) **without corrupting fill accounting**, and keep the Constitution II cost
gate priced at the fee actually paid.

## Why

At scalper scale, fees are the dominant cost: the 132-entry study showed the
fee+slippage hurdle consumes 20–35% of a typical TP move. A 25% fee cut lowers
the round trip from 0.20% to 0.15% — that is 0.05% of pure expectancy added to
*every* trade, larger than several tuning changes we have measured. The gate
also loosens slightly (`tp_eff > cost + edge` with lower cost), so entry
frequency rises marginally — aligned with the standing HF directive.

## The accounting problem (why this needs code, not just a toggle)

When "Use BNB for fees" is active, Binance reports fill commissions with
`commissionAsset: "BNB"`. The current parsers
([executor.py](../../../trading_bot/executor.py) `place_entry`, `market_sell`,
`get_order_fills`) assume commission is USDT or the traded base asset:

1. **Sellable-qty corruption**: `place_entry` subtracts the commission from the
   sellable base quantity whenever `fee_asset != "USDT"`. A BNB commission does
   NOT come out of the purchased asset — the OCO would be placed for the wrong
   quantity (dust drift, and conceptually wrong).
2. **Fee valuation corruption**: non-USDT commissions are converted with
   `fee_amount × fill_price` — the *traded asset's* price. A BNB commission
   valued at, say, ONG's price records a fee ~1000× wrong. Constitution V
   (PnL from actual fills) is violated the moment the first BNB fill lands.

## Clarifications

### Session 2026-08-16 (operator delegated decisions: "i will do your recomendation")

- **Q1 — How to value a BNB commission in USDT?** Live BNBUSDT ticker at fill
  time (`get_price("BNB")`, public endpoint, no signature). Fills are rare
  (a few per day), so no caching needed. **Fallback**: if the ticker is
  unreachable, estimate the fee as `notional × per-leg rate` — an estimate is
  Constitution-V-preferable to recording 0 for a fee that was really paid.
- **Q2 — Is the BNB reserve part of equity?** **No.** `get_equity()` counts
  USDT + open positions; the BNB balance is a *fee reserve*, deliberately
  excluded — it is spent, not traded, and including it would inflate every
  percent-of-equity risk number by the reserve size.
- **Q3 — What happens when BNB runs out?** Binance silently reverts to
  base/quote-asset fees at the full 0.1% rate. Never block trading (the bot
  still works, just costs more). Two detectors instead:
  a startup warning when the reserve is < $2, and a runtime warning whenever
  `cex_fee_bnb=true` but a fill's commission arrives in a non-BNB asset.
- **Q4 — Gate rate and sequencing?** New bool setting `cex_fee_bnb`;
  `cex_round_trip_fee_pct` 0.20 → **0.15** in the same DB push. The operator
  MUST enable the Binance account toggle and hold BNB *before* restarting the
  bot with the new DB, otherwise the gate under-prices by 0.05% (detectors from
  Q3 catch the mistake; it degrades expectancy, never safety).
- **Q5 — Mid-test config change?** Yes, acknowledged: this changes the frozen
  5-day-test configuration. Ruling: the *real cost of trading* changes the
  moment the discount is active, and Constitution II requires the gate to track
  reality. The test log is annotated at the deploy boundary.

## Acceptance

1. A fill with `commissionAsset: "BNB"` records `fee_amount` = BNB × live
   BNBUSDT price (USDT terms) and `sellable_qty` = full filled quantity.
2. Base-asset commissions behave exactly as before (sellable reduced, valued at
   fill price) — regression-proof.
3. BNB ticker failure records the per-leg estimate, never 0.
4. `cex_fee_bnb=true` + non-BNB commission on a real fill ⇒ warning logged.
5. Startup: `cex_fee_bnb=true` + BNB reserve < $2 ⇒ `[STARTUP]` warning.
6. Validator: `cex_fee_bnb` and `cex_round_trip_fee_pct` cross-checked — BNB on
   with rate ≫ 0.15 warns (over-pricing), BNB off with rate ≪ 0.20 warns
   (under-pricing).
7. Full suite green; DB carries `cex_fee_bnb=true`, `cex_round_trip_fee_pct=0.15`.

## Out of scope

Futures/Orderly (no BNB concept); auto-buying BNB (operator manages the
reserve); counting BNB in equity (Q2); the stale payoff-ratio validator fix
(shipped alongside as pre-017 hygiene, documented in CHANGELOG — it aligns the
validator with constitution v1.1.0 and is not part of this feature).
