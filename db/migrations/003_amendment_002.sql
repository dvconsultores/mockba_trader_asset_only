-- Amendment 002 migration — Settings validator + LLM helper
-- Idempotent on re-run.

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS settings_baseline (
    key            TEXT PRIMARY KEY,
    baseline_value TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unvalidated',
    evidence       TEXT,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     REAL NOT NULL,
    source         TEXT NOT NULL,
    key            TEXT NOT NULL,
    current_value  TEXT NOT NULL,
    proposed_value TEXT NOT NULL,
    reason         TEXT NOT NULL,
    evidence       TEXT,
    confidence     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    decided_at     REAL,
    model          TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON settings_proposals(status, created_at);

-- LLM helper settings
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_helper_enabled', 'true');
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_language', 'es');
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_model', 'deepseek-v4-pro');
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_timeout_sec', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_explain_cache_days', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('llm_max_calls_per_hour', '20');

-- Ensure atr_interval exists (was missing from Amendment 001 seed)
INSERT OR IGNORE INTO settings (key, value) VALUES ('atr_interval', '5m');

-- Seed baselines — all unvalidated until dry-run evidence
INSERT OR IGNORE INTO settings_baseline (key, baseline_value, status, evidence, updated_at)
  SELECT key, value, 'unvalidated',
         'Amendment 001 placeholder — no measurement performed',
         CAST(strftime('%s','now') AS REAL)
  FROM settings
  WHERE key NOT IN (SELECT key FROM settings_baseline);

COMMIT;
