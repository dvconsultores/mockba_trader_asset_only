# MockbaV4 — Calibration Study

> Phase 1, Section 1.5 | Generated: 2026-07-26

---

## 1. Realized Fee Rates

### Orderly DEX

**Source:** `data/all_trades.json` — 25 trades, 2026-06-18 to 2026-06-20.

| Metric | Value |
|---|---|
| Trades analyzed | 25 |
| Total notional | $5,367.76 |
| Total fees paid | $1.61 |
| **Per-trade fee rate** | **0.0300%** (exactly) |
| **Round-trip fee rate** | **0.0600%** |

Every single trade had exactly 0.0300% — Orderly's standard taker fee. No maker rebates observed.

**For net-edge validation:** `round_trip_fee_pct = 0.06`.

### Binance Spot

**No Binance bot trade data available.** `data/binance_trades.json` (989 entries) and `data/accumulated_trades.json` (1,450 entries) contain raw account trade history — likely a mix of manual trades and bot trades with no way to distinguish them.

Binance spot fees are typically 0.10% per trade (0.075% with BNB discount). **Use `round_trip_fee_pct = 0.20` as default, configurable as a setting.**

---

## 2. Slippage

### Orderly DEX

**Cannot measure accurately.** The 25 trades in `all_trades.json` have real fill prices, but there is no signal price recorded alongside them. `signal_history` records `live_price` (the trigger price), but the labeler matches trades to signals by timestamp proximity (± a tolerance window), not by order ID. This makes slippage measurement unreliable without order-ID-level reconciliation.

**Recommendation:** The dry-run harness (Phase 2.7) must capture `(fill_price - signal_price) / signal_price` for every filled entry. Until then, use `assumed_slippage_pct = 0.03` for DEX (conservative given 0.1–0.3% typical spreads on Orderly).

### Binance Spot

**No data.** Use `assumed_slippage_pct = 0.05` for spot (conservative; Binance spot has tighter spreads but limit orders may not fill immediately).

---

## 3. Win Rate & Trade Outcomes

**Source:** `signal_history` — 1,370 labeled signals with trade outcomes, matched by the labeler against the full Orderly trade history API (1,450 trades across 44 symbols) and Binance trade history (989 entries).

**GATE 1 clarification:** The 25 trades in `all_trades.json` are a snapshot. The labeler had direct API access to the complete trade history. The 1,370 labeled outcomes span 727 DEX + 643 CEX matches across the full ~100-day signal history range. This is the ground truth, not an extrapolation from 25 trades.

| Outcome | Count | Percentage |
|---|---|---|
| Win | 1,132 | 82.6% |
| Loss | 234 | 17.1% |
| Breakeven | 4 | 0.3% |

**⚠️ This 82.6% win rate is likely inflated.** The labeler matches signals to trades by timestamp proximity. Only signals that actually became trades get labeled. Rejected signals (33,854 of 39,047) are never labeled. The true win rate of the strategy — including signals that would have been taken — is unknown.

Additionally, the labeler does not account for fees in its `win`/`loss` classification. A "win" is any trade with positive gross PnL regardless of fee impact.

**For breakeven win-rate calculation:**

```
breakeven_win_rate = (sl_pct + round_trip_fee_pct) / (tp_pct + sl_pct)
                  = (0.5 + 0.06) / (0.8 + 0.5)
                  = 0.56 / 1.3
                  = 43.1%
```

At the proposed defaults (tp=0.8%, sl=0.5%, fee=0.06%), the breakeven win rate is **43.1%**. Even with inflated numbers, 82.6% clears this comfortably. The real question is what the win rate will be under the new AND-condition entry rules (price extreme + OBI both required).

---

## 4. Regime Distribution

### Status: CANNOT COMPLETE

This study requires 90 days of historical OHLCV data (1h and 4h candles) for each candidate asset. **No historical OHLCV data is stored in the database.** The `historical_data.py` module fetches it fresh from Orderly's API each cycle and does not persist it.

However, we can observe from `signal_history` (39,047 signals over ~100 days):

| Regime | Count | % |
|---|---|---|
| TREND_DOWN | Appears frequently in rejection reasons | Unknown exact % |
| TREND_UP | Appears frequently | Unknown exact % |
| RANGE | Unknown | Unknown exact % |

The signal_history records the regime at the moment a pattern was detected, not the regime distribution over time. A regime that produces fewer patterns will be underrepresented.

### What the dry-run must capture

The dry-run harness (Phase 2.7) must log regime classification on every cycle, not just when a signal fires. This gives the true regime distribution. Until then:

- **Default `SLOPE_THRESHOLD`**: 0.0012 (0.12% per candle) as proposed in ARCHITECTURE.md
- **Default `regime_cache_sec`**: 300 (5 minutes)
- Both are adjustable settings. If the dry-run shows >80% RANGE or >80% TREND, thresholds are wrong.
- Different assets may need different slope thresholds. Make `slope_threshold` a per-asset setting if the dry-run shows wide variance.

---

## 5. Implied Thresholds for the Rebuild

From measured data:

| Parameter | Measured | Recommended Default | Source |
|---|---|---|---|
| `round_trip_fee_pct` (DEX) | 0.06% | 0.06 | Measured from 25 trades |
| `round_trip_fee_pct` (CEX) | Unknown | 0.20 | Binance standard taker ×2 |
| `assumed_slippage_pct` (DEX) | Unknown | 0.03 | Conservative estimate |
| `assumed_slippage_pct` (CEX) | Unknown | 0.05 | Conservative estimate |
| `tp_pct` | N/A | 0.8 | Design decision (must exceed sl_pct) |
| `sl_pct` | N/A | 0.5 | Design decision |
| `min_net_edge_pct` | N/A | 0.30 | `0.8 - 0.06 - 0.03 = 0.71` clears this easily |
| Implied breakeven WR | 43.1% | — | `(0.5+0.06)/(0.8+0.5)` |
| `SLOPE_THRESHOLD` | Unknown | 0.0012 | ARCHITECTURE.md proposal; must validate in dry-run |

### What the net-edge validation will check at startup

```
DEX: 0.8 - 0.06 - 0.03 = 0.71% >= 0.30% ✅
CEX: 0.8 - 0.20 - 0.05 = 0.55% >= 0.30% ✅
```

Both clear the minimum with room. The risk is that real slippage is worse than assumed — the dry-run will measure this.

---

## 6. Open Questions for GATE 1

1. **Is the 82.6% win rate real?** The labeler methodology (timestamp proximity matching) is loose. Cross-referencing `all_trades.json` order IDs with `signal_history` would give a ground-truth win rate for 25 trades — but only if we can link them.

2. **What is the actual Binance spot fee rate for this account?** Use `GET /api/v3/account` to check fee tier and BNB discount status before going live.

3. **Are there additional DEX trades beyond the 25 in `all_trades.json`?** The file only covers 3 days. The `trades_daily` table shows 9 trading days. Where are the other trades?

4. **Does the account use cross or isolated margin on Orderly?** `all_trades.json` shows `"margin_mode": "CROSS"`. Cross margin means liquidation price depends on total equity — the liquidation distance guard must account for this.
