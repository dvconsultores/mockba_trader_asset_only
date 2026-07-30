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
from pathlib import Path
from urllib.parse import unquote, parse_qs

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


# ── Asset config helpers (Amendment 004) ───────────────────────────

def _get_asset_configs() -> list[dict]:
    """Return all asset_configs rows."""
    db = _get_db()
    rows = db.execute(
        "SELECT symbol, capital_dex, capital_cex, active_dex, active_cex "
        "FROM asset_configs ORDER BY symbol"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _get_asset_open_position_count(symbol: str) -> int:
    """Count open positions for an asset across both venues."""
    db = _get_db()
    row = db.execute(
        "SELECT COUNT(*) as c FROM open_positions WHERE asset = ?", (symbol,)
    ).fetchone()
    db.close()
    return row["c"] if row else 0


def _upsert_asset_config_inline(symbol: str, capital_dex: float = 0.0,
                                 capital_cex: float = 0.0, active_dex: bool = False,
                                 active_cex: bool = False):
    """Insert or update asset_config row."""
    db = _get_db_rw()
    db.execute(
        "INSERT INTO asset_configs (symbol, capital_dex, capital_cex, active_dex, active_cex) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
        "capital_dex = excluded.capital_dex, capital_cex = excluded.capital_cex, "
        "active_dex = excluded.active_dex, active_cex = excluded.active_cex, "
        "updated_at = datetime('now')",
        (symbol, capital_dex, capital_cex, int(active_dex), int(active_cex)),
    )
    db.commit()
    db.close()


def _delete_asset_config_inline(symbol: str):
    """Delete an asset_config row."""
    db = _get_db_rw()
    db.execute("DELETE FROM asset_configs WHERE symbol = ?", (symbol,))
    db.commit()
    db.close()


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
        return {"ok": True, "key": key, "value": value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Asset Management API (Amendment 004 — multi-asset) ───────────────

@app.get("/api/assets")
async def api_assets_get():
    """Return all asset_configs rows with open position counts and allocation summary."""
    try:
        configs = _get_asset_configs()
        assets = []
        for c in configs:
            assets.append({
                "symbol": c["symbol"],
                "capital_dex": float(c.get("capital_dex", 0) or 0),
                "capital_cex": float(c.get("capital_cex", 0) or 0),
                "active_dex": bool(c.get("active_dex", 0)),
                "active_cex": bool(c.get("active_cex", 0)),
                "open_positions": _get_asset_open_position_count(c["symbol"]),
            })
        summary = []
        for vk, vl in [("dex", "orderly"), ("cex", "binance")]:
            total = sum(a[f"capital_{vk}"] for a in assets if a[f"active_{vk}"] and a[f"capital_{vk}"] > 0)
            active = sum(1 for a in assets if a[f"active_{vk}"] and a[f"capital_{vk}"] > 0)
            # Exchange equity query requires API credentials — not available in dashboard context.
            # The bot's startup validation gate checks overallocation via settings_rules.py.
            summary.append({
                "venue": vl,
                "total_allocated": round(total, 2),
                "active_pairs": active,
                "remaining": None,
                "balance_error": "Equity query unavailable in dashboard — use Telegram bot for balance-checked saves",
            })
        return {"ok": True, "assets": assets, "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/assets")
async def api_assets_add(request: Request):
    """Add an asset configuration. Requires auth."""
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    body = await request.json()
    symbol = (body.get("symbol") or "").strip()
    if not symbol: raise HTTPException(status_code=422, detail="Symbol is required")
    cd, cc = float(body.get("capital_dex", 0) or 0), float(body.get("capital_cex", 0) or 0)
    ad, ac = bool(body.get("active_dex", False)), bool(body.get("active_cex", False))
    if cd < 0 or cc < 0: raise HTTPException(status_code=422, detail="Capital must be >= 0")
    configs = _get_asset_configs()
    if any(c["symbol"] == symbol for c in configs):
        raise HTTPException(status_code=409, detail=f"Asset {symbol} already exists")
    _upsert_asset_config_inline(symbol, cd, cc, ad, ac)
    return {"ok": True, "symbol": symbol, "capital_dex": cd, "capital_cex": cc,
            "active_dex": ad, "active_cex": ac, "open_positions": 0}


@app.put("/api/assets/{symbol}")
async def api_assets_edit(symbol: str, request: Request):
    """Edit an existing asset configuration. Partial update."""
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    symbol = symbol.strip()
    configs = _get_asset_configs()
    ex = next((c for c in configs if c["symbol"] == symbol), None)
    if ex is None: raise HTTPException(status_code=404, detail=f"Asset {symbol} not found")
    body = await request.json()
    cd = float(body.get("capital_dex", ex.get("capital_dex", 0) or 0))
    cc = float(body.get("capital_cex", ex.get("capital_cex", 0) or 0))
    ad = bool(body.get("active_dex", ex.get("active_dex", 0)))
    ac = bool(body.get("active_cex", ex.get("active_cex", 0)))
    if cd < 0 or cc < 0: raise HTTPException(status_code=422, detail="Capital must be >= 0")
    if not ad and not ac and cd == 0 and cc == 0:
        oc = _get_asset_open_position_count(symbol)
        if oc > 0:
            raise HTTPException(status_code=409,
                detail=f"Cannot remove {symbol} — {oc} open position(s). Deactivate first.")
    _upsert_asset_config_inline(symbol, cd, cc, ad, ac)
    return {"ok": True, "symbol": symbol, "capital_dex": cd, "capital_cex": cc,
            "active_dex": ad, "active_cex": ac, "open_positions": _get_asset_open_position_count(symbol)}


@app.delete("/api/assets/{symbol}")
async def api_assets_remove(symbol: str, request: Request):
    """Remove an asset configuration. Blocked if open positions exist."""
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    symbol = symbol.strip()
    configs = _get_asset_configs()
    if not any(c["symbol"] == symbol for c in configs):
        raise HTTPException(status_code=404, detail=f"Asset {symbol} not found")
    oc = _get_asset_open_position_count(symbol)
    if oc > 0:
        raise HTTPException(status_code=409,
            detail=f"Cannot remove {symbol} — {oc} open position(s). Deactivate first.")
    _delete_asset_config_inline(symbol)
    remaining = [c["symbol"] for c in _get_asset_configs()]
    return {"ok": True, "removed": symbol, "assets": remaining}


# ── Asset Validation & Force-Save (Amendment 004) ─────────────────

@app.post("/api/assets/validate")
async def api_assets_validate(request: Request):
    """Dry-run validation for asset config without saving. Requires auth.

    Body: { symbol, capital_dex, capital_cex, active_dex, active_cex }
    Returns: { ok, warnings: [{field, message}], errors: [{field, message}] }
    """
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    body = await request.json()
    symbol = (body.get("symbol") or "").strip()
    cd = float(body.get("capital_dex", 0) or 0)
    cc = float(body.get("capital_cex", 0) or 0)
    ad = bool(body.get("active_dex", False))
    ac = bool(body.get("active_cex", False))

    warnings = []
    errors = []

    if not symbol:
        errors.append({"field": "symbol", "message": "Symbol is required"})
    if cd < 0:
        errors.append({"field": "capital_dex", "message": "Capital must be >= 0"})
    if cc < 0:
        errors.append({"field": "capital_cex", "message": "Capital must be >= 0"})
    if ad and cd == 0:
        warnings.append({"field": "capital_dex", "message": "DEX active but capital is $0"})
    if ac and cc == 0:
        warnings.append({"field": "capital_cex", "message": "CEX active but capital is $0"})

    # Check for duplicate symbol (skip if editing existing)
    existing = body.get("is_edit")
    if not existing:
        configs = _get_asset_configs()
        if any(c["symbol"] == symbol for c in configs):
            errors.append({"field": "symbol", "message": f"Asset '{symbol}' already exists"})

    # Overallocation check (best-effort — exchange balance query may fail)
    try:
        configs = _get_asset_configs()
        total_dex = sum(float(c.get("capital_dex", 0) or 0) for c in configs if c.get("active_dex") and c["symbol"] != symbol)
        total_cex = sum(float(c.get("capital_cex", 0) or 0) for c in configs if c.get("active_cex") and c["symbol"] != symbol)
        if ad:
            total_dex += cd
        if ac:
            total_cex += cc
        # Warn if total > 0 but actual balance check requires exchange API
        if total_dex > 0 and ad:
            warnings.append({"field": "capital_dex", "message": f"Total DEX allocation would be ${total_dex:,.0f} — verify against exchange balance"})
        if total_cex > 0 and ac:
            warnings.append({"field": "capital_cex", "message": f"Total CEX allocation would be ${total_cex:,.0f} — verify against exchange balance"})
    except Exception:
        warnings.append({"field": "_global", "message": "Could not verify total allocation — exchange balance query unavailable"})

    return {"ok": len(errors) == 0, "warnings": warnings, "errors": errors}


@app.post("/api/assets/{symbol}/force-save")
async def api_assets_force_save(symbol: str, request: Request):
    """Save asset config bypassing balance check. Requires auth.

    Logs the override prominently. Use only when balance query fails (Constitution IV emergency).
    """
    _a = await _check_auth(request)
    if not _a: raise HTTPException(status_code=403, detail="Invalid auth")
    symbol = symbol.strip()
    body = await request.json()
    cd = float(body.get("capital_dex", 0) or 0)
    cc = float(body.get("capital_cex", 0) or 0)
    ad = bool(body.get("active_dex", False))
    ac = bool(body.get("active_cex", False))
    if cd < 0 or cc < 0:
        raise HTTPException(status_code=422, detail="Capital must be >= 0")

    configs = _get_asset_configs()
    ex = next((c for c in configs if c["symbol"] == symbol), None)

    _upsert_asset_config_inline(symbol, cd, cc, ad, ac)
    logger.warning(f"[FORCE-SAVE] Asset '{symbol}' saved without balance check. "
                   f"capital_dex={cd}, capital_cex={cc}, active_dex={ad}, active_cex={ac}")

    return {
        "ok": True,
        "symbol": symbol,
        "capital_dex": cd, "capital_cex": cc,
        "active_dex": ad, "active_cex": ac,
        "force_saved": True,
        "open_positions": _get_asset_open_position_count(symbol),
    }


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
