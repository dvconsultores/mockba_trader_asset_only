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
    upsert_setting, get_active_pairs, initialize_database_tables,
)
from logs.log_config import apolo_trader_logger as logger
from trade.regime import detect_regime, invalidate_cache
from trade.pnl import is_entry_blocked, is_entry_blocked_per_pair, can_trade_venue, compute_slot_size, max_effective_slots, check_global_daily_loss
from trading_bot.executor import BinanceSpot, OrderlyFutures
from trading_bot.spot_scalper import manage_open_positions as spot_manage, scalp_cycle as spot_cycle
from trading_bot.futures_scalper import manage_open_positions as futures_manage, scalp_cycle as futures_cycle


# ═══════════════════════════════════════════════════════════════════════════════
# Startup validation gate — refuses to trade if config is invalid
# ═══════════════════════════════════════════════════════════════════════════════

def validate_startup() -> bool:
    """Run all startup checks via settings_rules. Returns True if trading can proceed."""
    from trade.settings_rules import validate_all, SettingsContext, validate_all_assets
    results = validate_all()
    errors = [f"{k}: {v.message}" for k, v in results.items() if v.level == "error"]
    warns = [f"{k}: {v.message}" for k, v in results.items() if v.level == "warn"]

    for w in warns:
        logger.warning(f"[STARTUP] {w}")
    if errors:
        for e in errors:
            logger.warning(f"[STARTUP] {e}")
        return False

    pairs = get_active_pairs()
    if not pairs:
        logger.info("[STARTUP] no active pairs configured — add assets via Telegram or Mini App")
        return False

    # Per-asset validation (Amendment 004)
    try:
        from db.db_ops import get_all_asset_configs
        asset_ctx = SettingsContext(asset_configs=get_all_asset_configs())
        asset_results = validate_all_assets(asset_ctx)
        for symbol, verdicts in asset_results.items():
            if symbol == "__overallocation__":
                for v in verdicts:
                    if v.level == "error":
                        logger.warning(f"[STARTUP] overallocation: {v.message}")
                        return False
                    else:
                        logger.warning(f"[STARTUP] overallocation warn: {v.message}")
            else:
                for v in verdicts:
                    if v.level == "error":
                        logger.warning(f"[STARTUP] {symbol}: {v.message}")
    except Exception as e:
        logger.error(f"[STARTUP] per-asset validation failed: {e}")
        return False

    tp = get_setting_float("tp_min_pct", 0.8); sl = get_setting_float("sl_min_pct", 0.5)
    fee_key = "dex_round_trip_fee_pct"  # DEX is the stricter venue
    fee = get_setting_float(fee_key, 0.06); slip = get_setting_float("assumed_slippage_pct", 0.03)
    net = tp - fee - slip
    logger.info(f"[STARTUP] validation OK — tp={tp} sl={sl} net_edge={net:.2f}% active_pairs={len(pairs)}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Startup reconciliation — Constitution VI: restart safety for all active pairs
# ═══════════════════════════════════════════════════════════════════════════════

def _reconcile_startup(binance, orderly):
    """Reconcile exchange state with local DB for all active (asset, venue) pairs."""
    from db.db_ops import get_active_pairs, load_all_positions, save_position, delete_position
    logger.info("[RECONCILE] starting multi-asset reconciliation")

    pairs = get_active_pairs()
    if not pairs:
        logger.info("[RECONCILE] no active pairs — skipping reconciliation")
        return

    exchange_map = {"binance": binance, "orderly": orderly}

    for asset, venue, capital in pairs:
        ex = exchange_map.get(venue)
        if ex is None:
            continue
        try:
            local_positions = load_all_positions(asset=asset, venue=venue)
            local_ids = {p["id"] for p in local_positions}

            # Query exchange for open positions (dry-run skips actual API calls)
            exchange_positions = ex.get_open_positions(asset) if hasattr(ex, "get_open_positions") else []
            if exchange_positions is None:
                exchange_positions = []

            exchange_ids = set()
            for ep in exchange_positions:
                pid = ep.get("id") or ep.get("orderId", "")
                if not pid:
                    continue
                exchange_ids.add(str(pid))

                # Adopt exchange position with no local DB record
                if str(pid) not in local_ids:
                    logger.warning(
                        f"[RECONCILE] adopting orphan position {venue}:{asset}:{pid}"
                    )
                    # Minimal save — details will be filled on next manage cycle
                    try:
                        save_position({
                            "id": str(pid),
                            "asset": asset,
                            "venue": venue,
                            "side": ep.get("side", "long"),
                            "qty": float(ep.get("qty", 0)),
                            "entry_price": float(ep.get("entryPrice", 0)),
                            "signal_price": float(ep.get("entryPrice", 0)),
                            "tp_price": 0.0,
                            "sl_price": 0.0,
                            "tp_order_id": None,
                            "sl_order_id": None,
                            "opened_at": time.time(),
                        })
                    except Exception:
                        logger.error(f"[RECONCILE] failed to save orphan {venue}:{asset}:{pid}")

            # Close DB records with no matching exchange position
            for pid in local_ids - exchange_ids:
                logger.warning(
                    f"[RECONCILE] closing stale DB record {venue}:{asset}:{pid}"
                )
                delete_position(asset, venue, pid)

        except Exception as e:
            logger.error(f"[RECONCILE] {venue}:{asset} reconciliation failed: {e}")
            # Continue to next pair — do not abort

    logger.info("[RECONCILE] complete")


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    """Main autotrade loop. Never returns unless killed."""
    initialize_database_tables()

    dry = get_setting_bool("dry_run", True)
    logger.info(f"[STARTUP] dry_run={dry}")

    if not validate_startup():
        # Determine WHY validation returned False — not always a failure
        pairs = get_active_pairs()
        trading_on = get_setting_bool("trading_enabled", True)
        if not pairs:
            logger.info("[STARTUP] no active pairs configured — bot will wait for configuration")
        elif not trading_on:
            logger.info("[STARTUP] trading is disabled — enable via Telegram or Mini App to start")
        else:
            logger.warning("[STARTUP] settings validation found issues — trading disabled until resolved")
        upsert_setting("trading_enabled", "0")
    else:
        # Validation passed — ensure trading_enabled is not stuck at 0 from a previous run
        if not get_setting_bool("trading_enabled", True):
            logger.info("[STARTUP] re-enabling trading after validation passed")
            upsert_setting("trading_enabled", "1")

    binance = BinanceSpot()
    orderly = OrderlyFutures()

    # ── Legacy migration (Amendment 004) ───────────────────────────
    try:
        from db.db_ops import migrate_legacy_assets
        dex_eq = orderly.get_equity() if not dry else 0.0
        cex_eq = binance.get_equity() if not dry else 0.0
        mig_result = migrate_legacy_assets(dex_eq, cex_eq)
        if mig_result.get("migrated"):
            logger.info(f"[MIGRATE] legacy→multi-asset: {len(mig_result.get('assets', []))} assets migrated, "
                        f"legacy keys removed: {mig_result.get('legacy_keys_removed')}")
            # Telegram notification — best-effort
            try:
                from trading_bot.send_bot_message import send_message
                assets_str = ", ".join(a["symbol"] for a in mig_result.get("assets", []))
                send_message(f"🔄 Migration complete: {len(mig_result.get('assets', []))} assets migrated to per-asset model.\n"
                             f"Primary asset: {mig_result['assets'][0]['symbol'] if mig_result.get('assets') else 'none'}\n"
                             f"Legacy keys removed.")
            except Exception:
                pass
        elif mig_result.get("reason"):
            logger.info(f"[MIGRATE] skipped: {mig_result['reason']}")
    except Exception as e:
        logger.error(f"[MIGRATE] migration failed: {e}")

    # ── Startup reconciliation (Amendment 004, Constitution VI) ──────────
    _reconcile_startup(binance, orderly)

    _last_settings: dict[str, str] = {}
    _last_validation = time.time()
    VALIDATION_INTERVAL = 300  # re-validate every 5 minutes

    logger.info("[LOOP] entering main loop")
    _last_mode_log = 0.0

    while True:
        try:
            # ── Periodic mode log (every 5 min) ─────────────────
            if time.time() - _last_mode_log > 300:
                dex_m = _normalize_venue_mode(get_setting("auto_trade_orderly"))
                cex_m = _normalize_venue_mode(get_setting("auto_trade_binance"))
                logger.info(f"[MODE] DEX={dex_m} CEX={cex_m} pairs={len(get_active_pairs())}")
                _last_mode_log = time.time()

            # Refresh settings
            current = {k: get_setting(k) or "" for k in [
                "tp_min_pct", "sl_min_pct", "dip_min_pct", "pump_min_pct",
                "dip_k", "pump_k", "tp_k", "sl_k", "adaptive_enabled",
                "cooldown_sec", "min_entry_spacing_pct",
                "daily_loss_limit", "daily_loss_limit_pct", "max_consecutive_losses",
                "global_daily_loss_limit", "global_daily_loss_limit_pct",
                "max_active_pairs", "max_concurrent_positions",
                "trading_enabled", "dry_run", "leverage", "max_leverage",
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

            # ── Multi-asset iteration (Amendment 004) ──────────────────
            pairs = get_active_pairs()
            max_pairs = get_setting_int("max_active_pairs", 6)
            if len(pairs) > max_pairs:
                logger.warning(f"[LIMIT] {len(pairs)} active pairs exceeds max_active_pairs={max_pairs} — capping")
                pairs = pairs[:max_pairs]

            if not pairs:
                if time.time() % 300 < 30:  # Log once every ~5 min
                    logger.debug("[LOOP] no active pairs")
                time.sleep(30)
                continue

            # ── Max concurrent positions enforcement ────────────────
            from db.db_ops import load_all_positions
            max_positions = get_setting_int("max_concurrent_positions", 9)
            all_open = load_all_positions()
            if len(all_open) >= max_positions:
                logger.info(f"[LIMIT] max_concurrent_positions={max_positions} reached ({len(all_open)} open) — no new entries this cycle")

            exchange_map = {"binance": binance, "orderly": orderly}
            _venue_failures: dict[str, int] = {}

            # ── Read per-venue mode (False / Signal / Automatic) ──
            dex_mode = _normalize_venue_mode(get_setting("auto_trade_orderly"))
            cex_mode = _normalize_venue_mode(get_setting("auto_trade_binance"))

            for asset, venue, capital in pairs:
                try:
                    # ── Skip if venue mode is False ──────────────
                    venue_mode = dex_mode if venue == "orderly" else cex_mode
                    if venue_mode == "False":
                        continue

                    # ── Automatic mode requires capital ──────────
                    if venue_mode == "Automatic" and capital <= 0:
                        logger.warning(f"[CONFIG] {venue}:{asset} automatic mode but capital={capital} — skipping")
                        continue

                    ex = exchange_map.get(venue)
                    if ex is None:
                        continue

                    # ── Equity query ──────────────────────────────
                    try:
                        equity = ex.get_equity()
                    except Exception:
                        _venue_failures[venue] = _venue_failures.get(venue, 0) + 1
                        logger.error(f"[ERROR] {venue}:{asset} equity query failed")
                        continue

                    signal_only = (venue_mode == "Signal")

                    # ── Per-pair kill switch (skip for signal-only) ──
                    blocked = False
                    reason = ""
                    if not signal_only:
                        blocked, reason = is_entry_blocked_per_pair(asset, venue, equity)
                        if len(all_open) >= max_positions:
                            blocked = True
                            reason = f"max_concurrent_positions={max_positions} reached"
                    regime = detect_regime(asset, venue)

                    # ── Manage exits FIRST ────────────────────────
                    if venue == "binance":
                        spot_manage(asset, binance)
                        if not blocked and regime != "UNKNOWN":
                            obi = _get_obi_binance(asset)
                            price = _get_live_price_binance(asset)
                            if obi is not None and price is not None:
                                result = spot_cycle(asset, binance, regime, obi, price, signal_only=signal_only)
                                if signal_only and result:
                                    _notify_signal(asset, "CEX", regime, result, price)
                    else:
                        futures_manage(asset, orderly, regime)
                        if not blocked and regime != "UNKNOWN":
                            obi = _get_obi_orderly(asset)
                            price = _get_live_price_orderly(asset)
                            if obi is not None and price is not None:
                                result = futures_cycle(asset, orderly, regime, obi, price, signal_only=signal_only)
                                if signal_only and result:
                                    _notify_signal(asset, "DEX", regime, result, price)

                except Exception as e:
                    logger.error(f"[ERROR] {venue}:{asset} cycle failed: {e}")
                    _venue_failures[venue] = _venue_failures.get(venue, 0) + 1
                    # Continue to next pair — do not abort

            # ── Venue-level failure escalation (Constitution IV) ────
            for venue, fails in _venue_failures.items():
                if fails >= 5:
                    logger.warning(f"[KILL] {venue} disabled after {fails} consecutive failures")
                    upsert_setting(f"auto_trade_{venue}", "false")

            # ── Global daily loss check ────────────────────────────
            should_halt, g_reason = check_global_daily_loss()
            if should_halt:
                logger.warning(f"[KILL] global_daily_loss_limit breached: {g_reason}")
                upsert_setting("trading_enabled", "0")

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


def _normalize_venue_mode(raw: str | None) -> str:
    """Normalize legacy values (true/false) to canonical mode names."""
    if not raw:
        return "False"
    v = raw.strip().lower()
    if v in ("true", "automatic"):
        return "Automatic"
    if v in ("signal",):
        return "Signal"
    return "False"


def _notify_signal(asset: str, exchange_label: str, regime: str, result: dict, price: float):
    """Send a Telegram notification when a signal fires in Signal mode."""
    try:
        from trading_bot.send_bot_message import send_message
        direction = result["direction"]
        tp = result["tp"]
        sl = result["sl"]
        emoji = "🟢" if direction == "buy" else "🔴"
        if exchange_label == "CEX":
            action = "BUY" if direction == "buy" else "SELL"
            close_action = "SELL" if direction == "buy" else "BUY"
            msg = (
                f"{emoji} {asset} ({exchange_label}) — {action} at {price:.4f}\n"
                f"{close_action} at {tp:.4f}  |  SL at {sl:.4f}\n"
                f"Regime: {regime}"
            )
        else:
            action = "LONG entry" if direction == "buy" else "SHORT entry"
            close_action = "close" if direction == "buy" else "close"
            msg = (
                f"{emoji} {asset} ({exchange_label}) — {action} at {price:.4f}\n"
                f"Close at {tp:.4f}  |  SL at {sl:.4f}\n"
                f"Regime: {regime}"
            )
        send_message(msg)
    except Exception as e:
        logger.warning(f"[SIGNAL] failed to send notification: {e}")


if __name__ == "__main__":
    run()
