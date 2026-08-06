# Mockba Trader (MockbaV4)

A mean-reversion / scalping **crypto trading bot** that trades **Binance Spot** (CEX) and **Orderly DEX futures** (DEX) using adaptive, volatility-based thresholds, toxicity filters, and persistent position management. It ships with a **Telegram control bot** and a **web dashboard** (FastAPI + React mini app).

> ⚠️ **Risk warning**: This bot places real orders when trading is enabled. It is open-source software — use it entirely at your own risk.

---

## Highlights

- **Universe-driven trading** — the tradable set comes from a daily scan (`asset_universe`), not a static list.
- **Regime detection** — entries only in `RANGE` / `TREND_UP`; `TREND_DOWN` blocks new entries.
- **Adaptive thresholds** — dip / pump / TP / SL scale with ATR volatility (`dip_k`, `pump_k`, `tp_k`, `sl_k`).
- **Toxicity filters** — velocity / spread / depth / OBI checks (observe-only by default).
- **Equity-based position sizing** — slot size compounds automatically from live equity, per venue.
- **Kill switches** — daily loss limit, consecutive-loss limit, and a global trading kill switch.
- **Binance Spot** — market buy + **OCO** TP/SL (one-cancels-the-other), with **real fill prices and real fees** recorded.
- **Orderly DEX futures** — bracket orders (entry + TP + SL) with leverage.
- **Real trade data** — closed trades store actual Binance fills/commissions; the dashboard shows a running balance, win/loss stats, open positions with **live unrealized PnL**, and live equity.
- **Telegram bot** — commands, notifications, and settings control.
- **Web dashboard** — live log stream (SSE), signals, capital/equity view, status, trades, settings.
- **Docker** — `mockba.yml` runs the bot, dashboard API, dashboard UI, and Watchtower auto-updates.

---

## Project structure

| Path | Purpose |
|---|---|
| `bot.py` | Main trading loop (universe scan, regime, exits, entries, equity, kill switches). |
| `telegram.py` | Telegram bot: control, notifications, settings. Initializes the database on start. |
| `forever.py` | Process supervisor — runs `telegram.py` + `bot.py` and restarts them on exit (Docker `CMD`). |
| `mockba.yml` | Docker Compose: bot + dashboard API + dashboard UI + Watchtower. |
| `reset-mockba.sh` | Stop, remove, rebuild and restart all Docker services. |
| `trading_bot/` | Executors (`executor.py` — `BinanceSpot`, `OrderlyFutures`) and scalpers (`spot_scalper.py`, `futures_scalper.py`), `send_bot_message.py`, `types.py`. |
| `trade/` | `pnl.py` (PnL tracking, kill switches, equity sizing), `regime.py`, `toxicity.py`, `universe.py` (scanner), `settings_rules.py` / `settings_schema.py` (validation). |
| `db/` | `db_ops.py` (SQLite CRUD), `schema_v2.sql`, `migrations/` (idempotent schema upgrades). |
| `dashboard/` | FastAPI backend (signals, capital, closed trades, open positions, SSE logs, settings). |
| `dashboard-ui/` | React + Vite + Tailwind mini app (Live, Signals, Capital, Status, Trades, Settings). |
| `logs/` | `log_config.py` (rotating `apolo_trader_logger`). |
| `research/` | LLM helpers (`settings_llm.py`). |
| `tests/` | Test suite (amendments + dashboard pages). |
| `.github/workflows/` | CI — builds and pushes the Docker images to Docker Hub on push to `main`. |

---

## Prerequisites

- **Python 3.11+** (or Docker + Docker Compose)
- **Binance API keys** — required for CEX trading (spot)
- **Orderly account keys** — required for DEX futures trading (optional)
- **Telegram bot token + chat ID** — for the Telegram bot (optional but recommended)
- **DeepSeek API key** — for optional LLM analysis

---

## Configuration (`.env`)

Create a `.env` file in the project root with the variables used by the bot, dashboard, and Telegram:

