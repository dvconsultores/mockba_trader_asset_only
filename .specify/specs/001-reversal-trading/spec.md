# Feature Specification: 001 — Reversal Trading Bot (founding spec)

**Created**: 2026-08-16 | **Status**: Authorized ("proceed", operator) — implementing
**Flow**: specify → clarify → plan → checklist → tasks → implement (phased) → analyze → converge
**Supersedes**: all scalper-era specs (000–017, archived under `specs/archive/`).
The scalper was retired after 24 live trades at 46% WR against a ~75% breakeven
requirement — a structurally unwinnable payoff profile.

## What

MockbaV4 becomes a **reversal-trading bot** implementing the operator's book
method (the **3MS principle** — 3 Market Structure) on Binance spot (long
reversals) and Orderly perps (long + short), with a deterministic structure
engine feeding a DeepSeek v4-pro reasoning judge. Existing UI (dashboard,
capital, logs, terminal) unchanged. Fresh database. Observe mode first.

## The method (from the book, confirmed with operator)

A reversal is valid only when ALL of:

1. **Key S/R area** (compulsory): price is at a key support/resistance *zone*
   (tolerance band, not a line), evidenced by ≥2 prior touches/rejections.
2. **Market structure change** (compulsory) — the 3MS sequence, in order:
   - *Up→Down*: (a) failure to make a higher high — must occur FIRST;
     (b) price breaks the last low of the uptrend (the X/neckline);
     (c) two lower highs form, the second NOT higher than the last low of the
     previous uptrend. Mirror for *Down→Up*.
   - Misleading variants (book pp. 35, 37: second pivot violating criterion c)
     are explicitly rejected — encoded as engine test fixtures.
3. **Trigger candle**: engulfing / pin bar / double top-bottom carrying the
   reversal sign at the zone. Confluence (trend-line break) raises confidence.

**Trade construction**: enter on the **retest** of the broken neckline (never
chase the break); stop beyond the structural point (T2/K); TP at the next key
level; only take trades with **R:R ≥ 1:2.5** (breakeven WR 28.6%);
**capital-based sizing** — the committed capital per position is 100% at
risk (see Q10; the book's 1–1.5%-of-equity rule is deliberately not used —
operator directive); **≤10 trades/month**; patience over FOMO.

## Clarifications (operator-ratified 2026-08-16)

- **Q1 Timeframes**: 1d (trend context) / 4h (central 3MS analysis) / 1h
  (entry timing + trigger candle). 5m/15m/30m dropped — the book itself warns
  against short-timeframe analysis; crypto mapping shifts the book's
  weekly/daily/4h down one level.
- **Q2 Engine split**: deterministic code computes pivots, HH/HL/LL/LH, trend,
  key zones, 3MS state, retest proximity; **DeepSeek v4-pro (reasoner,
  thinking enabled, effort high)** verifies each criterion and prices the
  trade (entry zone, stop, target, R:R, confidence). AI never free-forms; it
  judges the engine's candidate against the checklist. Full prompt + verdict +
  `reasoning_content` logged per signal.
- **Q3 Cadence**: cycle every 30 min (2/hour). Judge called only when the
  engine has a candidate (state ≥ neckline-break + retest proximity) and at
  most once per 4h candle per asset. Expected cost ≈ $2–5/month.
- **Q4 Assets**: operator-curated table (reuses `asset_universe` so the UI's
  universe view keeps working), NEAR first: **NEAR, SOL, ARB, GRAM, INJ**.
  (TON and GRAM verified to both trade on Binance; operator chose GRAM.)
  Candles fetched per venue: Binance public REST for CEX; Orderly's native
  kline endpoint for DEX (Binance data as fallback if unavailable) —
  operator directive 2026-08-16.
- **Q9 Concurrency** (operator question, recommended answer): at most **one
  position per asset** and `max_concurrent_positions = 2` by default. When
  several assets confirm in the same cycle, take the highest judge
  confidence × R:R first; the rest are recorded as signals. Observe mode
  records everything, so the policy can be re-tuned on evidence before live
  trading.
- **Q10 Sizing** (operator directive 2026-08-16): **capital-based, not
  risk-%**. The operator's venue capital (spot USDT, DEX margin) is 100%
  working capital — position size = `position_size_pct` of available venue
  capital (default 50%, so 2 slots deploy 100%); on Orderly perps, notional
  = committed margin × `dex_leverage` (default 3x — e.g. $20 margin → $60
  notional). The committed capital is what's at risk; the structural stop
  bounds the realized loss (stop distance × notional), it does not shrink
  the position. This replaces the book's risk$-÷-stop-distance formula and
  restores the scalper-era slot model the operator ran live. `rr_min`,
  `max_trades_per_month`, and the kill switches remain the loss governors.
- **Q5 Fresh DB**: all old tables dropped (exchange history is the archive).
  New schema keeps the six UI table names/shapes (`settings`,
  `asset_universe`, `venue_state`, `open_positions`, `closed_trades`,
  `signals` — signals gains structure/AI columns; `SELECT *` compatible).
- **Q6 Rollout**: Phase 1 **observe** (signals + Telegram, zero orders) →
  Phase 2 execution (spot entries CEX; perps long/short DEX after the manual
  dry-run checklist) once signal hit-rate is measured.
- **Q7 Kept from the old bot**: executor (orders/fills/BNB fee accounting),
  kill-switch framework (daily loss %, consecutive losses — live phase),
  Telegram, dashboard, deploy pipeline, folder structure.
- **Q8 Removed**: scalper modules (spot/futures scalpers, regime, toxicity,
  universe scanner, market gate), scalper settings, scalper tests, old specs
  and docs (archived via git).

## Acceptance (Phase 1)

1. Structure engine passes fixtures: valid up→down and down→up 3MS sequences
   accepted; the two book "misleading" variants rejected; pivots/trend/zones
   deterministic and unit-tested.
2. Judge client: valid JSON verdict parsed; malformed output retried then
   rejected fail-closed; API failure → signal recorded as `judge_unavailable`,
   never a crash; reasoning logged.
3. Bot loop: 30-min cycles over the asset table; signals recorded with
   structure packet + verdict; Telegram on confirmed signals; equity cache
   maintained; no orders placed in observe mode.
4. Fresh DB seeds settings + 5 assets; dashboard renders all pages unchanged.
5. Full test suite green; README and docs describe the new bot only.

## Out of scope (Phase 1)

Order execution (Phase 2, incl. exchange-native brackets and capital-based
sizing per Q10), DEX dry-run checklist, judge A/B (flash vs pro — config hook exists),
backtesting harness (candidate for spec 002).
