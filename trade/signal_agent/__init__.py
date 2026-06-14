"""
Signal Agent — AI-powered trade decision overlay for the reversal scalper.

Modules:
- labeler: Downloads trade history, matches outcomes to signals ✅
- features: Feature extraction for ML model ✅
- model: XGBoost classifier for enter/skip decisions ✅
- train: Model training pipeline with cross-validation ✅
- sl_manager: Structure-based stop loss placement — COMING
"""

from logs.log_config import apolo_trader_logger as logger

_READY = False

try:
    from trade.signal_agent.labeler import label_signals
    from trade.signal_agent.features import extract_features_live, FEATURE_NAMES
    from trade.signal_agent.model import get_model
    _READY = True
except ImportError as e:
    logger.warning(f"[SIGNAL_GATE] Some modules unavailable: {e}")

if _READY:
    logger.info("[SIGNAL_GATE] ✅ Signal agent fully loaded (labeler + features + model)")
else:
    logger.warning("[SIGNAL_GATE] Signal agent partially loaded")
