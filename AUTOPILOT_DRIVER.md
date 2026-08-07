# AUTOPILOT DRIVER — paste this as the first message of the session

You are executing a multi-spec hardening program on this trading bot repository, autonomously, in the order defined below. Work continuously without asking for approval on file edits, test runs, package installs, or commits. Stop only at the conditions explicitly listed in **HARD STOPS**.

---

## Specs, in execution order

1. `PROMPT_signal_gate_model_ADDENDUM.md` — **tasks A1 and B2 only**
2. `PROMPT_signal_analyzer_restructure.md` — **P0 + Phase 1 only** (not Phase 2)
3. `PROMPT_futures_grid_scalper_orderly_hardening.md` — full spec
4. `PROMPT_signal_gate_training_hardening.md` — full spec, **conditional on GATE 1**
5. `PROMPT_signal_gate_model_ADDENDUM.md` — remaining tasks, **conditional on stage 4**

Load exactly one spec into working context at a time. When you finish a stage, drop that spec from your working set before opening the next. If a task in one spec appears to require changes described in a different spec, note it in `PROGRESS.md` and leave it for that spec's stage — do not implement it early.

---

## STAGE 0 — Preflight (do this first, before any code)

1. Confirm the git working tree is clean. If not, stop and report.
2. Read `.env` and check whether `ORDERLY_API_KEY`, `ORDERLY_SECRET`, `BINANCE_API_KEY`, and `BINANCE_API_SECRET` hold non-empty values. **If any is non-empty, STOP immediately** and tell me to clear them before autopilot continues. Do not edit `.env` yourself.
3. Confirm the test suite runs (`pytest --collect-only`). If there is no test infrastructure, create it as part of stage 1.
4. Read `db/db_ops.py` and the `signal_history` schema. Record the actual column names, types, and timestamp storage format in `PROGRESS.md` — later stages depend on these and must not guess.
5. Create `PROGRESS.md` at repo root using the template at the end of this document.

Report a one-paragraph preflight summary, then continue to Stage 1 without waiting.

---

## Per-stage protocol

For every stage, in this exact order:

1. `git checkout -b harden/<stage-slug>` from the current main branch.
2. Read the spec fully. Write your implementation plan into `PROGRESS.md`: files to modify, tasks in order, and anything you cannot implement as written.
3. Implement.
4. Write the tests the spec's acceptance criteria call for. Run them.
5. If tests fail, fix and retry — up to **3 attempts per criterion**. After 3, record the failure in `PROGRESS.md`, mark that criterion FAILED, and continue to the next task. Do not silently skip.
6. `git add -A && git commit -m "harden(<stage-slug>): <summary>"`. Commit per logical task, not once per stage.
7. Update `PROGRESS.md`: status, pass/fail per acceptance criterion **by number**, and any deviation from the spec with the reason.
8. Continue to the next stage without waiting, unless a HARD STOP or a GATE applies.

Never mark a criterion passed without a test that demonstrates it.

---

## STAGE 1 — Model safety (spec 05, tasks A1 + B2 only)

Implement only:

- **A1** — the feature schema contract: sidecar meta file, hash validation at load, fail on mismatch.
- **B2** — the explicit fail-closed contract: `is_healthy`, `GateUnavailable`, and the audit of every caller of `get_model()` / `predict()` / `decide()`.

These are independent of everything else and close two silent-corruption paths. B2 requires a written audit: list every call site and its current behavior when the gate is unavailable, in `PROGRESS.md`. Fix any call site that proceeds ungated.

Do not implement A2, A3, A4, or any other B task at this stage.

---

## STAGE 2 — Analyzer (spec 02, P0 + Phase 1)

Implement P0.1 through P0.5 and F1.1 through F1.4. **Do not implement Phase 2** — it depends on a replay labeler that does not exist.

Then run the analyzer against the real database. It is read-only; running it is permitted and required.

Produce the expectancy-by-regime table (F1.3) and write the full numbers into `PROGRESS.md`.

### GATE 1 — decision rule

Apply this rule mechanically to the reversal strategy's results in TREND regimes:

- **PROCEED** if: `mean_pnl > 0` **and** `n_closed >= 30` **and** the sign of `mean_pnl` agrees across both halves of the window (the F1.2 stability check).
- **HALT** otherwise.

If PROCEED: continue to Stage 3, and Stages 4 and 5 remain in scope.

