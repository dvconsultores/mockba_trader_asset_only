"""
XGBoost binary classifier for signal gate decisions.

Uses all 8 available CPU cores (nthread=8) for training and inference.
Model is saved to data/signal_model.json and loaded at startup.

Usage:
    model = SignalGateModel()
    model.load()                        # load from disk
    score = model.predict(features)     # 0-1 win probability
    decision = model.decide(features)   # "approved" or "rejected"
"""

import json
import os
import numpy as np
from pathlib import Path
from typing import Optional, Tuple

import xgboost as xgb

from logs.log_config import apolo_trader_logger as logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = PROJECT_ROOT / "data" / "signal_model.json"

# Decision threshold (configurable)
DEFAULT_THRESHOLD = 0.75  # score > 0.75 → enter trade


class SignalGateModel:
    """XGBoost model for enter/skip trade decisions."""

    def __init__(self, nthread: int = 8):
        self.nthread = nthread
        self.model: Optional[xgb.XGBClassifier] = None
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded and self.model is not None

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: list[str],
    ) -> dict:
        """
        Train a new model. Returns training metrics dict.

        Args:
            X: Feature matrix (n_samples, n_features)
            y: Labels (0=loss, 1=win)
            feature_names: Names for each column
        """
        n_wins = int(y.sum())
        n_losses = len(y) - n_wins
        scale_pos_weight = n_losses / n_wins if n_wins > 0 else 1.0

        logger.info(
            f"[MODEL] Training XGBoost: {len(y)} samples "
            f"({n_wins}W/{n_losses}L, scale_pos_weight={scale_pos_weight:.2f})"
        )

        self.model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=scale_pos_weight,
            nthread=self.nthread,
            eval_metric="logloss",
            random_state=42,
            use_label_encoder=False,
        )
        self.model.fit(X, y)

        # Feature importance
        importance = dict(zip(feature_names, self.model.feature_importances_))
        top = sorted(importance.items(), key=lambda x: -x[1])[:5]

        self._loaded = True
        return {
            "n_samples": len(y),
            "n_wins": n_wins,
            "n_losses": n_losses,
            "top_features": top,
        }

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Return win probability [0-1] for each sample."""
        if not self.is_loaded:
            raise RuntimeError("Model not loaded or trained")
        return self.model.predict_proba(X)[:, 1]

    def decide(self, X: np.ndarray, threshold: float = DEFAULT_THRESHOLD) -> Tuple[str, float]:
        """
        Make enter/skip decision.

        Returns:
            (decision, score) where decision is "approved" or "rejected"
        """
        score = float(self.predict(X.reshape(1, -1))[0])
        decision = "approved" if score > threshold else "rejected"
        return decision, score

    def save(self, path: Optional[Path] = None):
        """Save model to JSON file."""
        if not self.is_loaded:
            logger.warning("[MODEL] Nothing to save — model not trained")
            return

        save_path = path or MODEL_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(save_path))
        logger.info(f"[MODEL] Saved to {save_path}")

    def load(self, path: Optional[Path] = None) -> bool:
        """Load model from JSON file. Returns True if successful."""
        load_path = path or MODEL_PATH

        if not load_path.exists():
            logger.info(f"[MODEL] No model file at {load_path} — skipping load")
            return False

        try:
            self.model = xgb.XGBClassifier(nthread=self.nthread)
            self.model.load_model(str(load_path))
            self._loaded = True
            logger.info(f"[MODEL] Loaded from {load_path}")
            return True
        except Exception as e:
            logger.warning(f"[MODEL] Failed to load model: {e}")
            self._loaded = False
            return False


# Singleton for use across the trading loop
_gate_model: Optional[SignalGateModel] = None


def get_model() -> SignalGateModel:
    """Get or create the global signal gate model instance."""
    global _gate_model
    if _gate_model is None:
        _gate_model = SignalGateModel(nthread=8)
    return _gate_model
