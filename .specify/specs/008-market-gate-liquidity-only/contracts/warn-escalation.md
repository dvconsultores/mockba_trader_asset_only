# Contract: WARN Escalation & Notification Scope

**Feature**: 008-market-gate-liquidity-only | **Date**: 2026-08-12

Which WARN verdicts **escalate** (count toward suspension and fire the ⚠️/✅
Telegram pair) vs stay **mild** (informational, log-only). Single source of
truth: the pure classifier `trade.market_check._warn_is_strong(reasons,
settings)`, consumed by both `update_gate_state` (suspension state machine)
and `bot._gate_apply` (⚠️/✅ notification gate).

## Classification

| `reasons[0]` prefix | Strong? | Condition |
|---|---|---|
| `liquidity_partial=` | **yes** | `float(value) >= settings["market_gate_warn_liquidity_share"]` (default 0.25); `>=` boundary is strong |
| `liquidity_partial=` | no | below the share (a lone bad asset) — mild |
| `regime_trending=` / `regime_unknown=` | **yes** | `settings["market_gate_regime_escalates"]` is truthy (`get_setting_bool(..., False)` default) |
| `regime_trending=` / `regime_unknown=` | no | setting `false`/unset (default) — mild, informational |
| anything else / empty `reasons` | no | mild (defensive; `_evaluate` never produces a bare WARN with no reason) |

> `liquidity_fail_share=` is a **FAIL** verdict, not a WARN — it escalates via
> the FAIL branch (unchanged). It is listed here only to complete the picture.

## Behavior

| Classification | `update_gate_state` | `bot._gate_apply` notification | Log |
|---|---|---|---|
| strong | `bad_streak`+1, `good_streak`=0; suspend when `bad_streak >= market_gate_bad_streak` | ⚠️ WARNING on start (`warn_active` False→True); ✅ warning cleared on PASS (`warn_active` True→False) | `[GATE] … action=warn_start` / `action=warn_clear` (plus the generic `action=hold`) |
| mild | `bad_streak`=0, `good_streak`=0 — never suspends | **none** — `warn_active` untouched, no ⚠️/✅ | `[GATE] … action=hold` only |

## Invariants

1. **Single classifier, no divergence**: `_warn_is_strong(reasons, settings)`
   is the only place that decides strong/mild. `_gate_apply` and
   `update_gate_state` call it with the **same** `settings` dict and the
   **same** `report["reasons"]`, so the notification trigger and the suspension
   classification are identical by construction.
2. **No report-field change**: the report contract (`market-report.md`) is
   untouched — no `strong_warn` flag; strong-ness is derived at the consumer
   from `reasons` + settings.
3. **Verdicts unchanged**: `_evaluate` still downgrades PASS→WARN for
   `regime_trending=`/`regime_unknown=` with identical reasons — the operator
   still sees `verdict=WARN` in the structured `[GATE]` log even when the WARN
   is mild (AC2).
4. **Mild never notifies, never suspends**: a mild WARN (default regime WARN or
   `liquidity_partial` below the share) cannot increment `bad_streak`, cannot
   suspend, and cannot fire ⚠️/✅ — its only signal is the structured log line.
5. **⚠️/✅ scope**: the pair fires **only** for strong/escalating WARNs —
   `liquidity_partial` ≥ `market_gate_warn_liquidity_share`, or regime WARNs
   when `market_gate_regime_escalates=true`. Debounce mechanics (one message
   per transition/lifecycle) unchanged.

## Settings involved

| Setting | Role | Default |
|---|---|---|
| `market_gate_warn_liquidity_share` | strong-threshold for `liquidity_partial=` | 0.25 |
| `market_gate_regime_escalates` **(new)** | re-enables regime-WARN escalation (005 behavior) | false |
| `market_gate_bad_streak` / `market_gate_good_streak` | suspension / resume streaks (unchanged) | 2 / 2 |
