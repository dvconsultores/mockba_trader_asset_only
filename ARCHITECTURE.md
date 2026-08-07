# MockbaV4 — Simplified Architecture

> **Goal:** Profitable mean-reversion scalping, starting at $50, same math for $15,000.
> **Principle:** Price goes up, then down, and vice versa. Buy dips, sell rips. Nothing else.

---

## Current State vs. Proposed

### What exists now (3,200+ lines)

```
┌─────────────────────────────────────────────────────────┐
│                    trade/main.py (2,171 lines)           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐  │
│  │Pattern   │ │ML Gate   │ │LLM Gate  │ │Regime     │  │
│  │Detection │ │(XGBoost) │ │(DeepSeek)│ │Detection  │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────────┐  │
│  │Manip.    │ │Swing     │ │CEX Smart Entry Gates   │  │
│  │Detection │ │Mode      │ │(6 sub-gates)           │  │
│  └──────────┘ └──────────┘ └────────────────────────┘  │
│                    ↓ (sends signal)                     │
│  ┌──────────────────┐  ┌──────────────────┐            │
│  │Grid scalper DEX  │  │Grid scalper CEX  │            │
│  │(326 lines, buggy)│  │(330 lines, works)│            │
│  └──────────────────┘  └──────────────────┘            │
└─────────────────────────────────────────────────────────┘
```

**The problem:** `trade/main.py` is 2,171 lines of gates, models, and pattern detection
that all boil down to the same question the grid scalpers answer in ~50 lines:
*"Is price at an extreme and likely to revert?"*

### Proposed (1,400 lines total)

```
┌──────────────────────────────────────────────────────┐
│                    bot.py (~200 lines)                │
│              Single entry point, autotrade loop       │
│                                                      │
│  for asset in assets:                                │
│      regime = detect_regime(asset, 1h, 4h)           │
│      if regime == "RANGE":                           │
│          grid_scalp_cycle(asset, exchange)            │
│      # That's it. Nothing else.                      │
└──────────┬───────────────────────────┬───────────────┘
           │                           │
┌──────────▼──────────┐    ┌───────────▼──────────────┐
│  spot_scalper.py    │    │  futures_scalper.py      │
│  (~250 lines)       │    │  (~300 lines)            │
│                     │    │                          │
│  dip → buy          │    │  dip → long (with SL)    │
│  pump → sell        │    │  pump → short (with SL)  │
│  TP fill → closed   │    │  bracket TP/SL           │
│                     │    │                          │
│  Works for ANY      │    │  Works for ANY           │
│  Binance spot pair  │    │  Orderly perp pair       │
└─────────────────────┘    └──────────────────────────┘
           │                           │
┌──────────▼───────────────────────────▼──────────────┐
│              executor.py (~400 lines)               │
│  Unified order placement for both exchanges         │
│  - Binance: market buy + limit sell TP              │
│  - Orderly: bracket (entry + TP + SL)               │
│  - Position tracking: open/close/fill verification  │
│  - Size calculation: capital / price, tick rounding │
└────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────┐
│              regime.py (~150 lines)                  │
│  Pure price-based regime detection                  │
│  - 5m candles for execution timing                  │
│  - 1h candles for trend context                     │
│  - 4h candles for macro direction                   │
│  Output: RANGE, TREND_UP, TREND_DOWN                │
│  NO ML, NO LLM — just slope + volatility            │
└────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────┐
│              pnl.py (~100 lines)                     │
│  Real PnL tracking per trade, per day, per asset    │
│  - Entry price, exit price, fees, net PnL           │
│  - Daily PnL with reset at midnight UTC             │
│  - Kill switch: daily_loss_limit hit → stop         │
│  - Stored in SQLite, queried by UI                  │
└────────────────────────────────────────────────────┘
```

---

## 1. Real Live PnL

**Current state:** There is NO real PnL tracking. The bot fires orders and sends
Telegram messages but never computes `(exit_price - entry_price) * qty - fees`.
The `signal_history` table has `trade_outcome` and `realized_pnl` columns but
they're populated by a labeler that guesses from trade history every 2 hours.

**What to build:**

```python
# pnl.py — track per position

@dataclass
class ClosedTrade:
    asset: str
    exchange: str        # "binance" | "orderly"
    side: str            # "long" | "short"
    entry_price: float
    exit_price: float
    qty: float
    fee_entry: float     # in quote currency
    fee_exit: float
    pnl_net: float       # = (exit - entry) * qty * direction - fees
    pnl_pct: float       # relative to capital deployed
    opened_at: float     # timestamp
    closed_at: float
    exit_reason: str     # "tp" | "sl" | "time_stop" | "manual"

# Stored in SQLite table: closed_trades
# Daily summary: SELECT SUM(pnl_net) FROM closed_trades WHERE date(today) = date
```

