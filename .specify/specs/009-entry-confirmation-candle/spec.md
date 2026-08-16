# Feature Specification: 009 — Entry Confirmation Candle

**Feature Branch**: `009-entry-confirmation-candle` *(implementation on `main` per repo convention — the repo tracks only main, no feature branches)*

**Created**: 2026-08-15

**Status**: Clarified — ready for `/speckit.plan`. **Implementation NOT authorized.**

**Flow**: constitution → specify → **clarify** → plan → checklist → tasks → analyze → implement (AUTHORIZATION REQUIRED) → converge

---

## What

One setting-driven entry filter: an entry may only fire when the asset's **last completed 5m return is positive** (`close > open`; a flat bar does not confirm) — evidence that the fall the dip rule detected has actually paused. Today `_is_dip` measures only *displacement* from the rolling peak; it never checks whether price is still falling, so the bot routinely buys mid-fall.

Two modes, mirroring the toxicity convention (Amendment 001 — "observe-only by default"):

- **Observe (default).** The confirmation state is computed and recorded on every `signals` row, but never blocks. Zero behavior change; produces the A/B data to decide enforcement.
- **Enforce.** `entry_confirm_candle=true` makes an unconfirmed entry a recorded skip with reason `entry_not_confirmed`.

The candle series is the **same 5m series `get_atr_pct` already fetches and caches** (`trade/regime.py::_candle_cache`), so the filter costs **zero additional API calls**.

### Part 1 — Confirmation helper (`trade/regime.py`)

- NEW `last_closed_return_up(asset, venue) -> bool | None`: returns whether the last **completed** 5m bar's return is **positive** (`close > open`). A flat bar returns `False` — not confirmed (clarify Q1). Reuses the `_candle_cache` fetch-and-cache path used by `get_atr_pct` (so it works whether or not `adaptive_enabled` is true), honouring `candle_cache_sec` — up to ~60s staleness is accepted (clarify Q6).
- The **last** element of a Binance klines response is the **in-progress** bar; the helper must read the last *closed* one (`candles[-2]`), never the forming one.
- Returns `None` when the series is unavailable or has fewer than 2 bars (Constitution IV — unknown state is never treated as confirmation).

### Part 2 — Scalper integration (`trading_bot/spot_scalper.py`, `trading_bot/futures_scalper.py`)

- After `direction` is determined and the toxicity check passes, evaluate confirmation for that direction:
  - spot (long only): confirmed ⇔ last closed candle **up**
  - futures: `long` ⇔ **up**, `short` ⇔ **down** (symmetric; a short is confirmed by the pump pausing)
- **Observe mode** (`entry_confirm_candle=false`, default): record the state, do not block.
- **Enforce mode** (`true`): unconfirmed ⇒ `_log(..., "skipped", "entry_not_confirmed")` and return `None`.
- `None` (indeterminate) in enforce mode ⇒ **skip** (fail closed, Constitution IV), reason `entry_not_confirmed`.
- Nothing else in the entry path moves: regime gate, kill switches, thresholds, toxicity, cooldown, spacing, sizing and order placement are untouched.

### Part 3 — Signal record (`db/migrations/009_entry_confirmation.sql`, both `_log` writers)

- NEW nullable column `signals.entry_confirmed INTEGER` — `1` confirmed, `0` not confirmed, `NULL` indeterminate/not evaluated. Same shape as the existing `tox_*` verdict columns.
- Both `_log` implementations gain the value. Idempotent migration, no backfill (existing rows stay `NULL`).

### Part 4 — Setting (`trade/settings_schema.py`)

- NEW `entry_confirm_candle` (bool, group `entry`, default `false`, no ranges — same pattern as `market_gate_enabled` / `adaptive_enabled`). No DB row required; the `get_setting_bool` fallback carries the default.

### Part 5 — Tests (`tests/test_entry_confirmation.py`, new)

Helper unit tests (up / down / doji / single-candle / empty / in-progress-candle-ignored), observe-mode no-op, enforce-mode skip with the correct reason, futures long/short symmetry, `None` fails closed, and the settings-schema registration.

