"""
MockbaV4 — Main trading loop.

For each asset: refresh settings, detect regime, manage open positions, evaluate entries.
Startup validation gate runs before first cycle and on setting changes.
dry_run stays true throughout Phase 2.
"""

from __future__ import annotations
import os
import sys
import time
import math
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import (
    get_setting, get_setting_float, get_setting_int, get_setting_bool,
    upsert_setting, get_asset_list, initialize_database_tables,
)
from logs.log_config import apolo_trader_logger as logger
from trade.regime import detect_regime, invalidate_cache
from trade.pnl import is_entry_blocked, can_trade_venue, compute_slot_size, max_effective_slots
from trading_bot.executor import BinanceSpot, OrderlyFutures
from trading_bot.spot_scalper import manage_open_positions as spot_manage, scalp_cycle as spot_cycle
from trading_bot.futures_scalper import manage_open_positions as futures_manage, scalp_cycle as futures_cycle


# ═══════════════════════════════════════════════════════════════════════════════
# Startup validation gate — refuses to trade if config is invalid
# ═══════════════════════════════════════════════════════════════════════════════

def validate_startup() -> bool:
    """Run all startup checks via settings_rules. Returns True if trading can proceed."""
    from trade.settings_rules import validate_all, SettingsContext
    results = validate_all()
    errors = [f"{k}: {v.message}" for k, v in results.items() if v.level == "error"]
    warns = [f"{k}: {v.message}" for k, v in results.items() if v.level == "warn"]

    for w in warns:
        logger.warning(f"[STARTUP] {w}")
    if errors:
        for e in errors:
            logger.warning(f"[STARTUP] {e}")
        return False

    assets = get_asset_list()
    if not assets:
        logger.warning("[STARTUP] no assets configured")
        return False

    tp = get_setting_float("tp_min_pct", 0.8); sl = get_setting_float("sl_min_pct", 0.5)
    fee_key = "dex_round_trip_fee_pct" if get_setting_bool("auto_trade_orderly", False) else "cex_round_trip_fee_pct"
    fee = get_setting_float(fee_key, 0.06); slip = get_setting_float("assumed_slippage_pct", 0.03)
    net = tp - fee - slip
    logger.info(f"[STARTUP] validation OK — tp={tp} sl={sl} net_edge={net:.2f}% assets={assets}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    """Main autotrade loop. Never returns unless killed."""
    initialize_database_tables()

    dry = get_setting_bool("dry_run", True)
    logger.info(f"[STARTUP] dry_run={dry}")

    if not validate_startup():
        logger.warning("[STARTUP] validation failed — trading disabled")
        upsert_setting("trading_enabled", "0")
        # Don't exit — user may fix config via Telegram and validation re-runs

    binance = BinanceSpot()
    orderly = OrderlyFutures()
    _last_settings: dict[str, str] = {}
    _last_validation = time.time()
    VALIDATION_INTERVAL = 300  # re-validate every 5 minutes

    while True:
        try:
            # Refresh settings
            current = {k: get_setting(k) or "" for k in [
                "tp_min_pct", "sl_min_pct", "dip_min_pct", "pump_min_pct",
                "dip_k", "pump_k", "tp_k", "sl_k", "adaptive_enabled",
                "max_slots", "cooldown_sec", "min_entry_spacing_pct",
                "daily_loss_limit", "daily_loss_limit_pct", "max_consecutive_losses",
                "trading_enabled", "dry_run", "leverage", "max_leverage",
                "auto_trade_binance", "auto_trade_orderly",
            ]}

            # Log setting changes
            for k, v in current.items():
                old = _last_settings.get(k)
                if old is not None and old != v:
                    logger.info(f"[CONFIG] {k}: {old} → {v}")
            _last_settings = current

            trading_enabled = get_setting_bool("trading_enabled", True)
            if not trading_enabled:
                time.sleep(30)
                continue

            # Periodic re-validation
            if time.time() - _last_validation > VALIDATION_INTERVAL:
                if not validate_startup():
                    logger.warning("[VALIDATION] failed — halting new entries")
                    upsert_setting("trading_enabled", "0")
                _last_validation = time.time()

            assets = get_asset_list()
            cex_active = get_setting_bool("auto_trade_binance", False)
            dex_active = get_setting_bool("auto_trade_orderly", False)

            for asset in assets:
                # ── Binance (spot) ──────────────────────────────────
                if cex_active:
                    try:
                        equity_b = binance.get_equity()
                        blocked, reason = is_entry_blocked("binance", equity_b)
                        regime = detect_regime(asset, "binance")

                        # Manage exits FIRST
                        spot_manage(asset, binance)

                        if not blocked and regime != "UNKNOWN":
                            # Get OBI and live price
                            obi = _get_obi_binance(asset)
                            price = _get_live_price_binance(asset)
                            if obi is not None and price is not None:
                                spot_cycle(asset, binance, regime, obi, price)
                    except Exception as e:
                        logger.error(f"[ERROR] binance:{asset} cycle failed: {e}")

                # ── Orderly (futures) ───────────────────────────────
                if dex_active:
                    try:
                        equity_o = orderly.get_equity()
                        blocked, reason = is_entry_blocked("orderly", equity_o)
                        regime = detect_regime(asset, "orderly")

                        # Manage exits FIRST
                        futures_manage(asset, orderly, regime)

                        if not blocked and regime != "UNKNOWN":
                            obi = _get_obi_orderly(asset)
                            price = _get_live_price_orderly(asset)
                            if obi is not None and price is not None:
                                futures_cycle(asset, orderly, regime, obi, price)
                    except Exception as e:
                        logger.error(f"[ERROR] orderly:{asset} cycle failed: {e}")

            time.sleep(30)

        except Exception as e:
            logger.error(f"[ERROR] main loop: {e}")
            time.sleep(60)


# ── Price & OBI helpers (thin wrappers — executor provides them) ──────────────

def _get_obi_binance(asset: str) -> float | None:
    try:
        import requests
        r = requests.get("https://api.binance.com/api/v3/depth",
                         params={"symbol": f"{asset}USDT", "limit": 10}, timeout=5)
        data = r.json()
        bids = sum(float(b[1]) for b in data.get("bids", []))
        asks = sum(float(a[1]) for a in data.get("asks", []))
        if asks == 0:
            return 2.0
        return bids / asks
    except Exception:
        return None


def _get_live_price_binance(asset: str) -> float | None:
    try:
        import requests
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": f"{asset}USDT"}, timeout=5)
        return float(r.json()["price"])
    except Exception:
        return None


def _get_obi_orderly(asset: str) -> float | None:
    """Orderly public orderbook is restricted — use Binance as proxy (same asset, correlated books)."""
    return _get_obi_binance(asset)


def _get_live_price_orderly(asset: str) -> float | None:
    """Orderly public ticker is restricted — use Binance as proxy."""
    return _get_live_price_binance(asset)


if __name__ == "__main__":
    run()
