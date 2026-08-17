-- MockbaV4 schema v3 — reversal trading bot (spec 001, 2026-08-16).
-- Fresh start: scalper-era tables dropped by operator decision (exchange
-- history is the archive). The six table names/shapes the dashboard reads are
-- preserved so the UI runs unchanged.

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Operator-curated asset list (name kept from the scanner era — the
-- dashboard's universe view reads it). rank = operator ordering, NEAR first.
CREATE TABLE IF NOT EXISTS asset_universe (
    venue                TEXT    NOT NULL,      -- 'binance' | 'orderly'
    asset                TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,      -- venue-native symbol
    rank                 INTEGER NOT NULL,
    scanned_at           REAL    NOT NULL,      -- last stats refresh
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

-- Live equity cache; read by the Capital view.
CREATE TABLE IF NOT EXISTS venue_state (
    venue      TEXT PRIMARY KEY,
    equity     REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL
);

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
    fee_entry       REAL NOT NULL DEFAULT 0,
    opened_at       REAL NOT NULL,
    signal_id       INTEGER,
    UNIQUE(asset, venue, id)
);

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

-- Every cycle evaluation is recorded (Constitution VIII). Core columns keep
-- the dashboard's filters working (ts, asset, venue, action, reason, price);
-- the rest carry the structure packet and the AI verdict.
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    asset           TEXT    NOT NULL,
    venue           TEXT    NOT NULL,
    direction       TEXT,                       -- 'long' | 'short' | NULL
    price           REAL    NOT NULL,
    action          TEXT    NOT NULL,           -- 'signal'|'skipped'|'entered'|'observe'
    reason          TEXT    NOT NULL,
    timeframe       TEXT,                       -- central analysis TF ('4h')
    tf_1d_trend     TEXT,                       -- 'up'|'down'|'range'
    ms_state        TEXT,                       -- 3MS state machine state
    neckline        REAL,
    structure_json  TEXT,                       -- pivots/zones/state packet
    ai_valid        INTEGER,                    -- 1/0/NULL (judge not called)
    ai_confidence   REAL,
    ai_entry        REAL,
    ai_stop         REAL,
    ai_target       REAL,
    ai_rr           REAL,
    ai_reasons      TEXT,                       -- JSON array
    ai_reasoning    TEXT,                       -- reasoning_content (audit)
    judge_model     TEXT,
    position_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_asset ON signals(asset, venue);
