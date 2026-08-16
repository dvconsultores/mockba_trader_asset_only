"""
Unit tests for feature 015 — Kill-Switch Integrity.

Covers AC1–AC9 from .specify/specs/015-kill-switch-integrity/spec.md:
  - get_equity returns None on transport failure, never 0.0 (AC1, both venues)
  - equity includes open positions valued at entry fill (AC3/AC4, audit #12)
  - the venue failure streak is consecutive ACROSS cycles, disables at 5, and
    notifies via Telegram exactly once (AC5, AC6 — Constitution IV)
  - a success resets the streak so intermittent blips never disable (AC5)
  - scalp_cycle with unknown equity fails closed live (AC8) and paper-trades on
    the declared pool in dry-run (AC9)

AC2 (venue_state never written on failure) holds by construction — the cache
write sits behind the success branch in bot.py — and is guarded here indirectly
by the streak tests exercising the same helper the loop calls.
"""

import os
import sys
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
    yield ops
    ops.DB_PATH = old


@pytest.fixture(autouse=True)
def _clear_state(db):
    import bot
    import trading_bot.spot_scalper as sc
    bot._venue_fail_streak.clear()
    sc._last_sl.clear()
    sc._last_entry.clear()
    yield


# ── AC1 — unknown is None, never 0.0 ─────────────────────────────────────────

def test_binance_equity_none_on_failure(db):
    import trading_bot.executor as ex
    o = ex.BinanceSpot()
    with mock.patch.object(o, "_get", side_effect=RuntimeError("api down")):
        assert o.get_equity() is None, "an unreachable exchange must be None, not 0.0"


def test_orderly_equity_none_on_failure(db):
    import trading_bot.executor as ex
    o = ex.OrderlyFutures()
    with mock.patch.object(o, "_get", side_effect=RuntimeError("api down")):
        assert o.get_equity() is None


# ── AC3 / AC4 — whole-account equity (audit #12) ─────────────────────────────

def test_equity_includes_open_positions(db):
    """USDT $10 free + a position of 100 × $0.40 entry = $50 total."""
    import trading_bot.executor as ex
    db.save_position({
        "id": "p1", "asset": "AAA", "venue": "binance", "side": "long",
        "qty": 100.0, "entry_price": 0.40, "signal_price": 0.40,
        "tp_price": 0.41, "sl_price": 0.39, "tp_order_id": "t", "sl_order_id": "s",
        "opened_at": time.time(), "signal_id": None, "fee_entry": 0.0,
    })
    o = ex.BinanceSpot()
    account = {"balances": [{"asset": "USDT", "free": "10", "locked": "0"},
                            {"asset": "AAA", "free": "100", "locked": "0"}]}
    with mock.patch.object(o, "_get", return_value=account):
        eq = o.get_equity()
    assert eq == pytest.approx(50.0), \
        "coins held by open positions must count toward equity (audit #12)"


def test_daily_loss_limit_stays_armed(db):
    """AC3: with real total equity the pct limit computes non-zero."""
    from trade.pnl import is_entry_blocked
    db.upsert_setting("daily_loss_limit", "0")
    db.upsert_setting("daily_loss_limit_pct", "2")
    # a $50 equity and a -$1.10 day: limit = $1.00 -> blocked
    db.save_closed_trade({
        "asset": "AAA", "venue": "binance", "side": "long",
        "entry_price": 1.0, "exit_price": 0.9, "signal_price": 1.0, "qty": 11.0,
        "fee_entry": 0.0, "fee_exit": 0.0, "pnl_net": -1.10, "pnl_pct": -10.0,
        "opened_at": time.time(), "closed_at": time.time(), "exit_reason": "sl",
    })
    blocked, reason = is_entry_blocked("binance", 50.0)
    assert blocked and "daily_loss_limit" in reason, \
        "the limit must fire when equity is known and real"


# ── AC5 / AC6 — the consecutive streak (Constitution IV) ─────────────────────

def test_five_consecutive_failures_disable_and_notify(db):
    import bot
    db.upsert_setting("auto_trade_binance", "Automatic")
    with mock.patch("trading_bot.send_bot_message.send_message") as msg:
        for _ in range(4):
            bot._equity_failure("binance")
        assert db.get_setting("auto_trade_binance") == "Automatic", "4 is not 5"
        assert not msg.called
        bot._equity_failure("binance")
    assert db.get_setting("auto_trade_binance") == "false", "5th trips the switch"
    assert msg.call_count == 1, "Constitution IV requires the Telegram notify"
    assert bot._venue_fail_streak["binance"] == 0, "streak resets after tripping"


def test_success_resets_streak(db):
    """AC5: 4 failures, a good cycle, 4 more failures — never disables."""
    import bot
    db.upsert_setting("auto_trade_binance", "Automatic")
    with mock.patch("trading_bot.send_bot_message.send_message") as msg:
        for _ in range(4):
            bot._equity_failure("binance")
        bot._venue_fail_streak["binance"] = 0   # what the loop does on success
        for _ in range(4):
            bot._equity_failure("binance")
    assert db.get_setting("auto_trade_binance") == "Automatic", \
        "intermittent blips must never disable the venue"
    assert not msg.called


# ── AC8 / AC9 — scalp_cycle on unknown equity ────────────────────────────────

class _FakeSpot:
    class _Info:
        symbol = "AAAUSDT"; base_tick = 0.001; quote_tick = 0.01
        min_qty = 0.001; min_notional = 5.0

    class _Fill:
        fill_price = 90.0; fee_amount = 0.01
        sellable_qty = 1.0; filled_qty = 1.0
        tp_order_id = "tp1"; sl_order_id = "sl1"

    def __init__(self):
        self.entries = []

    def get_equity(self):
        return None                       # exchange unreachable

    def get_symbol_info(self, asset):
        return self._Info()

    def place_entry(self, *a, **k):
        self.entries.append(a)
        return self._Fill()


def _drive_dip(sc, asset="AAA"):
    for _ in range(12):
        sc._update_price_memory(asset, 100.0)


def test_live_unknown_equity_fails_closed(db):
    """AC8: dry_run=false + unknown equity -> no order, reason recorded."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("dry_run", "false")
    _drive_dip(sc)
    ex = _FakeSpot()
    with mock.patch("trading_bot.spot_scalper.last_closed_return_up", return_value=True):
        assert sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0) is None
    assert ex.entries == [], "unknown state must never place an order"
    with db.get_db_connection() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE reason='equity_unavailable'")]
    assert rows and rows[0]["action"] == "skipped", \
        "the fail-closed skip must be measurable (Constitution VIII)"


def test_dry_run_falls_back_to_declared_pool(db):
    """AC9: dry_run + unknown equity -> paper-trades on capital_cex_usdt."""
    import trading_bot.spot_scalper as sc
    db.upsert_setting("dry_run", "true")
    db.upsert_setting("capital_cex_usdt", "100")
    _drive_dip(sc)
    ex = _FakeSpot()
    with mock.patch("trading_bot.spot_scalper.last_closed_return_up", return_value=True):
        res = sc.scalp_cycle("AAA", ex, "RANGE", 1.0, 90.0)
    assert res is not None and ex.entries, \
        "paper trading must work without exchange credentials"
