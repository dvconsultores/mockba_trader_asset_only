# Feature Specification: 011 — Spot Exit Parity

**Feature Branch**: `011-spot-exit-parity` *(implementation on `main` per repo convention)*

**Created**: 2026-08-16

**Status**: Clarified — trivially, see below. Ready for plan/implement (AUTHORIZED by the operator: "agree proceed", numbering fixed to 011).

**Constitution**: v1.1.0. Compliance repair — Constitution V on the **spot** path, closing audit items 6 and 7 to the standard feature 010 just set for futures. Spot is the venue actually trading real money.

---

## What

Two Constitution V defects in `spot_scalper`, plus one constant→setting cleanup:

### Part 1 — Real fills on exchange-SL exits (audit item 7)

When Binance reports the OCO stop-loss leg FILLED, both branches (crash-guard
pre-check and the main fill check) record the **theoretical** `sl_price` with
`fee_exit=0.0`. A STOP_LOSS market leg can slip past its trigger, so recorded
spot losses are systematically optimistic — and `closed_trades.pnl_net` feeds
the daily-loss and consecutive-loss kill switches, so understated losses delay
them. The TP branches already use `_real_fill`; the SL branches now do too.

### Part 2 — Real `opened_at` (audit item 6)

`spot_scalper._close` hardcodes `opened_at=0` — 80+ of 86 rows in the live DB.
This destroyed hold-time analytics and forced the 009 study to reconstruct entry
times from `signals`. The position's `opened_at` (`op`, already loaded in
`manage_open_positions`) is now passed through every `_close` call.

### Part 3 — Fee fallback from settings

`_close`'s fee fallback is a hardcoded `0.001` per leg. It becomes
`cex_round_trip_fee_pct / 2` (same numeric value at the current setting of 0.20,
so **zero behaviour change today**) — "settings, never constants".

## Why

Feature 010 brought futures — the venue that is *off* — to a standard spot does
not meet. Every defect here is live on the venue trading real money. The PUMP
incident (a corrupted fee booking a −44% trade that never happened, poisoning
the kill-switch inputs) already demonstrated what bad exit accounting does on
this venue.

## Clarifications

All six 010 questions were venue-mechanics questions (reduce-only, escalation,
close-failure policy) that do not arise here: spot exits already close correctly
(OCO + market sell + orphan handling, hardened in 006); only the **recording**
is wrong. One decision worth stating: `_real_fill` failure falls back to the
stored price with the settings-based fee estimate — same rule as 010's
`_real_exit`, and the fallback keeps the existing behaviour as the floor.

## Scope

**In**: `trading_bot/spot_scalper.py` (SL branches ×2, `_close`, all `_close`
call sites), `tests/test_spot_exit_parity.py` (new), docs.
**Out**: exit *logic* (crash guard, orphan handling, time stop — all unchanged;
only recorded values change), `BinanceSpot`, futures, entries, 009, any
migration (columns exist), historical-row backfill.

## Acceptance criteria

1. **SL real fills** — both exchange-SL branches record price and fee from
   `_real_fill`; the stored `sl_price` is used only as its built-in fallback.
2. **Real `opened_at`** — every spot `closed_trades` row written by
   `manage_open_positions` carries the position's true `opened_at`.
3. **Fee fallback from settings** — `_close`'s zero-fee fallback reads
   `cex_round_trip_fee_pct / 2`; no hardcoded rate remains.
4. **No behaviour change** — exit decisions (when to sell, cancel, orphan) are
   byte-identical; only recorded values differ. `test_spot_exit_hardening.py`
   passes unmodified except where it asserted the old wrong values.
5. **Tests** — new `tests/test_spot_exit_parity.py`; full suite green.
6. **Docs** — CURRENT_STATE feature-011 note + CHANGELOG `fix:`.
