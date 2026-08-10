"""
Unit tests for feature 005 — Market Conditions Check & Auto-Gate.

Mirrors tests/test_amendment003.py conventions: tmp-DB fixture via
db.db_ops.DB_PATH monkeypatch; network isolation via mock.patch.object on
trade.universe / trade.regime (the check calls the shared functions through
their module namespaces, so a patch is observed by the check's call sites —
AC1 non-divergence).

Covers AC1–AC12 (AC13 docs are verified as a checklist, not a unit test):
  - shared-function non-divergence patch test (AC1)
  - live/observed same verdict contract (AC2)
  - stale scan triggers refresh / unrefreshed fails closed (AC3, IV)
  - verdict correctness matrix (AC4)
  - disabled-by-default zero behavior change (AC5)
  - debounce transitions (AC6)
  - entries-only, never exits (AC7)
  - exactly-one transition notification (AC8)
  - observed mode zero API load (AC9)
  - compact manual report (AC10)
  - not-near-zero-trade (AC11)
  - settings registration + validation (AC12)
  - observations flow during suspension (AC6/AC7 resume-deadlock regression)
"""

import os
import sys
import time
from collections import deque, Counter

import pytest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def db(tmp_path):
    """Point db_ops at a throwaway SQLite DB and initialize the schema."""
    import db.db_ops as ops
    old = ops.DB_PATH
    ops.DB_PATH = str(tmp_path / "test.db")
    ops.initialize_database_tables()
    yield ops
    ops.DB_PATH = old


@pytest.fixture(autouse=True)
def _clear_pnl_day_cache():
    """compute_slot_size caches per UTC day — clear between tests."""
    import trade.pnl as pnl
    pnl._day_cache.clear()
    pnl._day_cache_date.clear()
    import trade.universe as u
    u._force_rescan.clear()
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

SHARE_SETTINGS = {
    "market_gate_fail_share": 0.5,
    "market_gate_trend_share": 0.6,
    "market_gate_unknown_share": 0.5,
}


def _healthy_settings(db):
    for k, v in {
        "universe_min_volume_usd": "5000000",
        "universe_depth_slot_multiple": "3",
        "universe_spread_ratio_max": "0.10",
        "universe_spread_degradation_multiple": "3",
        "tp_min_pct": "0.8",
        "cex_slot_pct": "10",
        "dex_slot_pct": "10",
        "universe_max_age_hours": "36",
        "universe_scan_interval_hours": "24",
        "market_gate_enabled": "false",
        "market_gate_interval_min": "5",
        "market_gate_bad_streak": "2",
        "market_gate_good_streak": "2",
        "market_gate_fail_share": "0.5",
        "market_gate_trend_share": "0.6",
        "market_gate_unknown_share": "0.5",
    }.items():
        db.upsert_setting(k, v)


def _seed_universe(db, venue="binance", assets=("A0", "A1", "A2", "A3"),
                   scanned_at=None, volume=100_000_000.0, spread=0.05,
                   depth=1_000_000.0, atr=0.5):
    """Healthy stored universe by default (volume 100M, spread 0.05%,
    depth 1M each side, atr_median 0.5)."""
    rows = []
    for i, asset in enumerate(assets):
        rows.append({
            "asset": asset, "symbol": f"{asset}USDT", "rank": i + 1,
            "scanned_at": scanned_at if scanned_at is not None else time.time(),
            "quote_volume_24h": volume, "spread_pct": spread,
            "depth_bid_top10": depth, "depth_ask_top10": depth,
            "atr_pct_median": atr, "signals_count": 30, "recovery_rate": 0.8,
        })
    db.replace_universe(venue, rows)


def _mk_observations(assets, spread=0.05, regime="RANGE"):
    now = time.time()
    obs = {}
    for a in assets:
        q = deque(maxlen=40)
        q.append({"ts": now, "regime": regime})
        q.append({"ts": now, "spread": spread, "obi": 1.0})
        obs[a] = q
    return obs


def _fake_book(assets=("A0", "A1", "A2", "A3")):
    return [{"symbol": f"{a}USDT", "bid": 100.0, "ask": 100.05,
             "bid_qty": 1000.0, "ask_qty": 1000.0} for a in assets]


def _fake_vol(assets=("A0", "A1", "A2", "A3")):
    return {f"{a}USDT": 100_000_000.0 for a in assets}


def _fake_info(assets=("A0", "A1", "A2", "A3")):
    return {f"{a}USDT": {"status": "TRADING", "spot_allowed": True,
                         "min_notional": 10.0} for a in assets}


def _mk_facts(passing, total, regime="RANGE"):
    return {
        f"A{i}": {
            "passes_liquidity": i < passing,
            "volume_ok": i < passing, "depth_ok": i < passing, "spread_ok": i < passing,
            "live_spread_degraded": False, "regime": regime,
        }
        for i in range(total)
    }