```env
# ── Binance Spot (CEX) ─────────────────────────────
BINANCE_API_KEY=your_binance_api_key
BINANCE_SECRET_KEY=your_binance_secret_key

# ── Orderly DEX Futures (DEX, optional) ────────────
ORDERLY_ACCOUNT_ID=your_orderly_account_id
ORDERLY_PUBLIC_KEY=your_orderly_public_key
ORDERLY_SECRET=your_orderly_secret
# ORDERLY_BASE_URL=https://api-evm.orderly.org

# ── Telegram ───────────────────────────────────────
API_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
# BOT_LANGUAGE=en

# ── LLM analysis (optional) ────────────────────────
DEEP_SEEK_API_KEY=your_deepseek_api_key

# ── Dashboard mini app (optional) ──────────────────
MINI_APP_URL=https://your-mini-app-url
```

| Variable | Required for | Purpose |
|---|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | CEX trading | Binance Spot API credentials. |
| `ORDERLY_ACCOUNT_ID` / `ORDERLY_PUBLIC_KEY` / `ORDERLY_SECRET` | DEX trading | Orderly futures credentials (`ORDERLY_BASE_URL` optional). |
| `API_TOKEN` | Telegram | Telegram bot token (also used by the dashboard auth). |
| `TELEGRAM_CHAT_ID` | Telegram | Authorized chat ID for commands/notifications. |
| `BOT_LANGUAGE` | — | Bot language (default `en`). |
| `DEEP_SEEK_API_KEY` (or `DEEPSEEK_API_KEY`) | LLM | Optional LLM trade analysis. |
| `MINI_APP_URL` | Dashboard | Mini-app URL passed to the bot container. |

Dashboard container environment (overridable): `DB_PATH` (default `/app/data/trading.db`), `LOG_PATH` (default `/app/apolo.log`), `MODEL_PATH` (default `/app/data/signal_model.json`).

---

## Running

### Option 1 — Docker (recommended)

```bash
# Start all services (builds images if needed)
docker compose -f mockba.yml up -d --build

# View logs
docker compose -f mockba.yml logs -f

# Stop
docker compose -f mockba.yml down
```

**Full reset / rebuild** (stops, removes containers + images, prunes cache, rebuilds):

```bash
./reset-mockba.sh
```

`mockba.yml` starts four services:
- `micro-mockba-asset-futures-bot` — the bot (`forever.py` → `telegram.py` + `bot.py`).
- `mockba-dashboard-api` — FastAPI backend, exposed on port **8080**.
- `mockba-dashboard-ui` — React mini app, exposed on port **86**.
- `watchtower` — automatically pulls and restarts updated images (labeled containers).

### Option 2 — Direct (Python)

```bash
pip install -r requirements.txt

# Telegram bot (also initializes the SQLite database)
python telegram.py

# Trading loop (separate terminal)
python bot.py

# Or run both under the supervisor
python forever.py
```

---

## Dashboard

- **API**: `http://<host>:8080`
- **UI**: `http://<host>:86` (or as a Telegram Mini App)

Main endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Bot uptime and DEX/CEX modes. |
| `GET /api/signals` | Recent signal history. |
| `GET /api/stats/daily` | Today's signals / closed trades / net PnL. |
| `GET /api/capital` | Declared capital vs live exchange equity, slot sizing, free/deployed. |
| `GET /api/trades/closed` | Month-to-date closed trades with running balance, win/loss and fees. |
| `GET /api/positions/open` | Open positions with **live unrealized PnL**, equity and realized-today. |
| `GET /api/logs/stream` | Real-time log stream (SSE). |
| `GET /api/universe/{venue}` | Current tradable universe with scan age. |
| `POST /api/bot/control` | Start/stop trading per exchange (auth required). |
| `GET /api/health` | Health check. |

UI tabs: **Live** (log stream), **Signals**, **Capital**, **Status**, **Trades**, **Settings**.

---

## Telegram bot

| Command | Purpose |
|---|---|
| `/start` | Show main menu. |
| `/list` | List open positions. |
| `/trades` | Recent closed trades / stats. |
| Settings keyboards | Toggle dry-run, adjust thresholds, kill switches, etc. |

Only the authorized chat (`TELEGRAM_CHAT_ID`) can issue commands.

---

## How the bot trades

Each cycle (`bot.py`) does:

