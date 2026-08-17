# Feature Specification: 006 — Spot Exit Hardening (gap/crash protection)

**Feature Branch**: `006-spot-exit-hardening` *(implementation on `develop` per repo convention — no feature branches)*

**Created**: 2026-08-12

**Status**: Draft — awaiting implementation authorization

**Flow**: constitution → specify → clarify → plan → checklist → tasks → analyze → **implement (AUTHORIZATION REQUIRED)** → converge

---

## What

Two small, setting-driven protections that hard-cap the worst-case loss of a spot position when price gaps or crashes through the stop, plus the settings and docs that ship with them. The normal TP/SL/time-stop behavior is untouched — these additions only (1) keep crash-prone, ultra-high-volatility names out of the universe, and (2) add an emergency exit floor that fires regardless of the configured SL distance.

### Part 1 — Volatility cap in the universe scan (`universe_max_atr_pct`)

- NEW setting `universe_max_atr_pct` (float, default **1.5**, %).
- Applied in the universe scan hard-filter stage (`trade/universe.py` `scan_venue` Stage 2): a candidate whose 24h/median ATR% exceeds the cap is rejected and never enters the stored universe, so crash-prone names never become tradeable.
- Strictly **additive**: it never loosens the existing volume / spread / rank / fundability hard filters; it only removes the most extreme names. Setting-driven, read fresh each cycle — no constants.
- Expected impact (Constitution VIII — the bot must still trade): the current universe stores assets with ATR up to ~1.86% (BICO-class). A 1.5 cap removes only the BICO-class names while keeping PUMP (~0.6%) and MMT (~0.87%). This complements the recent `universe_rank_min` 15→30 change, which already excluded the most manipulated micro-caps.

### Part 2 — Catastrophic-move guard (`max_loss_per_position_pct`)

- NEW setting `max_loss_per_position_pct` (float, default **3.0**, %).
- In `trading_bot/spot_scalper.py` `manage_open_positions`, a hard floor check per position: if the live price falls below `entry_price × (1 − max_loss_per_position_pct/100)`, the bot immediately cancels the open TP/SL orders and market-sells — **regardless of the configured SL distance**. This caps the worst-case loss when price gaps through the stop.
- Runs **BEFORE** the existing exchange-fill checks, so a gap is caught in the same ~30s cycle.
- On missing live price (`None`): do nothing — keep the position to retry (Constitution IV: unknown state = no action).
- The exit is recorded through the existing `_close` path with the **real** market-sell fill price and real fee (Constitution V), using a new `exit_reason = 'crash_guard'`.
- Constitution III (stops never removed): the guard only ADDS an emergency exit; the normal TP/SL/time-stop behavior is unchanged. It fires only when price has already crashed through the floor — it never relaxes or removes a working stop.

### Part 3 — Settings, validation, docs