**Critical:** The executor must return the **actual fill price**, not the signal
price. Today, `futures_grid_scalper.py` computes TP/SL from `live_price` (the
trigger), not the fill. Every unit of slippage silently eats your edge.

---

## 2. Use Any Asset

**Current state:** Hardcoded to NEAR (`PERP_NEAR_USDC`, `NEARUSDT` everywhere).

**What changes:**

- Asset list in DB setting `assets` (e.g., `"NEAR,ETH,SOL,BNB"`)
- The autotrade loop iterates over assets
- Each scalper accepts `asset: str` and derives the exchange symbol:
  - Binance: `f"{asset}USDT"` → validate via `/api/v3/exchangeInfo`
  - Orderly: `f"PERP_{asset}_USDC"` → validate via exchange info endpoint
- Exchange info (tick size, min notional, min qty, lot size) fetched once
  per asset and cached for the session
- Capital per position: configurable per asset or uniform (e.g., `$15` per slot)

**Scaling from $50 to $15,000:**

| Parameter | $50 account | $15,000 account |
|---|---|---|
| `capital_per_slot` | $15 | $15 (same slot, more slots) |
| `max_slots` | 1 | 10 ($150 deployed) |
| `dip_pct` | 0.4% | 0.4% (same market) |
| `tp_pct` | 0.5% | 0.5% |
| `sl_pct` | 0.8% | 0.8% |
| Profit per win | $15 × 0.5% = $0.075 | $15 × 0.5% × 10 = $0.75 |
| Daily loss limit | $10 (20% of account) | $10 (0.07% of account) |

The scaling knob is `max_slots × capital_per_slot`, not the percentages.
A 0.4% dip is a 0.4% dip regardless of account size.

---

## 3. GitHub Specs

Create these files at the repo root:

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | This document |
| `CHANGELOG.md` | Per-version changes |
| `.github/copilot-instructions.md` | Already exists, keep |
| `tests/test_spot_scalper.py` | Unit test: dip detection, OBI math, tick rounding |
| `tests/test_futures_scalper.py` | Unit test: DEX position sizing, SL validation |
| `tests/test_pnl.py` | Unit test: PnL calculation accuracy |
| `tests/test_regime.py` | Unit test: regime detection from OHLCV data |

---

## 4. What to Remove

### 4a. Database — DROP these tables

| Table | Why |
|---|---|
| `signal_history` | ML gate log. Gone with ML gate. |
| `dex_asset_wallets` | Legacy wallet mapping. Not needed. |
| `trades_daily` | Replaced by `closed_trades` (per-trade PnL). |
| `arbitrage_compounding` | Legacy, never used. |
| `arbitrage_cycle_steps` | Legacy, never used. |
| `arbitrage_inventory` | Legacy, never used. |
| `arbitrage_capital_allocation` | Legacy, never used. |
| `arbitrage_observations` | Legacy, never used. |
| `arbitrage_rotation_decisions` | Legacy, never used. |
| `ai_recommendations` | AI autonomy, never used. |
| `performance_metrics` | AI autonomy, never used. |
| `strategy_parameters` | AI autonomy, never used. |
| `execution_errors` | AI autonomy, never used. |
| `market_regimes` | AI autonomy, never used. |

### 4b. Keep only these DB tables

```sql
settings          -- key/value config (survives restart)
closed_trades     -- (new) every completed trade with PnL
open_positions    -- (new) current open positions for restart safety
```

**3 tables. Not 15+.**

### 4c. Code — DELETE these files

| File | Why |
|---|---|
| `trade/main.py` | 2,171-line monster. Replaced by `bot.py` + scalpers. |
| `trade/signal_analyzer.py` | ML/LLM signal analysis. Not needed. |
| `trade/signal_agent/` (entire folder) | ML model + labeler + trainer. Move to separate `research/` repo. |
| `trade/performance-llm.py` | LLM trade analysis. Move to `research/`. |
| `db/migrations/` (all 8 files) | Schema migrations for removed tables. |
| `trade/historical_data.py` | DEX data fetching. Regime detection gets its own OHLCV fetcher. |
| `PROMPT_*.md` (all 5 files) | Implementation prompts. Archive to `docs/archive/`. |

### 4d. Code — KEEP and simplify

