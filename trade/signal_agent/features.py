"""
Feature extraction for the ML signal gate.

Two modes:
1. Historical (from DB): extracts features from signal_history rows for training.
2. Live (from DataFrames): computes features in real-time during analyze_signal().

Features:
  regime_RANGE, regime_TREND_UP, regime_TREND_DOWN  — one-hot
  obi                                                — order book imbalance
  atr                                                — average true range
  atr_pct                                            — atr / entry_price
  candle_count                                       — consecutive directional candles
  side_is_buy                                        — 1=BUY, 0=SELL
  sl_distance_pct                                    — |entry - sl| / entry
  tp_distance_pct                                    — |tp - entry| / entry
  rr_ratio                                           — tp_distance / sl_distance
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, List


# Feature names (must match training order)
FEATURE_NAMES = [
    "regime_RANGE",
    "regime_TREND_UP",
    "regime_TREND_DOWN",
    "obi",
    "atr",
    "atr_pct",
    "candle_count",
    "side_is_buy",
    "sl_distance_pct",
    "tp_distance_pct",
    "rr_ratio",
]


def extract_features_from_db(signal_row: dict) -> Optional[Dict[str, float]]:
    """
    Extract features from a signal_history row (for training).
    Returns None if required fields are missing.
    """
    try:
        regime = signal_row.get("regime", "RANGE")
        obi = float(signal_row.get("obi", 1.0))
        atr = float(signal_row.get("atr", 0.0))
        candle_count = int(signal_row.get("candle_count", 0))
        side = signal_row.get("side", "BUY")
        entry = float(signal_row.get("entry_price", 0))
        sl = float(signal_row.get("stop_loss", 0))
        tp = float(signal_row.get("take_profit", 0))
    except (TypeError, ValueError):
        return None

    if entry <= 0:
        return None

    atr_pct = atr / entry if atr > 0 else 0.0

    sl_distance_pct = abs(entry - sl) / entry if sl > 0 else 0.0
    tp_distance_pct = abs(tp - entry) / entry if tp > 0 else 0.0
    rr_ratio = tp_distance_pct / sl_distance_pct if sl_distance_pct > 0 else 0.0

    return {
        "regime_RANGE": 1.0 if regime == "RANGE" else 0.0,
        "regime_TREND_UP": 1.0 if regime == "TREND_UP" else 0.0,
        "regime_TREND_DOWN": 1.0 if regime == "TREND_DOWN" else 0.0,
        "obi": obi,
        "atr": atr,
        "atr_pct": atr_pct,
        "candle_count": float(candle_count),
        "side_is_buy": 1.0 if side == "BUY" else 0.0,
        "sl_distance_pct": sl_distance_pct,
        "tp_distance_pct": tp_distance_pct,
        "rr_ratio": rr_ratio,
    }


def extract_features_live(
    regime: str,
    obi: float,
    atr: float,
    entry_price: float,
    side: str,
    stop_loss: float,
    take_profit: float,
    candle_count: int = 0,
) -> Dict[str, float]:
    """
    Extract features from live trading data (for inference during analyze_signal).
    Same feature set as extract_features_from_db.
    """
    if entry_price <= 0:
        return {}

    atr_pct = atr / entry_price if atr > 0 else 0.0
    sl_distance_pct = abs(entry_price - stop_loss) / entry_price if stop_loss > 0 else 0.0
    tp_distance_pct = abs(take_profit - entry_price) / entry_price if take_profit > 0 else 0.0
    rr_ratio = tp_distance_pct / sl_distance_pct if sl_distance_pct > 0 else 0.0

    return {
        "regime_RANGE": 1.0 if regime == "RANGE" else 0.0,
        "regime_TREND_UP": 1.0 if regime == "TREND_UP" else 0.0,
        "regime_TREND_DOWN": 1.0 if regime == "TREND_DOWN" else 0.0,
        "obi": obi,
        "atr": atr,
        "atr_pct": atr_pct,
        "candle_count": float(candle_count),
        "side_is_buy": 1.0 if side == "BUY" else 0.0,
        "sl_distance_pct": sl_distance_pct,
        "tp_distance_pct": tp_distance_pct,
        "rr_ratio": rr_ratio,
    }


def features_to_array(features: Dict[str, float]) -> np.ndarray:
    """Convert features dict to numpy array in FEATURE_NAMES order."""
    return np.array([features.get(name, 0.0) for name in FEATURE_NAMES], dtype=np.float32)
