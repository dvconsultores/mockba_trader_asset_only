-- MockbaV4 Schema v2
-- Creates new tables alongside legacy ones. Does NOT drop old tables (that's cleanup, 2.9).
-- Idempotent: safe to run multiple times.

-- Enable WAL mode for concurrent access (bot.py + telegram.py)
PRAGMA journal_mode=WAL;

-- ── settings (legacy table, preserved as-is) ────────────────────────────────
-- The existing `settings` table is already created by the legacy code.
-- This migration just ensures new default keys exist.

INSERT OR IGNORE INTO settings (key, value) VALUES ('assets', 'NEAR');
INSERT OR IGNORE INTO settings (key, value) VALUES ('tp_pct', '0.8');
INSERT OR IGNORE INTO settings (key, value) VALUES ('sl_pct', '0.5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dip_pct', '0.4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('pump_pct', '0.4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('obi_buy_threshold', '0.96');
INSERT OR IGNORE INTO settings (key, value) VALUES ('obi_sell_threshold', '1.22');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_slots', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cooldown_sec', '300');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_entry_spacing_pct', '0.6');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_hold_minutes', '240');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_leverage', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_loss_limit', '0');
INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_loss_limit_pct', '5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_consecutive_losses', '4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('round_trip_fee_pct', '0.06');
INSERT OR IGNORE INTO settings (key, value) VALUES ('assumed_slippage_pct', '0.03');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_net_edge_pct', '0.30');

-- Venue-specific
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_slot_pct', '15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cex_slot_pct', '15');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_compound_pct', '100');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_round_trip_fee_pct', '0.06');
INSERT OR IGNORE INTO settings (key, value) VALUES ('dex_assumed_slippage_pct', '0.03');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cex_round_trip_fee_pct', '0.20');
INSERT OR IGNORE INTO settings (key, value) VALUES ('cex_assumed_slippage_pct', '0.05');

-- Operational
INSERT OR IGNORE INTO settings (key, value) VALUES ('dry_run', 'true');
INSERT OR IGNORE INTO settings (key, value) VALUES ('trading_enabled', '1');
INSERT OR IGNORE INTO settings (key, value) VALUES ('direction', 'both');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_trade_binance', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_trade_orderly', 'false');
INSERT OR IGNORE INTO settings (key, value) VALUES ('regime_cache_sec', '300');
INSERT OR IGNORE INTO settings (key, value) VALUES ('interval', '5m');
INSERT OR IGNORE INTO settings (key, value) VALUES ('slope_threshold', '0.0012');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_hold_minutes_futures', '240');
INSERT OR IGNORE INTO settings (key, value) VALUES ('max_hold_minutes_spot', '120');

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
    exit_reason     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_date ON closed_trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_closed_trades_asset ON closed_trades(asset, venue);

-- ── signals ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    asset           TEXT NOT NULL,
    venue           TEXT NOT NULL,
    regime          TEXT,
    obi             REAL,
    extreme_pct     REAL,
    action          TEXT NOT NULL,   -- 'entered' | 'skipped'
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_asset ON signals(asset, venue);
