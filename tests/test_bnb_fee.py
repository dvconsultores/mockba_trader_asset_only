"""
Unit tests for feature 017 — BNB Fee Discount.

With "Use BNB for fees" active, Binance reports commissionAsset "BNB". The old
parsers subtracted the commission from the sellable base quantity and valued it
at the traded asset's price — both wrong for BNB (Constitution V). These tests
pin the corrected accounting plus the coherence detectors.

Fixture pins the live production settings (016 lesson: threshold-interacting
code must be tested against the configuration that actually runs).
"""
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
    # live production configuration
    ops.upsert_setting("dry_run", "false")
    ops.upsert_setting("cex_fee_bnb", "true")
    ops.upsert_setting("cex_round_trip_fee_pct", "0.15")
    ops.upsert_setting("tp_min_pct", "0.8")
    ops.upsert_setting("sl_min_pct_spot", "1.5")
    ops.upsert_setting("tp_k", "1.2")
    ops.upsert_setting("sl_k_spot", "2.0")
    yield ops
    ops.DB_PATH = old


@pytest.fixture()
def ex(db):
    from trading_bot.executor import BinanceSpot
    return BinanceSpot()


# ── _fee_to_usdt helper ──────────────────────────────────────────────────────

def test_bnb_fee_valued_at_bnb_price(ex):
    with mock.patch.object(ex, "get_price", return_value=600.0):
        assert ex._fee_to_usdt(0.001, "BNB", 0.5, 100.0) == pytest.approx(0.6)


def test_bnb_ticker_failure_falls_back_to_estimate(ex):
    """AC3: ticker down -> per-leg estimate (notional x 0.15/200), never 0."""
    with mock.patch.object(ex, "get_price", return_value=None):
        fee = ex._fee_to_usdt(0.001, "BNB", 0.5, 100.0)
    assert fee == pytest.approx(100.0 * 0.15 / 200.0)  # $0.075
    assert fee > 0


def test_quote_asset_passthrough(ex):
    assert ex._fee_to_usdt(0.05, "USDT", 0.5, 100.0) == pytest.approx(0.05)


def test_base_asset_valued_at_fill_price(ex):
    """Regression (AC2): base-asset commission still valued at the fill price."""
    assert ex._fee_to_usdt(2.0, "ONG", 0.5, 100.0) == pytest.approx(1.0)


def test_mismatch_warning_when_bnb_expected(ex, caplog):
    """AC4: cex_fee_bnb=true but fee arrives in another asset -> warning."""
    import logging
    with caplog.at_level(logging.WARNING):
        ex._fee_to_usdt(0.05, "USDT", 0.5, 100.0)
    assert any("BNB reserve may be exhausted" in r.message for r in caplog.records)


def test_no_warning_when_bnb_paid(ex, caplog):
    import logging
    with caplog.at_level(logging.WARNING), \
         mock.patch.object(ex, "get_price", return_value=600.0):
        ex._fee_to_usdt(0.001, "BNB", 0.5, 100.0)
    assert not any("exhausted" in r.message for r in caplog.records)


# ── place_entry integration (mocked HTTP) ────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)
    def json(self):
        return self._payload


def _entry_with_commission(ex, commission_asset, commission):
    from trading_bot.types import SymbolFilters
    info = SymbolFilters(symbol="ONGUSDT", base_tick=0.1, quote_tick=0.0001,
                         min_qty=0.1, min_notional=5.0)
    order = {"orderId": 1, "executedQty": "100", "cummulativeQuoteQty": "10",
             "fills": [{"price": "0.1", "qty": "100",
                        "commission": str(commission),
                        "commissionAsset": commission_asset}]}
    oco = {"orderReports": [
        {"type": "LIMIT_MAKER", "orderId": 2},
        {"type": "STOP_LOSS", "orderId": 3},
    ]}
    responses = iter([_Resp(order), _Resp(oco)])
    with mock.patch.object(ex, "get_symbol_info", return_value=info), \
         mock.patch.object(ex, "get_price", return_value=600.0), \
         mock.patch.object(ex, "_post", side_effect=lambda *a, **k: next(responses)):
        return ex.place_entry("ONG", "long", 100.0, 0.1, 1.0, "p1", sl_pct=1.5)


def test_entry_bnb_commission_full_sellable(ex):
    """AC1: BNB commission -> sellable == filled, fee in USDT at BNB price."""
    fill = _entry_with_commission(ex, "BNB", 0.00001)
    assert fill is not None
    assert fill.sellable_qty == pytest.approx(100.0)
    assert fill.fee_amount == pytest.approx(0.00001 * 600.0)
    assert fill.fee_asset == "USDT"


def test_entry_base_commission_reduces_sellable(ex):
    """AC2 regression: base-asset commission -> sellable reduced, fee at fill price."""
    fill = _entry_with_commission(ex, "ONG", 0.1)
    assert fill is not None
    assert fill.sellable_qty == pytest.approx(99.9)
    assert fill.fee_amount == pytest.approx(0.1 * 0.1)


# ── Validator coherence (AC6) ────────────────────────────────────────────────

def test_validator_bnb_on_full_rate_warns(db):
    from trade.settings_rules import validate
    v = validate("cex_round_trip_fee_pct", 0.20)
    assert v.level == "warn" and v.suggested_value == 0.15


def test_validator_bnb_off_discount_rate_warns(db):
    from trade.settings_rules import validate
    db.upsert_setting("cex_fee_bnb", "false")
    v = validate("cex_round_trip_fee_pct", 0.15)
    assert v.level == "warn" and v.suggested_value == 0.20


def test_validator_coherent_pair_ok(db):
    from trade.settings_rules import validate
    assert validate("cex_round_trip_fee_pct", 0.15).level == "ok"
    assert validate("cex_fee_bnb", True).level == "ok"


# ── Payoff-ratio validator hygiene (Constitution II v1.1.0) ──────────────────

def test_ratified_wide_stop_config_is_silent(db):
    """sl floor 1.5 vs tp floor 0.8 (ratio 1.9) — valid under v1.1.0, no warn."""
    from trade.settings_rules import validate
    assert validate("sl_min_pct_spot", 1.5).level == "ok"
    assert validate("sl_k_spot", 2.0).level == "ok"


def test_extreme_payoff_ratio_still_warns(db):
    from trade.settings_rules import validate
    v = validate("sl_min_pct_spot", 2.5)  # 3.1x the 0.8 tp floor
    assert v.level == "warn" and "breakeven win rate" in v.message
