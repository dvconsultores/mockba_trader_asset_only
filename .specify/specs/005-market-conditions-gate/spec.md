# Feature Specification: 005 — Market Conditions Check & Auto-Gate

**Feature Branch**: `005-market-conditions-gate` *(implementation on `develop` per repo convention — no feature branches)*

**Created**: 2026-08-09

**Status**: Draft — awaiting implementation authorization

**Flow**: constitution → specify → clarify → plan → checklist → tasks → analyze → **implement (AUTHORIZATION REQUIRED)** → converge

---

## What

Three parts built on one shared core.

### Part 1 — `trade/market_check.py` (the shared core, one source of truth)

A new module with a pure function that produces a structured per-venue market-health report. Two evaluation modes, one verdict contract:

- **live snapshot** (manual button): fresh whole-exchange calls — bookTicker + 24hr + depth per survivor — token-bucket rate-limited exactly as the universe scanner does.
- **observed** (automatic gate): evaluates venue health from observations the bot loop ALREADY collects per cycle — live spread vs scan spread from `_get_obi_and_spread`, regime, OBI. A rolling window, no meaningful extra API load.

The gate and the button must produce the **same verdict shape**: a structured dict per venue, never formatted text, so both the gate and the Telegram renderer consume it identically.

**HARD RULE (non-divergence).** The check MUST reuse the existing live functions and must NEVER reimplement threshold/depth/regime logic:

- `trade/universe.compute_thresholds` (thresholds)
- `trade/regime.detect_regime` (regime classification)
- `trade/universe._fetch_depth`, `_fetch_binance_book_ticker`, `_fetch_binance_24hr`, `_fetch_binance_exchange_info` (market data)
- `trade/universe.run_scans_if_due` / `force_rescan` (scan freshness)
- `trade/pnl.compute_slot_size` (slot size)
- settings via `db/db_ops.get_setting*`

This mirrors the repo's "replay must never diverge from live logic" principle, already enforced for the universe replay by `tests/test_amendment003.py` (patch the shared function, observe both call sites). The same test pattern is applied to the market check in a new `tests/test_market_check.py`.

`compute_thresholds` is called with the stored median ATR to derive the venue's effective thresholds, carried in the report as a **non-gating diagnostic** `thresholds` field that never influences the verdict (it is the AC1-observed call site).

**Scan freshness.** If the stored universe scan is stale/due, the check triggers `run_scans_if_due()` (or `force_rescan`) BEFORE evaluating — it never judges on stale data (Constitution IV: unknown state means no trading).

**Verdict per venue:** `PASS` / `FAIL` / `WARN` with reasons, plus:

- which universe assets currently pass liquidity — `volume >= universe_min_volume_usd`, `depth >= universe_depth_slot_multiple × slot size`, `spread <= universe_spread_ratio_max × tp_min_pct`, live spread not degraded beyond `universe_spread_degradation_multiple × scan spread`
- regime mix (counts of RANGE / TREND_UP / TREND_DOWN / UNKNOWN across the venue universe)
- scan freshness

Verdict aggregation is settings-driven, never constants: a venue FAILs when the share of universe assets failing liquidity reaches `market_gate_fail_share`; PASS downgrades to WARN when `market_gate_trend_share` or `market_gate_unknown_share` is met (exact rules in `data-model.md` §2).

Output is structured (a dict), not formatted text — both consumers (gate, renderer) read the same contract.

### Part 2 — Automatic gate in `bot.py` (active continuous detection)

- **Opt-in** via new setting `market_gate_enabled` (default `false` — zero behavior change until enabled).
- Active only when the venue's `auto_trade_*` mode is not `"False"` (i.e. `Signal` or `Automatic`).
- Runs inside the existing main loop — no new process, no new thread — following the same pattern as the existing periodic 5-minute mode-log block.
- Evaluates venue health every `market_gate_interval_min` minutes (default 5; a setting).
- **Debounced state machine per venue**: suspend NEW entries only after `market_gate_bad_streak` consecutive bad evaluations (default 2); resume after `market_gate_good_streak` consecutive good evaluations (default 2). Settings, not hardcoded — prevents flapping.
- When suspended: blocks **new entries only** (same effect as the existing stale-universe guard — exits and position management ALWAYS still run) and sends **ONE** debounced Telegram notification on the state transition ("suspended — poor market conditions" / "recovered"). No per-check messages. Delivery uses `trading_bot/send_bot_message.py`, via the same mechanism bot.py already uses for entry notifications (`_notify_entry`).
- Explicitly does **NOT** replace the existing per-asset guards: regime gating, spread-degradation guard, stale-universe block, kill switches (daily-loss, consecutive-loss). It is an additional venue-level "should this venue trade at all right now" layer.

