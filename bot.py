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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import (
    get_setting, get_setting_float, get_setting_int, get_setting_bool,
    upsert_setting, initialize_database_tables,
    get_tradeable_universe, set_venue_equity, load_all_positions,
)
from logs.log_config import apolo_trader_logger as logger
from trade.regime import detect_regime, invalidate_cache
from trade.pnl import is_entry_blocked, is_entry_blocked_per_pair, can_trade_venue, compute_slot_size, max_effective_slots, check_global_daily_loss
from trade.universe import run_scans_if_due, is_universe_stale, force_rescan, add_degraded_exclusion
from trading_bot.executor import BinanceSpot, OrderlyFutures
from trading_bot.spot_scalper import manage_open_positions as spot_manage, scalp_cycle as spot_cycle
from trading_bot.futures_scalper import manage_open_positions as futures_manage, scalp_cycle as futures_cycle


# Consecutive cycles a venue's universe has been majority-blocked by the
# spread-degradation guard — used to trigger an early rescan of stale spreads.
_degraded_streak: dict[str, int] = {}
# Per-asset consecutive blocked cycles, and last forced-rescan time per venue
# (cooldown prevents hammering the scanner when a rescan doesn't help).
_asset_degraded_streak: dict[tuple[str, str], int] = {}
_last_forced_rescan: dict[str, float] = {}
# Venue -> UTC date string last warned about the consecutive-losses kill switch
# (logs once per venue per day, showing when entries will reset).
_consec_loss_warned: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# Startup validation gate — refuses to trade if config is invalid
# ═══════════════════════════════════════════════════════════════════════════════

