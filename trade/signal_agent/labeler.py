"""
Signal Outcome Labeler — downloads trade history from Orderly & Binance,
matches executed trades to signal_history rows by timestamp proximity,
and writes realized_pnl + trade_outcome back for ML training.

Usage:
    python -m trade.signal_agent.labeler           # label all unlabeled signals
    python -m trade.signal_agent.labeler --dry-run # preview matches only
    python -m trade.signal_agent.labeler --days 30 # limit trade download window
"""

import json
import os
import sys
import time
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from base58 import b58decode
from base64 import urlsafe_b64encode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from db.db_ops import get_db_connection
from logs.log_config import apolo_trader_logger as logger

# ── Orderly API helpers (mirrors get_trades.py) ──────────────────────────
ORDERLY_BASE = os.getenv("ORDERLY_BASE_URL", "https://api.orderly.org")
ORDERLY_ACCOUNT_ID = os.getenv("ORDERLY_ACCOUNT_ID")
ORDERLY_SECRET = os.getenv("ORDERLY_SECRET")
ORDERLY_PUBLIC_KEY = os.getenv("ORDERLY_PUBLIC_KEY")

# ── Binance API helpers ──────────────────────────────────────────────────
BINANCE_BASE = "https://api.binance.com"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ── Matching parameters ──────────────────────────────────────────────────
MATCH_WINDOW_MINUTES = 15      # max time diff between signal and entry trade
EXIT_WINDOW_HOURS = 12         # max time to look for closing trade after signal
PNL_THRESHOLD_WIN = 0.01       # PnL > $0.01 = win
PNL_THRESHOLD_LOSS = -0.01     # PnL < -$0.01 = loss


def _build_private_key(secret: str) -> Ed25519PrivateKey:
    normalized = secret.replace("ed25519:", "") if secret.startswith("ed25519:") else secret
    return Ed25519PrivateKey.from_private_bytes(b58decode(normalized))


