-- Migration 005: Add tp_price and sl_price to signals table
-- Enables post-hoc signal review with take-profit and stop-loss targets.

ALTER TABLE signals ADD COLUMN tp_price REAL;
ALTER TABLE signals ADD COLUMN sl_price REAL;
