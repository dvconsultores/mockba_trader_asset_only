-- Amendment 003 migration — Dynamic asset universe & Capital view
-- Idempotent on re-run.
--
-- NOTE (per-asset capital report): on this codebase per-asset capital lives
-- in the `asset_configs` table (capital_dex/capital_cex), NOT in `settings`.
-- No settings rows are deleted here — asset_configs rows are preserved
-- untouched (read-only legacy) and the new venue pools are the
-- capital_dex_usdc / capital_cex_usdt settings seeded below.
-- The legacy global 'fee_round_trip_pct' key never existed on this codebase;
-- per-venue fees are already `dex_round_trip_fee_pct` / `cex_round_trip_fee_pct`
-- (Amendment 004) and are reused as-is.

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS asset_universe (
    venue                TEXT    NOT NULL,      -- 'binance' | 'orderly'
    asset                TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,      -- venue-native
    rank                 INTEGER NOT NULL,
    scanned_at           REAL    NOT NULL,
    quote_volume_24h     REAL,
    spread_pct           REAL,
    depth_bid_top10      REAL,
    depth_ask_top10      REAL,
    atr_pct_median       REAL,
    signals_count        INTEGER,
    recovery_rate        REAL,
    median_minutes_to_tp REAL,
    blacklisted          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (venue, asset)
);
CREATE INDEX IF NOT EXISTS idx_universe_rank ON asset_universe(venue, rank);

CREATE TABLE IF NOT EXISTS venue_state (
    venue      TEXT PRIMARY KEY,
    equity     REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES
  ('universe_scan_interval_hours','24'),
  ('universe_max_age_hours','36'),
  ('universe_size','20'),
  ('universe_min_volume_usd','5000000'),
  ('universe_spread_ratio_max','0.10'),
  ('universe_rank_min','15'),
  ('universe_rank_max','90'),
  ('universe_depth_slot_multiple','3'),
  ('universe_replay_days','7'),
  ('universe_min_signals','20'),
  ('universe_min_recovery_rate','auto'),
  ('universe_spread_degradation_multiple','3'),
  ('capital_cex_usdt','0'),
  ('capital_dex_usdc','0'),
  ('max_slots_cex','9'),
  ('max_slots_dex','9');

-- fee_round_trip_pct would become per-venue; here it never existed.
-- Guard against any stale global fee key so the per-venue keys are authoritative.
DELETE FROM settings WHERE key = 'fee_round_trip_pct';

-- Per-venue fee keys already exist (Amendment 004). Ensure they are seeded
-- if a fresh DB somehow lacks them.
INSERT OR IGNORE INTO settings (key, value) VALUES
  ('dex_round_trip_fee_pct','0.06'),
  ('cex_round_trip_fee_pct','0.20');

-- seed baselines for the new keys, unvalidated per Amendment 002
INSERT OR IGNORE INTO settings_baseline (key, baseline_value, status, evidence, updated_at)
  SELECT key, value, 'unvalidated',
         'Amendment 003 placeholder; no measurement performed',
         strftime('%s','now')
  FROM settings
  WHERE key LIKE 'universe_%'
     OR key IN ('capital_cex_usdt','capital_dex_usdc','max_slots_cex','max_slots_dex');

COMMIT;
