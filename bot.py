"""
MockbaV4 — reversal trading bot (spec 001, founding rewrite 2026-08-16).

The 3MS method: deterministic structure engine (trade/structure.py) finds
reversal candidates on 4h with 1d trend context; the DeepSeek judge
(trading_bot/reversal_judge.py) verifies every criterion and prices the
trade; every evaluation is recorded in `signals`. Phase 1 runs in observe
mode — signals and Telegram only, no orders. Execution is Phase 2, behind
`trade_mode = live` and its own authorization.

Cycle: every `cycle_seconds` (default 1800 = 2/hour) over the operator's
asset table (`asset_universe`, NEAR first). Candles are fetched per venue —
Binance public REST for CEX, Orderly's native kline endpoint for DEX with
Binance data as the fallback (operator directive).
"""

from __future__ import annotations
import json
import time

from db.db_ops import (
    initialize_database_tables, get_setting, upsert_setting,
    get_setting_bool, get_setting_float, get_setting_int,
    get_tradeable_universe, replace_universe, set_venue_equity, record_signal,
)
from logs.log_config import apolo_trader_logger as logger
from trade.structure import analyze, near_zone
from trading_bot.executor import BinanceSpot, OrderlyFutures
from trading_bot.reversal_judge import judge

VENUES = ("binance", "orderly")
ASSETS_SEED = ["NEAR", "SOL", "ARB", "GRAM", "INJ"]   # operator list, NEAR first

DEFAULT_SETTINGS = {
    "trade_mode": "observe",            # observe | live (Phase 2)
    "cycle_seconds": "1800",
    "rr_min": "2.5",
    "risk_pct": "1.0",                  # % equity risked per trade (Phase 2 sizing)
    "max_concurrent_positions": "2",    # spec 001 Q9
    "max_trades_per_month": "10",
    "pivot_k": "2",
    "zone_tolerance_pct": "0.75",
    "retest_tolerance_pct": "1.0",
    "judge_model": "deepseek-v4-pro",
    "judge_effort": "high",
    "auto_trade_binance": "false",
    "auto_trade_orderly": "false",
    "dry_run": "true",
    "cex_fee_bnb": "true",
    "cex_round_trip_fee_pct": "0.15",
    "dex_round_trip_fee_pct": "0.06",
    "daily_loss_limit_pct": "2.0",
    "max_consecutive_losses": "6",
    "telegram_signals": "true",
}

# in-memory cycle state: last reported ms_state and last judged 4h candle
_last_state: dict[tuple[str, str], str] = {}
_last_judged: dict[tuple[str, str], float] = {}


def ensure_seed():
    """Seed default settings and the operator asset list (idempotent)."""
    for k, v in DEFAULT_SETTINGS.items():
        if get_setting(k) is None:
            upsert_setting(k, v)
    for venue in VENUES:
        if not get_tradeable_universe(venue):
            rows = [{
                "asset": a,
                "symbol": f"{a}USDT" if venue == "binance" else f"PERP_{a}_USDC",
                "rank": i + 1, "scanned_at": time.time(),
            } for i, a in enumerate(ASSETS_SEED)]
            replace_universe(venue, rows)
            logger.info(f"[SEED] {venue}: asset list seeded {ASSETS_SEED}")


def _notify(text: str):
    if not get_setting_bool("telegram_signals", True):
        return
    try:
        from trading_bot.send_bot_message import send_message
        send_message(text)
    except Exception as e:
        logger.warning(f"[TELEGRAM] send failed: {e}")


def _fetch(binance: BinanceSpot, orderly: OrderlyFutures, venue: str,
           asset: str, interval: str, limit: int) -> list[dict] | None:
    if venue == "orderly":
        rows = orderly.get_klines(asset, interval, limit)
        if rows:
            return rows
        # fallback per spec 001 Q4 — perps track spot within basis points
    return binance.get_klines(asset, interval, limit)


