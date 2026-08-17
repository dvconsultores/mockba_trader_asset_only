"""Tests for the Orderly kline circuit breaker in bot._fetch."""
import os, sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bot

CANDLES = [{"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]


def _reset():
    bot._orderly_fail_streak = 0
    bot._orderly_skip_until = 0.0


def _clients(orderly_rows):
    binance = mock.Mock()
    binance.get_klines.return_value = CANDLES
    orderly = mock.Mock()
    orderly.get_klines.return_value = orderly_rows
    return binance, orderly


def test_breaker_trips_after_consecutive_failures():
    _reset()
    binance, orderly = _clients(None)
    for _ in range(bot.ORDERLY_BREAK_AFTER):
        out = bot._fetch(binance, orderly, "orderly", "NEAR", "1h", 120)
        assert out == CANDLES                      # Binance fallback each time
    assert orderly.get_klines.call_count == bot.ORDERLY_BREAK_AFTER
    assert bot._orderly_skip_until > 0

    bot._fetch(binance, orderly, "orderly", "SOL", "1h", 120)
    assert orderly.get_klines.call_count == bot.ORDERLY_BREAK_AFTER  # skipped


def test_breaker_reopens_after_cooldown():
    _reset()
    binance, orderly = _clients(CANDLES)
    bot._orderly_skip_until = 1.0                  # already expired vs real clock
    out = bot._fetch(binance, orderly, "orderly", "NEAR", "1h", 120)
    assert out == CANDLES
    assert orderly.get_klines.call_count == 1      # probed again


def test_success_resets_streak():
    _reset()
    binance, orderly = _clients(None)
    for _ in range(bot.ORDERLY_BREAK_AFTER - 1):
        bot._fetch(binance, orderly, "orderly", "NEAR", "1h", 120)
    orderly.get_klines.return_value = CANDLES
    bot._fetch(binance, orderly, "orderly", "NEAR", "1h", 120)
    assert bot._orderly_fail_streak == 0
    assert bot._orderly_skip_until == 0.0          # never tripped


def test_binance_venue_never_touches_orderly():
    _reset()
    binance, orderly = _clients(CANDLES)
    bot._fetch(binance, orderly, "binance", "NEAR", "1h", 120)
    assert orderly.get_klines.call_count == 0