**Constitution compliance:**

- **IV (unknown state = no trading)** — an indeterminate check (scan stale/unavailable, data missing) never judges "good"; it must not produce a resume.
- **III (stops never removed)** — the gate suspends entries only; every open position is still managed to its normal TP/SL/time-stop/regime exit.
- **VIII (the bot trades)** — the gate must not become a near-zero-trade configuration; it suspends only on genuinely poor aggregate conditions, and defaults are lenient enough that normal RANGE markets pass.
- Settings are read fresh each cycle (Telegram/UI changes take effect without restart).
- Structured single-line logs, no emoji: `[GATE] venue=... verdict=... reason=...`.

### Part 3 — Manual Telegram button/command

- New `/market` command in `telegram.py`, plus a button in the existing `/list` menu (telegram.py currently has only `/start` and `/list`; the callback dispatch lives in `_dispatch_callback`).
- Runs the shared check in **live-snapshot** mode and renders a compact, Telegram-friendly verdict per venue — `PASS` / `FAIL` / `WARN` + one-line reasons + counts — NOT a giant table. Respects `TELEGRAM_MAX_MESSAGE_LEN = 4096`.
- Follows the existing patterns of the other handlers: `translate()` with `BOT_LANGUAGE`, and the `TELEGRAM_CHAT_ID` authorization check.
- The manual button is also the operator's **escape hatch / override** against the automatic gate's false positives: it shows the same verdict shape on demand (e.g., before a manual Sunday restart), and the operator decides.

## Why

The operator manually stops the bot on Fridays and restarts on Sundays because weekend/thin markets are hostile to the mean-reversion scalper. Last Friday the bot lost money because aggregate market conditions were poor — not because of the bot's own logic. The operator wants:

1. an automatic, continuously-running detection that suspends NEW entries when market conditions are poor and notifies via Telegram — so they no longer have to stop on Fridays, guess, or babysit; and
2. a manual Telegram button/command that runs the same analysis on demand — e.g., before a manual Sunday restart, or as an override/escape hatch when the operator disagrees with the gate.

---

## Resolved decisions (T01 + clarify)

| # | Question | Decision |
|---|---|---|
| Q1 | Cadence | Evaluation every ~5 min per venue (`market_gate_interval_min`, default 5), not every 30s cycle — weekend badness is structural/persistent; per-asset 30s guards remain as-is. |
| Q2 | Debounce | Suspend after `market_gate_bad_streak` (default 2) consecutive bad evaluations; resume after `market_gate_good_streak` (default 2) consecutive good evaluations. All settings, not hardcoded. |
| Q3 | Notifications | Only on state transitions (suspended/resumed), debounced, via `trading_bot/send_bot_message.py`. No per-check messages. |
| Q4 | Data | Automatic gate uses observed per-cycle data (no extra API load); manual button uses a fresh live snapshot. Same verdict shape. |
| Q5 | Scan freshness | The check triggers `run_scans_if_due()` / `force_rescan` before judging if the stored scan is stale/due — it never judges on stale data. |
| Q6 | Scope of blocking | New entries only; existing positions always managed (Constitution III). |
| Q7 | Opt-in | `market_gate_enabled` default `false`; informational/report behavior is first-class, the enforcement gate is opt-in. Zero behavior change until enabled. |
| Q8 | Out of scope | The PUMP closed-trade #65 fee anomaly (43% fee, −44% PnL at entry≈exit) is a SEPARATE execution/fill investigation, explicitly NOT part of this spec — recorded as a follow-up. |
| Q9 | Never near-zero-trade | The venue-level gate must never create a near-zero-trade configuration (Constitution VIII): thresholds default lenient enough that normal RANGE markets pass. |
| Q10 | Settings naming | Seven keys: `market_gate_enabled`, `market_gate_interval_min`, `market_gate_bad_streak`, `market_gate_good_streak`, `market_gate_fail_share`, `market_gate_trend_share`, `market_gate_unknown_share`. New settings are registered in `trade/settings_schema.py` (with hard/soft ranges) and, only if strictly necessary, cross-checks in `trade/settings_rules.py`, so validation passes (Amendment 002 validator). |