If HALT: continue to Stage 3 anyway (the futures grid work is independent), then **STOP** and report. Do not begin Stage 4 or 5. Training a model to gate a strategy with no measured edge is work with a guaranteed zero at the end, and I want to see the numbers before that decision is reversed.

Record the rule's inputs and the resulting decision explicitly in `PROGRESS.md`.

---

## STAGE 3 — Futures grid (spec 03, full)

Full spec, P0 through P2, in the order the spec gives.

Two tasks in it are load-bearing and must not be summarized or deferred:

- **P0.3** — bracket verification, including the path where a filled entry with no confirmed stop is market-closed within the same cycle. This is the difference between a bug and a liquidation.
- **P1.1** — the reward/risk inversion, plus the startup refusal when the ratio is below the minimum.

The spec requires reading `trading_bot/futures_executor_apolo.py` in full before writing code, to confirm actual signatures and return types. Do that; do not assume.

End the stage with the module in dry-run (`grid_dry_run = 1`). **Do not run the bot.**

---

## STAGE 4 — Training pipeline (spec 04, full) — only if GATE 1 said PROCEED

Full spec, P0 and P1.

- **P1.4** (the look-ahead feature audit) requires a written table: feature name → data source → timestamp of latest input → verdict. Put it in `PROGRESS.md`. This is analysis, not code, and it is not optional.
- **Acceptance criterion 1** is the proof the temporal leak is closed: a synthetic dataset whose label depends on a future value must score near chance under the new validation and near-perfect under the old `StratifiedKFold(shuffle=True)`. Assert both. If the new validation also scores well on that fixture, something is still leaking — stop and report rather than continuing.

You may run `python -m trade.signal_agent.train --dry-run` and `--summary`. **You may not run training without `--dry-run`.**

---

## STAGE 5 — Model addendum remainder (spec 05) — only if Stage 4 completed

Remaining tasks: A2, A3, A4, B1, B3–B9. A1 and B2 are already done.

Implement in the order given at the end of that spec. A2 and B1 come last, because a threshold tuned before Stage 4's honest validation splits exist is the same arbitrary number as the hardcoded 0.80.

---

## HARD STOPS — stop and ask, even in autopilot

- Any `.env` value is non-empty at preflight.
- A task requires live API credentials, or would place, cancel, or modify a real order.
- Running any bot entry point, `main.py`, or any module that can trade. Dry-run and read-only analysis are fine; live execution is never.
- `python -m trade.signal_agent.train` without `--dry-run`.
- `git push`, `git reset --hard`, `rm -rf`, or anything that destroys uncommitted work.
- GATE 1 returns HALT (stop after Stage 3).
- A spec's stated invariant cannot be satisfied as written.
- Actual DB schema or executor signatures contradict a spec's assumptions in a way that changes the task.
- The same acceptance criterion fails 3 times.
- Any change that would alter live trading configuration values (leverage, capital, position limits, thresholds) in the database.

At a hard stop: commit what works, write the reason in `PROGRESS.md`, report, and wait.

---

## Final report

When you stop — at completion, at GATE 1 HALT, or at a hard stop — produce:

1. Stages completed, with branch names and commit counts.
2. Acceptance criteria pass/fail by number, per spec.
3. The GATE 1 numbers and decision.
4. Every deviation from a spec, with the reason.
5. Anything you could not implement, and what it would need.
6. The exact commands I should run to verify, and what output to expect.

Do not claim a stage complete if any acceptance criterion failed. Report partial completion honestly.

---

## PROGRESS.md template

```markdown
# Hardening progress

## Preflight
- Git tree:
- Credentials cleared:
- Test infrastructure:
- signal_history schema (columns, types, timestamp format):

## Stages

| # | Spec | Scope | Status | Branch | Criteria passed |
|---|------|-------|--------|--------|-----------------|
| 1 | model ADDENDUM | A1, B2 | ☐ | | |
| 2 | analyzer | P0 + Phase 1 | ☐ | | |
| 3 | futures grid | full | ☐ | | |
| 4 | training | full (conditional) | ☐ | | |
| 5 | model ADDENDUM | remainder (conditional) | ☐ | | |

## GATE 1 — reversal expectancy in TREND
mean_pnl:
n_closed:
first half / second half:
Decision (PROCEED / HALT):

## Caller audit (Stage 1, task B2)
| Call site | Behavior when gate unavailable | Fixed? |

## Feature look-ahead audit (Stage 4, task P1.4)
| Feature | Source | Latest input timestamp | Verdict |

## Deviations from spec

## Failed criteria
```
