"""Tests for bot.sync_orderly_listings — Orderly listing check (transport mocked)."""
import os, sys
import time
import pytest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def db(tmp_path):
    import db.db_ops as ops
    old = ops.DB_PATH
    ops.DB_PATH = str(tmp_path / "test.db")
    ops.initialize_database_tables()
    for venue in ("binance", "orderly"):
        ops.replace_universe(venue, [
            {"asset": a,
             "symbol": f"{a}USDT" if venue == "binance" else f"PERP_{a}_USDC",
             "rank": i + 1, "scanned_at": time.time()}
            for i, a in enumerate(["NEAR", "GRAM"])
        ])
    yield ops
    ops.DB_PATH = old


def _resp(symbols):
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json.return_value = {"data": {"rows": [{"symbol": s} for s in symbols]}}
    return m


def _sync(response=None, side_effect=None):
    import bot
    with mock.patch("requests.get", return_value=response, side_effect=side_effect):
        bot.sync_orderly_listings()


def test_unlisted_asset_blacklisted_on_orderly_only(db):
    _sync(_resp(["PERP_NEAR_USDC"]))
    assert db.get_universe_row("orderly", "GRAM")["blacklisted"] == 1
    assert db.get_universe_row("orderly", "NEAR")["blacklisted"] == 0
    assert db.get_universe_row("binance", "GRAM")["blacklisted"] == 0


def test_operator_blacklist_never_auto_cleared(db):
    db.set_blacklist("orderly", "NEAR", True)      # operator decision
    _sync(_resp(["PERP_NEAR_USDC", "PERP_GRAM_USDC"]))
    assert db.get_universe_row("orderly", "NEAR")["blacklisted"] == 1


def test_endpoint_failure_keeps_state(db):
    _sync(side_effect=Exception("down"))
    assert db.get_universe_row("orderly", "GRAM")["blacklisted"] == 0