| File | Changes |
|---|---|
| `trading_bot/spot_grid_scalper.py` | Generalize to any asset. Position persistence via DB. |
| `trading_bot/futures_grid_scalper.py` | Fix P0 bugs. Generalize. Add position persistence. |
| `trading_bot/spot_executor_binance.py` | Add `get_fill_price()` return value. Keep rest. |
| `trading_bot/futures_executor_apolo.py` | Add position tracking helpers. Keep rest. |
| `db/db_ops.py` | Strip to: `get_setting`, `upsert_setting`, trade CRUD. ~150 lines. |
| `logs/log_config.py` | Keep rotating file handler. Simplify log format. |
| `telegram.py` | Keep, update commands for new settings. |
| `dashboard/main.py` | Keep SSE stream. Replace signal/ML endpoints with PnL/positions. |
| `dashboard-ui/` | Keep React app. Replace Signals/ML tabs with Positions/History. |
| `forever.py` | Keep as-is. Just runs `bot.py` instead of `telegram.py`. |

---

## 5. What to Remove in Logs

**Current state:** `logs/log_config.py` with custom `DateRotatingFileHandler`.
All modules log at DEBUG level. `trade/main.py` logs at INFO with emoji-heavy
messages like `"📊 DEX Grid: OBI 0.953 ok but no price dip"`.

**What changes:**

- Keep the rotating file handler — it works fine
- Remove emoji log prefixes (bloat Docker logs, harder to grep)
- Log only meaningful events:
  - Entry opened / filled
  - Exit filled (TP, SL, time stop, manual)
  - PnL realized
  - Errors (API failures, order rejections)
  - Kill switch triggered
- Remove noise: "waiting for price dip", "OBI ok but no dip", "cooldown active"
- Structured format:

```
[TRADE]   asset=NEAR side=long entry=3.452 exit=3.467 pnl=+0.22 qty=4.3 reason=tp
[ERROR]   asset=ETH reason="min_notional: qty 0.0012 below 0.005"
[KILL]    reason="daily_loss_limit: -12.50 USDT"
[STARTUP] assets=NEAR,ETH mode=auto capital_per_slot=15 max_slots=1
```

- Log rotation: 5MB × 3 files is fine for $50–$15,000 operation.

---

## 6. UI and Settings

### UI Tabs

The current UI (`dashboard-ui/`) has: Live, Signals, ML, Status, Settings, Assets.

**After simplification:**

| Tab | Content |
|---|---|
| **Live** | Terminal log stream (SSE, keep as-is) |
| **Positions** | Open positions per asset/exchange, unrealized PnL, entry price, time open |
| **History** | Closed trades table: asset, side, entry, exit, PnL, reason, timestamp |
| **Settings** | All configurable parameters (see below) |
| **Status** | Uptime, exchange connectivity, daily PnL, kill switch state |

Remove: Signals tab, ML Monitor tab (depend on deleted `signal_history` table).

### Settings — The Complete List

```
┌─ Trading ─────────────────────────────────────────────┐
│ assets              NEAR,ETH,SOL     # comma-separated│
│ exchange            binance,orderly  # or "both"      │
│ interval            5m               # candle interval│
├─ Entry ───────────────────────────────────────────────┤
│ dip_pct             0.4              # % below peak to trigger long/buy │
│ pump_pct            0.4              # % above trough to trigger short/sell │
│ obi_buy_threshold   0.96             # OBI < this → bullish confirmation  │
│ obi_sell_threshold  1.22             # OBI > this → bearish confirmation  │
├─ Exit ────────────────────────────────────────────────┤
│ tp_pct              0.5              # take profit % from fill price │
│ sl_pct              0.8              # stop loss % from fill price (DEX only) │
│ max_hold_minutes    240              # time stop: close if open > N minutes │
├─ Risk ────────────────────────────────────────────────┤
│ capital_per_slot    15               # USDT/USDC per entry │
│ max_slots           1                # max concurrent positions │
│ cooldown_sec        300              # minimum seconds between entries │
│ leverage            3                # DEX only, 1-10 │
│ daily_loss_limit    10               # USDT, stop trading if breached. 0=off │
│ max_consecutive_loss 4               # stop after N losses in a row. 0=off │
├─ Mode ────────────────────────────────────────────────┤
│ auto_trade_binance  false            # false | signal | auto │
│ auto_trade_orderly  false            # false | signal | auto │
│ direction           both             # long | short | both │
│ dry_run             true             # true = simulate, no real orders │
└──────────────────────────────────────────────────────┘
```

**19 settings.** Every one has a clear purpose. No ML threshold, no LLM key,
no pattern count, no ATR multiplier — those were indirection.

