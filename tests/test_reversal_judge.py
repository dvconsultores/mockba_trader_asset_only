"""Unit tests for the DeepSeek judge (spec 001) — transport mocked."""
import json
import os, sys
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


CANDLES = [{"ts": i, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
           for i in range(100)]

GOOD = {"valid": True, "direction": "long", "confidence": 78, "entry": 100.0,
        "stop": 96.0, "target": 112.0, "rr": 3.0, "reasons": ["all criteria met"]}


def _resp(content, reasoning="thought about it"):
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json.return_value = {"choices": [{"message": {
        "content": content, "reasoning_content": reasoning}}]}
    return m


def _judge(db, response, api_key="k"):
    from trading_bot import reversal_judge as rj
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": api_key}), \
         mock.patch.object(rj.requests, "post", return_value=response) as post:
        out = rj.judge("NEAR", "{}", "down", CANDLES, CANDLES, rr_min=2.5)
    return out, post


def test_valid_verdict_parsed(db):
    (verdict, reasoning, model), post = _judge(db, _resp(json.dumps(GOOD)))
    assert verdict is not None and verdict["valid"] is True
    assert verdict["rr"] == pytest.approx(3.0)
    assert reasoning == "thought about it"
    assert post.call_count == 1


def test_rr_recomputed_and_gated(db):
    bad_rr = dict(GOOD, target=104.0, rr=9.9)   # real rr = 1.0 < 2.5
    (verdict, _, _), _ = _judge(db, _resp(json.dumps(bad_rr)))
    assert verdict is not None
    assert verdict["valid"] is False             # gated by computed rr
    assert verdict["rr"] == pytest.approx(1.0)


def test_incoherent_prices_fail_closed(db):
    incoherent = dict(GOOD, stop=105.0)          # stop above entry on a long
    (verdict, _, _), post = _judge(db, _resp(json.dumps(incoherent)))
    assert verdict is None
    assert post.call_count == 2                  # retried once, then closed


def test_markdown_fenced_json_accepted(db):
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    (verdict, _, _), _ = _judge(db, _resp(fenced))
    assert verdict is not None and verdict["valid"] is True


def test_malformed_retries_then_fails_closed(db):
    (verdict, _, _), post = _judge(db, _resp("not json at all"))
    assert verdict is None
    assert post.call_count == 2


def test_api_error_fails_closed(db):
    from trading_bot import reversal_judge as rj
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}), \
         mock.patch.object(rj.requests, "post", side_effect=Exception("down")):
        verdict, _, _ = rj.judge("NEAR", "{}", "down", CANDLES, CANDLES)
    assert verdict is None


def test_missing_key_fails_closed(db):
    from trading_bot import reversal_judge as rj
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
         mock.patch.object(rj.requests, "post") as post:
        verdict, _, _ = rj.judge("NEAR", "{}", "down", CANDLES, CANDLES)
    assert verdict is None and post.call_count == 0


def test_prompt_carries_engine_claim(db):
    from trading_bot.reversal_judge import build_prompt
    p = build_prompt("NEAR", '{"ms_state": "CONFIRMED"}', "down", CANDLES, CANDLES, 2.5)
    assert "CONFIRMED" in p and "1d trend context: down" in p and "2.5" in p
