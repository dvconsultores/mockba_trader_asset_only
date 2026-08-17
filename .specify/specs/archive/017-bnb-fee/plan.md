# Plan: BNB Fee Discount

**Feature**: 017-bnb-fee | **Date**: 2026-08-16 | **Spec**: `specs/017-bnb-fee/spec.md`
**Branch**: `main`

## Summary

One conversion helper on `BinanceSpot` applied at the three fill-parsing sites,
a sellable-qty condition fix, two cheap detectors (startup reserve, runtime
commission-asset mismatch), one new bool setting, one validator cross-check,
and a coupled DB change (`cex_round_trip_fee_pct` 0.20 → 0.15). No schema
migration; no futures changes.

## Constitution Check

| Principle | Compliance |
|---|---|
| **II** (v1.1.0) | ✅ The gate must price the *actual* round-trip cost. With the discount active, 0.20 would over-price and skip entries whose true edge clears; 0.15 restores truth. Detectors guard the inverse error (paying 0.20 while gating at 0.15). |
| **V** | ✅ This spec *repairs* V for BNB fills: commission valued at BNB's own price, from the actual fill payload; ticker-failure fallback is an estimate, never 0. |
| III / IV / VI | ✅ No order, stop, or state path changes — parsing and valuation only. |
| VII | ✅ ~+35 lines in executor/bot/validator (pre-existing overrun tracked by spec 014). |
| **VIII** | ✅ Frequency can only *rise* (lower cost ⇒ gate passes more entries). Nothing new is skipped. |

## Pinned mechanisms

- **M1 — `_fee_to_usdt(fee_amount, fee_asset, ref_price, notional)`** on
  `BinanceSpot`: USDT/USDC pass through; BNB valued via `get_price("BNB")`,
  fallback `notional × rate/200` (per-leg % of the configured round trip);
  other assets (base-asset commission) valued at `ref_price` as before. Also
  hosts the Q3 runtime mismatch warning (single choke point — all three
  parsers call it).
- **M2 — Sellable condition**: subtract commission from sellable base qty only
  when the commission asset is neither USDT/USDC **nor BNB**.
- **M3 — Call sites**: `place_entry` (entry fills), `market_sell` (time-stop /
  crash exits), `get_order_fills` (TP/SL/OCO real-fill lookups). All three feed
  recorded PnL — missing any one corrupts books on that exit path.
- **M4 — Startup reserve check** in `bot.py run()` after executors exist:
  `cex_fee_bnb && !dry_run` ⇒ `get_balance("BNB") × get_price("BNB") < $2` ⇒
  `[STARTUP]` warning. Warn-only (Q3: never block).
- **M5 — Validator cross-check** in `settings_rules.validate()` for keys
  `cex_fee_bnb` / `cex_round_trip_fee_pct`: on+rate>0.16 ⇒ warn suggest 0.15;
  off+rate<0.19 ⇒ warn suggest 0.20.
- **M6 — Settings**: `cex_fee_bnb` (bool, risk group, default false, related to
  `cex_round_trip_fee_pct`). DB: `cex_fee_bnb=true`, `cex_round_trip_fee_pct=0.15`
  (backup first). Operator sequence pinned in converge.

## Testing

`tests/test_bnb_fee.py`: helper unit tests (BNB, fallback, base-asset
regression, passthrough), a live-path `place_entry` integration test with
mocked `_post` (BNB commission ⇒ full sellable + correct USDT fee; base
commission ⇒ old behavior), mismatch-warning caplog test, validator
cross-check tests. Fixture pins production settings (016 lesson).

## Out of scope

Orderly; BNB auto-top-up; equity inclusion; payoff-validator hygiene fix
(separate commit).