---

## Core Logic (Pseudocode)

### regime.py

```python
def detect_regime(asset: str, exchange: str) -> str:
    """
    Returns: "RANGE" | "TREND_UP" | "TREND_DOWN"

    Uses 1h and 4h candles to determine market structure.
    5m candles are used for execution timing, not regime.
    """
    df_1h = fetch_ohlcv(asset, exchange, "1h", limit=30)
    df_4h = fetch_ohlcv(asset, exchange, "4h", limit=30)

    # Linear regression slope over lookback window
    slope_1h = linreg_slope(df_1h["close"], window=20)
    slope_4h = linreg_slope(df_4h["close"], window=20)

    # Volume confirmation
    vol_ratio_1h = df_1h["volume"].tail(5).mean() / df_1h["volume"].mean()

    # Thresholds
    SLOPE_THRESHOLD = 0.0012   # 0.12% per candle
    VOL_THRESHOLD = 1.2        # 120% of average

    if abs(slope_1h) < SLOPE_THRESHOLD and abs(slope_4h) < SLOPE_THRESHOLD:
        return "RANGE"

    if slope_1h > SLOPE_THRESHOLD and vol_ratio_1h >= VOL_THRESHOLD:
        if slope_4h > 0:       # 4h confirms
            return "TREND_UP"
        return "RANGE"         # 1h disagrees with 4h → treat as range

    if slope_1h < -SLOPE_THRESHOLD and vol_ratio_1h >= VOL_THRESHOLD:
        if slope_4h < 0:       # 4h confirms
            return "TREND_DOWN"
        return "RANGE"

    return "RANGE"
```

### spot_scalper.py (core cycle)

```python
def spot_scalp_cycle(asset: str, regime: str, obi: float, live_price: float) -> str | None:
    """
    Returns: "buy" | "sell" | None
    Only enters in RANGE regime.
    """
    if regime != "RANGE":
        return None

    # Update rolling price memory
    update_price_memory(live_price)

    # Check position limits
    open_count = count_open_positions(asset, "binance")
    if open_count >= max_slots:
        return None

    # Check daily loss limit
    if daily_pnl() <= -daily_loss_limit:
        return None

    # Check cooldown
    if time_since_last_entry(asset) < cooldown_sec:
        return None

    # LONG: price dip + OBI confirmation
    if is_price_dip(live_price) and obi < obi_buy_threshold:
        qty = calculate_qty(asset, "binance", capital_per_slot, live_price)
        if qty is None:
            return None
        result = place_spot_buy(asset, qty, live_price)
        if result and result.filled:
            place_tp_sell(asset, result.filled_qty, result.fill_price * (1 + tp_pct/100))
            record_open_position(asset, "binance", "long", result.fill_price, result.filled_qty)
            return "buy"

    return None
```

### futures_scalper.py (core cycle)

```python
def futures_scalp_cycle(asset: str, regime: str, obi: float, live_price: float) -> str | None:
    """
    Returns: "buy" | "sell" | None
    Same logic as spot but with mandatory SL bracket.
    """
    if regime != "RANGE":
        return None

    update_price_memory(live_price)

    open_count = count_open_positions(asset, "orderly")
    if open_count >= max_slots:
        return None

    if daily_pnl() <= -daily_loss_limit:
        return None

    if time_since_last_entry(asset) < cooldown_sec:
        return None

    # LONG: price dip + OBI confirmation
    if is_price_dip(live_price) and obi < obi_buy_threshold:
        qty = calculate_qty(asset, "orderly", capital_per_slot, live_price)
        if qty is None:
            return None
        result = place_futures_bracket(
            asset, "BUY", qty, live_price,
            tp_pct=tp_pct, sl_pct=sl_pct, leverage=leverage
        )
        if result and result.filled:
            # Verify SL is attached (P0.3 from hardening doc)
            if not verify_stop_loss_exists(asset):
                emergency_close(asset)
                return None
            record_open_position(asset, "orderly", "long", result.fill_price, result.filled_qty)
            return "buy"

    # SHORT: price pump + OBI confirmation
    if is_price_pump(live_price) and obi > obi_sell_threshold:
        # ... mirror of LONG with SELL side
        return "sell"

    return None
```

### pnl.py