---

## Layout

### 1. `trade/market_check.py` — the shared check

One pure function (plus a thin per-venue wrapper) returning the verdict contract:

```
venue, mode (live|observed), timestamp, scan_fresh, scan_age_hours,
regime_mix {RANGE, TREND_UP, TREND_DOWN, UNKNOWN},
verdict (PASS|FAIL|WARN), reasons [one-line strings],
assets { symbol: { passes_liquidity, volume_ok, depth_ok,
                   spread_ok, live_spread_degraded, regime } },
thresholds (diagnostic, non-gating)
```

- **live mode**: whole-exchange bookTicker + 24hr + exchangeInfo, then depth per survivor, token-bucket rate-limited exactly as the scanner; calls `run_scans_if_due()`/`force_rescan` first if the stored scan is stale.
- **observed mode**: consumes the rolling observations the loop already keeps — live spread vs scan spread from `_get_obi_and_spread`, `detect_regime` results, OBI — over the last `market_gate_interval_min` window. No new network calls.
- Never imports from `trade/main.py` or `trade/signal_agent/` (Constitution VII).

### 2. `bot.py` — periodic gate evaluation

A block next to the existing periodic mode-log block (same `time.time() - last > interval` pattern, same loop, no thread):

- Read gate settings fresh each cycle; skip everything if `market_gate_enabled` is false or the venue mode is `"False"`.
- Every `market_gate_interval_min`, run the observed-mode check per venue; maintain per-venue debounce streaks in memory.
- `bad_streak` reached → suspend: block new entries (same effect as the stale-universe guard — exits still managed), send one transition notification, log `[GATE]`.
- `good_streak` reached → resume: clear suspension, send one transition notification, log `[GATE]`.
- Suspension state is checked before entry logic per venue (alongside the existing stale-universe and kill-switch checks).
- Observations are recorded at the two existing call sites (`detect_regime`, `_get_obi_and_spread`) **before** the entry-block guard, so live-spread observations keep flowing during a suspension and resume is never deadlocked.

### 3. `telegram.py` — `/market` command + button

- New `@bot.message_handler(commands=['market'])` with the same private-chat + `TELEGRAM_CHAT_ID` authorization checks as the other handlers.
- A button added to the `/list` menu (callback data routed through the existing `_dispatch_callback`).
- Handler runs the check in **live** mode and renders a compact per-venue block: verdict + one-line reasons + counts (regime mix, assets passing liquidity), translated via `translate()`, chunked within `TELEGRAM_MAX_MESSAGE_LEN = 4096`.

### 4. Settings, docs, tests

- New settings registered in `trade/settings_schema.py` (group `universe` or a new `gate` group; hard/soft ranges; defaults documented) so the Amendment 002 validator accepts them; `settings_rules.py` cross-checks added only if strictly necessary (e.g., `market_gate_bad_streak >= 1`, `market_gate_good_streak >= 1`, `market_gate_interval_min >= 1`). Prefer no DB migration — defaults live in `get_setting_*` calls.
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md` updated (Convention: `feat:` per `how-to-work-with-specs.md`).
- New `tests/test_market_check.py`.

---

## Scope

**In scope**

- `trade/market_check.py` (new) — shared pure check function + both modes (live snapshot, observed).
- `bot.py` — periodic gate evaluation, per-venue suspend/resume state machine, transition notifications, entry blocking when suspended.
- `telegram.py` — `/market` command + button in `/list`.
- `trade/settings_rules.py` / `trade/settings_schema.py` — register the new `market_gate_*` settings so validation passes (only what is strictly necessary).
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md` — update.
- Tests — new `tests/test_market_check.py` asserting: the check reuses live threshold functions (non-divergence), verdict correctness, debounce transitions, and that the gate blocks only entries, never exits.

**Out of scope**

- No changes to the executor or scalpers unless strictly required (prefer none).
- No DB schema change / migration unless strictly required (prefer none).
- PUMP closed-trade #65 fee anomaly (43% fee, −44% PnL at entry≈exit) — a SEPARATE execution/fill investigation. Follow-up only; not part of this spec.
- No new process, thread, or external service for the gate — it lives in the existing main loop.
- No change to the existing per-asset guards (regime gating, spread-degradation guard, stale-universe block, kill switches, daily-loss / consecutive-loss).

