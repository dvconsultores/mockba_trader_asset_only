# Implementation Prompt — Fix `trade/signal_agent/train.py` (ML signal gate)

## Role and scope

You are fixing the training pipeline for an **XGBoost signal gate** that decides which reversal signals get traded during TREND regimes in a live crypto trading bot. Target file: `trade/signal_agent/train.py`, with supporting changes in `trade/signal_agent/model.py` and `trade/signal_agent/features.py`.

This model gates real capital. A model that scores well on a leaky validation split and fails live is worse than no model, because it is trusted.

**Do not rewrite the pipeline from scratch.** Keep the CLI surface (`--dry-run`, `--retrain`, `--summary`), the `SignalGateModel` interface, and the existing imports. No new third-party dependencies beyond what scikit-learn and XGBoost already provide.

Before writing code, **read `trade/signal_agent/features.py` and `trade/signal_agent/model.py` in full.** Several tasks depend on how features are computed and how the model is serialized. Task P1.3 and P1.5 in particular cannot be done correctly without knowing what is actually in `FEATURE_NAMES`.

---

## Non-negotiable invariants

1. **No future data may influence a validation or test score.** Every split is temporal. Ever.
2. **No metric computed on data the model trained on may be reported, logged, or sent to Telegram** — not to the console, not in a notification, nowhere. If a number is reported, it came from data the model has never seen.
3. **A saved model may never be overwritten by a worse one.** Replacement requires a measured improvement on the same held-out data.
4. **A model may never run inference on features it was not trained on.** Feature schema is validated at load time and mismatches are fatal.
5. **The gate is scored by money, not by accuracy.**

---

## P0 — Every number this pipeline currently reports is invalid

### P0.1 — Cross-validation leaks the future into the past

```python
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
scores = cross_val_score(cv_model, X, y, cv=cv, scoring="accuracy")
```

Rows are loaded `ORDER BY timestamp ASC` and then shuffled into random folds. The model trains on later trades and validates on earlier ones. Because market state is strongly autocorrelated — signals minutes apart share nearly identical conditions — the validation set is effectively inside the training set. The reported CV accuracy is meaningless and will be optimistically high.

Replace with walk-forward validation plus an embargo gap that prevents a trade still open at split time from bleeding across the boundary:

```python
from sklearn.model_selection import TimeSeriesSplit

GAP_SAMPLES = _embargo_samples()   # see below
cv = TimeSeriesSplit(n_splits=5, gap=GAP_SAMPLES)
```

`_embargo_samples()` must be derived from data, not hardcoded: compute the maximum trade holding period present in `signal_history` (exit timestamp minus entry timestamp), convert to a sample count using the median inter-signal interval, and round up. Log the computed value. If holding-period columns are unavailable, add them to the schema — do not silently use a guess.

`load_labeled_data` must guarantee and assert that rows are strictly ordered by timestamp before any split is taken.

Also move the `scale_pos_weight` computation **inside** each fold. Currently it is computed from the full dataset's class balance and then used in every fold, which leaks the global label distribution.

### P0.2 — Final metrics are computed on the training set

```python
y_pred = model.model.predict(X)     # X is exactly what it just trained on
report = classification_report(y, y_pred, target_names=["loss", "win"], output_dict=True)
```

XGBoost at `max_depth=4` with 100 estimators will memorize a few hundred noisy samples. This training accuracy is then logged *and sent to Telegram*, so every retrain will announce an excellent model.

Restructure into three temporally ordered partitions:

```
[ ------------ train (70%) ------------ ][ embargo ][ -- validation (15%) -- ][ embargo ][ -- holdout (15%) -- ]
```

- **Train**: fitting only.
- **Validation**: hyperparameters and the decision threshold from P1.1.
- **Holdout**: touched exactly once, at the end, to produce the reported numbers. Never used for any selection decision.

Every logged metric and every Telegram field must come from the holdout. Delete the `classification_report` call on `X`. If you want a train-set number for diagnosing overfit, log it at DEBUG explicitly labelled `train_only_do_not_trust`, and keep it out of notifications.

### P0.3 — `save if improved` is documented but not implemented

The docstring says "6. Save if improved or new". `existing_accuracy` is assigned and never read. `model.save()` runs unconditionally. A retrain over a bad data window silently replaces a working production model.

Implement it properly:

1. If a model exists at `MODEL_PATH`, load it and score it on the **same holdout partition** as the candidate, using the primary metric from P1.1.
2. Save only if the candidate beats the incumbent by at least `min_improvement` (new setting, default `0.02` in expectancy terms — not a raw accuracy delta).
3. On save, write to a versioned path (`model_YYYYMMDD_HHMMSS.json`) and update `MODEL_PATH` as a copy or symlink, retaining the previous N versions for rollback.
4. Write a sidecar `model_meta.json` recording: training timestamp, sample count, class balance, date range of the training data, holdout metrics, chosen threshold, feature schema hash (P1.3), and library versions.
5. If the candidate loses, log the comparison and exit non-zero without saving.