```python
def close_position(asset: str, exchange: str, exit_price: float, exit_reason: str):
    """Called when a TP/SL fills or time stop triggers."""
    pos = load_open_position(asset, exchange)
    if not pos:
        return

    fee_entry = pos.entry_price * pos.qty * FEE_RATE
    fee_exit = exit_price * pos.qty * FEE_RATE

    if pos.side == "long":
        gross = (exit_price - pos.entry_price) * pos.qty
    else:
        gross = (pos.entry_price - exit_price) * pos.qty

    pnl_net = gross - fee_entry - fee_exit
    pnl_pct = pnl_net / (pos.entry_price * pos.qty) * 100

    save_closed_trade(ClosedTrade(
        asset=asset, exchange=exchange, side=pos.side,
        entry_price=pos.entry_price, exit_price=exit_price,
        qty=pos.qty, fee_entry=fee_entry, fee_exit=fee_exit,
        pnl_net=pnl_net, pnl_pct=pnl_pct,
        opened_at=pos.opened_at, closed_at=time.time(),
        exit_reason=exit_reason,
    ))

    delete_open_position(asset, exchange)

    # Kill switch check
    if get_daily_pnl() <= -DAILY_LOSS_LIMIT:
        disable_trading()
        logger.error(f"[KILL] daily_loss_limit breached: {get_daily_pnl():.2f}")

    logger.info(
        f"[TRADE] asset={asset} side={pos.side} "
        f"entry={pos.entry_price:.4f} exit={exit_price:.4f} "
        f"pnl={pnl_net:+.2f} pnl_pct={pnl_pct:+.2f}% reason={exit_reason}"
    )
```

---

## Migration Plan (Order of Operations)

### Phase 1: Build the new modules (don't touch existing code)

1. Create `trade/regime.py` — regime detection from OHLCV
2. Create `trade/pnl.py` — trade tracking + kill switch
3. Create `db/schema_v2.sql` — new minimal schema

### Phase 2: Rewrite the scalpers

4. Rewrite `trading_bot/spot_scalper.py` (new file, any asset)
5. Rewrite `trading_bot/futures_scalper.py` (new file, any asset)
6. Create `bot.py` — the new autotrade loop

### Phase 3: Wire up and test

7. Update `forever.py` to run `bot.py`
8. Run in dry-run mode against live data for 48 hours
9. Verify PnL tracking matches manual calculation on 5+ trades

### Phase 4: Clean up

10. Delete `trade/main.py`
11. Delete `trade/signal_agent/`, `trade/signal_analyzer.py`, `trade/performance-llm.py`
12. Drop old DB tables, run schema migration
13. Update UI tabs
14. Update Telegram commands
15. Archive PROMPT_*.md files

---

## File Tree (After Cleanup)

```
mockba_trader_asset_only/
├── bot.py                          # NEW: autotrade loop
├── forever.py                      # Process supervisor (updated)
├── telegram.py                     # Telegram bot (updated commands)
├── requirements.txt                # Dependencies (trimmed)
├── ARCHITECTURE.md                 # This document
├── CHANGELOG.md                    # NEW: version history
├── SPEC.md                         # NEW: technical spec
├── .github/
│   └── copilot-instructions.md     # Keep
├── trading_bot/
│   ├── spot_scalper.py             # REWRITTEN: any Binance asset
│   ├── futures_scalper.py          # REWRITTEN: any Orderly perp
│   ├── spot_executor_binance.py    # Keep, add fill price return
│   ├── futures_executor_apolo.py   # Keep, add position helpers
│   └── send_bot_message.py         # Keep
├── trade/
│   ├── regime.py                   # NEW: 5m/1h/4h regime detection
│   ├── pnl.py                      # NEW: trade tracking + kill switch
│   └── binance_data.py             # Keep (orderbook, price fetching)
├── db/
│   ├── db_ops.py                   # SIMPLIFIED: ~150 lines
│   └── schema_v2.sql              # NEW: minimal schema
├── logs/
│   └── log_config.py              # Keep, simplify log format
├── dashboard/
│   ├── main.py                     # Updated: PnL/position endpoints
│   └── requirements.txt
├── dashboard-ui/
│   └── src/                        # Updated: Positions, History tabs
├── tests/
│   ├── test_spot_scalper.py        # NEW
│   ├── test_futures_scalper.py     # NEW
│   ├── test_pnl.py                 # NEW
│   └── test_regime.py              # NEW
├── data/
│   └── trading.db                  # SQLite (new schema on migration)
├── docs/
│   └── archive/
│       └── PROMPT_*.md             # Archived implementation prompts
└── research/                       # NEW: offline analysis (moved here)
    ├── signal_agent/               # ML model (from trade/signal_agent/)
    ├── signal_analyzer.py          # (from trade/signal_analyzer.py)
    └── performance-llm.py          # (from trade/performance-llm.py)
```