- Both new settings registered in `trade/settings_schema.py` (SettingSpec entries with type, group, hard/soft ranges) so the Amendment 002 deterministic validator accepts them; defaults live in the `get_setting_*` fallbacks — **no DB migration**.
- Cross-checks in `trade/settings_rules.py` **are required**: most importantly `max_loss_per_position_pct` must not be strictly inside the spot SL floor (`sl_min_pct_spot`) — a **hard validation error** fires when `max_loss_per_position_pct < sl_min_pct_spot`, while equality (`==`) is allowed and benign — so the guard is a gap-catcher and never becomes the primary stop.
- Dashboard/UI exposure is minimal — the operator manages settings locally. At most, the Closed Trades page label map (`dashboard/main.py` `REASON_LABELS`) gains a `"crash_guard"` entry so the new reason renders as "Crash guard" instead of the raw fallback.
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md` updated per repo convention (`feat:`).

## Why

On 2026-08-07 a spot position in PUMP closed at **−44.35%** (`pnl_net` −$4.83, `exit_reason='sl'`) — a single catastrophic loss that wiped out the entire week's profit. The entry places a Binance spot OCO with `aboveType: LIMIT_MAKER` (TP) and `belowType: STOP_LOSS` + `belowStopPrice` (SL); on Binance spot that stop is a **market-on-trigger** order, and on a fast crash of an illiquid micro-cap the market stop still slips enormously over a thin/empty book. The software backstop in `manage_open_positions` (every ~30s: `live <= slp` → cancel TP/SL + `market_sell`) has the same weakness — its market sell also slips on a thin book.

The universe also stores very high-ATR names (BICO ≈ 2.1%, PUMP ≈ 0.6%, MMT ≈ 0.87%) — high-ATR micro-caps are exactly the crash-prone names. The bot just raised `universe_rank_min` 15→30 to exclude the most manipulated micro-caps, but there is still **no upper bound on volatility**.

The operator is a small-retail spot scalper (~$30–50 capital, 10% of equity per slot, 2 concurrent positions, TP 0.8% / SL 0.5–0.6%, mean-reversion dip-buying only per Constitution I). With that capital, one gap through the stop is enough to erase a week of micro-scalps. The worst-case loss per position must be capped — both by keeping crash-prone names out of the universe and by cutting any position that has already crashed below a hard floor.

---

## Clarifications

### Session 2026-08-12

- Q: Crash-guard ordering & fill handling → A: Guard-first & fill-aware — the crash guard runs as the first check in `manage_open_positions`; it verifies the TP/SL order fill status first (if already FILLED → record the real fill with its real reason `tp`/`sl`, never market-sell); only when neither order filled does it cancel + market-sell and record `exit_reason='crash_guard'` (Constitution V, AC8).
- Q: Crash-guard exit & re-entry cooldown → A: A crash-guard exit counts as a stop-loss against the existing `_last_sl` re-entry cooldown (`cooldown_sec × SL_COOLDOWN_MULT`, in-memory, ~10 min) — the same `(asset, side)` is blocked from re-entry exactly like an `sl` exit; longer-horizon exclusion is handled by `universe_max_atr_pct` on the next scan (Constitution VII — minimal change).
- Q: `universe_max_atr_pct` venue scope → A: Spot (`binance`) venue only — the cap applies via a venue-branch in `scan_venue` Stage 2 (precedent: `max_hold_minutes_spot` vs `max_hold_minutes_futures`); the Orderly/futures universe is untouched (DEX uses exchange-side bracket stops and is currently off; a futures cap could empty that universe under Constitution VIII).
- Q: Validation severity for `max_loss_per_position_pct` vs the spot SL floor → A: **Hard validation error** when `max_loss_per_position_pct < sl_min_pct_spot` (strictly inside); equality (`==`) is allowed and benign — guard and exchange stop trigger at the same level, so the guard stays a pure gap-catcher (mirrors the `tp_min_pct <= sl_min_pct` hard-error pattern; Constitution II startup gates, not warnings; Constitution III stops never removed).

---

## Resolved decisions (from feature description)

| # | Decision |
|---|---|
| Q1 | `universe_max_atr_pct` (float, default 1.5) applied in the scan hard-filter stage; additive only; removes only the most extreme (BICO-class) names. |
| Q2 | `max_loss_per_position_pct` (float, default 3.0); floor = `entry × (1 − pct/100)`; fires regardless of SL distance; runs before the exchange-fill checks; `None` live price → no action (Constitution IV). |
| Q3 | New `exit_reason='crash_guard'` via the existing `_close` / `record_closed_trade` path with real fill price + real fee (Constitution V). |
| Q4 | Normal TP/SL/time-stop behavior unchanged — the guard only ADDS an emergency exit (Constitution III). |
| Q5 | Trade frequency preserved: the max-ATR cap removes only BICO-class names; PUMP/MMT remain (Constitution VIII). |
| Q6 | Settings registered in `settings_schema.py`; cross-checks in `settings_rules.py` required; minimal/no dashboard exposure; defaults in `get_setting_*` fallbacks — no DB migration. |
| Q7 | Implementation on `develop` directly — no feature branches (repo convention). |

---

## Layout

### 1. `trade/universe.py` — volatility cap in `scan_venue` Stage 2

- **Spot venue only** — the cap is applied via a venue-branch in Stage 2 (precedent: `max_hold_minutes_spot` vs `max_hold_minutes_futures`); the Orderly/futures universe is untouched (DEX uses exchange-side bracket stops and is currently off — a futures cap could empty that universe under Constitution VIII).
- Read `universe_max_atr_pct` fresh via `get_setting_float` and reject candidates whose ATR% exceeds the cap. **Placement (pinned in planning):** the filter runs immediately after the Stage-4 replay loop and before Stage-5 `select_ranked` — because the calibrated measure is the replay's `atr_pct_median` (the Stage-1 24hr ticker exposes only `quoteVolume`, no usable high–low range). The cap guarantees such names are **never stored**.
- The ATR measure is the crash-risk screen: the replay median ATR (`atr_pct_median`), calibrated so the 1.5 default excludes only BICO-class names (ATR ≈ 1.86%) and keeps PUMP (≈0.6%) / MMT (≈0.87%).
- The scan summary reports how many candidates the cap dropped (observability — `dropped_by_max_atr` surfaced in the scan summary message), and the filter must never loosen the existing volume/spread/rank/fundability checks.

### 2. `trading_bot/spot_scalper.py` — catastrophic-move guard in `manage_open_positions`

- Per-position hard floor: `floor = entry_price × (1 − max_loss_per_position_pct/100)`.
- The live price is fetched for every open position (not only when a stored `sl_price` exists), because the crash floor applies to all positions.
- If `live is not None and live < floor`: **fill-aware, guard-first** — verify the TP/SL order fill status first; if either already FILLED, record that real fill with its real reason (`tp`/`sl`) and never market-sell; only when neither order has filled, cancel the open TP/SL orders (existing cancel path), `market_sell`, and close via the existing `_close` path with `exit_reason='crash_guard'` — real fill price and real fee (Constitution V).
- Runs as the FIRST check in `manage_open_positions` (guard-first), so a gap is caught in the same ~30s cycle; the fill-status verification happens inside the guard before any market-sell.
- **Re-entry cooldown**: a crash-guard exit stamps `_last_sl` for the `(asset, side)` exactly like an `sl` exit, so the same asset is blocked from re-entry for `cooldown_sec × SL_COOLDOWN_MULT` (in-memory, ~10 min at the default `cooldown_sec`); longer-horizon exclusion comes from `universe_max_atr_pct` on the next scan (Constitution VII).
- `live is None` → skip, keep the position to retry (Constitution IV).
- Reuses the existing no-balance / orphan-recovery patterns so a position already closed by an exchange fill (TP or SL) is never phantom-sold or mis-recorded.

### 3. `trade/settings_schema.py` — register the two new settings

| Key | Type | Group | Default | Hard range | Soft range |
|---|---|---|---|---|---|
| `universe_max_atr_pct` | float | universe | 1.5 | 0.1–20 | 0.5–5 |
| `max_loss_per_position_pct` | float | exit | 3.0 | 0.1–20 | 1–5 |

### 4. `trade/settings_rules.py` — cross-checks (required)

- `max_loss_per_position_pct` must not be strictly inside the spot SL floor `sl_min_pct_spot` — **hard validation error** (startup gate, not a warning) when `max_loss_per_position_pct < sl_min_pct_spot`, mirroring the `tp_min_pct <= sl_min_pct` hard-error pattern (Constitution II); equality (`==`) is allowed and benign — guard and exchange stop trigger at the same level, so the guard stays a pure gap-catcher and never becomes the primary stop (a strictly-inside floor would pre-empt and cancel the stop, violating Constitution III).
- `universe_max_atr_pct`: optional warn when set so low that the stored universe would be empty (mirrors the existing depth-multiple warn).

### 5. Docs & UI label

- `docs/CURRENT_STATE.md` — document both protections.
- `docs/CHANGELOG.md` — `feat:` entry.
- `dashboard/main.py` — `REASON_LABELS` gains `"crash_guard": "Crash guard"` (Closed Trades page).

---

## Scope

**In scope**

- `trade/universe.py` — max-ATR hard filter in `scan_venue` Stage 2, **spot (`binance`) venue only** via a venue-branch (Orderly/futures universe untouched).
- `trading_bot/spot_scalper.py` — catastrophic-move guard in `manage_open_positions`.
- `trade/settings_schema.py` / `trade/settings_rules.py` — register + validate the two new settings.
- `dashboard/main.py` — `crash_guard` label (minimal).
- `docs/CURRENT_STATE.md` + `docs/CHANGELOG.md`.

**Out of scope**

- No change to entry logic, TP/SL sizing, adaptive thresholds, or the OCO placement itself (`trading_bot/executor.py` untouched).
- No change to the futures/DEX path (Orderly bracket + `futures_scalper`) — this is spot-specific (the observed failure is spot); no futures-side ATR cap is added (a futures cap could empty that universe under Constitution VIII, and DEX already uses exchange-side bracket stops).
- No DB schema change / migration; no new process, thread, or external service.
- The PUMP closed-trade fee anomaly (43% fee) remains a separate execution/fill investigation (feature 005 Q8) — out of scope here; this feature targets the gap/crash-slippage mechanism.

---

## Constraints

- **Minimum modification.** Preserve existing behavior, styles, and functionality; the two additions are small and live inside existing functions.
- **Constitution I** — mean-reversion only; this is exit-safety, not a new strategy.
- **Constitution II** — reward > risk untouched; `tp_pct > sl_pct` and net-edge validations unchanged.
- **Constitution III** — stops are never removed; the guard only adds an emergency exit for positions already crashed through the floor.
- **Constitution IV** — missing live price ⇒ no action (position kept to retry); the guard never guesses.
- **Constitution V** — the crash-guard exit records the real market-sell fill price and real fee through the existing `_close`/`record_closed_trade` path; `exit_reason='crash_guard'`.
- **Constitution VII** — minimal lines; small additions to existing functions, no new modules; no import from `trade/main.py` or `trade/signal_agent/`.
- **Constitution VIII** — the max-ATR cap removes only the most extreme names (documented expected impact), so trade frequency is not meaningfully reduced.
- **Settings read fresh each cycle** — both new settings take effect without restart; startup validations re-run on setting change.
- **Settings validation** — both new settings pass the Amendment 002 deterministic validator (registered in `settings_schema.py`, cross-checks in `settings_rules.py`); invalid values rejected with clear messages.
- **Structured single-line logs, no emoji** — crash-guard exits log as `[EXIT] asset=... reason=crash_guard ...`.
- **`dry_run` unchanged** — every order path and its checks are untouched.
- **No hardcoded numbers** — the cap and the floor come from settings, never constants.

---

## Assumptions

- The operator manages settings locally (DB/Telegram/UI); dashboard exposure of the two new settings is optional and minimal.
- The exact ATR source for the universe cap (24h high–low range vs replay median ATR) is pinned during planning; the 1.5 default is calibrated against `atr_pct_median` so only BICO-class names are removed.
- A default floor of 3.0% keeps the guard wider than the spot SL (0.5–0.6% default) and the effective adaptive SL, so it fires only on genuine gaps/crashes.
- The universe cap applies to the spot (`binance`) venue only, via a venue-branch in `scan_venue` Stage 2 (precedent: `max_hold_minutes_spot`); the Orderly/futures universe is untouched.
- The guard applies to spot positions only (`binance`); the futures/DEX path already uses exchange-side bracket stops.

---

## Acceptance criteria

1. **Universe cap rejects high-ATR names (spot only)** — a candidate whose replay `atr_pct_median` exceeds `universe_max_atr_pct` is rejected in the spot (`binance`) scan (post-Stage-4, pre-`select_ranked`) and never stored; BICO-class names (ATR ≈ 1.86%) are excluded while PUMP (≈0.6%) and MMT (≈0.87%) remain (Constitution VIII); the Orderly/futures universe is unaffected (venue-branch, `max_hold_minutes_spot` precedent).
2. **Additive only** — the cap never loosens the existing volume/spread/rank/fundability filters; any candidate failing the pre-existing filters still fails after the change.
3. **Setting-driven, fresh** — both new settings are read via `get_setting_*` each cycle; changing them takes effect without restart; defaults live in fallbacks (no DB migration).
4. **Crash guard fires on floor breach** — with live price below `entry × (1 − max_loss_per_position_pct/100)`, the position is cancelled + market-sold in the same cycle regardless of SL distance, and closed with `exit_reason='crash_guard'` using the real fill price and fee (Constitution V); the exit also counts as a stop-loss for the re-entry cooldown — the `(asset, side)` is blocked from re-entry for `cooldown_sec × SL_COOLDOWN_MULT` (~10 min), identical to an `sl` exit.
5. **Guard-first, fill-aware ordering** — the floor check runs as the first check in the cycle; if either TP/SL order already FILLED, the real fill is recorded with its real reason (`tp`/`sl`) and no market-sell occurs; only when neither filled does the guard cancel + market-sell with `exit_reason='crash_guard'`.
6. **Unknown price = no action** — with live price `None`, the guard does nothing and the position is kept for the next cycle (Constitution IV).
7. **Normal exits unchanged** — while price is above the floor, TP/SL/time-stop behavior is unchanged (Constitution III); a position at or above the floor is never affected by the guard.
8. **No phantom double-close** — the guard verifies the TP/SL fill status before acting; a position already closed by an exchange fill (TP or SL) is recorded with its real reason (`tp`/`sl`) and never market-sold or mis-recorded (reuses the existing no-balance/orphan recovery).
9. **Real fills only** — whether the exit is an already-filled TP/SL order (real reason `tp`/`sl`) or a crash-guard market sell, the guard records the actual fill price and fee, never an assumed price (Constitution V).
10. **Settings pass validation** — both new settings are accepted by the deterministic validator with sensible hard/soft ranges; a `max_loss_per_position_pct` strictly inside the spot SL floor (`< sl_min_pct_spot`) is a **hard validation error**, while equality (`==`) is allowed (the guard and the static SL floor trigger at the same level). NOTE: the exchange stop is adaptive (`sl_eff = max(sl_k_spot × ATR%, sl_min_pct_spot)`), so for members whose effective SL is wider than the floor the guard fires first — benign in practice because the 3.0% default floor ≫ typical effective SL (~0.6–0.8%); invalid values are rejected with clear messages.
11. **Docs & UI** — `docs/CURRENT_STATE.md` describes both protections; `docs/CHANGELOG.md` gains a `feat:` entry; the Closed Trades page renders `crash_guard` with a human label.
12. **Minimal footprint** — the changes are small additions to existing functions with no new module, no import from `trade/main.py`/`trade/signal_agent/`, and no change to `dry_run` behavior (Constitution VII).