Add a `--force-save` flag distinct from `--retrain` for the rare case of deliberate override, and log loudly when it is used.

### P0.4 — Minimum sample count is off by two orders of magnitude

```python
if len(y) < 20:
    logger.warning(...)
    return
```

Eleven features, a tree ensemble, and noisy financial labels. Twenty samples is not undertrained — it is noise-fitting with a confidence score attached.

Replace with a hard refusal (not a warning) at `min_training_samples`, default `500`, plus a minimum of `100` samples in the minority class. Below the threshold, log what is missing and exit non-zero. Additionally require a minimum **calendar span** (`min_training_days`, default `30`) so the model cannot be trained on 500 samples all drawn from a single week of one market regime.

---

## P1 — Required for the gate to be worth having

### P1.1 — Score by expectancy, not accuracy

Accuracy is the wrong objective. A gate that correctly rejects 90% of signals and passes the 10% that lose big scores well on accuracy and loses money. The purpose of the gate is the PnL of the trades it lets through.

`load_labeled_data` must additionally return a `pnl` array aligned with `X` and `y`, read from `realized_pnl`. Then implement the primary metric:

```python
def gate_expectancy(proba, pnl, threshold):
    """Mean PnL per signal that the gate would have allowed through."""
    passed = proba >= threshold
    if not passed.any():
        return 0.0, 0
    return float(pnl[passed].mean()), int(passed.sum())
```

Report and compare models on:

- **Gate expectancy** (mean PnL per passed signal) — the primary metric.
- **Total PnL** of passed signals.
- **Pass rate** — the fraction of signals allowed through. A gate that passes 2% of signals may have great expectancy and trade twice a month; report it so the tradeoff is visible.
- **Uplift vs no gate**: expectancy of passed signals minus expectancy of all signals. **This is the number that decides whether the model is worth running at all.** If uplift is not clearly positive on the holdout, the gate is not adding value and should not be deployed.
- Win-class precision and recall at the chosen threshold, as secondary diagnostics.

**Tune the decision threshold** on the validation partition by sweeping `0.30` to `0.80` in steps of `0.01`, choosing the threshold that maximizes expectancy subject to a minimum pass rate (`min_pass_rate`, default `0.10`) so the model cannot win by trading almost never. Persist the chosen threshold in `model_meta.json` and make the inference path use it. Do not use `predict()` with its implicit 0.5 cutoff anywhere.

### P1.2 — Labels discard magnitude

```python
y_list.append(1 if row["trade_outcome"] == "win" else 0)
```

A +0.05% scratch and a +3% winner are the same label; so are a -0.1% and a -4%. A model optimizing this objective will happily learn to select many tiny wins and a few catastrophic losses — the exact failure shape this bot is already exposed to elsewhere.

Two changes:

1. Define a win as PnL clearing round-trip costs by a margin, not merely by sign. Add `label_min_win_pct` (default: round-trip fee plus slippage, times 1.5). Trades between the thresholds are **excluded from training entirely** rather than assigned to a class — ambiguous labels teach the model noise.
2. Pass `sample_weight=np.abs(pnl)` (normalized) to `fit()` so large outcomes dominate the loss function. Verify `SignalGateModel.train` accepts and forwards sample weights; add the parameter if it does not.

### P1.3 — Feature schema contract

Nothing ties a saved model to the feature layout it was trained on. If `features.py` changes order, adds, or removes a feature, an old model silently receives misaligned inputs and produces confident garbage in live trading.

Compute `feature_schema_hash = sha256("|".join(FEATURE_NAMES))`, store it in `model_meta.json` at save time, and validate it in `SignalGateModel.load()`. On mismatch: refuse to load, log an ERROR naming both hashes, send a Telegram alert, and make the live inference path fail closed — **no gate means take no trades in that regime**, not take every trade.

### P1.4 — Audit features for look-ahead (do this before trusting any metric)

Read every feature in `features.py` and confirm each is computable **strictly from data available at the signal timestamp**. Any feature derived from the bar the trade closed on, from the exit price, from a forward-looking rolling window, or from a column written after the trade completed is look-ahead leakage and will produce excellent offline scores and zero live edge.

Produce a written table in the PR description: feature name → data source → timestamp of latest input → verdict. Remove or fix anything that fails. Add a unit test that reconstructs features for a historical signal using only rows with `timestamp <= signal_timestamp` and asserts they match what the live path produces.

### P1.5 — Enable multi-asset pooling

The gate is trained on one asset, which caps the dataset at that asset's signal rate. The intent is to extend it to assets with a similar volume and movement profile, and doing so multiplies available data — but only if features are scale-free.

