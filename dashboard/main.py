"""
Mockba Dashboard API — FastAPI backend
Serves: SSE log stream, signal history, ML stats, bot status, Mini App settings
"""
import asyncio
import hashlib
import hmac
import json
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


def _get_asset_list() -> list:
    """Returns the asset setting as a list of strings."""
    val = _get_setting("assets")
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def _add_asset(asset: str):
    """Adds an asset to the list if not present."""
    assets = _get_asset_list()
    if asset not in assets:
        assets.append(asset)
        _upsert_setting("assets", ",".join(assets))


def _remove_asset(asset: str):
    """Removes an asset from the list."""
    assets = _get_asset_list()
    if asset in assets:
        assets.remove(asset)
        _upsert_setting("assets", ",".join(assets))


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


# ── Asset Management API ─────────────────────────────────────────
@app.get("/api/assets")
async def api_assets_get():
    """Return asset list and current active asset."""
    try:
        assets = _get_asset_list()
        current = assets[0] if assets else ""
        return {"ok": True, "assets": assets, "current_asset": current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/assets")
async def api_assets_add(request: Request):
    """Add an asset to the list. Requires auth."""
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
    asset = (body.get("asset") or "").strip()
    if not asset:
        raise HTTPException(status_code=400, detail="Missing asset name")

    try:
        _add_asset(asset)
        assets = _get_asset_list()
        return {"ok": True, "assets": assets, "added": asset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/assets/{asset_name}")
async def api_assets_remove(asset_name: str, request: Request):
    """Remove an asset from the list. Requires auth."""
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

    asset_name = asset_name.strip()
    if not asset_name:
        raise HTTPException(status_code=400, detail="Missing asset name")

    try:
        _remove_asset(asset_name)
        # If the removed asset was the first one, the first asset changes naturally
        remaining = _get_asset_list()
        assets = _get_asset_list()
        return {"ok": True, "assets": assets, "removed": asset_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/assets/select")
async def api_assets_select(request: Request):
    """Set the active (current) asset. Requires auth."""
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
    asset = (body.get("asset") or "").strip()
    if not asset:
        raise HTTPException(status_code=400, detail="Missing asset name")

    # Validate asset exists in the list
    assets = _get_asset_list()
    if asset not in assets:
        raise HTTPException(status_code=400, detail=f"Asset '{asset}' not in list. Add it first.")

    try:
        # Asset is already in list — no need to set "current", just return
        return {"ok": True, "current_asset": asset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