### Part 6 — Docs

`docs/CURRENT_STATE.md` (new feature-009 section) and `docs/CHANGELOG.md` (`feat:` entry).

---

## Why

A study over the **113 real `action='entered'` signals** (venue `binance`, 2026-08-05 → 08-15), replaying each asset's actual 5m price path from its true entry timestamp, at TP 1.2% / SL 2.0% / 120-min hold, 0.2% round-trip fee:

| Entry timing | n | net/trade | TP hit | **stopped** |
|---|---|---|---|---|
| No filter (today) | 113 | +0.017% | 59% | **20%** |
| Last 5m candle **up** | 41 (36%) | **+0.387%** | 73% | **12%** |
| Last 5m candle **down** | 72 (64%) | −0.194% | 51% | 25% |

**64% of entries fire while price is still falling**, and those are the losing half. The effect is not an artefact of the TP/SL pair — "up" beats "down" in every configuration tested:

| config | all | after UP | after DOWN | spread |
|---|---|---|---|---|
| TP0.8/SL0.6 | −0.191% | −0.015% | −0.292% | 0.277% |
| TP1.2/SL2.0 | +0.017% | +0.387% | −0.194% | 0.581% |
| TP1.5/SL2.0 | −0.076% | +0.455% | −0.379% | 0.834% |
| TP1.2/SL none | +0.132% | +0.332% | +0.018% | 0.314% |

Secondary evidence for the same root cause: 79% of entries drop 0.6% within two hours while only 74% ever reach +0.8% — the entry is systematically early. And the all-time `signals` skip histogram is dominated by `max_consecutive_losses breached` (**20,374 rows**, over half of all skips): stop-outs caused by early entries generate loss streaks that lock the venue out for whole UTC days, so entry quality is also the largest throughput lever (Constitution VIII).

---

## Constitution impact

### A. Principle II — resolved by Constitution v1.1.0 (2026-08-15)

Commit **662837d** replaced the `te <= se` entry gate with a cost-based one, and the live DB runs `sl_k_spot=2.0 / sl_min_pct_spot=1.5` against `tp_k=1.2 / tp_min_pct=0.8` — a stop wider than the target on every universe asset. Under Principle II v1.0 (`tp_pct > sl_pct`, NON-NEGOTIABLE) that was a violation shipped ahead of governance.

**Ratified 2026-08-15**: Principle II is amended to *"Reward Must Exceed Cost"* — `tp_effective > round_trip_fee(venue) + slippage + min_net_edge`, enforced per entry, with the stop tied to asset volatility rather than to the target. Rationale, per-principle impact assessment and evidence caveat are recorded in `.specify/memory/constitution.md` → Amendment History. `662837d` and the four live DB values are compliant with v1.1.0.

009 is unaffected either way — it filters entry *timing* and is independent of the TP/SL pair.

### B. Principle I "no candlestick pattern detection" — resolved by clarify Q1

The rule is stated as a **sign test on the last completed 5m return**, not as a candle shape: no engulfing/hammer/doji taxonomy, no multi-bar patterns, no lookup table — one bit derived from a series the bot already fetches for ATR. It serves the second half of Principle I's own question, "is price at an extreme **and likely to revert?**": displacement answers "at an extreme", this answers "reverting yet". Compliant.

### C. Principle VIII — "The Bot Trades"

Enforcement keeps 36% of entries (~11/day → ~4/day). That is a large frequency cut and VIII explicitly calls near-zero frequency a bug. Mitigations, in the spec by design:

- **Default is observe-only** — zero frequency change until the operator opts in on the recorded data.
- Measured throughput is *slot-limited nowhere near capacity*: average hold is 42 min (≈34 trades/day/slot theoretical) against 6.5 actual — **19% of slot capacity** — and the last three days show zero `max_slots_cex` / `max_concurrent_positions` skips. Frequency lost to quality is recoverable from capacity (`max_active_pairs`, `max_concurrent_positions`, `universe_size`), which is **out of scope here** and belongs to a follow-up spec.
- Daily return on slot capital improves despite fewer trades: **+0.19%/day → +1.59%/day** on the study sample.

