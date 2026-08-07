"""
Mockba Dashboard API — FastAPI backend
Serves: SSE log stream, signal history, ML stats, bot status, Mini App settings
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, parse_qs
import urllib.request

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="Mockba Dashboard API")

logger = logging.getLogger("mockba_dashboard")

# Allow all origins (Docker internal + NPM proxy)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Paths (override via env vars) ──────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/app/data/trading.db")
LOG_PATH = os.getenv("LOG_PATH", "/app/apolo.log")
MODEL_PATH = os.getenv("MODEL_PATH", "/app/data/signal_model.json")
START_TIME = time.time()

# ── Inline settings helpers (avoid cross-module import from db/) ──

def _get_setting(key: str) -> str | None:
    """Read a single setting value."""
    db = _get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    db.close()
    return row["value"] if row else None


def _upsert_setting(key: str, value: str):
    """Insert or update a setting."""
    db = _get_db_rw()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()
    db.close()


# ── Capital & universe helpers (Amendment 003) ──────────────────────

def _get_setting_float(key: str, default: float) -> float:
    try:
        v = _get_setting(key)
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _get_setting_int(key: str, default: int) -> int:
    try:
        v = _get_setting(key)
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _get_venue_equity(venue: str) -> dict | None:
    """Live equity cached by bot.py each cycle."""
    db = _get_db()
    row = db.execute(
        "SELECT venue, equity, updated_at FROM venue_state WHERE venue = ?", (venue,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


def _get_venue_deployed(venue: str) -> float:
    """Notional of open positions on a venue (qty × entry price)."""
    db = _get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(qty * entry_price), 0) AS s FROM open_positions WHERE venue = ?",
        (venue,),
    ).fetchone()
    db.close()
    return float(row["s"]) if row else 0.0


def _fetch_live_price(asset: str, venue: str) -> float | None:
    """Live price from the Binance public ticker (Orderly proxied via Binance)."""
    try:
        with urllib.request.urlopen(
            f"https://api.binance.com/api/v3/ticker/price?symbol={asset}USDT",
            timeout=4,
        ) as resp:
            return float(json.loads(resp.read().decode())["price"])
    except Exception:
        return None


def _get_db() -> sqlite3.Connection:
    """Open a read-only connection (safe for concurrent access)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tail_file(path: str, lines: int = 200):
    """Return last N lines of a file via subprocess (fast on large files)."""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), path],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return result.stdout.rstrip("\n").split("\n")
    except Exception:
        pass
    return []


# ── API: Status ─────────────────────────────────────────────────
@app.get("/api/status")
def api_status():
    """Bot uptime, modes, open positions."""
    try:
        db = _get_db()
        dex_mode = db.execute(
            "SELECT value FROM settings WHERE key='auto_trade_orderly'"
        ).fetchone()
        cex_mode = db.execute(
            "SELECT value FROM settings WHERE key='auto_trade_binance'"
        ).fetchone()
        db.close()
    except Exception:
        dex_mode, cex_mode = None, None

    return {
        "uptime_seconds": round(time.time() - START_TIME),
        "dex_mode": dex_mode["value"] if dex_mode else "unknown",
        "cex_mode": cex_mode["value"] if cex_mode else "unknown",
        "model_loaded": os.path.exists(MODEL_PATH),
    }


