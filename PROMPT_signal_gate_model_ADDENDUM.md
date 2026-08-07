# Addendum — `trade/signal_agent/model.py`

## How to use this document

This is an **addendum to `PROMPT_signal_gate_training_hardening.md`**. Read both documents fully before writing any code. They describe one change set across three files: `train.py`, `model.py`, and `features.py`.

Four tasks (**A1–A4** below) span both files. Implement each as a single unit touching every file it needs. Do not implement the `train.py` half in one pass and the `model.py` half in another — half of a schema contract is worse than none, because it looks implemented.

Where this addendum and the base prompt differ regarding `model.py`, this document wins.

`model.py` currently contains no crashes. That is precisely why it is dangerous: every failure mode in it produces a plausible number rather than an error.

---

## Additional invariants

These extend the base prompt's invariant list.

1. **No gate means no trades.** A model that is missing, failed to load, or fails schema validation must result in *zero* trades in the affected regime. It must never result in unfiltered trading. This is the single most important rule in either document.
2. **No probability output is assumed calibrated.** Thresholds are chosen by measurement, never by intuition.
3. **Nothing is loaded without validating what it is.** A file existing at the expected path is not evidence it is the right model.
4. **A partial write must never become the live model.**

---

## A — Cross-file tasks

### A1 — Feature schema contract

Implements P1.3 of the base prompt. Touches `model.py`, `train.py`, `features.py`.

`save_model()` serializes the booster only. No feature names, no threshold, no training date, no sample count, no library version. `load()` validates none of it and sets `self._loaded = True` unconditionally on success. Reorder two entries in `FEATURE_NAMES` and this loads a model trained on the old layout, feeds it columns in the wrong positions, and returns confident nonsense indefinitely without raising.

Write a sidecar `signal_model_meta.json` next to the model file:

```json
{
  "schema_version": 1,
  "feature_schema_hash": "<sha256 of '|'.join(FEATURE_NAMES)>",
  "feature_names": ["..."],
  "n_features": 11,
  "threshold": 0.62,
  "trained_at": "2026-07-25T14:03:11Z",
  "n_samples": 1240,
  "class_balance": {"win": 0.41, "loss": 0.59},
  "train_date_range": ["2026-01-04", "2026-06-30"],
  "regime": "TREND",
  "symbols": ["PERP_NEAR_USDC"],
  "holdout_metrics": {"expectancy": 0.0031, "uplift_vs_no_gate": 0.0018, "pass_rate": 0.27},
  "hyperparameters": {"...": "..."},
  "library_versions": {"xgboost": "2.1.1", "numpy": "1.26.4"}
}
```

`save()` writes model and meta together or neither. `load()` reads the meta first, recomputes the hash from the current `FEATURE_NAMES`, and on mismatch: refuses to load, logs an ERROR naming both hashes and both feature lists, sends a Telegram alert, and leaves the gate unhealthy. A missing meta file is treated as a mismatch — legacy models without one are not loadable.

### A2 — Threshold ownership

Implements P1.1 of the base prompt.

```python
DEFAULT_THRESHOLD = 0.80  # score > 0.80 → enter trade
```

`train.py` never chooses a threshold and `model.py` hardcodes one. The two halves of the system do not communicate, and 0.80 was picked by intuition against scores that are not calibrated probabilities (see B1).

- `train.py` sweeps and selects the threshold on the validation partition, by expectancy, subject to `min_pass_rate`.
- The chosen value is persisted in the meta file.
- `decide()` reads it from `self.threshold`, populated at load. Change the signature to `decide(X, threshold: Optional[float] = None)` where `None` means "use the persisted value" and an explicit argument is for backtesting only.
- If no persisted threshold exists, `decide()` raises. It must not silently fall back to a constant.
- Keep `DEFAULT_THRESHOLD` only as a clearly-labelled backtest default, or delete it. It is never a live decision source.

### A3 — `sample_weight` support

Implements P1.2 of the base prompt. `train()` cannot currently accept magnitude-aware weights.

```python
def train(self, X, y, feature_names, sample_weight: Optional[np.ndarray] = None) -> dict:
    ...
    self.model.fit(X, y, sample_weight=sample_weight)
```

