# MockbaV4 Constitution

## Core Principles

### I. One Strategy

The bot executes exactly one trading strategy: **mean reversion.** Buy when price dips below a rolling peak; sell when price pumps above a rolling trough. No candlestick pattern detection, no ML models in the execution path, no LLM second opinions in the hot path. If a proposed feature does not directly serve the question "is price at an extreme and likely to revert?", it does not belong in the bot. ML and LLM tools are permitted in offline `research/` tooling only.

### II. Reward Must Exceed Cost (NON-NEGOTIABLE)

*(Amended in v1.1.0 — see Amendment History. The former payoff-ratio rule `tp_pct > sl_pct` is superseded.)*

Every entry must clear its own round-trip cost by the required margin:

```
tp_effective > round_trip_fee_pct(venue) + assumed_slippage_pct + min_net_edge_pct
```

This is a **per-entry gate** in both scalpers, evaluated fresh with the venue's own fee rate (CEX and DEX differ by ~3×). An entry failing it is never sent to the exchange and is recorded in `signals` with its reason.

The stop is sized to the **asset's volatility**, not to the target. A stop closer than roughly one ATR is noise, not risk management: it converts ordinary intrabar movement into realised losses and, through the consecutive-loss kill switch, into venue lockouts. `sl_k_spot` / `sl_min_pct_spot` (and the DEX equivalents) own this; they are not required to sit inside `tp_effective`.

**The payoff ratio may be below 1.** Reward-versus-risk is judged by expectancy after cost — hit rate × payoff — never by `tp > sl` alone. A configuration risking more than it targets is compliant when the hit-rate asymmetry supports it.

The implied breakeven win rate is computed and logged at startup as a **diagnostic**. Per-position tail risk is owned by `max_loss_per_position_pct` (the crash-guard floor), not by the entry gate.

### III. No Leveraged Position Without a Confirmed Stop (NON-NEGOTIABLE)

Every DEX futures entry is a bracket order: entry + take-profit + stop-loss. After the entry fills, the bot verifies the stop-loss exists at the expected price. If verification fails: place a standalone stop immediately. If that fails: market-close the position in the same cycle. No cycle ends with an unprotected leveraged position.

### IV. Unknown State Means No Trading (NON-NEGOTIABLE)

State queries fail closed. If position count, account equity, order fill status, or exchange connectivity cannot be determined, the bot opens nothing. It never assumes zero positions when the API is unreachable. Consecutive state-query failures escalate: after 5 consecutive failures, disable trading and notify via Telegram.

### V. Real Fills Only

Every PnL number derives from the exchange's actual fill price and actual fee, never from the signal price or an assumed rate. Brackets are computed from the filled entry price, rounded to symbol ticks away from the entry for the stop and toward the entry for the take-profit. Slippage is measured and logged on every fill. The `closed_trades` table records both the signal price and the fill price so slippage is independently auditable.

### VI. Restart Safety

Killing the process and restarting must not duplicate positions, orphan orders, or lose PnL history. On startup, before any entry logic: query the exchange for open positions and live orders; adopt any position with no local DB record; re-attach stops to any position missing one; close out any DB record with no matching exchange position.

### VII. Simplicity Is a Constraint

Target ≤1,500 lines across all hot-path modules (`bot.py`, `regime.py`, `pnl.py`, `executor.py`, `spot_scalper.py`, `futures_scalper.py`). Any module exceeding its line budget in the plan requires explicit justification. No module may import from `trade/main.py` or `trade/signal_agent/` — those are deleted or moved to `research/`.

### VIII. The Bot Trades

Filters exist to avoid bad trades, not to avoid trading. A configuration producing near-zero trade frequency is a bug, not a safe default. The bot logs skipped-entry reasons at DEBUG only; INFO-level logs record entries, exits, errors, and kill-switch events. The `signals` table records every skipped entry with its reason so filter strictness is measurable.

## Operational Constraints