# ── API: Signals ────────────────────────────────────────────────
@app.get("/api/signals")
def api_signals(limit: int = Query(50, ge=1, le=500),
                exchange: str = Query(None),
                outcome: str = Query(None)):
    """Recent signal history from signals table."""
    try:
        db = _get_db()
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if exchange:
            query += " AND venue = ?"
            params.append("binance" if exchange == "cex" else "orderly")
        if outcome:
            query += " AND action = ?"
            params.append(outcome)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        db.close()
        signals = [dict(r) for r in rows]
        return {"signals": signals, "count": len(signals)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/signals/{signal_id}")
def api_signal_detail(signal_id: int):
    """Full detail for one signal."""
    try:
        db = _get_db()
        row = db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        db.close()
        if row is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: Daily Stats ────────────────────────────────────────────
@app.get("/api/stats/daily")
def api_daily_stats():
    """Today's signal and trade stats."""
    try:
        db = _get_db()
        today = time.strftime("%Y-%m-%d")
        total = db.execute(
            "SELECT COUNT(*) as c FROM signals WHERE date(datetime(timestamp, 'unixepoch')) = ?",
            (today,)
        ).fetchone()["c"]
        entered = db.execute(
            "SELECT COUNT(*) as c FROM signals WHERE date(datetime(timestamp, 'unixepoch')) = ? AND action = 'entered'",
            (today,)
        ).fetchone()["c"]
        trades = db.execute(
            "SELECT COUNT(*) as c FROM closed_trades WHERE date(datetime(closed_at, 'unixepoch')) = ?",
            (today,)
        ).fetchone()["c"]
        pnl = db.execute(
            "SELECT COALESCE(SUM(pnl_net), 0) as p FROM closed_trades WHERE date(datetime(closed_at, 'unixepoch')) = ?",
            (today,)
        ).fetchone()["p"]
        db.close()
        return {
            "date": today,
            "total_signals": total,
            "entered": entered,
            "skipped": total - entered,
            "trades_closed": trades,
            "pnl_net": round(pnl, 4),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: PnL Info ───────────────────────────────────────────────
@app.get("/api/ml/info")
def api_pnl_info():
    """Recent trade PnL summary."""
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT pnl_net FROM closed_trades ORDER BY closed_at DESC LIMIT 50"
        ).fetchall()
        pnls = [r["pnl_net"] for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        db.close()
        return {
            "model_loaded": os.path.exists(MODEL_PATH),
            "recent_pnls": pnls,
            "recent_wins": wins,
            "recent_losses": losses,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: Logs (recent) ──────────────────────────────────────────
@app.get("/api/logs/recent")
def api_logs_recent(lines: int = Query(200, ge=1, le=2000)):
    """Return last N lines of the log file."""
    log_lines = _tail_file(LOG_PATH, lines)
    return {"lines": log_lines, "count": len(log_lines)}


# ── API: Logs (real-time SSE stream) ────────────────────────────
@app.get("/api/logs/stream")
async def api_logs_stream():
    """SSE stream that tails the log file in real-time."""
    async def event_generator():
        # Send initial backlog (last 50 lines)
        initial = _tail_file(LOG_PATH, 50)
        for line in initial:
            if line.strip():
                yield {"event": "log", "data": line}
        await asyncio.sleep(0.1)

        # Follow new lines via tail -F subprocess
        proc = await asyncio.create_subprocess_exec(
            "tail", "-F", "-n", "0", LOG_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if decoded:
                    yield {"event": "log", "data": decoded}
        except asyncio.CancelledError:
            pass
        finally:
            try:
                proc.terminate()
                await proc.wait()
            except Exception:
                pass

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Health check ─────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "db_exists": os.path.exists(DB_PATH),
            "log_exists": os.path.exists(LOG_PATH)}


# ── Settings validation (Amendment 002 — self-contained, no trade/ imports) ──
@app.post("/api/settings/validate")
async def api_settings_validate(request: Request):
    """Validate a setting value. Returns Verdict {level, message, suggested_value}."""
    try:
        body = await request.json()
        key = body.get("key", "")
        value = body.get("value", "")
        return {"ok": True, "verdict": _validate_setting(key, value)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/settings/analyze")
def api_settings_analyze():
    """Run full cross-validation on all settings. Returns {key: {level, message, suggested}}."""
    try:
        from trade.settings_rules import validate_all
        results = validate_all()
        verdicts = {}
        for key, v in results.items():
            entry = {"level": v.level, "message": v.message}
            if v.suggested_value is not None:
                entry["suggested"] = str(v.suggested_value)
            verdicts[key] = entry
        return {"ok": True, "verdicts": verdicts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _validate_setting(key: str, value: str) -> dict:
    """Standalone validator mirroring trade/settings_rules.py — no imports needed."""
    # Hard bounds for known settings
    bounds = {
        "tp_min_pct": (0.1, 10, "TP min %"), "sl_min_pct": (0.1, 10, "SL min %"),
        "dip_min_pct": (0.05, 5, "Dip min %"), "pump_min_pct": (0.05, 5, "Pump min %"),
        "cooldown_sec": (10, 3600, "Cooldown"), "max_slots": (1, 50, "Max slots"),
        "min_entry_spacing_pct": (0.05, 5, "Entry spacing"),
        "daily_loss_limit_pct": (0, 100, "Daily loss %"),
        "max_consecutive_losses": (0, 50, "Consec losses"),
        "leverage": (1, 10, "Leverage"), "max_leverage": (1, 10, "Max leverage"),
        "tp_k": (0.1, 5, "TP k"), "sl_k": (0.1, 5, "SL k"),
        "dip_k": (0.1, 5, "Dip k"), "pump_k": (0.1, 5, "Pump k"),
        "max_hold_minutes_spot": (5, 1440, "Spot hold"), "max_hold_minutes_futures": (5, 1440, "Futures hold"),
        "atr_period": (5, 50, "ATR period"),
    }
    if key in bounds:
        lo, hi, _ = bounds[key]
        try:
            v = float(value)
            if v < lo: return {"level": "error", "message": f"{key} = {v} below minimum {lo}"}
            if v > hi: return {"level": "error", "message": f"{key} = {v} above maximum {hi}"}
        except ValueError:
            return {"level": "error", "message": f"Invalid number: {value}"}
    # Cross-checks
    if key == "tp_min_pct":
        try:
            sl = float(_get_setting("sl_min_pct") or "0.5")
            if float(value) <= sl:
                return {"level": "error", "message": f"TP ({value}) must exceed SL ({sl})"}
        except: pass
    if key == "leverage":
        try:
            ml = float(_get_setting("max_leverage") or "3")
            if float(value) > ml:
                return {"level": "error", "message": f"Leverage ({value}x) > max ({ml}x)"}
        except: pass
    # Per-asset capital validation is handled by trade/settings_rules.py (Amendment 004)
    # Legacy dex_slot_pct/cex_slot_pct cross-check removed — replaced by asset_configs.capital_dex/capital_cex
    return {"level": "ok", "message": ""}


def _send_validation_alert(changed_key: str, changed_value: str, alerts: list[dict]):
    """Send a Telegram notification when validation finds issues after a setting change."""
    try:
        errors = [a for a in alerts if a["level"] == "error"]
        warns = [a for a in alerts if a["level"] == "warn"]

        if not errors and not warns:
            return

        lines = [f"⚠️ Setting changed: {changed_key} → {changed_value}"]
        lines.append("")

        if errors:
            lines.append("❌ Errors (trading blocked):")
            for e in errors:
                line = f"  • {e['key']}: {e['message']}"
                if e.get("suggested"):
                    line += f"  → try {e['suggested']}"
                lines.append(line)

        if warns:
            lines.append("")
            lines.append("⚠️ Warnings:")
            for w in warns:
                line = f"  • {w['key']}: {w['message']}"
                if w.get("suggested"):
                    line += f"  → try {w['suggested']}"
                lines.append(line)

        msg = "\n".join(lines)
        _send_telegram(msg)
    except Exception:
        pass  # best-effort notification


def _send_telegram(message: str):
    """Send a message to the configured Telegram chat."""
    try:
        import requests as req
        if not BOT_TOKEN or AUTHORIZED_CHAT_ID == 0:
            return
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        req.post(url, json={
            "chat_id": AUTHORIZED_CHAT_ID,
            "text": message[:4000],
            "parse_mode": "HTML",
        }, timeout=5)
    except Exception:
        pass


# ── Telegram Mini App: Auth ─────────────────────────────────────
BOT_TOKEN = os.getenv("API_TOKEN", os.getenv("BOT_TOKEN", ""))
AUTHORIZED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "556159355"))


def _validate_telegram_init_data(init_data: str) -> bool:
    """Validate Telegram Mini App initData using HMAC-SHA256."""
    if not BOT_TOKEN or not init_data:
        return False
    try:
        parsed = parse_qs(init_data)
        received_hash = parsed.pop("hash", [None])[0]
        if not received_hash:
            return False
        # Build data-check-string: sorted key=value pairs separated by \n
        parts = []
        for key in sorted(parsed.keys()):
            val = parsed[key][0]
            parts.append(f"{key}={val}")
        data_check_string = "\n".join(parts)
        # Compute HMAC-SHA256 with bot token as key
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return computed_hash == received_hash
    except Exception:
        return False


async def _validate_admin_session(request: Request) -> bool:
    """Validate browser admin session via signed cookie.

    Simple signed-cookie scheme using BOT_TOKEN as secret. Cookie value is
    'AUTHORIZED_CHAT_ID:sha256(BOT_TOKEN:timestamp):timestamp'. Valid for 24h.
    """
    if not BOT_TOKEN:
        return False
    cookie = request.cookies.get("mockba_session", "")
    if not cookie or ":" not in cookie:
        return False
    try:
        user_part, sig, ts_str = cookie.rsplit(":", 2)
        ts = int(ts_str)
    except ValueError:
        return False
    if time.time() - ts > 86400:
        return False
    expected = hmac.new(BOT_TOKEN.encode(), f"{user_part}:{ts_str}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    return user_part == str(AUTHORIZED_CHAT_ID)


@app.post("/api/admin/login")
async def api_admin_login(request: Request):
    """Issue a signed session cookie after verifying Telegram initData once."""
    init_data = request.headers.get("X-Telegram-InitData", "")
    if not _validate_telegram_init_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid Telegram auth")
    try:
        parsed = parse_qs(init_data)
        user_json = parsed.get("user", [None])[0]
        user = json.loads(unquote(user_json)) if user_json else {}
        if user.get("id") != AUTHORIZED_CHAT_ID:
            raise HTTPException(status_code=403, detail="Unauthorized user")
    except (json.JSONDecodeError, Exception):
        raise HTTPException(status_code=403, detail="Cannot parse user data")

    ts = int(time.time())
    sig = hmac.new(BOT_TOKEN.encode(), f"{AUTHORIZED_CHAT_ID}:{ts}".encode(), hashlib.sha256).hexdigest()
    response = {"ok": True}
    from starlette.responses import JSONResponse
    resp = JSONResponse(response)
    resp.set_cookie(
        key="mockba_session",
        value=f"{AUTHORIZED_CHAT_ID}:{sig}:{ts}",
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=86400,
        path="/"
    )
    return resp


def _get_db_rw() -> sqlite3.Connection:
    """Open a read-write connection for settings updates."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Mini App: Settings API (used by React MiniSettings tab) ──────
@app.get("/api/miniapp")
async def api_miniapp_get(request: Request):
    """Return all settings as key→value dict (read-only, no auth needed)."""
    try:
        db = _get_db()
        rows = db.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        db.close()
        settings = {row["key"]: row["value"] for row in rows}
        return {"ok": True, "settings": settings}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Mini App: Update a setting ───────────────────────────────────
@app.post("/api/miniapp")
async def api_miniapp_update(request: Request):
    """Update a single setting. Requires Telegram initData or a valid admin session."""
    body = await request.json()
    key = body.get("key", "").strip()
    value = str(body.get("value", "")).strip()

    if not key:
        raise HTTPException(status_code=400, detail="Missing key")

    if key == "__ping__":
        # Browser session probe: no DB write, just auth check.
        init_data = request.headers.get("X-Telegram-InitData", "")
        valid = False
        if init_data:
            valid = _validate_telegram_init_data(init_data)
        if not valid:
            valid = await _validate_admin_session(request)
        if not valid:
            raise HTTPException(status_code=403, detail="Invalid auth")
        return {"ok": True}

    init_data = request.headers.get("X-Telegram-InitData", "")
    telegram_ok = False
    if init_data:
        telegram_ok = _validate_telegram_init_data(init_data)
        if telegram_ok:
            try:
                parsed = parse_qs(init_data)
                user_json = parsed.get("user", [None])[0]
                if user_json:
                    user = json.loads(unquote(user_json))
                    if user.get("id") != AUTHORIZED_CHAT_ID:
                        telegram_ok = False
            except (json.JSONDecodeError, Exception):
                telegram_ok = False

    if not telegram_ok and not await _validate_admin_session(request):
        raise HTTPException(status_code=403, detail="Invalid auth")

    try:
        db = _get_db_rw()
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        db.commit()
        db.close()

        # ── Post-save validation ──────────────────────────────
        alerts = []
        try:
            from trade.settings_rules import validate_all
            results = validate_all()
            for k, v in results.items():
                if v.level in ("error", "warn"):
                    alerts.append({
                        "key": k,
                        "level": v.level,
                        "message": v.message,
                        "suggested": str(v.suggested_value) if v.suggested_value is not None else None,
                    })
        except Exception:
            pass  # validation is best-effort

        # ── Send Telegram alert for critical issues ───────────
        if alerts:
            _send_validation_alert(key, value, alerts)

        return {"ok": True, "key": key, "value": value, "alerts": alerts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Capital & Universe API (Amendment 003 — replaces Asset Management API) ──

@app.get("/api/capital")
async def api_capital_get():
    """Per-venue capital view.

    Declared capital (editable pool) vs live exchange equity (venue_state cache
    written by bot.py each cycle), slot sizing, deployed/free, fee + net edge.
    Live equity always wins — divergence is surfaced, never applied.
    """
    try:
        venues = []
        for venue, pool_key, pct_key, slots_key, fee_key in (
            ("binance", "capital_cex_usdt", "cex_slot_pct", "max_slots_cex", "cex_round_trip_fee_pct"),
            ("orderly", "capital_dex_usdc", "dex_slot_pct", "max_slots_dex", "dex_round_trip_fee_pct"),
        ):
            pool = _get_setting_float(pool_key, 0.0)
            st = _get_venue_equity(venue)
            equity = float(st["equity"]) if st else 0.0
            eq_age = st["updated_at"] if st else None
            slot_pct = _get_setting_float(pct_key, 10.0)
            slot = equity * slot_pct / 100 if equity > 0 else 0.0
            max_slots = _get_setting_int(slots_key, 9)
            deployed = _get_venue_deployed(venue)
            free = max(0.0, equity - deployed)
            fee = _get_setting_float(fee_key, 0.06 if venue == "orderly" else 0.20)
            tp = _get_setting_float("tp_min_pct", 0.8)
            slip = _get_setting_float("assumed_slippage_pct", 0.03)
            net_edge = tp - fee - slip
            divergence = None
            if equity > 0 and pool > 0:
                diff = abs(pool - equity) / equity
                if diff > 0.25:
                    divergence = {"declared": round(pool, 2), "live": round(equity, 2),
                                  "pct": round(diff * 100, 1)}
            mode = _get_setting("auto_trade_binance" if venue == "binance" else "auto_trade_orderly") or "False"
            venues.append({
                "venue": venue,
                "declared_capital": round(pool, 2),
                "live_equity": round(equity, 2),
                "equity_age": eq_age,
                "divergence": divergence,
                "slot_pct": slot_pct,
                "slot_size": round(slot, 2),
                "max_slots": max_slots,
                "deployed": round(deployed, 2),
                "free": round(free, 2),
                "fee_pct": fee,
                "net_edge_pct": round(net_edge, 3),
                "enabled": mode,
            })
        return {"ok": True, "venues": venues}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/universe/{venue}")
async def api_universe_get(venue: str):
    """Current universe for a venue with scan age (read-only)."""
    if venue not in ("binance", "orderly"):
        raise HTTPException(status_code=400, detail="Venue must be 'binance' or 'orderly'")
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT asset, symbol, rank, scanned_at, quote_volume_24h, spread_pct, "
            "depth_bid_top10, depth_ask_top10, atr_pct_median, signals_count, "
            "recovery_rate, median_minutes_to_tp, blacklisted "
            "FROM asset_universe WHERE venue = ? ORDER BY rank", (venue,)
        ).fetchall()
        age_row = db.execute(
            "SELECT MAX(scanned_at) AS t FROM asset_universe WHERE venue = ?", (venue,)
        ).fetchone()
        db.close()
        max_age = _get_setting_float("universe_max_age_hours", 36)
        age = float(age_row["t"]) if age_row and age_row["t"] is not None else None
        scan_age_hours = (time.time() - age) / 3600 if age else None
        return {
            "ok": True, "venue": venue,
            "rows": [dict(r) for r in rows],
            "scanned_at": age,
            "scan_age_hours": round(scan_age_hours, 2) if scan_age_hours is not None else None,
            "stale": scan_age_hours is not None and scan_age_hours > max_age,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/universe/{venue}/{asset}/blacklist")
async def api_universe_blacklist(venue: str, asset: str, request: Request):
    """Set the operator blacklist flag for an asset in a venue's universe.

    This is the only writable part of the Universe panel — assets are not
    manually added; the scanner decides the set.
    """
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    if venue not in ("binance", "orderly"):
        raise HTTPException(status_code=400, detail="Venue must be 'binance' or 'orderly'")
    body = await request.json()
    target = bool(body.get("blacklisted", False))
    asset = asset.strip().upper()
    db = _get_db_rw()
    cur = db.execute(
        "UPDATE asset_universe SET blacklisted = ? WHERE venue = ? AND asset = ?",
        (int(target), venue, asset),
    )
    db.commit()
    found = cur.rowcount > 0
    db.close()
    if not found:
        raise HTTPException(status_code=404, detail=f"{asset} not in {venue} universe")
    logger.info(f"[BLACKLIST] {venue}:{asset} blacklisted={target}")
    return {"ok": True, "venue": venue, "asset": asset, "blacklisted": target}


async def _check_auth(request: Request) -> bool:
    """Common auth check for asset endpoints."""
    init_data = request.headers.get("X-Telegram-InitData", "")
    if init_data:
        ok = _validate_telegram_init_data(init_data)
        if ok:
            try:
                parsed = parse_qs(init_data)
                uj = parsed.get("user", [None])[0]
                if uj and json.loads(unquote(uj)).get("id") != AUTHORIZED_CHAT_ID:
                    return False
            except Exception:
                return False
            return True
    return await _validate_admin_session(request)


# ── Bot Control API (start/stop) ──────────────────────────────────
VALID_MODES = {"False", "Signal", "Automatic"}


@app.post("/api/bot/control")
async def api_bot_control(request: Request):
    """Start or stop the bot for a given exchange. Requires auth.

    Body: { "exchange": "dex"|"cex", "mode": "False"|"Signal"|"Automatic" }
    """
    init_data = request.headers.get("X-Telegram-InitData", "")
    telegram_ok = False
    if init_data:
        telegram_ok = _validate_telegram_init_data(init_data)
        if telegram_ok:
            try:
                parsed = parse_qs(init_data)
                user_json = parsed.get("user", [None])[0]
                if user_json:
                    user = json.loads(unquote(user_json))
                    if user.get("id") != AUTHORIZED_CHAT_ID:
                        telegram_ok = False
            except (json.JSONDecodeError, Exception):
                telegram_ok = False

    if not telegram_ok and not await _validate_admin_session(request):
        raise HTTPException(status_code=403, detail="Invalid auth")

    body = await request.json()
    exchange = (body.get("exchange") or "").strip().lower()
    mode = (body.get("mode") or "").strip()

    if exchange not in ("dex", "cex"):
        raise HTTPException(status_code=400, detail="Exchange must be 'dex' or 'cex'")
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Mode must be one of: {', '.join(sorted(VALID_MODES))}")

    key = "auto_trade_orderly" if exchange == "dex" else "auto_trade_binance"

    try:
        db = _get_db_rw()
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, mode),
        )
        db.commit()
        db.close()

        # Read back current state for both exchanges
        current_dex = _get_setting("auto_trade_orderly") or "False"
        current_cex = _get_setting("auto_trade_binance") or "False"

        return {
            "ok": True,
            "exchange": exchange,
            "mode": mode,
            "dex_mode": current_dex,
            "cex_mode": current_cex,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Closed Trades API (read-only, month view) ─────────────────────────
CARACAS_TZ = timezone(timedelta(hours=-4))  # UTC-4, no DST since 2007 (matches dashboard-ui timezone.ts)
VENUE_LABELS = (("dex", "orderly"), ("cex", "binance"))
REASON_LABELS = {"tp": "TP", "sl": "SL", "time_stop": "Time stop", "orphan": "Orphan"}


def _caracas_month_bounds(now_ts: float | None = None) -> tuple[float, float, str]:
    """(start_epoch, end_epoch, month_label) for the current calendar month in Caracas (UTC-4).

    Membership is by close time (closed_at). End is the first instant of the next month.
    """
    now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz=CARACAS_TZ)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp(), start.strftime("%Y-%m")


def _caracas_day_bounds(now_ts: float | None = None) -> tuple[float, float, str]:
    """(start_epoch, end_epoch, label) for today in Caracas (UTC-4)."""
    now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz=CARACAS_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp(), start.strftime("%Y-%m-%d")


@app.get("/api/trades/closed")
def api_trades_closed(venue: str = Query("all"), today: bool = Query(False)):
    """Closed trades (read-only).

    - Window: current calendar month (or today when today=true), Caracas UTC-4.
    - Totals: per-venue pnl_net + count for the whole window.
    - Trades: most-recent 200, optionally narrowed by venue (venue=all|dex|cex).
    - reason mapped to human labels; pnl_net returned raw (no rounding).
    """
    if venue not in ("all", "dex", "cex"):
        raise HTTPException(status_code=400, detail="venue must be 'all', 'dex' or 'cex'")
    try:
        start, end, month = _caracas_day_bounds() if today else _caracas_month_bounds()
        db = _get_db()
        where = "closed_at >= ? AND closed_at < ?"
        args = [start, end]

        # Totals: full month, per venue (zero-filled so both cards always render)
        totals = [{"venue": lbl, "label": lbl.upper(), "pnl_net": 0.0, "count": 0, "wins": 0, "losses": 0}
                  for lbl, _ in VENUE_LABELS]
        for row in db.execute(
            "SELECT venue, COUNT(*) AS c, COALESCE(SUM(pnl_net), 0) AS p, "
            "COALESCE(SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END), 0) AS wins, "
            "COALESCE(SUM(CASE WHEN pnl_net < 0 THEN 1 ELSE 0 END), 0) AS losses "
            f"FROM closed_trades WHERE {where} GROUP BY venue",
            args,
        ):
            lbl = next((l for l, v in VENUE_LABELS if v == row["venue"]), None)
            if lbl is None:
                continue
            for t in totals:
                if t["venue"] == lbl:
                    t["pnl_net"] = float(row["p"])
                    t["count"] = int(row["c"])
                    t["wins"] = int(row["wins"])
                    t["losses"] = int(row["losses"])

        # Rows: most recent first, LIMIT 201 to detect truncation in one pass
        limit = 201
        if venue != "all":
            db_venue = dict(VENUE_LABELS)[venue]
            rows = db.execute(
                "SELECT id, asset, venue, side, entry_price, exit_price, qty, "
                "fee_entry, fee_exit, pnl_net, pnl_pct, exit_reason, closed_at "
                f"FROM closed_trades WHERE {where} AND venue = ? "
                "ORDER BY closed_at DESC LIMIT 201",
                args + [db_venue],
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, asset, venue, side, entry_price, exit_price, qty, "
                "fee_entry, fee_exit, pnl_net, pnl_pct, exit_reason, closed_at "
                f"FROM closed_trades WHERE {where} ORDER BY closed_at DESC LIMIT 201",
                args,
            ).fetchall()
        truncated = len(rows) > 200
        rows = rows[:200]

        # Running realized balance per venue (chronological) — the "account" line.
        bal_map: dict[int, float] = {}
        running: dict[str, float] = {}
        for brow in db.execute(
            "SELECT id, venue, pnl_net FROM closed_trades "
            f"WHERE {where} ORDER BY closed_at ASC",
            args,
        ):
            blbl = next((l for l, v in VENUE_LABELS if v == brow["venue"]), brow["venue"])
            running[blbl] = running.get(blbl, 0.0) + float(brow["pnl_net"])
            bal_map[brow["id"]] = running[blbl]

        trades = []
        for r in rows:
            lbl = next((l for l, v in VENUE_LABELS if v == r["venue"]), r["venue"])
            trades.append({
                "id": r["id"],
                "asset": r["asset"],
                "venue": lbl,
                "side": r["side"],
                "qty": float(r["qty"]),
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]),
                "fee_total": float(r["fee_entry"]) + float(r["fee_exit"]),
                "pnl_net": float(r["pnl_net"]),
                "pnl_pct": float(r["pnl_pct"]),
                "win": float(r["pnl_net"]) > 0,
                "balance": bal_map.get(r["id"], 0.0),
                "reason": r["exit_reason"],
                "reason_label": REASON_LABELS.get(r["exit_reason"], str(r["exit_reason"]).upper()),
                "closed_at": float(r["closed_at"]),
            })
        db.close()

        return {
            "ok": True,
            "month": month,
            "window": {
                "start": round(start, 3), "end": round(end, 3),
                "tz": "UTC-4 (Caracas)", "by": "today" if today else "close_time",
            },
            "totals": totals,
            "trades": trades,
            "truncated": truncated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Open Positions API (live unrealized PnL + current equity) ──────────────

@app.get("/api/positions/open")
def api_positions_open():
    """Open positions with live unrealized PnL (real Binance prices) + equity.

    Unrealized PnL is computed from the live ticker at request time, so it moves
    with the market ("gain + balance can vary"). Equity comes from the venue_state
    cache written by bot.py each cycle; realized_today is today's closed PnL.
    """
    try:
        db = _get_db()
        rows = db.execute("SELECT * FROM open_positions ORDER BY opened_at").fetchall()

        positions = []
        for r in rows:
            asset = r["asset"]; venue = r["venue"]
            entry = float(r["entry_price"]); qty = float(r["qty"])
            side = r["side"] or "long"
            price = _fetch_live_price(asset, venue)
            upnl = 0.0; upnl_pct = 0.0
            if price is not None and entry > 0:
                if side == "long":
                    upnl = (price - entry) * qty
                else:
                    upnl = (entry - price) * qty
                upnl_pct = upnl / (entry * qty) * 100 if entry * qty > 0 else 0.0
            positions.append({
                "asset": asset,
                "venue": next((l for l, v in VENUE_LABELS if v == venue), venue),
                "side": side,
                "qty": qty,
                "entry_price": entry,
                "tp_price": float(r["tp_price"]) if r["tp_price"] else None,
                "sl_price": float(r["sl_price"]) if r["sl_price"] else None,
                "live_price": price,
                "unrealized_pnl": round(upnl, 6),
                "pnl_pct": round(upnl_pct, 3),
                "opened_at": float(r["opened_at"]),
            })

        # Live equity (venue_state cache) + today's realized PnL
        equity = {}
        realized = {}
        for venue, lbl in (("binance", "cex"), ("orderly", "dex")):
            st = _get_venue_equity(venue)
            equity[lbl] = round(float(st["equity"]), 2) if st else 0.0
            rrow = db.execute(
                "SELECT COALESCE(SUM(pnl_net), 0) AS p FROM closed_trades WHERE venue=? "
                "AND date(datetime(closed_at, 'unixepoch')) = date('now')",
                (venue,),
            ).fetchone()
            realized[lbl] = round(float(rrow["p"]), 4) if rrow else 0.0
        db.close()

        return {
            "ok": True,
            "positions": positions,
            "equity": equity,
            "realized_today": realized,
            "fetched_at": round(time.time(), 3),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
