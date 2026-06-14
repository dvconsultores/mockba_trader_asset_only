"""
Training pipeline for the signal gate ML model.

Loads labeled signals from signal_history, extracts features,
trains XGBoost with cross-validation, saves the model.

Usage:
    python -m trade.signal_agent.train              # train & save
    python -m trade.signal_agent.train --dry-run    # evaluate only, don't save
    python -m trade.signal_agent.train --retrain    # force retrain even if model exists
"""

import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import classification_report, confusion_matrix

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import get_db_connection
from logs.log_config import apolo_trader_logger as logger
from trade.signal_agent.features import (
    extract_features_from_db,
    features_to_array,
    FEATURE_NAMES,
)
from trade.signal_agent.model import SignalGateModel, MODEL_PATH


def load_labeled_data() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """
    Load all labeled signals from DB, extract features and labels.

    Returns:
        X: Feature matrix (n_samples, 11)
        y: Labels (0=loss, 1=win)
        rows: Raw DB rows for inspection
    """
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM signal_history
            WHERE trade_outcome IN ('win', 'loss')
            ORDER BY timestamp ASC
        """)
        rows = [dict(r) for r in cur.fetchall()]

    X_list = []
    y_list = []
    valid_rows = []

    for row in rows:
        features = extract_features_from_db(row)
        if features is None:
            continue
        X_list.append(features_to_array(features))
        y_list.append(1 if row["trade_outcome"] == "win" else 0)
        valid_rows.append(row)

    if not X_list:
        raise ValueError("No valid labeled signals found for training")

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)

    logger.info(
        f"[TRAIN] Loaded {len(y)} labeled signals: "
        f"{int(y.sum())} wins, {len(y) - int(y.sum())} losses"
    )
    return X, y, valid_rows


def cross_validate(model: SignalGateModel, X: np.ndarray, y: np.ndarray) -> dict:
    """5-fold stratified cross-validation. Returns metrics dict."""
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    # Use a lightweight model for CV
    import xgboost as xgb
    n_wins = int(y.sum())
    n_losses = len(y) - n_wins
    sw = n_losses / n_wins if n_wins > 0 else 1.0

    cv_model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=sw,
        nthread=model.nthread,
        eval_metric="logloss",
        random_state=42,
        use_label_encoder=False,
    )

    scores = cross_val_score(cv_model, X, y, cv=cv, scoring="accuracy")
    return {
        "cv_folds": 5,
        "cv_accuracy_mean": round(float(scores.mean()), 4),
        "cv_accuracy_std": round(float(scores.std()), 4),
        "cv_scores": [round(float(s), 4) for s in scores],
    }


def train_and_save(dry_run: bool = False, retrain: bool = False):
    """
    Full training pipeline:
    1. Check if model exists (skip unless --retrain)
    2. Load labeled data
    3. Cross-validate
    4. Train final model
    5. Evaluate
    6. Save if improved or new
    """
    logger.info("=== Signal Gate Model Training ===")

    # Check existing model
    existing_accuracy = None
    if MODEL_PATH.exists() and not retrain:
        logger.info(f"Model exists at {MODEL_PATH} — use --retrain to force retraining")
        try:
            existing = SignalGateModel(nthread=8)
            existing.load()
            if existing.is_loaded and hasattr(existing.model, "best_score"):
                existing_accuracy = existing.model.best_score
        except Exception:
            pass
        if not retrain:
            logger.info("Skipping training (model exists). Run with --retrain to force.")
            return

    # Load data
    X, y, rows = load_labeled_data()

    if len(y) < 20:
        logger.warning(f"[TRAIN] Only {len(y)} samples — need at least 20 for meaningful training")
        return

    # Cross-validate
    logger.info("[TRAIN] Running 5-fold cross-validation...")
    model = SignalGateModel(nthread=8)
    cv_results = cross_validate(model, X, y)

    logger.info(
        f"[TRAIN] CV accuracy: {cv_results['cv_accuracy_mean']:.2%} "
        f"(±{cv_results['cv_accuracy_std']:.2%})"
    )

    # Train final model
    logger.info("[TRAIN] Training final model on all data...")
    train_results = model.train(X, y, FEATURE_NAMES)

    # Evaluate on training data
    y_pred = model.model.predict(X)
    report = classification_report(y, y_pred, target_names=["loss", "win"], output_dict=True)

    logger.info(f"[TRAIN] Training accuracy: {report['accuracy']:.2%}")
    logger.info(f"[TRAIN] Win precision: {report['win']['precision']:.2%}  recall: {report['win']['recall']:.2%}")
    logger.info(f"[TRAIN] Loss precision: {report['loss']['precision']:.2%}  recall: {report['loss']['recall']:.2%}")

    # Feature importance
    logger.info("[TRAIN] Top features:")
    for name, imp in train_results["top_features"]:
        logger.info(f"  {name:25s}: {imp:.4f}")

    if dry_run:
        logger.info("[TRAIN] Dry run — model NOT saved")
        return

    # Save
    model.save()
    logger.info(f"[TRAIN] ✅ Model saved to {MODEL_PATH}")
    logger.info(f"[TRAIN] Next: model will be loaded by main.py at startup for live inference")


def print_summary():
    """Print a summary of the current training dataset."""
    try:
        X, y, rows = load_labeled_data()
    except ValueError as e:
        logger.error(f"[TRAIN] {e}")
        return

    logger.info("=== Training Data Summary ===")
    logger.info(f"  Total labeled: {len(y)} ({int(y.sum())}W / {len(y)-int(y.sum())}L)")

    # By regime
    regimes = {}
    for r in rows:
        reg = r.get("regime", "?")
        regimes.setdefault(reg, {"win": 0, "loss": 0})
        if r["trade_outcome"] == "win":
            regimes[reg]["win"] += 1
        else:
            regimes[reg]["loss"] += 1
    for reg, counts in sorted(regimes.items()):
        total = counts["win"] + counts["loss"]
        wr = counts["win"] / total * 100 if total else 0
        logger.info(f"  {reg:12s}: {total:3d} total, {wr:.0f}% win rate")

    # Avg PnL by regime
    for reg in sorted(regimes):
        pnls = [float(r["realized_pnl"]) for r in rows if r.get("regime") == reg and r.get("realized_pnl")]
        if pnls:
            logger.info(f"  {reg:12s}: avg PnL={np.mean(pnls):+.3f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Train signal gate ML model")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without saving model")
    parser.add_argument("--retrain", action="store_true", help="Force retrain even if model exists")
    parser.add_argument("--summary", action="store_true", help="Print dataset summary only")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        train_and_save(dry_run=args.dry_run, retrain=args.retrain)
