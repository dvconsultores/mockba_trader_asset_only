# Converge: BNB Fee Discount (017)

**Date**: 2026-08-16 | **Status**: Implemented — 9/9 tasks, **133/133 tests green** (120 + 13)
**Cycle**: specify → clarify (Q1–Q5, operator-delegated) → plan → checklist → tasks → analyze → implement → converge ✅

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 BNB commission: USDT-valued fee, full sellable | ✅ | `test_entry_bnb_commission_full_sellable` (mocked live path) + helper unit test |
| 2 Base-asset commission unchanged | ✅ | `test_entry_base_commission_reduces_sellable`, `test_base_asset_valued_at_fill_price` |
| 3 Ticker failure → estimate, never 0 | ✅ | `test_bnb_ticker_failure_falls_back_to_estimate` |
| 4 Mismatch warning | ✅ | caplog test + negative test (no warning when BNB paid) |
| 5 Startup reserve warning | ✅* | By inspection (deviation 2) |
| 6 Validator cross-check | ✅ | 3 tests (on+0.20 warns 0.15; off+0.15 warns 0.20; coherent pair ok) |
| 7 Suite + DB | ✅ | 133 green; DB carries `cex_fee_bnb=true`, `cex_round_trip_fee_pct=0.15` (backup `data/trading.db.bak-20260816-*`) |

## Deviations

1. **Mid-test config change, ruled and annotated (clarify Q5).** The 5-day
   frozen-config test absorbs a real-cost change: from this deploy on, every
   trade costs 0.05% less. Day-boundary comparisons must treat 08-16 → 08-17
   as the discount boundary. The change direction is favorable and uniform, so
   the test's pass/fail gates (≥75% of backtest expectancy) remain meaningful.
2. **AC5 has no automated test** — the startup check runs in `bot.py run()`
   pre-loop, outside every harness. 5 lines, warn-only, null-safe on both API
   calls. Accepted by inspection.
3. **Shipped alongside (not part of 017)**: payoff-ratio validator aligned
   with Constitution II v1.1.0 (the "sl must be below tp" startup errors were
   pre-amendment law) and `universe_scan_interval_hours` soft_min 6 → 4. Both
   were the operator's "before proceed check" items; separate CHANGELOG entry.

## The number that justifies the feature

0.05% per round trip on the measured 132-entry base (+0.604%/trade confirmed
arm) is a **+8% relative expectancy improvement** for zero added risk. At the
scalper's ~10–15 trades/day, that compounds to roughly $0.05–0.15/day at $100
capital — small in dollars, but it is the cheapest expectancy the bot will
ever buy, and it scales linearly with capital.

## Operator deploy sequence (order matters)

1. **On Binance (app/web): buy ~$5 BNB and enable "Use BNB to pay fees"** —
   BEFORE the bot restarts, or the gate under-prices by 0.05% until you do
   (the mismatch detector will nag on every fill).
2. Commit → push `main` → Watchtower pulls the image.
3. Stop docker → `push-db.sh` (carries `cex_fee_bnb=true`, fee 0.15,
   `max_slots_cex=1` from 016 if not yet pushed) → start.
4. Watch the first live fill's log line: fee should arrive as
   `commissionAsset: BNB` with no mismatch warning.

## Remaining work / queue

- Startup-warning check on next server boot: only the truthful
  `universe_size` short-universe warn should remain.
- Queue unchanged: **018-swing-mode** (renumbered from 017; observe-first) ·
  013 loop latency · 014 VII re-baseline · audit items 5, 8–11 · futures
  `fee_entry` / dangling-TP · DEX 010 dry-run checklist before any DEX capital.
