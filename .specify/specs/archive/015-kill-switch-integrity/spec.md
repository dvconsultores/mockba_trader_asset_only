# Feature Specification: 015 — Kill-Switch Integrity

**Feature Branch**: `015-kill-switch-integrity` *(implementation on `main` per repo convention)*

**Created**: 2026-08-16

**Status**: Clarified — Q1–Q4 resolved. Implementation authorized (operator: "proceed with all cycle of 015").

**Constitution**: v1.1.0 — Principle IV (**NON-NEGOTIABLE**) repair on the live venue: "State queries fail closed… It never assumes zero positions when the API is unreachable. Consecutive state-query failures escalate: after 5 consecutive failures, disable trading **and notify via Telegram**."

---

## What

Three defects in the systems that protect deployed capital, all repaired together because they share one root: **unknown equity is currently treated as zero, and failures are mis-counted**.

### Part 1 — `get_equity` returns `None` on failure, never `0.0` (audit item 4)

Both executors swallow every exception and return `0.0`. Downstream,
`is_entry_blocked` computes `limit = equity × daily_loss_limit_pct / 100` and
**skips the check when the limit is 0** — so during any Binance API outage the
percentage daily-loss kill switch silently turns off, at exactly the moment it
should be armed. The swallow also means bot.py's `except` branch never runs, so
the venue-failure escalation can never fire for equity failures.

New contract: `get_equity() -> float | None`. `None` = unknown (Constitution
IV). Callers handle it explicitly; the `venue_state` cache is **never written
with a failure zero** — it keeps the last-known-good value.

### Part 2 — Consecutive means consecutive, and escalation notifies (audit item 3)

`bot.py` recreates `_venue_failures` **inside the loop every cycle**, then
disables a venue at `fails >= 5` while logging "consecutive failures". Reality:
5 assets failing in a *single* network blip permanently writes
`auto_trade_binance=false` — a false kill on the live venue — while a venue
failing 4 times every cycle forever never trips it. The Telegram notification
Constitution IV mandates was also never implemented.

New behaviour: a module-level per-venue streak counts **consecutive cycles**
with a failed equity query; any successful query resets it. At 5, the venue is
disabled **and a Telegram alert fires**. Per-asset cycle errors (regime fetch,
price fetch) are logged but no longer feed venue escalation — they are not
venue-level state queries.

### Part 3 — Equity sees the whole account (audit item 12)

`BinanceSpot.get_equity` counts only USDT (free+locked). Coins held by open
positions are invisible, so with positions open, "equity" collapses toward free
cash (~$20 on the $100 account with 4 slots deployed): slot sizing shrinks
toward the `min_notional` floor and the daily-loss-pct limit computes against a
fifth of real capital. This directly undercuts feature 012's 4 × $20 plan.

New behaviour: equity = USDT (free+locked) **+ open positions valued at their
entry fill** from the local DB (quantities and prices are real fills —
Constitution V). Valuation error is bounded by the TP/SL band (~±2%), and the
approach costs **zero additional API calls** (clarify Q2).

---

## Clarifications

### Session 2026-08-16

- **Q1 — What does an unknown equity do to `scalp_cycle`?** Live (`dry_run=false`):
  skip the entry, recorded in `signals` with reason `equity_unavailable`
  (Constitution IV + VIII: fail closed, but measurably). **Dry-run**: fall back
  to the declared `capital_cex_usdt` / `capital_dex_usdc` pool — paper trading
  must work without exchange credentials, and failing closed there would make
  `dry_run` untestable. The fallback is only reachable in dry-run.
- **Q2 — Position valuation source** → entry fill from `open_positions` (local
  DB), not live tickers. Zero API calls, error bounded by the TP/SL band, and
  the DB quantities/prices are real fills. Live-price valuation would add one
  ticker call per open asset per cycle for ~2% precision that sizing does not
  need. Revisit only if positions ever ride far beyond their brackets.
- **Q3 — What counts toward the venue streak?** Only the **venue-level equity
  query** — the state query Constitution IV describes. Per-asset errors are
  heterogeneous (one delisted symbol could otherwise kill the venue) and remain
  log-only. One failed cycle = streak+1 regardless of how many assets it hit.
- **Q4 — The threshold 5: setting or constant?** Constant. Constitution IV
  names the number explicitly; making it a setting would let configuration
  contradict a NON-NEGOTIABLE principle. The "settings, never constants"
  convention governs tunables, not constitutional invariants.

---

## Scope

**In**: `trading_bot/executor.py` (both `get_equity`), `bot.py` (equity block,
streak helper, removal of the per-cycle counter and its escalation loop),
`trading_bot/spot_scalper.py` + `futures_scalper.py` (Q1 handling),
`tests/test_kill_switch_integrity.py` (new), docs.

**Out**: loop latency (013); `daily_loss_limit_pct` semantics; the market gate;
`OrderlyFutures` position valuation (its `/v1/client/holding` already returns
total collateral); dashboard (has no executor dependency — verified);
`futures_scalper._save_open` `fee_entry` (010 converge item); any migration.

---

## Acceptance criteria

1. **Unknown is None** — both `get_equity` implementations return `None` on
   request failure; no code path converts a failure into `0.0`.
2. **Cache never poisoned** — `set_venue_equity` is called only with a real
   number; on failure the venue keeps its last-known-good `venue_state` row.
3. **Daily-loss limit stays armed** — with positions open and USDT low, equity
   reflects total account value, so `limit = equity × pct` is non-zero.
4. **Whole-account equity** — USDT + Σ(qty × entry_price) over open binance
   positions (AC3's mechanism; audit #12).
5. **Streak is consecutive across cycles** — 4 failures, a success, then 4 more
   failures never disables; 5 consecutive failures do.
6. **Escalation notifies** — the disable writes `auto_trade_{venue}=false` AND
   sends a Telegram alert (Constitution IV's missing half).
7. **Per-asset errors do not escalate** — an asset-cycle exception is logged and
   skips that asset only.
8. **Live entry fails closed** — `scalp_cycle` with unknown equity and
   `dry_run=false` places nothing and records `equity_unavailable`.
9. **Dry-run falls back to the declared pool** — paper trading works without
   credentials (Q1).
10. **Tests** — new suite covers AC1–AC9; the existing 107 stay green.
11. **Docs** — CURRENT_STATE feature-015 section + CHANGELOG `fix:`.
