"""
Tests for feature 007 — Dashboard Settings Read-Only.

Verifies (spec ACs 1–11, unit-testable):
  - POST /api/miniapp with a settings key → 403 and the stored value is unchanged
  - POST /api/miniapp with __ping__ → unchanged auth probe behavior (no write)
  - POST /api/miniapp with a capital key (cex_slot_pct) → still writes (AC7)
  - GET /api/miniapp unchanged — the settings display stays fully readable

Mirrors tests/test_closed_trades_page.py conventions: FastAPI TestClient with
a throwaway SQLite DB via monkeypatch.setattr(api, "DB_PATH", ...). The admin
session validator is monkeypatched so auth is deterministic; the validation
Telegram alert is no-op'd so a best-effort validate_all can never fail the test.
"""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dashboard.main as api
from fastapi.testclient import TestClient

SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Throwaway settings DB + deterministic auth + no-op validation alert."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(SETTINGS_DDL)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tp_min_pct', '0.5')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(api, "DB_PATH", db_path)
    monkeypatch.setattr(api, "_send_validation_alert", lambda *a, **k: None)
    with TestClient(api.app) as c:
        yield c, db_path


def _value(db_path, key):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def _auth_ok(monkeypatch):
    async def _ok(request):
        return True
    monkeypatch.setattr(api, "_validate_admin_session", _ok)


def _auth_deny(monkeypatch):
    async def _deny(request):
        return False
    monkeypatch.setattr(api, "_validate_admin_session", _deny)


def test_settings_key_rejected_403_and_not_written(client, monkeypatch):
    c, db_path = client
    _auth_ok(monkeypatch)  # even a fully-authorized session cannot write settings
    r = c.post("/api/miniapp", json={"key": "tp_min_pct", "value": "0.9"})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    assert _value(db_path, "tp_min_pct") == "0.5"  # stored value unchanged


def test_ping_probe_unchanged_authorized(client, monkeypatch):
    c, _ = client
    _auth_ok(monkeypatch)
    r = c.post("/api/miniapp", json={"key": "__ping__", "value": ""})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_ping_probe_unchanged_unauthorized(client, monkeypatch):
    c, _ = client
    _auth_deny(monkeypatch)
    r = c.post("/api/miniapp", json={"key": "__ping__", "value": ""})
    assert r.status_code == 403
    assert r.json()["detail"] == "Invalid auth"


def test_capital_key_still_writable(client, monkeypatch):
    c, db_path = client
    _auth_ok(monkeypatch)
    r = c.post("/api/miniapp", json={"key": "cex_slot_pct", "value": "40"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert _value(db_path, "cex_slot_pct") == "40"  # capital key still writes (AC7)


def test_settings_get_still_readable(client):
    c, db_path = client
    r = c.get("/api/miniapp")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["settings"]["tp_min_pct"] == "0.5"  # display unchanged