---

## Constraints

- **Minimum modification.** Preserve existing behavior, styles, and functionality when the gate is disabled (default). Zero behavior change unless `market_gate_enabled` is set.
- **Non-divergence (hard rule).** `trade/market_check.py` reuses the live functions listed in What/Part 1 and never reimplements threshold/depth/regime logic. Enforced by a test that patches the shared functions and observes both call sites (same pattern as `tests/test_amendment003.py`).
- **Constitution compliance.** IV — unknown state never resumes trading; III — exits/stops always run, the gate blocks entries only; VIII — the gate must not produce a near-zero-trade configuration; II — untouched. All PRs in the trading path verify III, IV, V, VI.
- **No extra API load for the automatic gate.** The gate runs on observed per-cycle data; only the manual button makes fresh live-snapshot calls (token-bucket rate-limited as the scanner).
- **Structured single-line logs, no emoji.** Gate events log as `[GATE] key=value ...` at INFO; skipped-entry reasons stay at DEBUG with the reason recorded in `signals` (Constitution VIII).
- **Settings read fresh each cycle.** Gate settings take effect without restart; startup validations re-run on setting change.
- **Settings validation.** New `market_gate_*` settings must pass the Amendment 002 deterministic validator — registered in `settings_schema.py`, cross-checks in `settings_rules.py` only if strictly necessary.
- **Telegram message limits.** Manual report respects `TELEGRAM_MAX_MESSAGE_LEN = 4096`; transition notifications are one message per state change, debounced.
- **`dry_run` unchanged.** Every order path and its checks are untouched; the gate adds no bypass.
- **No hardcoded assets or numbers.** Gate defaults and thresholds come from settings, not constants.

---

## Acceptance criteria

1. **Non-divergence** — `trade/market_check.py` calls the live `compute_thresholds` / `detect_regime` / `_fetch_*` / `compute_slot_size` / `get_setting*` functions; patching a shared function is observed by the check's call site (mirrors `tests/test_amendment003.py`). No reimplemented threshold/depth/regime logic exists in the module.
2. **Same verdict contract** — live and observed modes return identical verdict shape (venue, mode, scan_fresh, regime_mix, verdict, reasons, per-asset liquidity fields) for equivalent inputs.
3. **Freshness** — a stale/due stored scan triggers `run_scans_if_due()`/`force_rescan` before the check produces a verdict; a check never judges on stale data.
4. **Verdict correctness** — synthetic per-asset inputs (volume, depth, spread, degradation multiple, regime, scan age) produce the expected PASS/FAIL/WARN and reasons per the documented thresholds.
5. **Disabled by default** — with `market_gate_enabled` unset/false, bot behavior is byte-for-byte unchanged (no entry blocking, no notifications, no gate logs beyond a single startup 'disabled' INFO log).
6. **Debounce transitions** — a venue suspends only after `market_gate_bad_streak` consecutive bad evaluations and resumes only after `market_gate_good_streak` consecutive good ones; flapping is prevented; all streaks are settings-driven.
7. **Entries only, never exits** — while a venue is suspended, no new entries are opened but open positions continue to be managed to TP/SL/time-stop/regime exit (asserted in tests).
8. **Transition notifications** — exactly one debounced Telegram message on suspend and one on resume; no per-check messages; delivered via the same mechanism as entry notifications.
9. **No extra gate API load** — the automatic gate consumes only per-cycle observations (live spread vs scan spread, regime, OBI); it issues no new market-data calls.
10. **Manual report** — `/market` and the `/list` button render a compact per-venue verdict (PASS/FAIL/WARN + one-line reasons + counts) within 4096 chars, localized via the existing translation path, and respect the `TELEGRAM_CHAT_ID` authorization.
11. **Not near-zero-trade** — with default settings, a normal RANGE market on a healthy venue evaluates PASS (Constitution VIII); a deliberately poor market evaluates FAIL.
12. **Settings pass validation** — all seven `market_gate_*` settings are accepted by the deterministic validator with sensible hard/soft ranges; invalid values are rejected with clear messages.
13. **Docs** — `docs/CURRENT_STATE.md` describes the market check, the gate, and the `/market` command; `docs/CHANGELOG.md` gains a `feat:` entry.
