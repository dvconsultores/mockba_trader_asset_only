-- Amendment 005 cleanup — remove legacy / unused schema elements.
-- Idempotent on re-run (runs every startup after earlier migrations).

-- asset_configs: LEGACY since Amendment 003 — never read by the bot loop,
-- Telegram, dashboard, or any module (verified 2026-08-09). Drop.
DROP TABLE IF EXISTS asset_configs;

-- settings_proposals: created by Amendment 002 for LLM setting proposals, but
-- no module ever reads or writes it (0 rows, no code references). Drop.
DROP TABLE IF EXISTS settings_proposals;

-- signals.position_id: never written by any INSERT path (the scalpers' _log
-- and the market-gate skip recorder omit it). Drop the unused column.
ALTER TABLE signals DROP COLUMN position_id;

-- 'assets' setting: legacy single-asset list, no code reads it. Remove.
DELETE FROM settings WHERE key = 'assets';