def _mix_from(facts):
    c = Counter(f["regime"] for f in facts.values())
    return {"RANGE": c["RANGE"], "TREND_UP": c["TREND_UP"],
            "TREND_DOWN": c["TREND_DOWN"], "UNKNOWN": c["UNKNOWN"]}


def _assert_same_contract(live, obs):
    assert set(live.keys()) == set(obs.keys())
    assert live["venue"] == obs["venue"]
    assert live["mode"] != obs["mode"]
    for k in ("scan_fresh", "verdict", "reasons", "regime_mix", "assets", "thresholds"):
        assert live[k] == obs[k], (k, live[k], obs[k])
    assert isinstance(live["timestamp"], float) and isinstance(obs["timestamp"], float)
    assert live["scan_age_hours"] == pytest.approx(obs["scan_age_hours"], abs=0.01)


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — non-divergence: the check calls the live shared functions
# ═════════════════════════════════════════════════════════════════════════════

def test_check_uses_shared_functions(db):
    """Patching a shared function must be observed by the check's call site,
    in both modes (mirrors test_amendment003's patch test)."""
    _healthy_settings(db)
    _seed_universe(db)
    db.set_venue_equity("binance", 100_000.0)
    import trade.universe as universe
    import trade.regime as regime
    import trade.pnl as pnl
    from trade import market_check

    with mock.patch.object(universe, "compute_thresholds",
                           wraps=universe.compute_thresholds) as m_thr, \
         mock.patch.object(pnl, "compute_slot_size",
                           wraps=pnl.compute_slot_size) as m_slot, \
         mock.patch.object(regime, "detect_regime", return_value="RANGE") as m_detect, \
         mock.patch.object(universe, "_fetch_depth",
                           return_value={"bid": 1_000_000.0, "ask": 1_000_000.0}) as m_depth, \
         mock.patch.object(universe, "_fetch_binance_book_ticker",
                           return_value=_fake_book()), \
         mock.patch.object(universe, "_fetch_binance_24hr",
                           return_value=_fake_vol()), \
         mock.patch.object(universe, "_fetch_binance_exchange_info",
                           return_value=_fake_info()):
        report_live = market_check.check_venue_live("binance")
        report_obs = market_check.check_venue_observed(
            "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)

    assert report_live["mode"] == "live" and report_obs["mode"] == "observed"
    assert m_thr.call_count >= 2, "compute_thresholds must be observed in both modes"
    assert m_slot.call_count >= 2, "compute_slot_size must be observed in both modes"
    assert m_detect.call_count >= 1, "detect_regime must be observed (live mode)"
    assert m_depth.call_count >= 1, "_fetch_depth must be observed (live mode)"


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — same verdict contract for equivalent inputs
# ═════════════════════════════════════════════════════════════════════════════

def test_live_and_observed_same_contract(db):
    """Identical synthetic universe/facts through both modes produce identical
    reports (same keys, values, semantics); only `mode` differs."""
    _healthy_settings(db)
    _seed_universe(db)
    db.set_venue_equity("binance", 100_000.0)
    import trade.universe as universe
    import trade.regime as regime
    from trade import market_check

    with mock.patch.object(universe, "_fetch_binance_book_ticker",
                           return_value=_fake_book()), \
         mock.patch.object(universe, "_fetch_binance_24hr",
                           return_value=_fake_vol()), \
         mock.patch.object(universe, "_fetch_binance_exchange_info",
                           return_value=_fake_info()), \
         mock.patch.object(universe, "_fetch_depth",
                           return_value={"bid": 1_000_000.0, "ask": 1_000_000.0}), \
         mock.patch.object(regime, "detect_regime", return_value="RANGE"):
        live = market_check.check_venue_live("binance")
    obs = market_check.check_venue_observed(
        "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)

    _assert_same_contract(live, obs)
    assert live["verdict"] == "PASS"


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — freshness: never judge on stale data
# ═════════════════════════════════════════════════════════════════════════════

def test_stale_scan_triggers_refresh(db):
    """A stale stored scan triggers run_scans_if_due before evaluation; a
    successful refresh means the verdict is NOT a stale-data FAIL."""
    _healthy_settings(db)
    _seed_universe(db, scanned_at=time.time() - 48 * 3600)
    import trade.universe as universe
    from trade import market_check

    def _refresh(venues=("binance", "orderly"), equity_fn=None, notify=None):
        _seed_universe(db, scanned_at=time.time())  # refresh happens
        return []

    with mock.patch.object(universe, "run_scans_if_due", side_effect=_refresh) as m:
        report = market_check.check_venue_observed(
            "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)
    m.assert_called_once()
    assert report["scan_fresh"] is True
    assert report["verdict"] == "PASS"


def test_stale_unrefreshed_fails(db):
    """A refresh that leaves the scan stale ⇒ verdict FAIL scan_stale
    (fail closed — the check never judges on stale data)."""
    _healthy_settings(db)
    _seed_universe(db, scanned_at=time.time() - 48 * 3600)
    import trade.universe as universe
    from trade import market_check

    with mock.patch.object(universe, "run_scans_if_due", return_value=[]):
        report = market_check.check_venue_observed(
            "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)
    assert report["scan_fresh"] is False
    assert report["verdict"] == "FAIL"
    assert "scan_stale" in report["reasons"]


def test_stale_while_kill_switch_paused_fails(db):
    """Under the consecutive-losses kill-switch pause run_scans_if_due does
    nothing → the scan stays stale ⇒ FAIL scan_stale, never PASS (IV)."""
    _healthy_settings(db)
    _seed_universe(db, scanned_at=time.time() - 48 * 3600)
    import trade.universe as universe
    from trade import market_check

    # run_scans_if_due's kill-switch pause means a no-op refresh
    with mock.patch.object(universe, "run_scans_if_due", return_value=[]):
        report = market_check.check_venue_observed(
            "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)
    assert report["scan_fresh"] is False
    assert report["verdict"] == "FAIL"
    assert "scan_stale" in report["reasons"]


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — verdict correctness (pure _evaluate matrix)
# ═════════════════════════════════════════════════════════════════════════════

def test_verdict_correctness(db):
    from trade.market_check import _evaluate

    def run(passing, total, regime="RANGE", scan_fresh=True):
        facts = _mk_facts(passing, total, regime)
        return _evaluate("binance", "observed", scan_fresh, 5.0,
                         facts, _mix_from(facts), SHARE_SETTINGS)

    # rule 1 — stale scan ⇒ FAIL scan_stale (before anything else)
    v, r = run(4, 4, scan_fresh=False)
    assert v == "FAIL" and r == ["scan_stale"]

    # rule 2 — fail_share >= 0.5 ⇒ FAIL liquidity_fail_share
    v, r = run(1, 4)  # 3/4 failing → 0.75
    assert v == "FAIL" and r == ["liquidity_fail_share=0.75"]

    # rule 3 — any fail below the FAIL share ⇒ WARN liquidity_partial
    v, r = run(3, 4)  # 1/4 failing → 0.25
    assert v == "WARN" and r == ["liquidity_partial=0.25"]

    # rule 4 — trend_share >= 0.6 downgrades PASS→WARN only
    v, r = run(4, 4, regime="TREND_UP")
    assert v == "WARN" and r == ["regime_trending=1.00"]

    # rule 5 — unknown_share >= 0.5 downgrades PASS→WARN only
    v, r = run(4, 4, regime="UNKNOWN")
    assert v == "WARN" and r == ["regime_unknown=1.00"]

    # rule 6 — healthy RANGE venue ⇒ PASS
    v, r = run(4, 4)
    assert v == "PASS" and r == []


# ═════════════════════════════════════════════════════════════════════════════
# AC9 — observed mode issues zero new market-data calls
# ═════════════════════════════════════════════════════════════════════════════

def test_observed_mode_no_api_load(db):
    _healthy_settings(db)
    _seed_universe(db)
    import trade.universe as universe
    from trade import market_check

    with mock.patch.object(universe, "_fetch_binance_book_ticker") as bt, \
         mock.patch.object(universe, "_fetch_binance_24hr") as v24, \
         mock.patch.object(universe, "_fetch_binance_exchange_info") as ei, \
         mock.patch.object(universe, "_fetch_depth") as fd:
        report = market_check.check_venue_observed(
            "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)

    bt.assert_not_called()
    v24.assert_not_called()
    ei.assert_not_called()
    fd.assert_not_called()
    assert report["mode"] == "observed"


# ═════════════════════════════════════════════════════════════════════════════
# AC11 — not near-zero-trade (Constitution VIII): healthy RANGE ⇒ PASS
# ═════════════════════════════════════════════════════════════════════════════

def test_not_near_zero_trade(db):
    _healthy_settings(db)
    _seed_universe(db)  # healthy stored universe
    from trade import market_check

    report = market_check.check_venue_observed(
        "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)
    assert report["verdict"] == "PASS"

    # deliberately poor universe → FAIL (never a PASS)
    _seed_universe(db, volume=1.0, spread=5.0, depth=1.0)
    report2 = market_check.check_venue_observed(
        "binance", _mk_observations(["A0", "A1", "A2", "A3"], spread=5.0), equity=100_000)
    assert report2["verdict"] == "FAIL"
    assert "liquidity_fail_share" in report2["reasons"][0]


def test_observed_live_spread_degradation_fails(db):
    """A confirmed live spread blowout must fail the asset's liquidity in
    observed mode (005 follow-up) — the gate can no longer be blind to an
    intraday deterioration hidden by the stored scan."""
    _healthy_settings(db)
    _seed_universe(db, spread=0.05)
    from trade.market_check import _asset_facts_observed, check_venue_observed
    from db.db_ops import get_tradeable_universe

    # live spread 0.5% vs stored 0.05% × 3.0 → degraded True → fails liquidity
    obs = _mk_observations(["A0", "A1", "A2", "A3"], spread=0.5)
    facts = _asset_facts_observed("binance", get_tradeable_universe("binance"),
                                  10_000, obs, 300)
    assert facts["A0"]["live_spread_degraded"] is True
    assert facts["A0"]["passes_liquidity"] is False

    # healthy live spread keeps the stored-scan verdict
    obs2 = _mk_observations(["A0", "A1", "A2", "A3"], spread=0.05)
    facts2 = _asset_facts_observed("binance", get_tradeable_universe("binance"),
                                   10_000, obs2, 300)
    assert facts2["A0"]["live_spread_degraded"] is False
    assert facts2["A0"]["passes_liquidity"] is True

    # full check: all assets degraded → fail_share=1.00 ⇒ FAIL
    report = check_venue_observed("binance", obs, equity=100_000)
    assert report["verdict"] == "FAIL"
    assert "liquidity_fail_share" in report["reasons"][0]


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — debounce state machine
# ═════════════════════════════════════════════════════════════════════════════

def test_debounce_transitions():
    from trade.market_check import update_gate_state
    settings = {"market_gate_bad_streak": 2, "market_gate_good_streak": 2}
    state = {"suspended": False, "bad_streak": 0, "good_streak": 0}

    # two FAILs → suspend only on the 2nd
    state, t = update_gate_state(state, "FAIL", settings)
    assert t is None and state["bad_streak"] == 1 and not state["suspended"]
    state, t = update_gate_state(state, "FAIL", settings)
    assert t == {"type": "suspend"} and state["suspended"] and state["bad_streak"] == 2

    # FAIL while suspended → no additional transition
    state, t = update_gate_state(state, "FAIL", settings)
    assert t is None and state["suspended"] and state["bad_streak"] == 3

    # two PASSes → resume only on the 2nd
    state, t = update_gate_state(state, "PASS", settings)
    assert t is None and state["good_streak"] == 1 and state["suspended"]
    state, t = update_gate_state(state, "PASS", settings)
    assert t == {"type": "resume"} and not state["suspended"] and state["good_streak"] == 2

    # STRONG WARN (broad liquidity failure) counts toward bad_streak and
    # suspends like FAIL (005 follow-up)
    strong = ["liquidity_partial=0.33"]
    state, t = update_gate_state(state, "WARN", settings, strong)
    assert t is None and state["bad_streak"] == 1 and state["good_streak"] == 0
    state, t = update_gate_state(state, "WARN", settings, strong)
    assert t == {"type": "suspend"} and state["suspended"] and state["bad_streak"] == 2
    # WARN while suspended → no additional transition
    state, t = update_gate_state(state, "WARN", settings, strong)
    assert t is None and state["suspended"] and state["bad_streak"] == 3
    # two PASSes → resume only on the 2nd
    state, t = update_gate_state(state, "PASS", settings)
    assert t is None and state["good_streak"] == 1 and state["suspended"]
    state, t = update_gate_state(state, "PASS", settings)
    assert t == {"type": "resume"} and not state["suspended"] and state["good_streak"] == 2

    # MILD WARN (a lone bad asset) never suspends — informational only, so a
    # single weak symbol can't block the whole venue on a small universe
    mild = ["liquidity_partial=0.11"]
    state2 = {"suspended": False, "bad_streak": 0, "good_streak": 0}
    for _ in range(5):
        state2, t = update_gate_state(state2, "WARN", settings, mild)
        assert t is None and not state2["suspended"]
        assert state2["bad_streak"] == 0 and state2["good_streak"] == 0

    # regime WARNs (trending/unknown) are always strong
    state3 = {"suspended": False, "bad_streak": 0, "good_streak": 0}
    state3, t = update_gate_state(state3, "WARN", settings, ["regime_trending=1.00"])
    assert t is None and state3["bad_streak"] == 1
    state3, t = update_gate_state(state3, "WARN", settings, ["regime_unknown=1.00"])
    assert t == {"type": "suspend"} and state3["suspended"]


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — disabled by default: zero behavior change
# ═════════════════════════════════════════════════════════════════════════════

def test_disabled_default_no_behavior_change(db):
    import bot as botmod
    import inspect
    from db.db_ops import get_setting_bool

    assert get_setting_bool("market_gate_enabled", False) is False
    assert botmod._gate_state == {}
    assert botmod._last_gate_eval == {}

    src = inspect.getsource(botmod)
    block = src[src.index("Periodic market-gate evaluation"):
                src.index("Periodic market-gate evaluation") + 700]
    # the enable check guards the whole block; disabled branch logs once only
    assert 'not get_setting_bool("market_gate_enabled", False)' in block
    assert "_gate_disabled_logged" in block
    disabled_branch = block[:block.index("else:")]
    assert "send_message" not in disabled_branch
    assert "_gate_state[" not in disabled_branch


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — entries only, never exits
# ═════════════════════════════════════════════════════════════════════════════

def test_entries_only_never_exits():
    """Structural + ordering: exit management runs before the gate guard, the
    guard sits before the scalper entry call, and gate code never invokes
    position management."""
    import bot as botmod
    import inspect
    src = inspect.getsource(botmod)
    guard = src.index("market gate suspended")

    assert src.index("spot_manage(asset, binance)") < guard
    assert src.index("futures_manage(asset, orderly, regime)") < guard
    assert src.index("detect_regime(asset, venue)") < guard
    assert src.index("_get_obi_and_spread(asset, venue)") < guard
    assert guard < src.index("spot_cycle(asset, binance")
    assert guard < src.index("futures_cycle(asset, orderly")

    gate_body = src[src.index("def _gate_apply"):src.index("def _notify_entry")]
    assert "spot_manage" not in gate_body
    assert "futures_manage" not in gate_body


# ═════════════════════════════════════════════════════════════════════════════
# AC8 — exactly one debounced transition notification
# ═════════════════════════════════════════════════════════════════════════════

def test_transition_notifications_once(db):
    import bot as botmod
    botmod._gate_state.clear()
    botmod._last_gate_eval.clear()
    fail = {"venue": "binance", "verdict": "FAIL", "reasons": ["liquidity_fail_share=1.00"]}
    warn = {"venue": "binance", "verdict": "WARN", "reasons": ["liquidity_partial=0.25"]}
    ok = {"venue": "binance", "verdict": "PASS", "reasons": []}

    with mock.patch("trading_bot.send_bot_message.send_message") as sm:
        botmod._gate_apply("binance", fail)   # FAIL #1 — no notification
        assert sm.call_count == 0
        botmod._gate_apply("binance", fail)   # FAIL #2 → suspend — exactly one
        assert sm.call_count == 1
        botmod._gate_apply("binance", fail)   # FAIL while suspended — none
        assert sm.call_count == 1
        botmod._gate_apply("binance", warn)   # WARN while suspended → WARNING notify
        assert sm.call_count == 2
        botmod._gate_apply("binance", ok)     # PASS #1 → warning cleared
        assert sm.call_count == 3
        botmod._gate_apply("binance", ok)     # PASS #2 → resume — exactly one
        assert sm.call_count == 4
        botmod._gate_apply("binance", ok)     # PASS after resume — none
        assert sm.call_count == 4

    # WARN now suspends like FAIL (005 follow-up): WARN #1 notifies WARNING,
    # WARN #2 suspends — exactly one message each
    botmod._gate_state.clear()
    botmod._last_gate_eval.clear()
    with mock.patch("trading_bot.send_bot_message.send_message") as sm:
        botmod._gate_apply("binance", warn)   # WARN #1 → WARNING notification
        assert sm.call_count == 1
        botmod._gate_apply("binance", warn)   # WARN #2 → suspend
        assert sm.call_count == 2
        assert botmod._gate_state["binance"]["suspended"] is True


def test_warn_lifecycle_notifications(db):
    """A WARN sends exactly one notification when it starts and one when it
    clears, each carrying the reason (005 follow-up)."""
    import bot as botmod
    # high bad_streak so this test exercises the WARN lifecycle, not suspension
    db.upsert_setting("market_gate_bad_streak", "10")
    botmod._gate_state.clear()
    botmod._last_gate_eval.clear()
    warn = {"venue": "binance", "verdict": "WARN", "reasons": ["liquidity_partial=0.33"]}
    ok = {"venue": "binance", "verdict": "PASS", "reasons": []}

    with mock.patch("trading_bot.send_bot_message.send_message") as sm:
        botmod._gate_apply("binance", warn)      # WARN starts — notify
        assert sm.call_count == 1
        assert "WARNING" in sm.call_args[0][0]
        assert "liquidity_partial=0.33" in sm.call_args[0][0]
        assert botmod._gate_state["binance"].get("warn_active") is True
        botmod._gate_apply("binance", warn)      # still WARN — no new message
        assert sm.call_count == 1
        botmod._gate_apply("binance", ok)        # WARN clears — notify
        assert sm.call_count == 2
        assert "cleared" in sm.call_args[0][0]
        assert botmod._gate_state["binance"].get("warn_active") is False
        botmod._gate_apply("binance", ok)        # still PASS — no new message
        assert sm.call_count == 2

    # WARN → FAIL clears the flag silently (its own message covers it)
    fail = {"venue": "binance", "verdict": "FAIL", "reasons": ["liquidity_fail_share=1.00"]}
    botmod._gate_state.clear()
    botmod._last_gate_eval.clear()
    with mock.patch("trading_bot.send_bot_message.send_message") as sm:
        botmod._gate_apply("binance", warn)      # WARN starts
        assert sm.call_count == 1
        botmod._gate_apply("binance", fail)      # FAIL — flag cleared, no WARN message
        assert sm.call_count == 1
        assert botmod._gate_state["binance"].get("warn_active") is False


def test_global_daily_loss_pct(db):
    """Global daily loss sums PnL across ALL venues and the percentage limit
    now actually blocks (was a no-op pass). Resets naturally per UTC day."""
    from trade.pnl import check_global_daily_loss, record_closed_trade

    db.upsert_setting("global_daily_loss_limit", "0")
    db.upsert_setting("global_daily_loss_limit_pct", "3")
    db.set_venue_equity("binance", 100.0)
    db.set_venue_equity("orderly", 100.0)

    # both limits off → never blocks
    db.upsert_setting("global_daily_loss_limit_pct", "0")
    assert check_global_daily_loss() == (False, "")

    # 3% of total equity (200) = 6 — small losses across venues stay under
    db.upsert_setting("global_daily_loss_limit_pct", "3")
    record_closed_trade("A0", "binance", "long", 10, 9.5, 10, 1, 0, 0, 0, time.time(), "sl")
    record_closed_trade("A1", "orderly", "long", 10, 9.7, 10, 1, 0, 0, 0, time.time(), "sl")
    assert check_global_daily_loss() == (False, "")

    # enough summed loss (across venues) → blocked
    record_closed_trade("A0", "binance", "long", 10, 4.0, 10, 1, 0, 0, 0, time.time(), "sl")
    blocked, reason = check_global_daily_loss()
    assert blocked
    assert "global_daily_loss_limit" in reason

    # absolute limit still works (pct disabled)
    db.upsert_setting("global_daily_loss_limit_pct", "0")
    db.upsert_setting("global_daily_loss_limit", "50")
    blocked, reason = check_global_daily_loss()
    assert not blocked
    db.upsert_setting("global_daily_loss_limit", "0.1")
    blocked, reason = check_global_daily_loss()
    assert blocked and "global_daily_loss_limit" in reason


# ═════════════════════════════════════════════════════════════════════════════
# AC10 — compact manual report (≤ 4096, verdict tokens + counts verbatim)
# ═════════════════════════════════════════════════════════════════════════════

def test_manual_report_compact(db):
    _healthy_settings(db)
    _seed_universe(db)
    from trade.market_check import check_venue_observed, format_report

    report = check_venue_observed(
        "binance", _mk_observations(["A0", "A1", "A2", "A3"]), equity=100_000)
    text = format_report(report)
    assert len(text) <= 4096
    assert "PASS" in text
    assert "4/4" in text

    # a large synthetic report still fits and keeps counts
    assets = {f"SYM{i}": {
        "passes_liquidity": True, "volume_ok": True, "depth_ok": True,
        "spread_ok": True, "live_spread_degraded": False, "regime": "RANGE",
    } for i in range(200)}
    big = {
        "venue": "binance", "mode": "live", "timestamp": 0.0,
        "scan_fresh": True, "scan_age_hours": 5.2,
        "verdict": "PASS", "reasons": [],
        "regime_mix": {"RANGE": 200, "TREND_UP": 0, "TREND_DOWN": 0, "UNKNOWN": 0},
        "assets": assets,
        "thresholds": {"atr_pct_median": 0.42, "dip_needed_pct": 0.36,
                       "tp_effective_pct": 0.8, "sl_effective_pct": 0.5},
    }
    big_text = format_report(big)
    assert len(big_text) <= 4096
    assert "200/200" in big_text


# ═════════════════════════════════════════════════════════════════════════════
# AC12 — settings registration + Amendment 002 validator
# ═════════════════════════════════════════════════════════════════════════════

def test_settings_validation(db):
    from trade.settings_schema import BY_KEY
    from trade.settings_rules import validate, validate_all

    keys = sorted(k for k in BY_KEY if k.startswith("market_gate_"))
    assert keys == sorted([
        "market_gate_enabled", "market_gate_interval_min", "market_gate_bad_streak",
        "market_gate_good_streak", "market_gate_fail_share", "market_gate_trend_share",
        "market_gate_unknown_share", "market_gate_warn_liquidity_share",
    ])
    for k in keys:
        assert BY_KEY[k].group == "gate"

    # valid values
    assert validate("market_gate_enabled", True).level == "ok"
    assert validate("market_gate_interval_min", 5).level == "ok"
    assert validate("market_gate_bad_streak", 2).level == "ok"
    assert validate("market_gate_good_streak", 2).level == "ok"
    assert validate("market_gate_fail_share", 0.5).level == "ok"
    assert validate("market_gate_trend_share", 0.6).level == "ok"
    assert validate("market_gate_unknown_share", 0.5).level == "ok"
    assert validate("market_gate_warn_liquidity_share", 0.25).level == "ok"

    # hard-range violations rejected with clear messages
    assert validate("market_gate_interval_min", 0).level == "error"
    assert validate("market_gate_interval_min", 1441).level == "error"
    assert validate("market_gate_bad_streak", 0).level == "error"
    assert validate("market_gate_bad_streak", 101).level == "error"
    assert validate("market_gate_good_streak", 0).level == "error"
    assert validate("market_gate_good_streak", 101).level == "error"
    assert validate("market_gate_fail_share", -0.1).level == "error"
    assert validate("market_gate_fail_share", 1.5).level == "error"
    assert validate("market_gate_trend_share", 1.1).level == "error"
    assert validate("market_gate_unknown_share", 1.5).level == "error"
    assert validate("market_gate_warn_liquidity_share", -0.1).level == "error"
    assert validate("market_gate_warn_liquidity_share", 1.5).level == "error"

    # Amendment 002 deterministic validator accepts the 7 settings unchanged
    for k, v in {
        "market_gate_enabled": "false", "market_gate_interval_min": "5",
        "market_gate_bad_streak": "2", "market_gate_good_streak": "2",
        "market_gate_fail_share": "0.5", "market_gate_trend_share": "0.6",
        "market_gate_unknown_share": "0.5",
    }.items():
        db.upsert_setting(k, v)
    results = validate_all()
    errors = {k: v for k, v in results.items() if v.level == "error"}
    assert not errors


# ═════════════════════════════════════════════════════════════════════════════
# AC6/AC7 regression — observations keep flowing during a suspension
# ═════════════════════════════════════════════════════════════════════════════

def test_observations_flow_during_suspension(db):
    """The entry guard sits AFTER observation recording, so while a venue is
    suspended the per-cycle observations still accumulate and the observed
    evaluator still consumes them — resume is never deadlocked."""
    import bot as botmod
    import inspect
    src = inspect.getsource(botmod)
    guard = src.index("market gate suspended")
    assert src.index("_get_obi_and_spread(asset, venue)") < guard
    assert src.index("detect_regime(asset, venue)") < guard

    # deques are bounded (maxlen=40) and always keep the newest observations
    q = deque(maxlen=40)
    now = time.time()
    for i in range(50):
        q.append({"ts": now - 1, "regime": "RANGE"})
        q.append({"ts": now - 1, "spread": 0.04, "obi": 1.0})
    assert len(q) == 40

    # the observed evaluator reads the latest spread/regime from the deque
    _healthy_settings(db)
    _seed_universe(db)
    from trade.market_check import _asset_facts_observed
    from db.db_ops import get_tradeable_universe
    facts = _asset_facts_observed("binance", get_tradeable_universe("binance"),
                                  10_000, {"A0": q}, 300)
    assert facts["A0"]["regime"] == "RANGE"
    # live spread 0.04 vs stored 0.05 × 3.0 → not degraded
    assert facts["A0"]["live_spread_degraded"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Constitution VIII — a gate-suspended entry skip is recorded in `signals`
# ═════════════════════════════════════════════════════════════════════════════

def test_gate_suspended_records_signal(db):
    """A market-gate-suspended entry skip writes a `signals` row with the gate
    reason (Constitution VIII — every skipped entry recorded with its reason so
    filter strictness is measurable). Reuses the scalpers' `_log` INSERT."""
    import bot as botmod
    from db.db_ops import get_db_connection

    botmod._record_gate_skip("binance", "PUMP", "RANGE", 1.0, 0.0023)
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT asset, venue, regime, direction, price, action, reason FROM signals"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["asset"] == "PUMP"
    assert row["venue"] == "binance"
    assert row["regime"] == "RANGE"
    assert row["direction"] is None
    assert row["price"] == pytest.approx(0.0023)
    assert row["action"] == "skipped"
    assert row["reason"] == "market_gate_suspended"

    # orderly venue records through the futures scalper's identical mechanism
    botmod._record_gate_skip("orderly", "PUMP", "RANGE", 1.0, 0.0023)
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT venue, action, reason FROM signals WHERE reason = 'market_gate_suspended'"
        ).fetchall()
    assert len(rows) == 2
    assert {r["venue"] for r in rows} == {"binance", "orderly"}

    # structural: the per-asset guard invokes the recorder before `continue`
    import inspect
    src = inspect.getsource(botmod)
    guard = src[src.index("market gate suspended"):]
    assert "_record_gate_skip(venue, asset, regime, obi, price)" in guard


# ═════════════════════════════════════════════════════════════════════════════
# Constitution VIII — a global-daily-loss skip is recorded with a REAL price
# (regression: passing price=None raised NOT NULL constraint on signals.price)
# ═════════════════════════════════════════════════════════════════════════════

def test_universe_rotate_inactive(db):
    """Universe members with no recent actionable signal (entered/signaled)
    are flagged for rotation so the scan can surface fresh tokens; assets with
    a recent signal stay. 0 disables rotation."""
    _healthy_settings(db)
    _seed_universe(db)  # A0..A3 in the stored universe
    from db.db_ops import get_db_connection
    now = time.time()
    with get_db_connection() as conn:
        conn.execute("INSERT INTO signals (ts,asset,venue,regime,price,action,reason) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (now - 3600, "A0", "binance", "RANGE", 1.0, "entered", "recent"))
        conn.execute("INSERT INTO signals (ts,asset,venue,regime,price,action,reason) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (now - 50 * 3600, "A1", "binance", "RANGE", 1.0, "entered", "old"))
        conn.commit()

    from trade.universe import _inactive_universe_assets, _gate_suspending
    inactive = _inactive_universe_assets("binance", 12)
    assert "A0" not in inactive          # active signal 1h ago → stays
    assert inactive == {"A1", "A2", "A3"}  # quiet 50h / never → rotated
    # disabled → nothing rotated
    assert _inactive_universe_assets("binance", 0) == set()

    # gate suspended recently → rotation guard trips (never wipe during a gate)
    with get_db_connection() as conn:
        conn.execute("INSERT INTO signals (ts,asset,venue,regime,price,action,reason) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (time.time(), "A0", "binance", "RANGE", 1.0, "skipped",
                      "market_gate_suspended"))
        conn.commit()
    assert _gate_suspending("binance", 12) is True


def test_universe_rotate_paused_venue(db):
    """A venue with no recent signal rows at all (auto-trade off / paused)
    is never rotated — inactivity can't be judged on a stopped venue."""
    _healthy_settings(db)
    _seed_universe(db)
    from trade.universe import _inactive_universe_assets
    assert _inactive_universe_assets("binance", 12) == set()


def test_broad_market_downtrend_gate(db):
    """Broad-market gate: blocks entries while the majors' average 24h change
    is a downtrend, allows in a green/neutral market, and fails closed on API
    errors. Disabled = no blocking (2026-08-10)."""
    import bot as botmod
    botmod._market_filter_cache["ts"] = 0.0
    db.upsert_setting("market_filter_enabled", "false")
    assert botmod._broad_market_downtrend() == (False, "")

    db.upsert_setting("market_filter_enabled", "true")
    db.upsert_setting("market_filter_max_downtrend_pct", "-1.0")
    db.upsert_setting("market_filter_assets", "BTC,ETH,SOL")

    class _R:
        def __init__(self, pct):
            self.pct = pct
        def json(self):
            return {"priceChangePercent": self.pct}

    # red market: avg −1.7 < −1.0 → blocked
    botmod._market_filter_cache["ts"] = 0.0
    vals = iter([_R(-2.0), _R(-1.5), _R(-1.6)])
    with mock.patch("requests.get", side_effect=lambda *a, **k: next(vals)):
        blocked, reason = botmod._broad_market_downtrend()
    assert blocked and "broad market" in reason

    # green/neutral market: avg +0.5 ≥ −1.0 → allowed
    botmod._market_filter_cache["ts"] = 0.0
    vals = iter([_R(0.5), _R(0.6), _R(0.4)])
    with mock.patch("requests.get", side_effect=lambda *a, **k: next(vals)):
        blocked, reason = botmod._broad_market_downtrend()
    assert not blocked and reason == ""

    # API failure → fail closed (don't trade blind)
    botmod._market_filter_cache["ts"] = 0.0
    with mock.patch("requests.get", side_effect=Exception("boom")):
        blocked, reason = botmod._broad_market_downtrend()
    assert blocked and "unavailable" in reason


def test_global_block_records_signal(db):
    """A global-daily-loss entry skip writes a valid `signals` row — the guard
    runs before the per-asset price snapshot, so the recorder must fetch the
    price itself (signals.price is NOT NULL; None previously caused
    sqlite3.IntegrityError spam on every blocked cycle)."""
    import bot as botmod
    from db.db_ops import get_db_connection

    with mock.patch.object(botmod, "_get_live_price_binance",
                           return_value=0.6253) as m_price:
        botmod._record_global_block(
            "binance", "EPIC", "RANGE",
            "global_daily_loss_limit breached: -1.69 <= -1.61")
    m_price.assert_called_once_with("EPIC")

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT asset, venue, regime, direction, price, action, reason FROM signals"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["asset"] == "EPIC"
    assert row["venue"] == "binance"
    assert row["regime"] == "RANGE"
    assert row["direction"] is None
    assert row["price"] == pytest.approx(0.6253)
    assert row["action"] == "skipped"
    assert row["reason"].startswith("global_daily_loss_limit breached")
    assert "global_daily_loss_limit breached:" in row["reason"]
