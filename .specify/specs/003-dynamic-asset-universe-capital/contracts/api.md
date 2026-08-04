# API Contracts: Dynamic Asset Universe & Capital View

**Feature**: 003-dynamic-asset-universe-capital | **Date**: 2026-08-04

These are the HTTP API endpoints exposed by the Dashboard backend
(`dashboard/main.py`) for the Capital view and the Universe panel. The Mini App
UI and the Telegram bot interact with the same SQLite database; the API is the
canonical write path for settings and blacklist overrides.

The Amendment 004 `/api/assets*` per-asset endpoints were **replaced** by these —
no per-asset capital remains reachable from the UI.

---

## GET `/api/capital`

Per-venue capital view. Public (read-only, no auth).

**Response** (200):
```json
{
  "ok": true,
  "venues": [
    {
      "venue": "binance",
      "declared_capital": 4000.0,
      "live_equity": 6200.0,
      "equity_age": 1751234567.0,
      "divergence": { "declared": 4000.0, "live": 6200.0, "pct": 35.5 },
      "slot_pct": 10.0,
      "slot_size": 620.0,
      "max_slots": 9,
      "deployed": 1240.0,
      "free": 4960.0,
      "fee_pct": 0.2,
      "net_edge_pct": 0.57,
      "enabled": "Automatic"
    },
    {
      "venue": "orderly",
      "declared_capital": 3000.0,
      "live_equity": 3000.0,
      "equity_age": 1751234567.0,
      "divergence": null,
      "slot_pct": 10.0,
      "slot_size": 300.0,
      "max_slots": 9,
      "deployed": 0.0,
      "free": 3000.0,
      "fee_pct": 0.06,
      "net_edge_pct": 0.71,
      "enabled": "False"
    }
  ]
}
```

**Notes**:
- `divergence` is present only when declared capital deviates from live equity
  by more than 25% — a UI warning. Sizing is never changed by it (exchange wins).
- `live_equity` comes from the `venue_state` cache written by `bot.py`; a null
  `equity_age` means the bot has not written one yet.
- `net_edge_pct = tp_min_pct − fee_pct − assumed_slippage_pct`, per venue.
- `enabled` is the venue mode (`False` | `Signal` | `Automatic`).

---

## GET `/api/universe/{venue}`

Current universe for a venue. Public (read-only, no auth). `venue` is
`binance` or `orderly`.

**Response** (200):
```json
{
  "ok": true,
  "venue": "binance",
  "rows": [
    {
      "asset": "NEAR",
      "symbol": "NEARUSDT",
      "rank": 1,
      "scanned_at": 1751234567.0,
      "quote_volume_24h": 240000000.0,
      "spread_pct": 0.021,
      "depth_bid_top10": 850000.0,
      "depth_ask_top10": 820000.0,
      "atr_pct_median": 0.42,
      "signals_count": 46,
      "recovery_rate": 0.82,
      "median_minutes_to_tp": 14.0,
      "blacklisted": 0
    }
  ],
  "scanned_at": 1751234567.0,
  "scan_age_hours": 5.2,
  "stale": false
}
```

**Errors**:
- `400` — venue not in `{"binance","orderly"}`
- `500` — database error

---

## PUT `/api/universe/{venue}/{asset}/blacklist`

Set the operator blacklist override. Requires Telegram initData auth or a valid
admin session. This is the **only** writable part of the Universe panel — assets
are not manually added.

**Request**:
```json
{ "blacklisted": true }
```

**Response** (200):
```json
{ "ok": true, "venue": "binance", "asset": "NEAR", "blacklisted": true }
```

**Errors**:
- `403` — invalid auth
- `404` — asset not in the venue's universe
- `400` — venue invalid

---

## Settings writes (unchanged)

Declared capital, slot %, and max slots are ordinary settings, edited through the
existing `POST /api/miniapp` (which runs post-save validation) or Telegram.

---

## Telegram commands

| Command | Purpose |
|---|---|
| `/capital` | Both venues: declared vs live equity, slot size, deployed, free |
| `/universe [cex\|dex]` | Current list with metrics and scan age (both if omitted) |
| `/blacklist add\|remove <ASSET>` | Operator override, applied to both venues where present |