def _orderly_headers(path_with_query: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    private_key = _build_private_key(ORDERLY_SECRET)
    message = f"{timestamp}GET{path_with_query}"
    signature = urlsafe_b64encode(private_key.sign(message.encode())).decode()
    return {
        "orderly-timestamp": timestamp,
        "orderly-account-id": ORDERLY_ACCOUNT_ID,
        "orderly-key": ORDERLY_PUBLIC_KEY,
        "orderly-signature": signature,
    }


def _binance_sign(params: dict) -> str:
    import hmac, hashlib
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(
        BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256
    ).hexdigest()


def download_orderly_trades(symbol_filter: str = "NEAR_USDC", days: int = 90) -> list[dict]:
    """
    Download ALL trades from Orderly with pagination.
    Orderly /v1/trades returns up to 100 trades per page.
    """
    all_trades = []
    page = 1
    page_size = 100

    while True:
        path = f"/v1/trades?page={page}&page_size={page_size}"
        headers = _orderly_headers(path)
        url = f"{ORDERLY_BASE}{path}"

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.error(f"Orderly trades page {page} error: {e}")
            break

        data = payload.get("data")
        if isinstance(data, dict):
            rows = data.get("rows", data.get("items", data.get("trades", [])))
        elif isinstance(data, list):
            rows = data
        else:
            rows = []

        if not rows:
            break

        for trade in rows:
            if symbol_filter in str(trade.get("symbol", "")):
                all_trades.append(trade)

        logger.info(f"  Orderly page {page}: {len(rows)} trades, {len(all_trades)} matched so far")
        page += 1

        if len(rows) < page_size:
            break
        time.sleep(0.15)  # rate limit

    logger.info(f"Downloaded {len(all_trades)} Orderly trades total")
    return all_trades


def download_binance_trades(symbol: str = "NEARUSDT", days: int = 90) -> list[dict]:
    """
    Download spot trade history from Binance.
    Binance /api/v3/myTrades returns up to 1000 trades per request.
    Uses fromId pagination to walk backward.
    """
    all_trades = []
    limit = 1000
    from_id = None

    while True:
        params: dict = {
            "symbol": symbol,
            "limit": limit,
            "timestamp": int(time.time() * 1000),
        }
        if from_id is not None:
            params["fromId"] = from_id
        params["signature"] = _binance_sign(params)

        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        url = f"{BINANCE_BASE}/api/v3/myTrades"

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                logger.warning("  Binance rate limited, waiting 10s...")
                time.sleep(10)
                continue
            resp.raise_for_status()
            trades = resp.json()
        except Exception as e:
            logger.error(f"Binance trades error: {e}")
            break

        if not trades:
            break

        all_trades.extend(trades)
        logger.info(f"  Binance batch: {len(trades)} trades, {len(all_trades)} total")

        if len(trades) < limit:
            break
        from_id = trades[-1]["id"]
        time.sleep(0.3)  # Binance rate limit

    logger.info(f"Downloaded {len(all_trades)} Binance trades total")
    return all_trades


def _normalize_trades_orderly(raw_trades: list[dict]) -> list[dict]:
    """Normalize Orderly trades to a common format for matching."""
    normalized = []
    for t in raw_trades:
        try:
            ts_ms = int(t.get("executed_timestamp", 0))
            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue

        normalized.append({
            "exchange": "dex",
            "trade_id": t.get("id"),
            "symbol": t.get("symbol", ""),
            "side": str(t.get("side", "")).upper(),
            "executed_price": float(t.get("executed_price", 0)),
            "executed_quantity": float(t.get("executed_quantity", 0)),
            "realized_pnl": float(t.get("realized_pnl", 0)),
            "timestamp_utc": dt,
            "timestamp_ms": ts_ms,
        })
    return normalized


def _normalize_trades_binance(raw_trades: list[dict]) -> list[dict]:
    """Normalize Binance trades to a common format for matching."""
    normalized = []
    for t in raw_trades:
        try:
            ts_ms = int(t.get("time", 0))
            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue

        # Binance spot: buy -> entry, sell -> exit (realized pnl on sell)
        symbol = t.get("symbol", "")
        side = "BUY" if t.get("isBuyer", False) else "SELL"
        price = float(t.get("price", 0))
        qty = float(t.get("qty", 0))
        quote_qty = float(t.get("quoteQty", 0))

        normalized.append({
            "exchange": "cex",
            "trade_id": t.get("id"),
            "symbol": symbol,
            "side": side,
            "executed_price": price,
            "executed_quantity": qty,
            "quote_qty": quote_qty,
            "realized_pnl": 0.0,  # Binance spot doesn't report PnL per trade
            "timestamp_utc": dt,
            "timestamp_ms": ts_ms,
        })
    return normalized


def _compute_binance_pnl(trades: list[dict]) -> list[dict]:
    """
    Binance spot doesn't provide per-trade PnL.
    Match BUY/SELL pairs by FIFO to compute realized PnL.
    """
    # Group by symbol
    by_symbol: dict[str, list] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    for sym_trades in by_symbol.values():
        sym_trades.sort(key=lambda x: x["timestamp_ms"])
        buy_queue: list[dict] = []  # FIFO queue of open buys

        for t in sym_trades:
            if t["side"] == "BUY":
                buy_queue.append(t)
            elif t["side"] == "SELL" and buy_queue:
                # Match against oldest buy
                buy = buy_queue.pop(0)
                buy_price = buy["executed_price"]
                sell_price = t["executed_price"]
                # Approximate: PnL based on price difference × sell qty (capped by buy qty)
                matched_qty = min(buy["executed_quantity"], t["executed_quantity"])
                pnl = (sell_price - buy_price) * matched_qty
                t["realized_pnl"] = round(pnl, 8)

                # If sell qty > buy qty, put remainder back
                if t["executed_quantity"] > buy["executed_quantity"]:
                    # Partial fill: remaining buy still open
                    pass  # simplified — assume full match for now

    return trades


def match_signals_to_trades(
    signals: list[dict],
    trades: list[dict],
    entry_window_minutes: int = MATCH_WINDOW_MINUTES,
    exit_window_hours: int = EXIT_WINDOW_HOURS,
) -> list[dict]:
    """
    Match each approved signal to its trade outcome.

    Logic:
    - A BUY signal (enter long) → find SELL trade AFTER signal with realized_pnl
    - A SELL signal (enter short) → find BUY trade AFTER signal with realized_pnl
    - The closing trade is the exit; its realized_pnl is the outcome.

    First, verify the entry trade exists near the signal (same side, within
    entry_window). Then, find the nearest closing trade (opposite side with
    non-zero PnL) within exit_window_hours.
    """
    entry_window = timedelta(minutes=entry_window_minutes)
    exit_window = timedelta(hours=exit_window_hours)
    matched_count = 0

    # Separate trades by side for fast lookup
    buy_trades = [t for t in trades if t["side"] == "BUY"]
    sell_trades = [t for t in trades if t["side"] == "SELL"]

    close_trades = [t for t in trades if abs(t["realized_pnl"]) > 0.0001]

    for sig in signals:
        sig_ts_str = sig.get("timestamp")
        if not sig_ts_str:
            continue
        try:
            ts_clean = str(sig_ts_str).replace("Z", "+00:00")
            sig_ts = datetime.fromisoformat(ts_clean)
            if sig_ts.tzinfo is None:
                sig_ts = sig_ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        sig_exchange = sig.get("exchange", "dex")
        sig_side = sig.get("side", "")

        # Step 1: Verify entry trade exists near signal
        entry_candidates = buy_trades if sig_side == "BUY" else sell_trades
        entry_found = False
        for trade in entry_candidates:
            if trade["exchange"] != sig_exchange:
                continue
            diff = abs(trade["timestamp_utc"] - sig_ts)
            if diff < entry_window:
                entry_found = True
                break

        if not entry_found:
            continue  # no entry trade confirmed near signal

        # Step 2: Find closing trade (opposite side) with PnL, AFTER signal
        closing_side = "SELL" if sig_side == "BUY" else "BUY"
        best_close = None
        best_diff = exit_window

        for trade in close_trades:
            if trade["exchange"] != sig_exchange:
                continue
            if trade["side"] != closing_side:
                continue
            # Must happen AFTER the signal
            diff = trade["timestamp_utc"] - sig_ts
            if timedelta(0) <= diff < best_diff:
                best_diff = diff
                best_close = trade

        if best_close is not None:
            sig["realized_pnl"] = best_close["realized_pnl"]
            pnl = best_close["realized_pnl"]
            if pnl > PNL_THRESHOLD_WIN:
                sig["trade_outcome"] = "win"
            elif pnl < PNL_THRESHOLD_LOSS:
                sig["trade_outcome"] = "loss"
            else:
                sig["trade_outcome"] = "breakeven"
            sig["matched_trade_id"] = best_close["trade_id"]
            sig["match_diff_seconds"] = best_diff.total_seconds()
            matched_count += 1

    logger.info(f"Matched {matched_count}/{len(signals)} signals to closing trades")
    return signals


def label_signals(dry_run: bool = False, days: int = 90, local_only: bool = False, full_sync: bool = False):
    """
    Main pipeline — designed to run periodically (cron / every few hours):

    1. Load accumulated trades from data/accumulated_trades.json
    2. Fetch latest trades from Orderly API (page 1 only by default, --full for all)
    3. Merge & deduplicate, save back to accumulated file
    4. Match unlabeled approved signals to closing trades
    5. Write realized_pnl + trade_outcome to signal_history

    Page 1 (200 trades) covers ~3 months — enough for periodic 2-hour runs.
    Use --full for initial seeding to paginate through entire history.
    """
    logger.info(f"=== Signal Outcome Labeler (dry_run={dry_run}, full_sync={full_sync}) ===")

    acc_path = PROJECT_ROOT / "data" / "accumulated_trades.json"

    # ── 1. Load accumulated trades ──────────────────────────────────────
    accumulated: dict[str, dict] = {}  # keyed by trade_id for dedup
    if acc_path.exists():
        try:
            with open(acc_path) as f:
                existing = json.load(f)
            for t in existing:
                tid = str(t.get("id", ""))
                if tid:
                    accumulated[tid] = t
            logger.info(f"Loaded {len(accumulated)} accumulated trades")
        except Exception:
            logger.warning("Could not load accumulated trades, starting fresh")

    # ── 2. Fetch fresh trades from Orderly ──────────────────────────────
    # Page 1 (200 most recent) covers ~3 months — enough for periodic runs.
    # Use --full to paginate through entire history (for initial seeding).
    if not local_only:
        if full_sync:
            logger.info("Fetching Orderly trades (FULL sync — all pages)...")
        else:
            logger.info("Fetching Orderly trades (page 1 only — last ~3 months)...")

        page = 1
        page_size = 200
        new_count = 0
        while True:
            path = f"/v1/trades?page={page}&size={page_size}"
            try:
                headers = _orderly_headers(path)
                r = requests.get(f"{ORDERLY_BASE}{path}", headers=headers, timeout=15)
                r.raise_for_status()
                payload = r.json()
                data = payload.get("data", {})
                raw_rows = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            except Exception as e:
                logger.error(f"Orderly API page {page} error: {e}")
                break

            if not raw_rows:
                break

            for t in raw_rows:
                tid = str(t.get("id", ""))
                if tid and tid not in accumulated:
                    accumulated[tid] = t
                    new_count += 1

            logger.info(f"  Page {page}: {len(raw_rows)} trades ({new_count} new this run)")
            if not full_sync or len(raw_rows) < page_size:
                break
            page += 1
            time.sleep(0.15)

        logger.info(f"  Total accumulated: {len(accumulated)} trades ({new_count} new this run)")

    # ── 3. Save accumulated trades ──────────────────────────────────────
    acc_list = list(accumulated.values())
    acc_path.parent.mkdir(parents=True, exist_ok=True)
    with open(acc_path, "w") as f:
        json.dump(acc_list, f, default=str)

    # ── 4. Normalize Orderly trades for matching ───────────────────────
    all_trades = _normalize_trades_orderly(acc_list)
    logger.info(f"Orderly trades for matching: {len(all_trades)}")

    # ── 4b. Fetch & normalize Binance trades for CEX signal matching ───
    if BINANCE_API_KEY and BINANCE_SECRET_KEY:
        try:
            binance_raw = download_binance_trades(days=days)
            if binance_raw:
                binance_norm = _normalize_trades_binance(binance_raw)
                binance_norm = _compute_binance_pnl(binance_norm)
                all_trades.extend(binance_norm)
                logger.info(f"Binance trades for matching: {len(binance_norm)}")
        except Exception as e:
            logger.warning(f"Could not download Binance trades for labeling: {e}")

    logger.info(f"Total trades for matching: {len(all_trades)}")

    # ── 5. Fetch unlabeled approved signals ─────────────────────────────
    with get_db_connection() as conn:
        cur = conn.cursor()
        from db.db_ops import _ensure_signal_history_schema
        _ensure_signal_history_schema(cur)

        cur.execute("""
            SELECT * FROM signal_history
            WHERE approved = 1
              AND (realized_pnl IS NULL OR trade_outcome IS NULL)
            ORDER BY timestamp ASC
        """)
        rows = cur.fetchall()
        signals = [dict(r) for r in rows]

    logger.info(f"Unlabeled approved signals: {len(signals)}")

    if not signals:
        logger.info("Nothing to label.")
        return

    # ── 6. Match ────────────────────────────────────────────────────────
    signals = match_signals_to_trades(signals, all_trades)

    # ── 7. Write back ───────────────────────────────────────────────────
    if dry_run:
        logger.info("=== DRY RUN — preview ===")
        win = sum(1 for s in signals if s.get("trade_outcome") == "win")
        loss = sum(1 for s in signals if s.get("trade_outcome") == "loss")
        be = sum(1 for s in signals if s.get("trade_outcome") == "breakeven")
        unmatched = sum(1 for s in signals if s.get("trade_outcome") is None)
        logger.info(f"  Win: {win}, Loss: {loss}, Breakeven: {be}, Unmatched: {unmatched}")
        for s in signals[:10]:
            logger.info(
                f"  ID={s['id']} side={s.get('side')} outcome={s.get('trade_outcome')} "
                f"pnl={s.get('realized_pnl')} diff={s.get('match_diff_seconds', 'N/A')}s"
            )
        return

    with get_db_connection() as conn:
        cur = conn.cursor()
        updated = 0
        for sig in signals:
            if sig.get("trade_outcome") is None:
                continue
            cur.execute("""
                UPDATE signal_history
                SET realized_pnl = ?, trade_outcome = ?
                WHERE id = ?
            """, (sig["realized_pnl"], sig["trade_outcome"], sig["id"]))
            updated += 1
        conn.commit()

    logger.info(f"✅ Labeled {updated} signals in database")

    # ── 8. Auto-retrain ML model if enough new labels ──────────────────
    if updated > 0 and not dry_run:
        _maybe_retrain_model()

    return updated


def _maybe_retrain_model(min_new_labels: int = 5):
    """
    Retrain the ML model if enough new labeled samples accumulated.
    Runs synchronously (labeler is already in background thread).
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM signal_history WHERE trade_outcome IN ('win','loss')")
            total = cur.fetchone()[0]

        if total < 20:
            return  # not enough data

        # Check if model exists and if retraining is worth it
        import os
        model_path = PROJECT_ROOT / "data" / "signal_model.json"
        last_train = 0.0
        if model_path.exists():
            last_train = os.path.getmtime(str(model_path))

        # Retrain at most once per 24 hours, unless ≥20 new labels since last train
        now = time.time()
        if last_train > 0 and (now - last_train) < 86400 and total < 220:
            logger.info(f"[LABELER] Skipping retrain — last was {int((now-last_train)/3600)}h ago, {total} labeled")
            return

        logger.info(f"[LABELER] Auto-retraining model with {total} labeled samples...")
        from trade.signal_agent.train import train_and_save
        train_and_save(dry_run=False, retrain=True)
    except Exception as e:
        logger.warning(f"[LABELER] Auto-retrain failed: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Label signal_history with trade outcomes")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without writing to DB")
    parser.add_argument("--full", action="store_true", dest="full_sync", help="Fetch ALL pages (not just page 1)")
    parser.add_argument("--local-only", action="store_true", help="Use cached accumulated_trades.json only, no API calls")
    args = parser.parse_args()
    label_signals(dry_run=args.dry_run, full_sync=args.full_sync, local_only=args.local_only)
