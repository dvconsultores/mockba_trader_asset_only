-- Migration 004: Multi-asset per-asset capital and venue activation
-- Creates asset_configs table to replace global capital/activation settings.
-- Idempotent. Safe to run against any schema state.

CREATE TABLE IF NOT EXISTS asset_configs (
    symbol      TEXT PRIMARY KEY,
    capital_dex REAL NOT NULL DEFAULT 0.0,
    capital_cex REAL NOT NULL DEFAULT 0.0,
    active_dex  INTEGER NOT NULL DEFAULT 0,
    active_cex  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Legacy keys that will be removed by migration logic (not here — in db_ops.py):
--   assets, dex_slot_pct, cex_slot_pct, max_slots,
--   auto_trade_binance, auto_trade_orderly, capital
