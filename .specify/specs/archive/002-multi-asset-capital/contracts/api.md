# API Contracts: Multi-Asset Capital

**Feature**: 002-multi-asset-capital | **Date**: 2026-07-27

These are the HTTP API endpoints exposed by the Dashboard backend (`dashboard/main.py`) for asset management. Both the Mini App UI and Telegram bot interact with the same SQLite database; the API is the canonical write path.

---

## GET `/api/assets`

List all assets with their configuration and open position counts.

**Response** (200):
```json
{
  "assets": [
    {
      "symbol": "NEAR",
      "capital_dex": 3000.0,
      "capital_cex": 5000.0,
      "active_dex": true,
      "active_cex": true,
      "open_positions": 1
    }
  ],
  "summary": [
    {
      "venue": "binance",
      "total_allocated": 12000.0,
      "active_pairs": 3,
      "remaining": 8000.0
    },
    {
      "venue": "orderly",
      "total_allocated": 3000.0,
      "active_pairs": 1,
      "remaining": 0.0
    }
  ]
}
```

**Notes**:
- `open_positions` counts rows in `open_positions WHERE asset=symbol`.
- `summary` is computed from all `asset_configs` rows joined against live exchange balance queries for `remaining`.
- If exchange balance query fails, `remaining` is `null` and a `balance_error` field is set per venue.

---

## POST `/api/assets`

Add a new asset configuration.

**Request**:
```json
{
  "symbol": "ETH",
  "capital_dex": 2000.0,
  "capital_cex": 4000.0,
  "active_dex": false,
  "active_cex": true
}
```

**Response** (201):
```json
{
  "symbol": "ETH",
  "capital_dex": 2000.0,
  "capital_cex": 4000.0,
  "active_dex": false,
  "active_cex": true,
  "open_positions": 0
}
```

**Errors**:
- `409 Conflict` — Duplicate symbol: `{"error": "Asset ETH already exists"}`
- `422 Unprocessable Entity` — Validation failure: `{"error": "CEX overallocation: total $14,000 exceeds available $10,000", "venue": "binance", "allocated": 14000, "available": 10000}`
- `422 Unprocessable Entity` — Balance unavailable: `{"error": "Cannot verify Binance balance — save blocked", "venue": "binance", "can_force": true}`
- `422 Unprocessable Entity` — Empty symbol: `{"error": "Symbol is required"}`
- `422 Unprocessable Entity` — Negative capital: `{"error": "capital_dex must be >= 0"}`

---

## PUT `/api/assets/{symbol}`

Edit an existing asset's configuration.

**Request** (partial update — all fields optional, only sent fields are changed):
```json
{
  "capital_cex": 7000.0,
  "active_cex": true
}
```

**Response** (200): Same shape as GET single asset.

**Errors**: Same as POST, plus:
- `404 Not Found` — `{"error": "Asset XYZ not found"}`
- `409 Conflict` — Removal blocked: `{"error": "Cannot remove — 2 open position(s). Deactivate first."}` (only for setting both `active_dex=false` and `active_cex=false` AND `capital_dex=0` AND `capital_cex=0` as a removal intent)

---

## DELETE `/api/assets/{symbol}`

Remove an asset configuration entirely.

**Response** (200):
```json
{
  "removed": "ETH",
  "assets": ["NEAR", "SOL"]
}
```

**Errors**:
- `409 Conflict` — `{"error": "Cannot remove ETH — 1 open position(s). Deactivate first, wait for positions to close, then remove."}`
- `404 Not Found` — `{"error": "Asset XYZ not found"}`

---

## POST `/api/assets/{symbol}/force-save`

Force-save an asset configuration, skipping the exchange balance validation check. Used when balance queries fail and the operator needs to make an emergency change.

**Request**: Same as PUT.

**Response** (200): Same as PUT, plus a `"force_saved": true` flag.

**Errors**:
- `404 Not Found` — Asset not found.

**Notes**: This endpoint is intentionally separate (not a query param on PUT) to make force-saves auditable at the API level. Every force-save is logged prominently.

---

## GET `/api/assets/validate`

Validate a proposed asset configuration without saving. Used for client-side inline validation in the Mini App.

**Query params**: `symbol`, `capital_dex`, `capital_cex`, `active_dex`, `active_cex`

**Response** (200):
```json
{
  "valid": false,
  "errors": [
    {"field": "capital_cex", "level": "error", "message": "CEX overallocation: total $14,000 exceeds available $10,000"}
  ],
  "warnings": [
    {"field": "capital_dex", "level": "warn", "message": "active_dex=true but capital_dex=0 — DEX will be skipped"}
  ]
}
```

---

## Telegram Bot Contract

The Telegram bot reads/writes the same SQLite database directly via `db_ops.py`. It does not call the Dashboard API. The inline keyboard contract is:

### `/assets` Command (new)
Shows the asset list with inline buttons:
```
📦 Assets (3)
━━━━━━━━━━━━━━━━━
🔵 NEAR  | DEX: $3,000 ✅ | CEX: $5,000 ✅ | 1 pos
🟢 ETH   | DEX: $0    ❌ | CEX: $4,000 ✅ | 0 pos
⚪ SOL   | DEX: $2,000 ✅ | CEX: $0    ❌ | 0 pos
━━━━━━━━━━━━━━━━━
CEX: $9,000 / $20,000 | DEX: $5,000 / $10,000

[➕ Add Asset] [✏️ Edit] [🗑️ Remove] [📊 Summary]
```

### Add Asset Flow
1. User taps `➕ Add Asset`
2. Bot prompts: "Enter symbol (e.g., NEAR):"
3. User replies with symbol
4. Bot prompts: "Enter CEX capital in USD (0 to skip):"
5. User replies with amount
6. Bot prompts: "Enter DEX capital in USD (0 to skip):"
7. User replies with amount
8. Bot prompts: "Activate CEX? [Yes] [No]"
9. Bot prompts: "Activate DEX? [Yes] [No]"
10. Bot validates and saves, shows confirmation

### Edit Asset Flow
1. User taps `✏️ Edit`, selects asset from inline list
2. Bot shows current values with edit buttons for each field
3. User changes individual fields via inline prompts
4. Bot re-validates on each change

### Remove Asset Flow
1. User taps `🗑️ Remove`, selects asset
2. If open positions exist: "Cannot remove — N position(s) open. Deactivate first."
3. If no positions: "Remove NEAR? [Confirm] [Cancel]"
4. On confirm: asset deleted, list refreshed