- **dry_run defaults to true.** Every order path checks it. No code path can bypass it.
- **Settings are read fresh each cycle.** Telegram and UI changes take effect without restart. Startup validations re-run on any setting change; trading halts if the new config fails.
- **Structured single-line logs.** Format: `[LEVEL] key=value key=value`. No emoji. No per-cycle "waiting for dip" noise at INFO.
- **No hardcoded assets.** The bot trades whatever assets are in the `assets` DB setting. Symbol derivation is mechanical: `PERP_{ASSET}_USDC` for Orderly, `{ASSET}USDT` for Binance, validated against exchange info.
- **Exchange credentials live in `.env` only.** No module reads them from `os.getenv` directly except the executor. The scalpers receive an `Exchange` object, not API keys.

## Governance

- This constitution supersedes all other design documents. `ARCHITECTURE.md` is advisory; where it conflicts with this document, this document wins.
- Principles marked NON-NEGOTIABLE cannot be relaxed by any spec, plan, or task.
- Amendments require: documented rationale, assessment of impact on each principle, and explicit approval before implementation.
- All PRs against modules in the trading path must verify compliance with principles III, IV, V, and VI.

## Amendment History

### v1.1.0 — 2026-08-15 — Principle II: "Reward Must Exceed Risk" → "Reward Must Exceed Cost"

**What changed.** The payoff-ratio rule `tp_pct > sl_pct` is replaced by the net-edge rule `tp_effective > round_trip_fee + slippage + min_net_edge`, enforced per entry per venue. The stop is explicitly freed from the target and tied to asset volatility instead.

**Rationale.** The old rule encoded reward-versus-risk as a *ratio*, which is only equivalent to positive expectancy under a symmetric hit rate. Measured over the 113 real `action='entered'` signals of 2026-08-05 → 08-15, replaying each asset's actual price path from its true entry timestamp:

- The hit rate is strongly asymmetric: **66%** of entries reach +1.2% within 2h, **38%** drop 2.0%. A payoff ratio below 1 is therefore profitable.
- The stop sat at a median **0.65 × ATR** — inside the noise band. 80% of entries carried a stop closer than 1 ATR, and those were stopped 80% of the time.
- Across a 7 × 6 grid of TP/SL pairs, **every** row improved as the stop widened, and `TP 0.8 / SL 0.6` — the configuration the old rule forced — was the single worst cell (−0.191%/trade). The best scalping cell (`TP 1.2 / SL 2.0`, +0.034%/trade) violates `tp > sl`.
- Realised results agree: 83 closed trades, 64% win rate, **net −$3.74** (+$1.08 excluding one corrupted-fee row), with fees at 69% of gross.

**Impact assessment.**

| Principle | Impact |
|---|---|
| I — One Strategy | None. Mean reversion unchanged; no new signal source. |
| **II** | **Amended.** Enforcement moves from a ratio test to a cost test, and from a startup check to a per-entry gate — strictly stronger at the point of risk. |
| III — Confirmed stop | None. Every futures entry remains a verified bracket. |
| IV — Unknown ⇒ no trading | None. Fail-closed behaviour untouched. |
| V — Real fills | None. No PnL or fee path modified. |
| VI — Restart safety | None. |
| VII — Simplicity | Neutral: replaces one condition with one condition (net +9 lines across two scalpers). |
| VIII — The Bot Trades | **Improved.** The old rule produced 2,656 recorded `tp_eff<=sl_eff` skips and, via noise-triggered stop-outs, 20,374 `max_consecutive_losses` skips — the single largest throughput loss in the `signals` history. |

**Implementation.** Shipped in commit `662837d` (2026-08-15) ahead of this ratification — the amendment was drafted and approved retroactively on 2026-08-15. Live DB values: `sl_k_spot=2.0`, `sl_min_pct_spot=1.5`, `tp_k=1.2`, `max_loss_per_position_pct=3.0`.

**Evidence caveat.** One venue (Binance spot), one 10-day window, one market regime, n=113. The amendment is ratified on this evidence and should be revisited if a materially larger sample contradicts it.

**Approved by**: repository owner, 2026-08-15.

---

**Version**: 1.1.0 | **Ratified**: 2026-07-26 | **Last Amended**: 2026-08-15
