# Feature Specification: 012 — Frequency Recovery

**Feature Branch**: `012-frequency-recovery` *(no code — settings + docs only)*

**Created**: 2026-08-16

**Status**: Implemented same-day (operator authorized: "proceed… it is only doc"; standing directive: **never lose the HF capacity — high frequency is what this bot is meant to be**).

**Constitution**: v1.1.0 — this is a Constitution VIII feature (The Bot Trades).

---

## What

Three DB settings change; zero code:

| Setting | Was | Now | Effect |
|---|---|---|---|
| `max_concurrent_positions` | 2 | **4** | Twice the concurrent shots; removes the slot-bound skips |
| `cex_slot_pct` | 40 | **20** | Slot = $20 at the $100 account: 4 × 20% = $80 deployed max |
| `capital_cex_usdt` | 50 | **100** | Declared pool updated to the operator's funded capital |

The remaining **$20 (20%) stays free as the loss buffer** — clarify Q1, the
operator's explicit capital plan. Everything else (thresholds, cooldowns,
stops, filters, universe criteria) is untouched — frequency is recovered
through **capacity**, never by loosening a quality gate.

## Clarifications

### Session 2026-08-16

- **Q1 — Capital plan → operator-directed.** Account funded to **$100**. Four
  concurrent positions at **20% each** ($20 slots, $80 deployed ceiling),
  keeping **$20 free to cover losses**. This supersedes the interim 25%/$50
  values applied earlier the same day (which assumed the old ~$50 account).
  Buffer arithmetic: the 3% per-position crash floor bounds a single loss at
  ~$0.60 and four simultaneous worst cases at ~$2.40 — the $20 buffer covers
  >8 consecutive max-loss rounds before touching deployed capital.
- **Q2 (implicit) — HF mandate.** Standing operator directive: the bot is meant
  to be high-frequency; capacity may rise, quality gates may not loosen, and no
  change may reduce trade frequency.

## Why — measured, 2026-08-16

- **Slots bind in bursts.** 183 `max_slots_cex` / `max_concurrent_positions`
  skips in the last 7 days. The bot's profitable day (08-06, 49 trades) is
  exactly the burst profile a 2-slot cap chokes.
- **Funded capital, structured deployment.** At $100: 4 × $20 slots = $80
  ceiling with a $20 standing buffer (Q1). Slot notional stays at the ~$20 the
  bot has been trading, well above the `min_notional × 1.5` floor (≈ $7.5);
  fees are percentage-based — unchanged economics.
- **Tail risk is spread, not grown.** `max_loss_per_position_pct=3` bounds a
  slot at ~$0.60; four names diversify what two concentrated.
- **Pre-positions 009 enforcement.** If `entry_confirm_candle` is ever enforced
  (~64% fewer entries), doubled capacity offsets the frequency cut — the HF
  mandate survives the quality filter.

## Deployment note

`compute_slot_size` caches per venue per UTC day (in-memory), so the new
`cex_slot_pct` takes effect on the next bot restart or UTC midnight — the
normal deploy (image restart, then `push-db.sh`) clears it anyway.

## Out of scope / recorded findings

- **`get_equity()` counts only USDT (free+locked), not coin holdings.** With
  positions open, "equity" collapses toward free cash: sizing and every
  `*_pct`-of-equity limit (daily loss, slot size) read ~$10 while the account
  holds ~$50. This systematically **shrinks** slot size and loss limits as
  positions open — conservative in direction but wrong in magnitude, and it
  fights this spec's intent. **New audit item #12**; candidate for 015
  (kill-switch integrity), which already touches `get_equity`.
- Loop latency (013) — the other half of HF capacity; unchanged here.
- `universe_*` filters — already operator-tuned (rank_max 120, min_volume 3M);
  not touched.

## Acceptance

1. DB carries the two new values; validator reports no error.
2. No source file changes; full test suite untouched and green.
3. CHANGELOG + CURRENT_STATE record the change and the equity-definition finding.
