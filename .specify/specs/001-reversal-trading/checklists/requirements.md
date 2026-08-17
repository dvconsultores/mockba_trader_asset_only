# Checklist: 001 requirements quality

- [x] Pivot decision traces to measured evidence (24 live trades, 46% WR vs ~75% breakeven; new profile breakeven 28.6%)
- [x] Method transcribed from the operator's book and confirmed back (3MS criteria incl. ordering and criterion-c bound; misleading variants become test fixtures)
- [x] Clarifications ratified by operator (timeframes cut to 1d/4h/1h; fresh DB; GRAM over TON verified as distinct listings; observe-first)
- [x] UI contract pinned: six tables keep names/shapes; dashboard untouched
- [x] AI is a judge with a checklist, never an oracle: deterministic engine owns structure; verdicts logged with reasoning; fail-closed
- [x] Cost bounded: judge only on candidates, ≤1/asset/4h-candle (~$2–5/mo)
- [x] Risk model per book: 1–1.5% equity risk, R:R ≥ 2.5, ≤10 trades/month
- [x] Phase gate: no orders until observe-mode hit-rate measured (Phase 2 separately authorized)
- [x] Acceptance criteria testable; misleading-pattern rejection unit-tested
