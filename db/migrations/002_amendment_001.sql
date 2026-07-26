-- Amendment 001 migration — for DBs where schema_v2 already shipped
-- Idempotent: safe to run multiple times.

BEGIN TRANSACTION;

-- Expand signals table
-- SQLite has no ADD COLUMN IF NOT EXISTS; guard manually.
-- These will fail harmlessly if columns already exist (caught by the script).

-- New columns for Amendment 001
ALTER TABLE signals ADD COLUMN direction     TEXT;
ALTER TABLE signals ADD COLUMN threshold_pct REAL;
ALTER TABLE signals ADD COLUMN atr_pct       REAL;
ALTER TABLE signals ADD COLUMN velocity_pct  REAL;
ALTER TABLE signals ADD COLUMN obi_z         REAL;
ALTER TABLE signals ADD COLUMN spread_pct    REAL;
ALTER TABLE signals ADD COLUMN spread_z      REAL;
ALTER TABLE signals ADD COLUMN depth_top10   REAL;
ALTER TABLE signals ADD COLUMN depth_ratio   REAL;
ALTER TABLE signals ADD COLUMN tox_velocity  INTEGER;
ALTER TABLE signals ADD COLUMN tox_spread    INTEGER;
ALTER TABLE signals ADD COLUMN tox_depth     INTEGER;
ALTER TABLE signals ADD COLUMN tox_obi       INTEGER;
ALTER TABLE signals ADD COLUMN tox_any       INTEGER;
ALTER TABLE signals ADD COLUMN tox_enforced  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN position_id   TEXT;

-- closed_trades → signal link
ALTER TABLE closed_trades ADD COLUMN signal_id INTEGER REFERENCES signals(id);

-- open_positions → signal link
ALTER TABLE open_positions ADD COLUMN signal_id INTEGER;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_signals_asset_ts ON signals(asset, venue, ts);
CREATE INDEX IF NOT EXISTS idx_signals_action   ON signals(action);
CREATE INDEX IF NOT EXISTS idx_closed_signal    ON closed_trades(signal_id);

-- Remove superseded settings
DELETE FROM settings WHERE key IN ('obi_buy_threshold','obi_sell_threshold','dip_pct','pump_pct');

-- Add Amendment 001 settings
INSERT OR IGNORE INTO settings (key, value) VALUES ('adaptive_enabled', 'true');
INSERT OR IGNORE INTO settings (key, value) VALUES ('atr_period', '14');
INSERT OR IGNORE INTO settings (key, value) VALUES ('atr_interval', '5m');
INSERT OR IGNORE INTO settings (key, value) VALUES ('candle_cache_sec', '60');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dip_k', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dip_min_pct', '0.15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('pump_k', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('pump_min_pct', '0.15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tp_k', '1.0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tp_min_pct', '0.8');
INSERT OR IGNORE INTO settings (key, value) VALUES ('sl_k', '0.6');
INSERT OR IGNORE INTO settings (key, value) VALUES ('sl_min_pct', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tox_window', '120');
INSERT OR IGNORE INTO settings (key, value) VALUES ('velocity_window', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tox_velocity_enforce', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tox_spread_enforce', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tox_depth_enforce', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tox_obi_enforce', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_extreme_velocity_pct', '0.25');
INSERT OR IGNORE INTO settings (key, value) VALUES ('spread_z_max', '2.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('depth_ratio_min', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('obi_z_max', '2.5');

COMMIT;
