-- Amendment 007 — real Binance fees: capture actual entry commission per position.
-- Idempotent: re-run errors (column already exists) are ignored by the migration runner.
ALTER TABLE open_positions ADD COLUMN fee_entry REAL NOT NULL DEFAULT 0;