def evaluate_asset(binance: BinanceSpot, orderly: OrderlyFutures,
                   venue: str, asset: str):
    """One asset, one venue: engine -> (maybe) judge -> record."""
    pivot_k = get_setting_int("pivot_k", 2)
    zone_tol = get_setting_float("zone_tolerance_pct", 0.75)
    retest_tol = get_setting_float("retest_tolerance_pct", 1.0)
    rr_min = get_setting_float("rr_min", 2.5)

    c1d = _fetch(binance, orderly, venue, asset, "1d", 200)
    c4h = _fetch(binance, orderly, venue, asset, "4h", 200)
    c1h = _fetch(binance, orderly, venue, asset, "1h", 120)
    if not c1d or not c4h or not c1h:
        logger.warning(f"[CYCLE] {venue}:{asset} candles unavailable")
        return

    trend_1d = analyze(c1d, pivot_k, zone_tol, retest_tol).trend
    s = analyze(c4h, pivot_k, zone_tol, retest_tol)
    key = (venue, asset)
    price = s.last_close or c4h[-1]["close"]

    base = dict(asset=asset, venue=venue, price=price, timeframe="4h",
                tf_1d_trend=trend_1d, ms_state=s.ms_state, neckline=s.neckline)

    # record state transitions (observability without drowning the table)
    if s.ms_state != _last_state.get(key):
        _last_state[key] = s.ms_state
        if s.ms_state != "CONFIRMED":
            record_signal(action="observe", reason=f"state:{s.ms_state}",
                          direction=s.direction, **base)

    if s.ms_state != "CONFIRMED" or not s.retest:
        return

    # judge at most once per 4h candle per asset+venue
    candle_ts = c4h[-2]["ts"] if len(c4h) > 1 else c4h[-1]["ts"]
    if _last_judged.get(key) == candle_ts:
        return
    _last_judged[key] = candle_ts

    logger.info(f"[CANDIDATE] {venue}:{asset} {s.direction} neckline={s.neckline}")
    verdict, reasoning, model = judge(asset, s.packet(), trend_1d, c4h, c1h, rr_min)

    if verdict is None:
        record_signal(action="skipped", reason="judge_unavailable",
                      direction=s.direction, structure_json=s.packet(),
                      judge_model=model, ai_reasoning=reasoning, **base)
        return

    common = dict(structure_json=s.packet(), judge_model=model,
                  ai_valid=int(verdict["valid"]), ai_confidence=verdict["confidence"],
                  ai_entry=verdict["entry"], ai_stop=verdict["stop"],
                  ai_target=verdict["target"], ai_rr=verdict["rr"],
                  ai_reasons=json.dumps(verdict["reasons"]), ai_reasoning=reasoning)

    if not verdict["valid"]:
        record_signal(action="skipped", reason="judge_rejected",
                      direction=verdict["direction"], **common, **base)
        return

    if venue == "binance" and verdict["direction"] == "short":
        record_signal(action="skipped", reason="short_not_possible_on_spot",
                      direction="short", **common, **base)
        return

    record_signal(action="signal", reason="3ms_confirmed", direction=verdict["direction"],
                  **common, **base)
    logger.info(f"[SIGNAL] {venue}:{asset} {verdict['direction']} "
                f"conf={verdict['confidence']:.0f} rr={verdict['rr']:.2f}")
    _notify(
        f"REVERSAL {verdict['direction'].upper()} {asset} ({venue})\n"
        f"entry {verdict['entry']:.6g} | stop {verdict['stop']:.6g} | "
        f"target {verdict['target']:.6g} | rr {verdict['rr']:.2f} | "
        f"confidence {verdict['confidence']:.0f}%\n"
        f"mode: {get_setting('trade_mode')} — no order placed"
    )
    # Phase 2 (trade_mode = live) plugs in here: retest entry, structural
    # bracket, risk-% sizing, concurrency + monthly caps. Deliberately absent
    # in Phase 1 — observe mode never places orders (spec 001 Q6).


def refresh_universe_stats(binance: BinanceSpot):
    """Hourly 24h-volume refresh so the dashboard's universe view stays live."""
    import requests as rq
    for venue in VENUES:
        rows = get_tradeable_universe(venue)
        out = []
        for r in rows:
            vol = None
            try:
                t = rq.get("https://api.binance.com/api/v3/ticker/24hr",
                           params={"symbol": f"{r['asset']}USDT"}, timeout=5).json()
                vol = float(t.get("quoteVolume", 0))
            except Exception:
                pass
            out.append({**r, "quote_volume_24h": vol, "scanned_at": time.time()})
        if out:
            replace_universe(venue, out)


def run():
    """Main loop. Never returns unless killed."""
    initialize_database_tables()
    ensure_seed()
    mode = get_setting("trade_mode")
    logger.info(f"[STARTUP] reversal bot (spec 001) — trade_mode={mode}")

    binance = BinanceSpot()
    orderly = OrderlyFutures()
    last_stats = 0.0

    logger.info("[LOOP] entering main loop")
    while True:
        cycle_start = time.time()
        try:
            for venue, ex in (("binance", binance), ("orderly", orderly)):
                eq = ex.get_equity()
                if eq is not None:
                    set_venue_equity(venue, eq)

            if time.time() - last_stats > 3600:
                refresh_universe_stats(binance)
                last_stats = time.time()

            for venue in VENUES:
                for row in get_tradeable_universe(venue):
                    try:
                        evaluate_asset(binance, orderly, venue, row["asset"])
                    except Exception as e:
                        logger.error(f"[CYCLE] {venue}:{row['asset']} failed: {e}")
        except Exception as e:
            logger.error(f"[LOOP] cycle error: {e}")

        elapsed = time.time() - cycle_start
        time.sleep(max(30.0, get_setting_int("cycle_seconds", 1800) - elapsed))


if __name__ == "__main__":
    run()