1. Refresh settings + validation; fetch per-venue equity → cached in `venue_state`.
2. For every asset in the universe (plus any asset with an open position):
   - **Manage exits first** — spot: check TP/SL fills, price-based SL, time-stop, and stale-position cleanup (real Binance balance); futures: bracket-order exits.
   - Detect the **regime**; block entries in `TREND_DOWN`.
   - Run **toxicity** checks (observe-only by default).
   - Compute **adaptive thresholds** from ATR (dip/pump/TP/SL).
   - Gate entries: universe freshness, max pairs, max concurrent positions, per-pair kill switches, cooldown, entry spacing, spread degradation.
   - Enter: **Binance** → market buy + **OCO** (TP limit + SL stop); **Orderly** → bracket order (entry + TP + SL).
3. Persist positions / closed trades / signals to SQLite and notify via Telegram.

### Binance Spot specifics

- Entry: market buy, then a single **OCO** order (TP leg = `LIMIT_MAKER`, SL leg = `STOP_LOSS`) — one fills, the other is auto-cancelled by Binance.
- **Real data**: entry/exit commissions are read from Binance fills; TP exits use the real fill from `myTrades`.
- **Stale-position cleanup**: if a market sell fails with `-2010 insufficient balance` and the account has no balance for the asset, the position is closed as `tp` (real fill) or `orphan` instead of retrying forever.

---

## Database

SQLite (single file, default `data/trading.db`). Main tables:

| Table | Purpose |
|---|---|
| `settings` | All tunables (dry-run, thresholds, capital, kill switches, modes). |
| `asset_universe` | Per-venue tradable set from the daily scan. |
| `venue_state` | Live equity cache written by the bot each cycle. |
| `open_positions` | Open positions incl. `tp_order_id` / `sl_order_id` / `fee_entry`. |
| `closed_trades` | Closed trades with real entry/exit price, fees, `pnl_net`, `pnl_pct`. |
| `signals` | Full signal history (entered / skipped / reasons / TP/SL). |

Schema upgrades are applied idempotently from `db/migrations/*.sql` on startup.

Key settings:

- **Modes**: `auto_trade_binance`, `auto_trade_orderly` (`False` / `Signal` / `Automatic`), `dry_run`, `trading_enabled`.
- **Risk**: `daily_loss_limit`, `daily_loss_limit_pct`, `max_consecutive_losses`, `global_daily_loss_limit(_pct)`, `max_concurrent_positions`, `max_active_pairs`.
- **Entry**: `cooldown_sec`, `min_entry_spacing_pct`, `max_slots_cex`, `max_hold_minutes_spot/futures`.
- **Adaptive**: `adaptive_enabled`, `atr_period`, `atr_interval`, `dip_k/dip_min_pct`, `pump_k/pump_min_pct`, `tp_k/tp_min_pct`, `sl_k/sl_min_pct`.
- **Toxicity**: `tox_velocity_enforce`, `tox_spread_enforce`, `tox_depth_enforce`, `tox_obi_enforce` (+ `max_extreme_velocity_pct`, `spread_z_max`, `depth_ratio_min`, `obi_z_max`).
- **Capital**: `capital_cex_usdt`, `capital_dex_usdc`, `cex_slot_pct`, `dex_slot_pct`, `cex_round_trip_fee_pct`, `dex_round_trip_fee_pct`, `assumed_slippage_pct`, `min_net_edge_pct`.

---

## CI / CD

`.github/workflows/build-main.yml` builds and pushes the three Docker images to Docker Hub (`andresdom2004/micro-mockba-asset-futures-bot`, `mockba-dashboard`, `mockba-dashboard-ui`) on every push to `main`, and can be triggered manually from the **Actions** tab (workflow_dispatch).

---

## Troubleshooting

- **`-2010 insufficient balance` on SL placement** — the bot now uses **OCO** (TP+SL together); this error should no longer occur on new entries.
- **`-2010` on market sell / `time_stop` loop** — a stale position whose coins were already sold. The bot now detects it (no balance) and closes it as `tp`/`orphan` automatically. Restart the bot to pick this up.
- **Nothing trades** — check `dry_run`, `auto_trade_binance/orderly`, `trading_enabled`, universe freshness (`/api/universe/{venue}`), and the kill switches.
- **Import errors** — `pip install -r requirements.txt`; clear `__pycache__` if needed.
- **Docker build not triggering on GitHub Actions** — the "job was not acquired by Runner" message is a GitHub infrastructure issue; re-run the job or trigger it manually via `workflow_dispatch`.

---

## License

MIT — open source. Use at your own risk.


## Enjoy...
