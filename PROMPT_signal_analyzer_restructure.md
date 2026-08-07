# Implementation Prompt — Restructure `signal_analyzer.py` around outcomes

## Role and scope

You are restructuring the signal history analyzer for a live crypto trading bot. Target file: `signal_analyzer.py` (project root or `trade/`, adjust to the repo).

**This is not primarily a bug-fix task.** The file is competently written and does what it was designed to do. The problem is that what it was designed to do is not useful: all eight analyses measure **approval rate** — how often the signal filter said yes — and none measure whether saying yes was correct. A filter approving 95% of signals and a filter approving 5% both produce a clean, healthy-looking report, and nothing in the output distinguishes a filter selecting winners from one selecting losers.

The `signal_history` table already contains `trade_outcome` and `realized_pnl` (`trade/signal_agent/train.py` reads both). No function in this file joins to them.

The rebuild has two phases. **Phase 1** is everything computable from data you have today. **Phase 2** unlocks once the historical replay labeler from `PROMPT_signal_gate_training_hardening.md` (task P1.4 / validation step) exists. Build Phase 1 completely and structure the code so Phase 2 is additive, not a rewrite.

Keep the CLI shape (`--days`, `--export`, `--limit`) and the `SignalAnalyzer` class. Read `db/db_ops.py` and the `signal_history` schema before starting — several tasks depend on the actual column names, types, and timestamp storage format. If `pandas` is already in the project requirements (likely, given the sklearn/XGBoost stack), use it; otherwise stay on plain Python and do not add it.

---

## Invariants

1. **Every breakdown reports outcomes alongside counts.** A dimension that cannot be tied to money is reported as activity, and labelled as activity.
2. **No rate is printed without its sample size.** `3/4 (75%)` and `300/400 (75%)` must not render with equal authority.
3. **Nothing is called "effectiveness" that measures behavior rather than result.** Honest naming is a requirement, not a style preference.
4. **The JSON export is machine-readable.** Numbers are numbers, never pre-formatted strings.
5. **Every number is reproducible from the log.** Window boundaries, row counts, and exclusions are always reported.

---

## P0 — Correctness. Fix before trusting any output, current or historical.

### P0.1 — The time window is wrong in two ways

```python
self.utc_now = datetime.now(timezone.utc)
self.user_now = self.utc_now - timedelta(hours=4)
self.cutoff_date = (self.user_now - timedelta(days=days_back)).isoformat()
```

**Wrong span:** UTC is shifted back 4 hours to local time, and the result is used as a cutoff against `timestamp` values stored in UTC. `--days 7` returns 7 days and 4 hours, offset in the wrong direction.

**Wrong type:** `user_now` retains `tzinfo=utc`, so `.isoformat()` produces `2026-07-18T10:00:00+00:00` — a local time string carrying a UTC label. That string is then compared against however the DB stores timestamps. Against naive ISO strings the `+00:00` suffix makes prefix comparisons unpredictable; against `Z`-suffixed strings or epoch integers it silently matches wrong rows or none.

Required:

- Compute the cutoff in UTC and compare UTC to UTC. Local time (`UTC-4`) is for **display only**; if the report shows local timestamps, label them as local.
- Determine the actual stored format by querying `MIN(timestamp)`, `MAX(timestamp)`, and `typeof(timestamp)` once, and build the parameter to match. Assert the format at startup and fail with a clear message on mismatch.
- Make the local offset a setting rather than a hardcoded `4`, and read it from the same place the rest of the bot does.
- Print the resolved window (`from … to …, UTC`) and the row count at the top of every report.

### P0.2 — `--limit` is inert, and truncation is silent

`--limit` is parsed and never passed to `SignalAnalyzer`; every method hardcodes `limit=10000`. And `ORDER BY timestamp DESC LIMIT 10000` over a 30-day window containing more signals returns the most recent 10k while the report header still claims 30 days.

Pass `--limit` through. Before fetching, `SELECT COUNT(*)` for the window; if the limit binds, print a prominent warning stating the true count and that results cover a shorter effective period. Default the limit to `None` (no cap) and treat it as a safety valve, not a normal operating mode.

The module docstring also advertises `--pattern` and `--approved-only`, neither of which exists. Implement or remove; do not ship a docstring that lies.

