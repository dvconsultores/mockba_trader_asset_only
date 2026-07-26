-- MockbaV4 Schema v2 + Amendment 001
-- Idempotent. WAL mode for concurrent access.

PRAGMA journal_mode=WAL;

-- ── settings ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Remove superseded keys
DELETE FROM settings WHERE key IN ('obi_buy_threshold','obi_sell_threshold','dip_pct','pump_pct');

-- Core
INSERT OR IGNORE INTO settings (key, value) VALUES ('assets', 'NEAR');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tp_pct', '0.8');
INSERT OR IGNORE INTO settings (key, value) VALUES ('sl_pct', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_slots', '9');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cooldown_sec', '60');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_entry_spacing_pct', '0.3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_leverage', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('leverage', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_loss_limit', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_loss_limit_pct', '5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_consecutive_losses', '4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_hold_minutes_spot', '120');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_hold_minutes_futures', '240');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_slot_pct', '15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cex_slot_pct', '15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_round_trip_fee_pct', '0.06');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cex_round_trip_fee_pct', '0.20');
INSERT OR IGNORE INTO settings (key, value) VALUES ('assumed_slippage_pct', '0.03');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_net_edge_pct', '0.30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('regime_cache_sec', '300');
INSERT OR IGNORE INTO settings (key, value) VALUES ('slope_threshold', '0.0012');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dry_run', 'true');
INSERT OR IGNORE INTO settings (key, value) VALUES ('trading_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_trade_binance', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_trade_orderly', 'false');

-- Amendment 001: Adaptive thresholds
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

-- Amendment 001: Toxicity (all enforcement OFF by default)
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

-- ── open_positions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS open_positions (
    id              TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,
    venue           TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    entry_price     REAL NOT NULL,
    signal_price    REAL NOT NULL,
    tp_price        REAL NOT NULL,
    sl_price        REAL,
    tp_order_id     TEXT,
    sl_order_id     TEXT,
    opened_at       REAL NOT NULL,
    signal_id       INTEGER,
    UNIQUE(asset, venue, id)
);

-- ── closed_trades ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS closed_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset           TEXT NOT NULL,
    venue           TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    signal_price    REAL NOT NULL,
    qty             REAL NOT NULL,
    fee_entry       REAL NOT NULL DEFAULT 0,
    fee_exit        REAL NOT NULL DEFAULT 0,
    pnl_net         REAL NOT NULL,
    pnl_pct         REAL NOT NULL,
    opened_at       REAL NOT NULL,
    closed_at       REAL NOT NULL,
    exit_reason     TEXT NOT NULL,
    signal_id       INTEGER REFERENCES signals(id)
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_date ON closed_trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_closed_trades_asset ON closed_trades(asset, venue);
CREATE INDEX IF NOT EXISTS idx_closed_signal ON closed_trades(signal_id);

-- ── signals (Amendment 001) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    asset            TEXT    NOT NULL,
    venue            TEXT    NOT NULL,
    regime           TEXT    NOT NULL,
    direction        TEXT,
    price            REAL    NOT NULL,
    extreme_pct      REAL,
    threshold_pct    REAL,
    atr_pct          REAL,
    velocity_pct     REAL,
    obi              REAL,
    obi_z            REAL,
    spread_pct       REAL,
    spread_z         REAL,
    depth_top10      REAL,
    depth_ratio      REAL,
    tox_velocity     INTEGER,
    tox_spread       INTEGER,
    tox_depth        INTEGER,
    tox_obi          INTEGER,
    tox_any          INTEGER,
    tox_enforced     INTEGER NOT NULL DEFAULT 0,
    action           TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    position_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_ts        ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_asset_ts  ON signals(asset, venue, ts);
CREATE INDEX IF NOT EXISTS idx_signals_action    ON signals(action);