### D. Other principles

| Principle | Impact |
|---|---|
| III — confirmed stop | None. Entry-side only; bracket placement and verification untouched. |
| IV — unknown ⇒ no trading | Honoured: indeterminate confirmation fails closed in enforce mode. |
| V — real fills | None. No PnL/fee path touched. |
| VI — restart safety | None. Filter is stateless; the candle cache is a rebuildable read-through cache. |
| VII — ≤1,500 hot-path lines | **Already exceeded: 2,442 lines** across the six hot-path modules before this feature. 009 adds ≈25 (helper ~12, two call sites ~5 each, `_log` args ~2). The overrun is pre-existing and must be acknowledged in the plan per VII's "explicit justification" clause. |
| VIII — the bot trades | See C. Every skip recorded with reason `entry_not_confirmed`, so strictness stays measurable. |

---

## Clarifications

### Session 2026-08-15

- **Q4 — Futures symmetry → BOTH venues, symmetric.** The filter applies to spot longs and to futures both directions: `long` confirmed by an up bar, `short` by a **down** bar (a short is confirmed by the pump pausing). **Accepted risk:** the short rule is inferred by symmetry and is *untested* — the study covers spot longs only. With `auto_trade_orderly=False` the futures arm will also collect no observe-mode data, so the assumption stays unvalidated until DEX is armed. AC8 is retained; the plan must mark the futures branch as evidence-free and unit-test it against synthetic series only.
- **Q1 — Framing → the return form.** The rule is *"the last completed 5m return is positive"* (`close > open`), not a candlestick pattern — this keeps Principle I ("no candlestick pattern detection") unambiguous. A **flat bar is NOT confirmed** (fail closed). The helper is named `last_closed_return_up` accordingly.
- **Q2 — Timeframe → fixed 5m, no new setting.** A configurable interval would need a second cache and a second API call, breaking the zero-additional-API-calls constraint; the study measured 5m. Revisit only if observe-mode data suggests the timeframe matters.
- **Q3 — Placement → after toxicity, before cooldown/spacing.** Confirmed as proposed. Rationale: `direction`, `tp_price` and `sl_price` are all populated by that point, so an `entry_not_confirmed` row is as informative as a `toxicity` or `cooldown` row, and cheaper checks (threshold, toxicity) still shed load first.
- **Q5 — Observe record → new nullable column.** `signals.entry_confirmed INTEGER` via an idempotent migration, matching the `tox_*` verdict-column precedent. Encoding in `reason` text would not be queryable for the A/B (AC14).
- **Q6 — Stale cache → accept up to `candle_cache_sec` (60s).** Forcing a fresh fetch when the cached series does not cover the current bar would contradict the zero-additional-API-calls constraint. Observe-mode measures live behaviour including this staleness, so the A/B remains honest. Revisit only if the data shows staleness-driven misclassification.

---

## Scope

**In scope**

- `trade/regime.py` — `last_closed_return_up` helper over the existing `_candle_cache`.
- `trading_bot/spot_scalper.py`, `trading_bot/futures_scalper.py` — observe/enforce call site + `_log` argument.
- `db/migrations/009_entry_confirmation.sql` — `signals.entry_confirmed`.
- `trade/settings_schema.py` — register `entry_confirm_candle`.
- `tests/test_entry_confirmation.py` — new.
- `docs/CURRENT_STATE.md`, `docs/CHANGELOG.md`.

**Out of scope**

- The Principle II amendment / 662837d ratification (section A) — separate governance decision.
- Any change to TP/SL values, `sl_k_spot`, thresholds, ATR, regime, toxicity, kill switches, market gate, broad-market filter, universe scanner, or exit/stop logic.
- Frequency recovery (`max_concurrent_positions`, `cex_slot_pct`, `max_active_pairs`, `universe_size`) — follow-up spec.
- The loop-latency / whole-exchange-snapshot work (bookTicker) — follow-up spec.
- Any new API call, thread, process, or dependency.

