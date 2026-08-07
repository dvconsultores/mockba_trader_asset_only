# OBI (Order Book Imbalance) in MockbaV4 — How it works

> For second-opinion review. The question: should OBI be mandatory (AND condition) or reference-only?

---

## What OBI measures

```
OBI = total_bid_quantity / total_ask_quantity  (top 10 levels)

OBI > 1.0 → more bids than asks → buyers are aggressive → bullish pressure
OBI < 1.0 → more asks than bids → sellers are aggressive → bearish pressure
OBI = 1.0 → balanced
```

## How the bot uses it

Entry requires **both** a price extreme **AND** OBI confirmation:

| Direction | Price condition | OBI condition |
|---|---|---|
| LONG / BUY | price dipped ≥ 0.4% from rolling peak | OBI < 1.20 (not heavily bullish) |
| SHORT / SELL | price pumped ≥ 0.4% from rolling trough | OBI > 0.80 (not heavily bearish) |

## Real OBI data (measured 2026-07-26, Binance)

| Asset | Observed OBI | Notes |
|---|---|---|
| NEAR | 1.08–1.13 | Slightly bid-skewed, normal |
| ETH | 1.47 | Heavily bid-skewed at time of measurement |
| SOL | 1.31 | Moderately bid-skewed |
| BNB | 0.65 | Heavily ask-skewed at time of measurement |

## The problem with a fixed threshold

A single threshold cannot work across assets. NEAR at 1.08 and BNB at 0.65 would need completely different thresholds to be meaningful. The bot currently applies the same threshold to all assets.

## Current state (after calibration)

```
obi_buy_threshold  = 1.20   → OBI < 1.20 passes buy check
obi_sell_threshold = 0.80   → OBI > 0.80 passes sell check
```

These thresholds are deliberately **wide** — they only block extreme cases:
- Blocks buying when the order book shows heavy bullish momentum (someone is pumping)
- Blocks selling when the order book shows heavy bearish momentum (someone is dumping)

In practice, with these thresholds on NEAR:
- Buy check: OBI 1.08 < 1.20 → **always passes** (normal range is 1.03–1.13)
- Sell check: OBI 1.08 > 0.80 → **always passes**

The dip/pump detection does the real filtering. OBI is a safety net, not a gate.

## The trade-off

| | OBI mandatory (AND) | OBI reference-only |
|---|---|---|
| False entries | Fewer (blocks during manipulation) | More (enters on noise dips) |
| Missed trades | More (blocks valid dips with neutral OBI) | Fewer (captures more dips) |
| Cross-asset | Breaks for assets with different OBI ranges | Works for any asset |
| Simplicity | Requires per-asset calibration | Zero configuration |

## The actual question

The bot's edge is **mean reversion in ranges** — price went down, it should go back up. The dip detection alone captures this. OBI adds a second filter: "is the order book confirming this dip is real selling pressure, not just noise?"

But with wide thresholds (1.20/0.80), OBI effectively only blocks the 1% of cases where the order book is screaming against the trade. The other 99% of the time, the dip/pump detection decides.

**Is this the right design?** Or should OBI be removed from the decision path entirely and logged as a reference metric for post-trade analysis?