### P0.3 — One fetch, not seventeen

Each of the eight analyses calls `get_signals()` independently, re-running the query and re-parsing JSON for every row; `export_json` then runs all eight a second time. Fetch once in the constructor or an explicit `load()`, cache, and pass the collection to each analysis. Analyses become pure functions of the loaded data.

### P0.4 — Missing data is being coerced into meaning

- `obi = sig.get('obi', 1.0) or 1.0` converts NULL **and a legitimate 0.0** into 1.0, silently padding the neutral bucket with rows that carry no information. Same coercion via `if sig['obi']:` in `analyze_by_regime`.
- `rejected = total - approved` treats "not yet evaluated" (NULL) identically to "the filter rejected it."

Exclude missing values explicitly, count the exclusions, and report them in every affected section. Filter approval on `approved IN (0, 1)`. A bucket where most rows were excluded must say so rather than presenting a rate computed from the remainder.

### P0.5 — `abs()` conceals the finding that matters

```python
'warning_impact': f"{abs(without_rate - with_rate):.1f}% difference"
```

"Manipulation warnings correlate with *higher* approval" would be an alarming result about your filter. It currently renders identically to the reassuring one. Keep the sign, and state the direction in words.

---

## Phase 1 — Outcome metrics computable today

For trades that were **approved and executed**, you have real outcomes. That is enough to answer the most important open question in this project: does the reversal strategy have positive expectancy in TREND regimes, and in which slices?

### F1.1 — Add an outcome layer to every breakdown

Every dimension — pattern, regime, asset, OBI bucket, candle count, manipulation warning presence — returns this structure:

```python
{
  "n_signals": 412,
  "n_approved": 280,
  "approval_rate": 0.68,          # float, not a string; demoted to a secondary metric
  "n_closed": 240,                # approved AND has a resolved outcome
  "win_rate": 0.44,
  "win_rate_ci95": [0.38, 0.50],  # Wilson interval
  "mean_pnl": 0.0031,
  "median_pnl": 0.0012,
  "total_pnl": 0.744,
  "largest_loss": -0.052,
  "profit_factor": 1.21,          # gross wins / gross losses
  "sufficient_data": true         # n_closed >= min_n
}
```

`mean_pnl` and `total_pnl` are the headline figures in the printed report. `approval_rate` moves to the end of each row. The report is sorted by `total_pnl` descending, so the slices making and losing money are the first thing visible.

### F1.2 — Statistical honesty

- `min_n` setting, default `30` closed trades. Below it, `sufficient_data` is false, the printed row is marked `insufficient data (n=7)`, and **no percentage is shown at all**. Most per-pattern breakdowns in this dataset are almost certainly in noise territory today; the report must say so rather than print a confident-looking number.
- Wilson score intervals on every rate. A 44% win rate on 240 trades and on 9 trades must look visibly different on the page.
- **Stability check:** split the window into two halves and report each slice's `mean_pnl` in both. Add a `stable` flag when the sign agrees. A pattern that made money only in the first half is a pattern that stopped working, and that is invisible in an aggregate.

### F1.3 — Two new analyses that directly drive decisions

**`analyze_expectancy_by_regime()`** — the single most useful output in the file. For each regime, mean PnL per closed trade, total PnL, trade count, and the split by strategy type (grid vs reversal). This answers whether reversal-in-TREND is worth gating at all, which currently blocks the ML work.

**`analyze_pnl_concentration()`** — sort closed trades by PnL and report what fraction of total profit comes from the top 5 trades, and what fraction of total loss from the worst 5. If a handful of outliers carry the result, the aggregate expectancy is not a reliable basis for any decision, and the report should say that in plain words.

### F1.4 — Rename to match what it measures

Phase 1 cannot measure filter effectiveness, because rejected signals have no outcomes. Rename `analyze_manipulation_impact` and the "filter effectiveness" language in the docstring and report headers to reflect what is actually computed: approved-signal outcomes and filter activity. The Phase 2 section below is where effectiveness becomes measurable. Do not let the honest naming slip.

---

## Phase 2 — Counterfactual evaluation (requires the replay labeler)

Gated behind a `--counterfactual` flag that errors clearly if replay labels are unavailable.

Once every signal — approved or rejected — carries a label for what it *would* have done, this file becomes the most valuable diagnostic in the repo.

