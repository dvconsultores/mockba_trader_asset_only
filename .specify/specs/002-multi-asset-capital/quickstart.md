# Quickstart: Multi-Asset Capital Validation

**Feature**: 002-multi-asset-capital | **Date**: 2026-07-27

Run these scenarios to validate the feature end-to-end after implementation. All commands run from the project root.

## Prerequisites

- Bot is running in dry-run mode (`dry_run=true` in settings)
- Exchange API credentials configured in `.env`
- Dashboard API accessible at `http://localhost:8000`
- Telegram bot token configured

---

## Scenario 1: Add Asset via Dashboard API

**Goal**: Verify per-asset capital and flags are stored correctly.

```bash
# 1. Add a new asset
curl -X POST http://localhost:8000/api/assets \
  -H "Content-Type: application/json" \
  -d '{"symbol":"ETH","capital_dex":3000,"capital_cex":5000,"active_dex":false,"active_cex":true}'

# Expected: 201 Created with the asset object
```

```bash
# 2. Verify it appears in the list
curl http://localhost:8000/api/assets

# Expected: "assets" array contains ETH with correct values
# Expected: "summary" shows updated CEX allocation
```

```bash
# 3. Verify bot picks it up (check logs)
tail -20 apolo.log | grep "ETH"

# Expected: log line showing ETH evaluated on binance (CEX) only
# Expected: no log line for ETH on orderly (DEX inactive)
```

---

## Scenario 2: Multiple Assets Active Simultaneously

**Goal**: Verify the bot evaluates all active pairs independently.

```bash
# 1. Add three assets
curl -X POST http://localhost:8000/api/assets \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC","capital_dex":2000,"capital_cex":3000,"active_dex":true,"active_cex":true}'

curl -X POST http://localhost:8000/api/assets \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SOL","capital_dex":1000,"capital_cex":2000,"active_dex":true,"active_cex":true}'

# 2. Check bot logs for all pairs
tail -50 apolo.log | grep -E "binance:|orderly:"

# Expected: logs for (BTC,binance), (BTC,orderly), (ETH,binance), (SOL,binance), (SOL,orderly)
# Note: ETH has active_dex=false, so (ETH,orderly) should NOT appear
```

---

## Scenario 3: Overallocation Guardrail

**Goal**: Verify save is hard-blocked when total allocation exceeds balance.

```bash
# 1. Try to add an asset with capital that exceeds available balance
curl -X POST http://localhost:8000/api/assets \
  -H "Content-Type: application/json" \
  -d '{"symbol":"DOGE","capital_cex":999999,"active_cex":true}'

# Expected: 422 with error message containing "overallocation", venue, and available balance
```

```bash
# 2. Verify the asset was NOT saved
curl http://localhost:8000/api/assets | grep DOGE

# Expected: no match (DOGE was not added)
```

---

## Scenario 4: Deactivation with Open Position

**Goal**: Verify deactivation stops new entries but preserves position management.

```bash
# 1. Activate an asset and wait for the bot to open a position (dry-run may skip — use manual test or check open_positions)
# 2. Deactivate the asset
curl -X PUT http://localhost:8000/api/assets/NEAR \
  -H "Content-Type: application/json" \
  -d '{"active_cex":false}'

# Expected: 200 OK
```

```bash
# 3. Verify the asset list shows "pending exit" state
curl http://localhost:8000/api/assets

# Expected: NEAR asset has active_cex=false but open_positions > 0
```

---

## Scenario 5: Migration

**Goal**: Verify legacy settings migrate correctly.

```bash
# 1. Stop the bot
# 2. Reset to legacy state: insert legacy keys into settings table
sqlite3 data/trading.db "
  INSERT OR REPLACE INTO settings (key, value) VALUES ('assets', 'NEAR,ETH');
  INSERT OR REPLACE INTO settings (key, value) VALUES ('dex_slot_pct', '10');
  INSERT OR REPLACE INTO settings (key, value) VALUES ('cex_slot_pct', '20');
  INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_trade_orderly', 'true');
  INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_trade_binance', 'false');
  DELETE FROM asset_configs;
"

# 3. Start the bot
python bot.py

# 4. Check migration output in logs
tail -30 apolo.log | grep -i migrat

# Expected: "migration complete — 2 assets, legacy keys removed"

# 5. Verify asset_configs was populated
sqlite3 data/trading.db "SELECT * FROM asset_configs;"

# Expected: NEAR has capital values, active_dex=true, active_cex=false
# Expected: ETH has capital=0, active_dex=false, active_cex=false
```

```bash
# 6. Verify legacy keys were removed
sqlite3 data/trading.db "SELECT key FROM settings WHERE key IN ('dex_slot_pct','cex_slot_pct','auto_trade_binance','auto_trade_orderly');"

# Expected: empty (no rows)
```

---

## Scenario 6: UI Parity — Mini App

**Goal**: Verify the Mini App Asset Manager reflects the new data model.

1. Open the Mini App at `http://localhost:5173` (or the nginx-proxied URL)
2. Navigate to the **Assets** tab
3. Verify the asset list shows: symbol, DEX capital, CEX capital, active status per venue, open position count
4. Add a new asset via the form: enter symbol, capital values, toggle venue flags
5. Verify the asset appears in the list and the allocation summary updates
6. Edit an existing asset: change CEX capital, verify the change persists on refresh
7. Remove an asset with no positions: verify it disappears from the list

---

## Scenario 7: Global Daily Loss Limit

**Goal**: Verify the global kill switch trips when aggregate PnL drops below the limit.

```bash
# 1. Set a very low global daily loss limit for testing
curl -X POST http://localhost:8000/api/miniapp \
  -H "Content-Type: application/json" \
  -d '{"key":"global_daily_loss_limit","value":"1"}'

# 2. Wait for the bot to accumulate $1 in losses across all pairs
# 3. Check trading_enabled
sqlite3 data/trading.db "SELECT value FROM settings WHERE key='trading_enabled';"

# Expected: "0" (trading disabled)
```

```bash
# 4. Reset for normal operation
curl -X POST http://localhost:8000/api/miniapp \
  -H "Content-Type: application/json" \
  -d '{"key":"global_daily_loss_limit","value":"0"}'
```

---

## Regression Check: Single-Asset Operation

**Goal**: Verify single-asset behavior is identical to before.

```bash
# 1. Remove all but one asset
curl -X DELETE http://localhost:8000/api/assets/ETH
curl -X DELETE http://localhost:8000/api/assets/SOL
curl -X DELETE http://localhost:8000/api/assets/BTC

# 2. Configure the remaining asset with both venues active
curl -X PUT http://localhost:8000/api/assets/NEAR \
  -H "Content-Type: application/json" \
  -d '{"active_dex":true,"active_cex":true,"capital_dex":3000,"capital_cex":5000}'

# 3. Verify the bot evaluates exactly two pairs: (NEAR, binance) and (NEAR, orderly)
tail -20 apolo.log | grep -E "binance:|orderly:" | sort | uniq

# Expected: exactly two unique pair identifiers
```

---

## Test Command Summary

```bash
# Run all unit tests
pytest tests/ -v

# Run only settings validation tests
pytest tests/ -v -k "validate"

# Run only DB migration tests
pytest tests/ -v -k "migration"

# Run the bot in foreground for manual testing
DRY_RUN=true python bot.py
```
