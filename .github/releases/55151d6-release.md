# Signal Agent v1.0.0

**Release Date:** June 14, 2026  
**Commit:** 55151d6569dcc65b7574e226c5420625dd30e81a

## Overview

This release introduces the **Signal Agent** — an AI-powered ML overlay that scores trade signals and makes enter/skip decisions using XGBoost. The system automatically labels trading outcomes from executed trades and retrains the model with new data.

## Features

### 🤖 ML Signal Gate
- XGBoost binary classifier for enter/skip decisions
- Configurable decision threshold (default: 0.50 score)
- 8-thread optimized inference for low-latency scoring
- Reference-only in Signal mode, blocking in Automatic mode

### 📊 Feature Extraction
- 11 ML features extracted from each signal:
  - Regime classification (RANGE, TREND_UP, TREND_DOWN)
  - Order book imbalance (OBI)
  - Average true range (ATR) and ATR percentage
  - Position sizing (SL/TP distances, R:R ratio)
  - Consecutive directional candles
  - Trade side (BUY/SELL)
- Live extraction during `analyze_signal()` for inference
- Historical extraction from DB for training

### 🏷️ Automatic Outcome Labeling
- Downloads trade history from Orderly API (DEX) and Binance (CEX)
- Matches executed trades to signals by timestamp proximity (±15 minutes)
- Labels outcomes: **win** (PnL > $0.01), **loss** (PnL < -$0.01), **breakeven**
- Runs periodically in background (every 2 hours)
- Accumulates trades in `data/accumulated_trades.json` for efficient re-runs

### 🎓 Training Pipeline
- Loads labeled signals from database
- 5-fold stratified cross-validation
- Balanced class weights (auto-scaled by win/loss ratio)
- Feature importance analysis
- Saves model to `data/signal_model.json`
- Auto-retrains when ≥5 new labels accumulate (max 1x per 24h)

### 📈 Database Schema
New columns added to `signal_history`:
- ML feature columns (slope, distance metrics, alignment score)
- `ml_score` (0-1 win probability)
- `ml_decision` (approved/rejected)
- `realized_pnl` (trade outcome PnL)
- `trade_outcome` (win/loss/breakeven)

## Usage

### Train the Model
```bash
# Train on all labeled signals with 5-fold CV
python -m trade.signal_agent.train

# Force retrain (skip existing model check)
python -m trade.signal_agent.train --retrain

# Dry-run (evaluate without saving)
python -m trade.signal_agent.train --dry-run

# Print dataset summary
python -m trade.signal_agent.train --summary
```

### Label Trade Outcomes
```bash
# Label unlabeled signals (matches to recent trades, auto-retrains if enough labels)
python -m trade.signal_agent.labeler

# Dry-run (preview matches without writing to DB)
python -m trade.signal_agent.labeler --dry-run

# Full sync (paginate through entire Orderly trade history)
python -m trade.signal_agent.labeler --full

# Use cached trades only (no API calls)
python -m trade.signal_agent.labeler --local-only
```

### Live Inference
Model loads automatically at startup in `trade/main.py`. Signals are scored and decisions logged:
```
🤖 ML Gate: score=0.652 → APPROVED ✅
🤖 ML Gate: score=0.387 → REJECTED ❌ (ML gate rejected: Score 0.387 < threshold 0.50)
```

## Configuration

### Decision Threshold
Edit `_ML_THRESHOLD` in `trade/main.py`:
```python
_ML_THRESHOLD = 0.50  # score > 0.50 → ML approves
```

### Labeler Interval
Edit `_LABELER_INTERVAL` in `trade/main.py`:
```python
_LABELER_INTERVAL = 7200  # run outcome labeler every 2 hours
```

### Training Params
Edit hyperparameters in `trade/signal_agent/model.py`:
- `n_estimators=100` — XGBoost trees
- `max_depth=4` — tree depth
- `learning_rate=0.05` — gradient descent rate
- `scale_pos_weight` — auto-balanced by class ratio

## Dependencies

- `xgboost>=1.5.0` — gradient boosted trees
- `scikit-learn>=1.0` — cross-validation & metrics
- `cryptography>=3.4` — Orderly API signing
- `base58>=2.1.0` — Ed25519 key decoding

Install:
```bash
pip install xgboost scikit-learn
```

## Files Changed

**New files (5):**
- `trade/signal_agent/__init__.py` — Package initialization
- `trade/signal_agent/features.py` — Feature extraction (121 lines)
- `trade/signal_agent/labeler.py` — Outcome labeling & matching (545 lines)
- `trade/signal_agent/model.py` — XGBoost wrapper (149 lines)
- `trade/signal_agent/train.py` — Training pipeline (226 lines)
- `db/migrations/005_add_trade_outcome_columns.py` — Schema migration

**Modified files (3):**
- `db/db_ops.py` — Added ML columns to schema, updated `save_signal_to_history()`
- `trade/main.py` — ML gate integration, labeler background task, removed daily trade limit (CEX unlimited)

## Removed

- **Daily trade limit (DEX only)** — Removed `MAX_TRADES_PER_DAY = 2` check. CEX (Binance) now has unlimited trades per day.

## Next Steps

1. **Seed training data:** Run labeler to match historical trades to signals
2. **Train initial model:** `python -m trade.signal_agent.train` once ≥20 labeled samples exist
3. **Monitor:** Check logs for `[ML GATE]` and `[LABELER]` messages
4. **Iterate:** Model auto-retrains every 24h with new outcomes

## Notes

- Model is **optional** — if no model file exists or loading fails, ML gate is skipped and all signals approved (unless blocked by other checks)
- Labeler runs in a background daemon thread every 2 hours during autotrade
- Outcome labeling requires valid Orderly & Binance API credentials in `.env`
- Feature importance logged after each training run
- Cross-validation scores printed to help detect overfitting

---

**Commits in this release:** 1  
**Total changes:** +1,270 lines, -38 lines