Log the weight distribution summary (min/median/max) when weights are supplied, so a broken weight array is visible in the logs rather than silently flattening the objective.

### A4 — One hyperparameter definition

The `XGBClassifier` kwargs are duplicated between `SignalGateModel.train()` and `cv_model` in `train.py`. They will drift, and when they do, cross-validation will be measuring a different model than the one deployed.

Define once in `model.py`:

```python
BASE_PARAMS = {
    "n_estimators": 100, "max_depth": 4, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8,
    "eval_metric": "logloss", "random_state": 42,
}
```

Import it in `train.py` for the CV estimator. Add a test asserting the CV model and the production model are constructed from the same dict.

---

## B — `model.py` only

### B1 — Score distribution and calibration

Two independent effects make the model's output not a probability:

**`scale_pos_weight` deliberately shifts the output distribution.** Setting it to `n_losses / n_wins` is reasonable for ranking, but the resulting scores are inflated estimates on a rebalanced objective. A "0.80" is not a claim that 80% of such signals win.

**Small samples plus tree ensembles produce extreme outputs.** With a few hundred noisy samples at `max_depth=4` and 100 estimators, the model largely memorizes, and memorized leaves push predictions toward 0 and 1. The score distribution clumps at the extremes rather than spreading across the range — so a 0.80 cutoff can *look* highly selective while passing nearly everything the model leans positive on. The current configuration may be running an effectively open gate that appears strict.

Required:

1. Add `diagnose_scores(X, y, pnl) -> dict`, called on the holdout by `train.py`. It logs a decile histogram of `predict_proba` output and a reliability table (predicted probability decile → realized win rate → mean PnL → count). Include it in the training log every run. **Look at this output before trusting any threshold.**
2. Make `scale_pos_weight` a parameter with default `1.0` (off). With magnitude-based `sample_weight` from A3 plus an expectancy-tuned threshold, class rebalancing is redundant and double-tilts the objective. Retain the option, but require it to be set deliberately.
3. Optional, only if the reliability table shows severe miscalibration and you want interpretable scores: wrap with `CalibratedClassifierCV(method="sigmoid")` fitted on the validation partition. Platt scaling over isotonic — isotonic overfits at these sample sizes. Note that calibration is cosmetic for gating: a tuned threshold on uncalibrated ranking works fine. Do this for interpretability, not performance.

### B2 — Explicit fail-closed contract

```python
except Exception as e:
    logger.warning(f"[MODEL] Failed to load model: {e}")
    self._loaded = False
    return False
```

`load()` returns `False`, and the safety direction is then decided by whatever the caller does — currently somewhere else in the codebase. That is the difference between "no trades in TREND regime" and "every reversal signal fires unfiltered."

- Add `is_healthy -> bool`: loaded, schema-validated, threshold present, and a smoke prediction succeeded (B7).
- Add a `GateUnavailable(Exception)` raised by `predict()` and `decide()` when not healthy, distinct from any data-shape error.
- **Audit every caller of `get_model()`, `predict()`, and `decide()` in the codebase.** Confirm each treats an unavailable gate as "take no trade." Report each call site and its current behavior. Fix any that proceed ungated.
- Add a module-level docstring stating the contract in one sentence.

### B3 — Input validation

XGBoost treats `NaN` as a legitimate missing value and will return a confident score for an entirely broken feature vector. A feature computation that silently fails becomes a trade decision.

In `predict()`, before inference: assert `X.ndim == 2`, assert `X.shape[1] == meta["n_features"]`, and reject any row containing `NaN` or `inf` by raising — logging which column index failed. Do not impute, do not clip. A broken feature vector means no trade.

In `decide()`, the blind `X.reshape(1, -1)` silently flattens a 2-D array into one nonsense sample. Assert the input is 1-D, or that it is 2-D with exactly one row.

### B4 — Threading

`nthread=8` for single-row inference is a latency penalty, not a speedup: spawning eight threads to evaluate 100 shallow trees on one sample costs more than the computation. Use `n_jobs=1` for inference and the full core count for training. Also `nthread` is the native-API parameter name; the sklearn wrapper expects `n_jobs`, so the current setting may be silently ignored — verify against the pinned XGBoost version and log the effective thread count once at load.