### F2.1 — Filter uplift

Per dimension: `mean_pnl(approved) - mean_pnl(all signals)`. This is the filter's actual contribution. Near zero means the filter is decoration. Negative means it is selecting your losers — which happens, and which the current report would render as a healthy approval rate.

### F2.2 — Per-rejection-reason accounting

**This is the highest-value analysis in the document.** For every distinct string in `rejection_reasons`, compute over the signals it rejected: count, hypothetical win rate, hypothetical mean PnL, and **net PnL saved** (losses avoided minus profits forgone).

Rank rules by net PnL saved. Rules at the top earn their place. Rules with negative net saved are destroying edge and should be removed or loosened. A rule firing on fewer than `min_n` signals is reported as unproven, not as neutral.

This turns a wall of rejection counts into a ranked list of which filter rules to keep, tune, or delete.

### F2.3 — ML gate contribution

Once the gate runs in shadow mode (base prompt, validation step 2), compare realized expectancy of trades taken against hypothetical expectancy of the set the gate would have passed. Report uplift with a confidence interval. This is the same number the training holdout estimates; a large divergence between them means leakage remains in the training pipeline.

---

## Output contract

- **JSON export:** all numeric values as `int`/`float`. `'approval_rate': f"{pct:.1f}%"` makes the export unusable for sorting, plotting, or aggregation downstream. Format only at print time.
- Export includes a metadata block: generation time, window bounds in UTC, total rows, rows excluded and why, `min_n`, schema version, and whether counterfactual labels were used.
- Write exports to a proper output directory, not next to the source file.
- Add `--format {text,json,both}` and make the JSON path an argument.
- Exit non-zero when the window contains no data, so scheduled runs surface failures.

---

## Robustness

- Wrap `json.loads` per row; one malformed `rejection_reasons` value currently kills the entire run. Count and report parse failures.
- `dict(row)` assumes a configured row factory — assert it, or select columns explicitly.
- `logger.error(f"Analysis error: {e}")` in `main` discards the traceback. Use `logger.exception`.
- Remove the unused `sqlite3` import; the DB layer is already abstracted and this file should not know the engine.
- If the query grows slow, push aggregation into SQL rather than fetching every row — but only after the metrics are correct. Correct and slow beats fast and wrong.

---

## Acceptance criteria

Tests in `tests/test_signal_analyzer.py` against a fixture DB, no network.

1. `--days 7` returns exactly the rows with UTC timestamps inside the last 7 days — verified against a fixture with rows placed deliberately just inside and just outside both boundaries, including one at the 4-hour offset that currently leaks in.
2. `--limit` reaches the query, and a bound limit produces a warning naming the true row count.
3. The database is queried once per run regardless of how many analyses execute.
4. A NULL `obi` lands in no bucket and increments an exclusion counter; a `0.0` OBI lands in `extreme_bearish`, not `neutral`.
5. A NULL `approved` is counted as neither approved nor rejected.
6. A slice with `n_closed < min_n` renders as insufficient data with no percentage shown.
7. Wilson intervals match a reference implementation on known inputs.
8. `warning_impact` retains its sign, and a positive-direction fixture is described as such in the text output.
9. Every value in the JSON export passes `isinstance(v, (int, float, bool, str, list, dict))` with no percent-suffixed strings among numeric fields.
10. One malformed JSON field produces a counted parse failure and does not abort the run.
11. With `--counterfactual` and no replay labels present, the command exits non-zero with an actionable message.
12. Per-rejection-reason net-PnL-saved matches a hand-computed fixture, including a rule whose net saved is negative.

---

## What this should let you decide

Build toward these questions; if the output does not answer them, the restructure is not finished.

1. **Does the reversal strategy have positive expectancy in TREND regimes?** If no slice is positive, the ML gate work should be deferred and the strategy fixed first.
2. **Which patterns and OBI buckets actually made money**, with enough samples to believe it, and did they keep working across both halves of the window?
3. **Which filter rules earn their place** (Phase 2) — ranked, with the ones destroying edge visible.
4. **Is the aggregate driven by a handful of outlier trades**, in which case none of the above is yet decidable and the honest answer is "collect more data."

Run Phase 1 as soon as it lands and report the regime expectancy table before starting Phase 2 or any further ML work.