---

## Constraints

- **Minimum modification.** One helper, one condition per scalper, one column, one setting. No restructuring of the entry path.
- **Zero additional API calls** — the filter reads the ATR candle cache. Any design needing a new fetch is out of scope for v1 (Q2/Q6).
- **Default off (observe-only)** — enabling is an operator decision made on recorded data, per the Amendment 001 toxicity precedent.
- **Settings, never constants** — `entry_confirm_candle` read fresh each cycle.
- **Fail closed** — indeterminate confirmation never counts as confirmed (Constitution IV).
- **Every skip recorded** — `entry_not_confirmed` in `signals.reason` (Constitution VIII).
- **Structured single-line logs, no emoji.** Skips stay at DEBUG.
- **`dry_run` untouched** — no order path is modified.
- **Migration is idempotent**, no backfill, existing rows keep `NULL`.
- **Implementation on `main`** — no feature branches.

---

## Assumptions

- The 113-entry study is directionally valid but **thin** (n=41 in the confirmed arm) and covers a single 10-day window on one venue in one market regime. Observe-mode is what upgrades it, so a decision to enforce should wait for a materially larger sample.
- 5m granularity approximates the ~60s live sampling cadence. Live behaviour may differ where the bot samples mid-bar; observe-mode measures the real thing.
- The `_candle_cache` series is populated for every asset the entry path reaches (via `get_atr_pct`), so observe-mode costs nothing; the helper's own fetch path covers `adaptive_enabled=false`.
- Reduced stop-outs shrink `max_consecutive_losses` lockouts, but that second-order throughput gain is **not** claimed as an acceptance criterion.
- Deployment is remote (Docker image → Watchtower; DB via `push-db.sh`), so the migration runs on the server's DB at container start via `initialize_database_tables`.

---

## Acceptance criteria

1. **Helper correctness** — `last_closed_return_up` returns `True` for an up bar, `False` for a down **or flat** bar, and never inspects the in-progress (last) element of the klines response.
2. **Indeterminate is None** — fewer than 2 bars, or an unavailable series, returns `None`.
3. **Observe mode is a no-op** — with `entry_confirm_candle` unset/false, entries fire exactly as before; no entry is blocked by this feature.
4. **Observe mode records** — every `signals` row written by either scalper carries `entry_confirmed` ∈ {0, 1, NULL} reflecting the evaluated state.
5. **Enforce blocks unconfirmed** — with `entry_confirm_candle=true`, an unconfirmed entry is skipped and recorded with reason `entry_not_confirmed`; nothing is sent to the exchange.
6. **Enforce passes confirmed** — a confirmed entry proceeds through the unchanged remainder of the entry path.
7. **Fail closed** — `None` in enforce mode skips, with reason `entry_not_confirmed`.
8. **Futures symmetry** — `long` confirmed by an up bar, `short` by a down bar (clarify Q4: both venues). Unit-tested against synthetic series; flagged evidence-free in the plan, since the study covers spot longs only and DEX is currently off.
9. **Zero new API calls** — the entry path issues the same number of market-data requests per asset per cycle as before, in both modes.
10. **Setting registered** — `entry_confirm_candle` (bool, group `entry`, default false) is in `trade/settings_schema.py`, passes the Amendment 002 validator, needs no DB row, and is read fresh each cycle.
11. **Migration idempotent** — re-running `initialize_database_tables` leaves the schema unchanged; pre-existing `signals` rows keep `entry_confirmed = NULL`.
12. **Tests** — `tests/test_entry_confirmation.py` covers AC1–AC8 and AC10; the existing 76 tests continue to pass.
13. **Docs** — `docs/CURRENT_STATE.md` gains a feature-009 section; `docs/CHANGELOG.md` gains a `feat:` entry.
14. **Measurability** — after one week of observe-mode, a single SQL query over `signals` returns entry counts and outcomes split by `entry_confirmed`, sufficient to decide enforcement.
