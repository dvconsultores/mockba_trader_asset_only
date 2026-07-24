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
            "SELECT value FROM settings WHERE key='auto_trade_dex'"
        ).fetchone()
        cex_mode = db.execute(
            "SELECT value FROM settings WHERE key='auto_trade_cex'"
        ).fetchone()
        ml_threshold_row = db.execute(
            "SELECT value FROM settings WHERE key='ml_threshold'"
        ).fetchone()
        db.close()
    except Exception:
        dex_mode, cex_mode, ml_threshold_row = None, None, None

    try:
        ml_threshold = float(ml_threshold_row["value"]) if ml_threshold_row else 0.80
    except (ValueError, TypeError):
        ml_threshold = 0.80

    # Current regime: read from latest signal in signal_history
    current_regime = None
    try:
        db = _get_db()
        regime_row = db.execute(
            "SELECT regime FROM signal_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if regime_row:
            current_regime = regime_row["regime"]
        db.close()
    except Exception:
        pass

    return {
        "uptime_seconds": round(time.time() - START_TIME),
        "dex_mode": dex_mode["value"] if dex_mode else "unknown",
        "cex_mode": cex_mode["value"] if cex_mode else "unknown",
        "ml_threshold": ml_threshold,
        "model_loaded": os.path.exists(MODEL_PATH),
        "current_regime": current_regime,
    }


# ── API: Signals ────────────────────────────────────────────────
@app.get("/api/signals")
def api_signals(limit: int = Query(50, ge=1, le=500),
                exchange: str = Query(None),
                outcome: str = Query(None)):
    """Recent signal history from signal_history table."""
    try:
        db = _get_db()
        query = "SELECT * FROM signal_history WHERE 1=1"
        params = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if outcome:
            query += " AND trade_outcome = ?"
            params.append(outcome)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()
        db.close()

        signals = []
        for row in rows:
            d = dict(row)
            # Parse JSON fields
            for field in ("rejection_reasons", "manipulation_warnings"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        d[field] = str(d[field]) if d[field] else []
            signals.append(d)
        return {"signals": signals, "count": len(signals)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/signals/{signal_id}")
def api_signal_detail(signal_id: int):
    """Full detail for one signal."""
    try:
        db = _get_db()
        row = db.execute(
            "SELECT * FROM signal_history WHERE id = ?", (signal_id,)
        ).fetchone()
        db.close()
        if row is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        d = dict(row)
        for field in ("rejection_reasons", "manipulation_warnings"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = str(d[field]) if d[field] else []
        return d
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: Daily Stats ────────────────────────────────────────────
@app.get("/api/stats/daily")
def api_daily_stats():
    """Today's signal stats."""
    try:
        db = _get_db()
        today = time.strftime("%Y-%m-%d")
        total = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE date(timestamp) = ?",
            (today,)
        ).fetchone()["c"]
        approved = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE date(timestamp) = ? AND approved = 1",
            (today,)
        ).fetchone()["c"]
        wins = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE date(timestamp) = ? AND trade_outcome = 'win'",
            (today,)
        ).fetchone()["c"]
        losses = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE date(timestamp) = ? AND trade_outcome = 'loss'",
            (today,)
        ).fetchone()["c"]
        # Avg ML score for today (non-null)
        avg_ml = db.execute(
            "SELECT AVG(ml_score) as a FROM signal_history WHERE date(timestamp) = ? AND ml_score IS NOT NULL",
            (today,)
        ).fetchone()["a"]
        db.close()
        return {
            "date": today,
            "total_signals": total,
            "approved": approved,
            "rejected": total - approved,
            "wins": wins,
            "losses": losses,
            "avg_ml_score": round(avg_ml, 4) if avg_ml else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── API: ML Info ────────────────────────────────────────────────
@app.get("/api/ml/info")
def api_ml_info():
    """ML model info + recent score distribution."""
    try:
        db = _get_db()
        # Score distribution (last 500 scored signals)
        rows = db.execute(
            "SELECT ml_score FROM signal_history WHERE ml_score IS NOT NULL ORDER BY id DESC LIMIT 500"
        ).fetchall()
        scores = [r["ml_score"] for r in rows if r["ml_score"] is not None]

        # Decision counts
        approved_count = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE ml_decision = 'approved'"
        ).fetchone()["c"]
        rejected_count = db.execute(
            "SELECT COUNT(*) as c FROM signal_history WHERE ml_decision = 'rejected'"
        ).fetchone()["c"]

        ml_threshold_row = db.execute(
            "SELECT value FROM settings WHERE key='ml_threshold'"
        ).fetchone()
        db.close()

        try:
            ml_threshold = float(ml_threshold_row["value"]) if ml_threshold_row else 0.80
        except (ValueError, TypeError):
            ml_threshold = 0.80

        # Histogram buckets
        if scores:
            hist = {}
            for bucket in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
                count = sum(1 for s in scores if bucket <= s < bucket + 0.1)
                hist[f"{bucket:.1f}-{bucket+0.1:.1f}"] = count
        else:
            hist = {}

        return {
            "threshold": ml_threshold,
            "model_loaded": os.path.exists(MODEL_PATH),
            "recent_scores": scores[-50:] if scores else [],
            "score_distribution": hist,
            "total_scored": len(scores),
            "approved_by_ml": approved_count,
            "rejected_by_ml": rejected_count,
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
AUTHORIZED_CHAT_ID = 556159355


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
    """Update a single setting. Requires valid Telegram initData + authorized user."""
    init_data = request.headers.get("X-Telegram-InitData", "")
    if not _validate_telegram_init_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid Telegram auth")

    # Extract user ID from initData
    try:
        parsed = parse_qs(init_data)
        user_json = parsed.get("user", [None])[0]
        if user_json:
            user = json.loads(unquote(user_json))
            if user.get("id") != AUTHORIZED_CHAT_ID:
                raise HTTPException(status_code=403, detail="Unauthorized user")
    except (json.JSONDecodeError, Exception):
        raise HTTPException(status_code=403, detail="Cannot parse user data")

    body = await request.json()
    key = body.get("key", "").strip()
    value = str(body.get("value", "")).strip()

    if not key:
        raise HTTPException(status_code=400, detail="Missing key")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
