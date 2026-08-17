# Plan: 001 Reversal Trading Bot

**Spec**: `spec.md` | **Date**: 2026-08-16 | **Branch**: `main`

## Architecture

```
bot.py (30-min loop)
  ├─ fetch 1d/4h/1h klines per venue                 binance: public REST · orderly: native
  │    (operator 2026-08-16)                          kline endpoint, Binance data as fallback
  ├─ trade/structure.py     deterministic engine     pivots → trend → zones → 3MS state → retest
  ├─ trading_bot/reversal_judge.py                   DeepSeek v4-pro verdict on candidates
  ├─ db: signals (+structure & AI columns)           every evaluation recorded
  ├─ telegram on confirmed signals
  └─ venue_state equity cache (dashboard Capital view)
trading_bot/executor.py     kept (orders/fills/fees) — used in Phase 2
dashboard/                  untouched
```

## Constitution check

| Principle | Compliance |
|---|---|
| I (measured evidence) | Engine deterministic + unit-tested; judge verdicts logged with reasoning; hit-rate measurable before capital (observe mode). |
| II (reward > cost) | R:R ≥ 2.5 gate; fees 0.15% vs 5%+ targets — cost is noise. |
| IV (unknown = no trading) | Judge failure / malformed verdict fails closed; observe mode places no orders at all. |
| V (real fills) | Executor accounting kept intact for Phase 2. |
| VIII (bot trades / records) | Every cycle evaluation recorded in signals with reason. |
| HF directive | Retired with the scalper era by operator decision (payoff math superseded frequency). |

## Mechanisms

- **M1 structure engine**: fractal pivots (k=2, closed candles only) →
  alternating zigzag → trend per book minimums (up = 1 HH + 2 HL) → key zones
  (pivot clustering within `level_tolerance_pct`, strength = touches) → 3MS
  state machine per asset (`TREND` → `FAIL_HH` → `NECK_BREAK` → `CONFIRMED`)
  enforcing criterion order and the criterion-c bound (second pivot vs old
  last low/high) → retest detection (price within `retest_tolerance_pct` of
  neckline after break).
- **M2 judge**: raw `requests` POST to DeepSeek chat completions (no SDK dep),
  `deepseek-v4-pro`, `thinking: enabled`, `reasoning_effort: high`; prompt =
  1d trend summary + 4h structure packet + compressed 4h/1h candles + engine
  claim; response instructed as strict JSON
  `{valid, direction, confidence, entry_zone, stop, target, rr, reasons[]}`;
  parse-validate with one retry; `reasoning_content` logged; fail-closed.
- **M3 sizing (Phase 2)**: qty = equity × risk_pct ÷ |entry − stop|; trade
  rejected if judge `rr < rr_min` or monthly trade count ≥ cap.
- **M4 fresh schema v3**: six UI-compatible tables; signals adds `timeframe,
  tf_1d_trend, structure_json, ai_valid, ai_confidence, ai_entry, ai_stop,
  ai_target, ai_rr, ai_reasons, ai_reasoning, judge_model`.
- **M5 removal**: scalper modules/tests/settings deleted; specs 000–017 and
  old docs archived; settings schema rebuilt (~20 keys).

## Testing

`tests/test_structure.py` — pivots, alternation, trend minimums, zone
clustering, full 3MS valid both directions, book misleading variants rejected,
criterion order enforced, retest window. `tests/test_reversal_judge.py` —
verdict parse, malformed retry→fail-closed, API error → None, prompt includes
engine claim. Kept: executor/BNB tests, dashboard tests. Deleted: scalper
tests.
