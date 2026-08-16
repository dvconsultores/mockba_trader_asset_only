-- Migration 009: entry confirmation verdict on signals (feature 009).
-- 1 = confirmed, 0 = not confirmed, NULL = indeterminate / not evaluated
-- (gate + global-loss skips never evaluate it, and pre-migration rows stay NULL).
-- Idempotent on re-run: _run_migrations wraps each script in try/except, so the
-- duplicate-column error on the second run is swallowed (same as migration 008).
ALTER TABLE signals ADD COLUMN entry_confirmed INTEGER;
