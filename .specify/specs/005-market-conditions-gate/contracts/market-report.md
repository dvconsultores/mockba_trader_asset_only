# Contract: Market-Health Report (shared verdict)

**Feature**: 005-market-conditions-gate | **Date**: 2026-08-09

The single structured contract consumed by **both** consumers — the automatic
gate in `bot.py` (observed mode) and the manual Telegram renderer in
`telegram.py` (live mode). Never formatted text at the boundary; text is a
renderer concern (`format_report`).

## Producers

| Producer | Mode | Where | Fresh data |
|---|---|---|---|
| `trade.market_check.check_venue_live(venue)` | `live` | telegram.py `/market` + `/list` button | whole-exchange bookTicker + 24hr + exchangeInfo, then per-survivor depth (token-bucket) |
| `trade.market_check.check_venue_observed(venue, observations, equity=None)` | `observed` | bot.py periodic gate block | rolling per-cycle observations only — zero new market-data calls |

## Shape

```python
{
  "venue": str,                        # "binance" | "orderly"
  "mode": str,                         # "live" | "observed"
  "timestamp": float,                  # time.time() at evaluation
  "scan_fresh": bool,                  # stored scan age <= universe_max_age_hours
  "scan_age_hours": float | None,      # None when no scan stored
  "verdict": str,                      # "PASS" | "WARN" | "FAIL"
  "reasons": [str, ...],               # one-line machine keys, e.g. "liquidity_fail_share=0.55"
  "regime_mix": {str: int},            # {"RANGE": n, "TREND_UP": n, "TREND_DOWN": n, "UNKNOWN": n}
  "assets": {
    "<ASSET>": {
      "passes_liquidity": bool,
      "volume_ok": bool,
      "depth_ok": bool,
      "spread_ok": bool,
      "live_spread_degraded": bool | None,
      "regime": str,                   # RANGE | TREND_UP | TREND_DOWN | UNKNOWN
    }
  },
  "thresholds": {                      # NON-GATING diagnostic (compute_thresholds)
    "atr_pct_median": float | None,
    "dip_needed_pct": float | None,
    "tp_effective_pct": float | None,
    "sl_effective_pct": float | None,
  }
}
```

## Invariants

1. **Shape parity (AC2):** for equivalent inputs, `live` and `observed` reports
   carry identical keys, types and semantics; only `mode` and the freshness of
   the per-asset facts differ.
2. **Deterministic verdict:** `verdict`/`reasons` derive only from the verdict
   rules in `data-model.md` §2 (scan freshness, liquidity shares, regime mix)
   and the `market_gate_*` settings — never from formatting, language, or
   `thresholds` (which is diagnostic-only and never gates).
3. **`scan_fresh == False` ⇒ `verdict == "FAIL"`** (Constitution IV — the
   contract is fail-closed at the freshness gate).
4. **Missing data never passes:** any `None` in stored volume/depth/spread makes
   the corresponding `*_ok` `False`; a missing live-spread observation yields
   `live_spread_degraded = None` (indeterminate — never treated as good).
5. **`assets` covers universe members only** (`get_tradeable_universe(venue)`).

## Example (observed mode, healthy venue)

```python
{
  "venue": "binance", "mode": "observed", "timestamp": 1785970000.0,
  "scan_fresh": True, "scan_age_hours": 5.2,
  "verdict": "PASS", "reasons": [],
  "regime_mix": {"RANGE": 14, "TREND_UP": 1, "TREND_DOWN": 1, "UNKNOWN": 0},
  "assets": {
    "NEAR": {"passes_liquidity": True, "volume_ok": True, "depth_ok": True,
             "spread_ok": True, "live_spread_degraded": False, "regime": "RANGE"},
    "SOL":  {"passes_liquidity": True, "volume_ok": True, "depth_ok": True,
             "spread_ok": True, "live_spread_degraded": False, "regime": "RANGE"}
  },
  "thresholds": {"atr_pct_median": 0.42, "dip_needed_pct": 0.36,
                 "tp_effective_pct": 0.8, "sl_effective_pct": 0.5}
}
```

## Text renderer (contract for the Telegram side)

`trade.market_check.format_report(report) -> str` — compact per-venue block,
no emoji in the structural text (localized static labels are applied by
`telegram.py` via `translate()`):

```
Market — CEX (binance) · observed
Verdict: PASS
Scan: fresh (5.2h) · regime mix: RANGE 14 · UP 1 · DOWN 1 · UNKNOWN 0
Liquidity: 16/16 assets pass
Thresholds (diag): dip 0.36% · tp 0.80% · sl 0.50%
```

- Respects `TELEGRAM_MAX_MESSAGE_LEN = 4096` (chunked by `telegram.py`).
- `PASS`/`WARN`/`FAIL` are rendered verbatim (not translated) so the operator
  always recognizes the verdict token; static labels are translated.