### B5 — Singleton staleness

```python
def get_model() -> SignalGateModel:
    global _gate_model
    if _gate_model is None:
        _gate_model = SignalGateModel(nthread=8)
    return _gate_model
```

Cached forever. A model saved by `train.py` is not picked up until the bot restarts — so a retrain appears to succeed and changes nothing. Add an mtime check against the model file on access (cheap) or an explicit `reload()` callable from the Telegram interface, and guard the singleton with a lock if any part of the loop is concurrent. Log both the old and new `trained_at` on a successful reload.

### B6 — Model paths per regime and symbol

`MODEL_PATH` as a module constant blocks the per-regime and multi-asset models the base prompt calls for in P1.5 and P1.6. Replace with `model_path(regime: str, symbols: list[str] | None = None) -> Path` now, so those become configuration changes later rather than a refactor. Keep a backward-compatible default resolving to the existing `data/signal_model.json`.

### B7 — Verify the load actually worked

Before setting `_loaded = True`, run a smoke prediction on a zero vector of the expected width and confirm it returns a finite float in `[0, 1]`. A truncated or corrupt model file can load without raising and then misbehave at inference.

### B8 — Atomic save

`self.model.save_model(str(save_path))` writes in place. A crash mid-write leaves a corrupt file at the live model path — and with B2 in effect, that means the gate is unhealthy and trading stops until someone notices. Write the model and meta to temporary files in the same directory, then `os.replace()` both into position. Combined with the versioned paths from P0.3 of the base prompt, this makes rollback trivial.

### B9 — Cleanup

- Remove unused imports: `json` (used once meta writing lands), `os` (same — keep if used by `os.replace`).
- `use_label_encoder=False` was removed in XGBoost 2.x; check the pinned version and drop it if so.
- `save()` logging a warning and returning silently when nothing is trained should raise instead — a caller asking to save an untrained model has a bug.
- Log the effective configuration (params, threshold, n_features, schema hash, model path) once at load, so any live decision is reproducible from logs alone.

---

## Additional acceptance criteria

Extend the base prompt's list. Tests in `tests/test_signal_agent_model.py`, no filesystem writes outside `tmp_path`, no network.

1. Saving then loading round-trips the model, threshold, feature names, and schema hash; the loaded threshold is the one `decide()` uses.
2. Mutating `FEATURE_NAMES` between save and load causes `load()` to return `False`, leaves `is_healthy` false, and makes `decide()` raise `GateUnavailable`.
3. A model file with no meta sidecar is not loadable.
4. `predict()` raises on `NaN`, on `inf`, and on wrong column count, naming the offending column.
5. `decide()` raises on a 2-D input with more than one row.
6. With no model file present, `is_healthy` is false and every caller path results in no trade — assert on the caller integration, not just on the model object.
7. A save interrupted before completion leaves the previous model intact and loadable.
8. `get_model()` returns a reloaded instance after the model file's mtime changes.
9. The CV estimator in `train.py` and the production estimator are built from identical parameters.
10. `train()` forwards `sample_weight` to `fit()`.
11. `diagnose_scores` on a synthetic set with a known win rate produces a reliability table whose deciles match within tolerance.

---

## Order of implementation

1. A1 and B2 first. Together they close the silent-corruption path and give every other failure a defined safety direction. Nothing else matters until an unhealthy gate reliably means no trading.
2. B7, B8, B3 — load verification, atomic save, input validation. Cheap, and they remove the remaining silent-failure routes.
3. A3, A4, B4, B5, B6, B9 — mechanical.
4. A2 and B1 last, and **in that order**, because the threshold cannot be chosen sensibly until the base prompt's honest validation splits exist. A threshold tuned on leaked data is the same arbitrary number as 0.80, wearing a lab coat.

Before deploying anything: run `diagnose_scores` on the holdout and report the decile histogram. If the scores clump above 0.9, say so explicitly rather than proceeding — it means the gate was never filtering, and every historical result attributed to it needs reinterpreting.