Audit every feature for scale dependence. Percent moves, z-scores, OBI ratios, and ATR-normalized distances pool across assets. Absolute prices, raw volumes, and any feature carrying one asset's price scale do not — a pooled model would learn asset identity instead of market structure. Convert or drop them.

Then: add a `symbol` column to the training query, add asset as an explicit categorical feature so the model can still specialize, and add a `--symbols` CLI argument defaulting to all available. Report holdout metrics **per asset as well as pooled** — a pooled model that works on the majority asset and loses on the others is not ready.

### P1.6 — Separate regimes

`print_summary` already buckets by regime, which implies training data mixes RANGE grid signals with TREND reversal signals. These have different label distributions and different generating processes; one model spends capacity learning to tell them apart.

Short term: add `regime` as an explicit categorical feature and report holdout metrics per regime. Longer term, once sample counts allow, train separate models per regime with independent thresholds. Make the model path regime-aware (`MODEL_PATH` becomes a function of regime) so this is a config change later, not a refactor.

### P1.7 — Data quality gate

Before training, validate and abort with a clear message on failure:

- Duplicate signal ids or identical feature rows with conflicting labels.
- `NaN` or `inf` in `X` (report which feature and how many).
- Any feature with zero variance across the dataset.
- Class balance outside a sane band (e.g. below 15% minority class).
- Rows where `trade_outcome` and the sign of `realized_pnl` disagree.
- Gaps in the time series longer than `max_data_gap_days` (default `7`), which usually indicate the bot was off and the sample is not continuous.

Log a one-screen data-quality report every run, before any training happens.

---

## P2 — Cleanup

- Remove unused imports: `json`, `time`, `Optional`, `confusion_matrix`. Either use `confusion_matrix` in the holdout report or drop it.
- `if r.get("realized_pnl")` in `print_summary` silently drops zero-PnL trades because `0.0` is falsy. Use `is not None`.
- `use_label_encoder=False` was removed in XGBoost 2.x — check the pinned version and drop the argument if it no longer applies.
- The `--retrain` branch checks `if not retrain` twice in nested scope; flatten it.
- `dict(r)` on cursor rows assumes a row factory is configured. Assert it, or select columns explicitly.
- The Telegram notification must report holdout expectancy, uplift vs no gate, pass rate, sample count, date range, and chosen threshold. Remove training accuracy from it entirely.
- Log the full model configuration and all computed split boundaries at the start of every run so any reported result is reproducible from the log alone.

---

## Acceptance criteria

Write tests in `tests/test_signal_agent_train.py` using synthetic labeled data — no DB or network access in tests.

1. Synthetic data where the label is a deterministic function of a *future* value scores near chance under the new validation, and scores near-perfect under the old `StratifiedKFold(shuffle=True)`. This test is the proof the leak is closed — assert both.
2. No reported or notified metric is computed on any sample present in the training partition. Assert by index-set intersection.
3. Train, validation, and holdout partitions are contiguous in time, ordered, and separated by the computed embargo.
4. A candidate model scoring worse than the incumbent on holdout expectancy does not overwrite `MODEL_PATH`, and the process exits non-zero.
5. Fewer than `min_training_samples`, fewer than 100 minority-class samples, or a span under `min_training_days` each cause a hard refusal.
6. The chosen threshold maximizes validation expectancy subject to `min_pass_rate`, and the persisted threshold is the one used at inference.
7. Loading a model whose `feature_schema_hash` does not match the current `FEATURE_NAMES` raises, and the live gate path fails closed to "take no trades".
8. Trades whose absolute PnL falls inside the ambiguous band are excluded from `X` and `y`.
9. `sample_weight` reaches `XGBClassifier.fit`.
10. Each data-quality violation in P1.7 produces a distinct, identifiable failure.
11. `--dry-run` produces the full holdout report and writes no files.

---

## Validation before the gate controls capital

The fixes above make the metrics honest. They do not establish that the gate helps.

1. **Run `--summary` first and report raw reversal expectancy in TREND regimes, per asset.** If the underlying strategy has no positive-expectancy subset, a gate cannot manufacture one, and this whole component should be deferred until the strategy itself is fixed.
2. **Shadow mode.** Add a `gate_shadow_mode` setting (default `1`). The gate runs live, logs its decision and probability for every signal, and changes nothing about which trades are taken. After at least 100 gated signals, compare realized expectancy of the trades the gate *would* have passed against all trades taken.
3. The gate is enabled for real only when shadow-mode uplift is positive and consistent with the holdout estimate. If live uplift is materially below the offline number, there is leakage remaining — return to P1.4.
4. Re-validate on a rolling basis. Add a scheduled job comparing recent live gate performance to the holdout baseline, alerting on decay. Signal edges are not stationary; a gate trained on last quarter's market can quietly stop working.

Report the shadow-mode comparison before enabling the gate.