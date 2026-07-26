# MockbaV4 Constitution

## Core Principles

### I. One Strategy

The bot executes exactly one trading strategy: **mean reversion.** Buy when price dips below a rolling peak; sell when price pumps above a rolling trough. No candlestick pattern detection, no ML models in the execution path, no LLM second opinions in the hot path. If a proposed feature does not directly serve the question "is price at an extreme and likely to revert?", it does not belong in the bot. ML and LLM tools are permitted in offline `research/` tooling only.

### II. Reward Must Exceed Risk (NON-NEGOTIABLE)

`tp_pct > sl_pct` always. The bot computes the implied breakeven win rate at startup and refuses to trade if reward does not exceed risk. Defaults: `tp_pct = 0.8`, `sl_pct = 0.5`. Net edge validation: `tp_pct - round_trip_fee_pct - assumed_slippage_pct >= min_net_edge_pct`. Both validations are startup gates, not warnings.

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

**Version**: 1.0.0 | **Ratified**: 2026-07-26 | **Last Amended**: 2026-07-26