def validate_startup() -> bool:
    """Run all startup checks via settings_rules. Logs issues, never blocks — trader is responsible."""
    from trade.settings_rules import validate_all, SettingsContext
    results = validate_all()
    errors = [f"{k}: {v.message}" for k, v in results.items() if v.level == "error"]
    warns = [f"{k}: {v.message}" for k, v in results.items() if v.level == "warn"]

    for w in warns:
        logger.warning(f"[STARTUP] {w}")
    for e in errors:
        logger.warning(f"[STARTUP] {e}")

    # Universe presence (Amendment 003) — the scanner populates this on startup
    if not get_tradeable_universe("binance") and not get_tradeable_universe("orderly"):
        logger.info("[STARTUP] no universe scan stored yet — the scanner thread will populate it")

    # Capital pools (Amendment 003) — declared vs live equity cache, slot caps
    try:
        from trade.settings_rules import validate_capital_pools
        from db.db_ops import get_venue_equity
        def _eq(venue: str) -> float:
            st = get_venue_equity(venue)
            return float(st["equity"]) if st else 0.0
        pool_ctx = SettingsContext(dex_equity=_eq("orderly"), cex_equity=_eq("binance"))
        for v in validate_capital_pools(pool_ctx):
            if v.level in ("error", "warn"):
                logger.warning(f"[STARTUP] {v.message}")
    except Exception:
        pass

    tp = get_setting_float("tp_min_pct", 0.8); sl = get_setting_float("sl_min_pct", 0.5)
    fee_key = "dex_round_trip_fee_pct"
    fee = get_setting_float(fee_key, 0.06); slip = get_setting_float("assumed_slippage_pct", 0.03)
    net = tp - fee - slip
    status = "WARN" if errors else "OK"
    universe_count = len(get_tradeable_universe("binance")) + len(get_tradeable_universe("orderly"))
    logger.info(f"[STARTUP] validation {status} — tp={tp} sl={sl} net_edge={net:.2f}% universe_assets={universe_count}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Startup reconciliation — Constitution VI: restart safety for all active pairs
# ═══════════════════════════════════════════════════════════════════════════════

def _reconcile_startup(binance, orderly):
    """Reconcile exchange state with local DB for every asset that could hold a
    position: current universe members unioned with assets that have a local
    record (Amendment 003 — churn must not orphan restart reconciliation)."""
    from db.db_ops import load_all_positions, save_position, delete_position, get_tradeable_universe
    logger.info("[RECONCILE] starting universe reconciliation")

    exchange_map = {"binance": binance, "orderly": orderly}
    reconciled = 0

    for venue, ex in exchange_map.items():
        universe_assets = {u["asset"] for u in get_tradeable_universe(venue)}
        local_assets = {p["asset"] for p in load_all_positions(venue=venue)}
        assets = sorted(universe_assets | local_assets)
        if not assets:
            continue

        for asset in assets:
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
                reconciled += 1

            except Exception as e:
                logger.error(f"[RECONCILE] {venue}:{asset} reconciliation failed: {e}")
                # Continue to next asset — do not abort

    if reconciled == 0:
        logger.info("[RECONCILE] no assets to reconcile")
    logger.info("[RECONCILE] complete")


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    """Main autotrade loop. Never returns unless killed."""
    initialize_database_tables()

    dry = get_setting_bool("dry_run", True)
    logger.info(f"[STARTUP] dry_run={dry}")

    # Run validation for logging only — never blocks (trader is responsible)
    validate_startup()

    # Ensure trading is enabled (recover from previous kill-switch state)
    if not get_setting_bool("trading_enabled", True):
        logger.info("[STARTUP] re-enabling trading")
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

    # ── Universe scanner thread (Amendment 003) ─────────────────────────
    # Never runs inside the trading cycle. Scans each venue when the stored
    # scan is missing or older than universe_scan_interval_hours.
    def _universe_scanner_loop():
        while True:
            try:
                def _equity_for(v: str) -> float | None:
                    try:
                        return (binance if v == "binance" else orderly).get_equity()
                    except Exception:
                        return None
                results = run_scans_if_due(equity_fn=_equity_for)
                for r in results:
                    if r.get("ok"):
                        logger.info(f"[UNIVERSE] {r['venue']} scan stored {r.get('stored_count', 0)} assets")
                    else:
                        logger.warning(f"[UNIVERSE] {r['venue']} scan not stored: {r.get('reason', 'unknown')}")
            except Exception as e:
                logger.error(f"[UNIVERSE] scanner loop error: {e}")
            time.sleep(600)  # re-check due-ness every 10 minutes

    threading.Thread(target=_universe_scanner_loop, daemon=True, name="universe-scanner").start()

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
                logger.info(f"[MODE] DEX={dex_m} CEX={cex_m} "
                            f"universe_dex={len(get_tradeable_universe('orderly'))} "
                            f"universe_cex={len(get_tradeable_universe('binance'))}")
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

            # Periodic re-validation (log only — never blocks)
            if time.time() - _last_validation > VALIDATION_INTERVAL:
                validate_startup()
                _last_validation = time.time()

            trading_enabled = get_setting_bool("trading_enabled", True)
            if not trading_enabled:
                time.sleep(30)
                continue

            # ── Universe-driven iteration (Amendment 003) ────────────────
            # Assets come from the daily scan (asset_universe), not from a
            # static list. Exits run for universe members AND assets that
            # dropped out — churn never forces an exit.
            max_pairs = get_setting_int("max_active_pairs", 6)
            max_positions = get_setting_int("max_concurrent_positions", 9)
            all_open = load_all_positions()
            if len(all_open) >= max_positions:
                logger.info(f"[LIMIT] max_concurrent_positions={max_positions} reached ({len(all_open)} open) — no new entries this cycle")

            _venue_failures: dict[str, int] = {}

            # ── Read per-venue mode (False / Signal / Automatic) ──
            dex_mode = _normalize_venue_mode(get_setting("auto_trade_orderly"))
            cex_mode = _normalize_venue_mode(get_setting("auto_trade_binance"))

            for venue, ex, venue_mode, label in (
                ("binance", binance, cex_mode, "CEX"),
                ("orderly", orderly, dex_mode, "DEX"),
            ):
                if venue_mode == "False":
                    continue

                universe = get_tradeable_universe(venue)
                if len(universe) > max_pairs:
                    logger.warning(f"[LIMIT] {venue}: {len(universe)} assets exceeds max_active_pairs={max_pairs} — capping")
                    universe = universe[:max_pairs]
                universe_by_asset = {u["asset"]: u for u in universe}

                # Exit management covers universe members AND dropped-out assets
                open_assets = {p["asset"] for p in load_all_positions(venue=venue)}
                assets = sorted(set(universe_by_asset) | open_assets)
                if not assets:
                    if time.time() % 300 < 30:
                        logger.debug(f"[LOOP] {venue}: no universe assets or open positions")
                    continue

                # ── Stale universe: no new entries, exits still managed ──
                stale = is_universe_stale(venue)
                if stale:
                    logger.warning(f"[UNIVERSE] {venue} scan is stale — new entries blocked until rescan (positions still managed)")

                # ── Equity query (once per venue) ──────────────
                try:
                    equity = ex.get_equity()
                    set_venue_equity(venue, equity)
                except Exception:
                    _venue_failures[venue] = _venue_failures.get(venue, 0) + 1
                    logger.error(f"[ERROR] {venue} equity query failed")
                    continue

                signal_only = (venue_mode == "Signal")

                deg_blocked = 0  # assets blocked by the spread-degradation guard this cycle

                for asset in assets:
                    try:
                        regime = detect_regime(asset, venue)

                        # ── Manage exits FIRST (always) ──────────
                        if venue == "binance":
                            spot_manage(asset, binance)
                        else:
                            futures_manage(asset, orderly, regime)

                        # Dropped out of the universe → exits only
                        if asset not in universe_by_asset:
                            if not signal_only and time.time() % 600 < 30:
                                logger.debug(f"[UNIVERSE] {asset} {venue} dropped out — entries stopped, position managed to exit")
                            continue

                        # Stale universe → no new entries (fail closed)
                        if stale:
                            continue

                        blocked = False
                        reason = ""
                        if not signal_only:
                            blocked, reason = is_entry_blocked_per_pair(asset, venue, equity)
                            if len(all_open) >= max_positions:
                                blocked = True
                                reason = f"max_concurrent_positions={max_positions} reached"
                        if blocked:
                            if "max_consecutive_losses" in reason:
                                today = time.strftime("%Y-%m-%d", time.gmtime())
                                if _consec_loss_warned.get(venue) != today:
                                    reset_dt = datetime.now(timezone.utc).replace(
                                        hour=0, minute=0, second=0, microsecond=0
                                    ) + timedelta(days=1)
                                    logger.warning(f"[KILL] {venue}: consecutive losses reached ({reason}) — entries will start over on {reset_dt:%Y-%m-%d %H:%M} UTC")
                                    _consec_loss_warned[venue] = today
                            logger.debug(f"[SKIP] {venue}:{asset} {reason}")
                            continue
                        if regime == "UNKNOWN":
                            continue

                        obi, spread = _get_obi_and_spread(asset, venue)
                        price = _get_live_price_orderly(asset) if venue == "orderly" else _get_live_price_binance(asset)

                        # ── Live spread degradation guard (Amendment 003) ──
                        # Compare against scan-time spread already stored; no
                        # extra API call (spread comes from the OBI snapshot).
                        row = universe_by_asset.get(asset)
                        scan_spread = float(row["spread_pct"]) if row and row.get("spread_pct") else None
                        if scan_spread and spread is not None:
                            mult = get_setting_float("universe_spread_degradation_multiple", 3.0)
                            if spread > scan_spread * mult:
                                deg_blocked += 1
                                _asset_degraded_streak[(venue, asset)] = _asset_degraded_streak.get((venue, asset), 0) + 1
                                logger.warning(f"[UNIVERSE] {asset} {venue} live spread {spread:.3f}% exceeds scan spread {scan_spread:.3f}% × {mult} — skipping entries this cycle")
                                continue
                            _asset_degraded_streak.pop((venue, asset), None)
                        else:
                            _asset_degraded_streak.pop((venue, asset), None)

                        if obi is not None and price is not None:
                            if venue == "binance":
                                result = spot_cycle(asset, binance, regime, obi, price, signal_only=signal_only)
                            else:
                                result = futures_cycle(asset, orderly, regime, obi, price, signal_only=signal_only)
                            if result:
                                _notify_entry(asset, label, regime, result, price, signal_only)
                                all_open = load_all_positions()
                        else:
                            logger.warning(f"[DATA] {asset} {label}: obi={obi} price={price} — skipping cycle")

                    except Exception as e:
                        logger.error(f"[ERROR] {venue}:{asset} cycle failed: {e}")
                        _venue_failures[venue] = _venue_failures.get(venue, 0) + 1
                        # Continue to next asset — do not abort

                # ── Persistent spread degradation → force a rescan ──
                # If a large share of the venue universe is blocked by the
                # spread-degradation guard for several consecutive cycles, the
                # stored scan spreads are stale — request an immediate rescan
                # instead of waiting for universe_scan_interval_hours.
                uv_size = len(universe_by_asset)
                if uv_size > 0:
                    share_thr = get_setting_float("universe_degradation_rescan_share", 0.5)
                    cycles_thr = get_setting_int("universe_degradation_rescan_cycles", 10)
                    if deg_blocked >= max(1, int(uv_size * share_thr)):
                        streak = _degraded_streak.get(venue, 0) + 1
                        _degraded_streak[venue] = streak
                        if streak >= cycles_thr:
                            force_rescan(venue)
                            logger.warning(f"[UNIVERSE] {venue}: {deg_blocked}/{uv_size} assets blocked by spread degradation for {streak} cycles — forcing rescan")
                            _degraded_streak[venue] = 0
                    else:
                        _degraded_streak[venue] = 0

                # Per-asset persistent blocking → force a rescan AND rotate the
                # stale-spread asset out of the next scan (covers a single asset
                # whose stored scan spread is stale, e.g. SKHYB).
                cycles_thr = get_setting_int("universe_degradation_rescan_cycles", 10)
                cooldown = get_setting_float("universe_rescan_cooldown_min", 60) * 60
                for key, streak in list(_asset_degraded_streak.items()):
                    if key[0] != venue:
                        continue
                    if streak < cycles_thr:
                        continue
                    if time.time() - _last_forced_rescan.get(venue, 0.0) < cooldown:
                        continue
                    force_rescan(venue)
                    ttl = get_setting_float("universe_degradation_exclude_hours", 6)
                    add_degraded_exclusion(venue, key[1], ttl)
                    _last_forced_rescan[venue] = time.time()
                    logger.warning(f"[UNIVERSE] {venue}: {key[1]} blocked by spread degradation for {streak} consecutive cycles — forcing rescan and excluding for {ttl:.0f}h")
                    _asset_degraded_streak[key] = 0

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

def _get_obi_and_spread(asset: str, venue: str) -> tuple[float | None, float | None]:
    """Return (obi, spread_pct) for an asset from a single order-book snapshot.

    Orderly public orderbook is restricted — Binance is used as proxy
    (same asset, correlated books), consistent with the existing pattern.
    The spread here feeds the per-cycle degradation guard (Amendment 003)
    with no additional API call.
    """
    try:
        import requests
        r = requests.get("https://api.binance.com/api/v3/depth",
                         params={"symbol": f"{asset}USDT", "limit": 10}, timeout=5)
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid_qty = sum(float(b[1]) for b in bids)
        ask_qty = sum(float(a[1]) for a in asks)
        obi = (bid_qty / ask_qty) if ask_qty > 0 else 2.0
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        spread = ((best_ask - best_bid) / best_bid * 100) if best_bid > 0 else None
        return obi, spread
    except Exception:
        return None, None


def _get_live_price_binance(asset: str) -> float | None:
    try:
        import requests
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": f"{asset}USDT"}, timeout=5)
        return float(r.json()["price"])
    except Exception:
        return None


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
def _price_decimals(price: float) -> int:
    """Return enough decimal places to show meaningful price differences."""
    if price <= 0:
        return 4
    import math
    magnitude = int(math.log10(price))
    if magnitude >= 2:    # $100+
        return max(2, 6 - magnitude)
    if magnitude >= 0:    # $1–$99
        return 4
    if magnitude >= -2:   # $0.01–$0.99
        return 5
    if magnitude >= -4:   # $0.0001–$0.0099
        return 7
    return 8              # sub-penny tokens


def _notify_entry(asset: str, exchange_label: str, regime: str, result: dict, price: float,
                  signal_only: bool = False):
    """Send a Telegram notification when a signal fires or a trade is entered."""
    try:
        from trading_bot.send_bot_message import send_message
        direction = result["direction"]
        tp = result["tp"]
        sl = result["sl"]
        tp_pct = result.get("tp_pct", 0)
        sl_pct = result.get("sl_pct", 0)
        mode = "🔍 SIGNAL" if signal_only else "💰 TRADE"
        emoji = "🟢" if direction == "buy" else "🔴"
        d = _price_decimals(price)

        action = "BUY" if exchange_label == "CEX" else "LONG"
        if direction == "sell":
            action = "SELL" if exchange_label == "CEX" else "SHORT"
        close_label = "SELL" if exchange_label == "CEX" and direction == "buy" else \
                      ("BUY" if exchange_label == "CEX" else "Close")
        tp_sign = "+" if direction == "buy" else "-"
        sl_sign = "-" if direction == "buy" else "+"
        msg = (
            f"{emoji} {asset} ({exchange_label}) —  [{mode}]\n"
            f"{action} at {price:.{d}f}\n"
            f"{close_label} at {tp:.{d}f}  ({tp_sign}{tp_pct:.2f}%)  |  SL at {sl:.{d}f}  ({sl_sign}{sl_pct:.2f}%)\n"
            f"Regime: {regime}"
        )
        send_message(msg)
        logger.info(f"[SIGNAL] {asset} {exchange_label} {direction} @ {price:.{d}f} tp={tp_pct:.2f}% sl={sl_pct:.2f}% regime={regime} mode={'signal' if signal_only else 'trade'}")
    except Exception as e:
        logger.warning(f"[SIGNAL] failed to send notification: {e}")


if __name__ == "__main__":
    run()
