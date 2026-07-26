import os
import re
import sys
import time
import threading
import importlib.util
import io
from contextlib import redirect_stdout
import re
import html
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'machine_learning')))
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.db_ops import (
    upsert_setting, get_all_settings, initialize_database_tables, get_setting,
    get_asset_list, get_setting_float, get_setting_bool,
)
import json
from datetime import timedelta
# Load environment variables
load_dotenv()
initialize_database_tables()

# Bot init
API_TOKEN = os.getenv("API_TOKEN")
bot = telebot.TeleBot(API_TOKEN)
TELEGRAM_MAX_MESSAGE_LEN = 4096





def is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False

def is_integer(value):
    try:
        int(value)
        return True
    except ValueError:
        return False


def translate(text, chat_id):
    lang = os.getenv("BOT_LANGUAGE", "en").lower()
    try:
        translated = GoogleTranslator(source='auto', target=lang).translate(text)
        return translated
    except Exception as e:
        print(f"Translation error: {e}")
        return text


def send_html_message_chunked(chat_id, message, max_len=TELEGRAM_MAX_MESSAGE_LEN):
    if not message:
        return

    lines = message.splitlines(keepends=True)
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) > max_len:
            if current_chunk:
                bot.send_message(chat_id, current_chunk, parse_mode='HTML')
                current_chunk = ""

        if len(line) > max_len:
            start = 0
            while start < len(line):
                chunk = line[start:start + max_len]
                bot.send_message(chat_id, chunk, parse_mode='HTML')
                start += max_len
        else:
            current_chunk += line

    if current_chunk:
        bot.send_message(chat_id, current_chunk, parse_mode='HTML')


def send_text_message_chunked(chat_id, message, max_len=TELEGRAM_MAX_MESSAGE_LEN):
    if not message:
        return

    lines = message.splitlines(keepends=True)
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) > max_len:
            if current_chunk:
                bot.send_message(chat_id, current_chunk)
                current_chunk = ""

        if len(line) > max_len:
            start = 0
            while start < len(line):
                chunk = line[start:start + max_len]
                bot.send_message(chat_id, chunk)
                start += max_len
        else:
            current_chunk += line

    if current_chunk:
        bot.send_message(chat_id, current_chunk)


# === Message Handlers ===

@bot.message_handler(commands=['start'])
def command_start(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    nom = m.chat.first_name
    text = translate("Welcome to Mockba! With this bot, you trade against Apolo Dex.", cid)
    bot.send_message(cid, f"{text}. {nom} - {cid}")
    command_list(m)

##
@bot.message_handler(commands=['trades'])
def command_trades(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    execute_trade_performance(m)


@bot.message_handler(commands=['list'])
def command_list(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(translate("📡 Process Signal", cid), callback_data="ProcessSignal"))
    markup.row(InlineKeyboardButton(translate("� Manage Assets", cid), callback_data="ManageAssets"))
    markup.row(InlineKeyboardButton(translate("▶️ Start / Stop Bot", cid), callback_data="StartStop"))
    markup.row(InlineKeyboardButton(translate("�📋 List All Settings", cid), callback_data="ListSettings"))
    markup.row(InlineKeyboardButton(translate("💡 Explain Setting", cid), callback_data="ExplainPrompt"))
    markup.row(InlineKeyboardButton(translate("🤖 Propose Changes", cid), callback_data="ProposeStart"))
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


@bot.message_handler(commands=['explain'])
def command_explain(m):
    """Explain a setting using LLM (cached). Usage: /explain <key>"""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    key = m.text.replace('/explain', '').strip()
    if not key or key.startswith('@'):
        bot.send_message(cid, translate("Usage: /explain <setting_key>\nExample: /explain tp_min_pct\nUse /list to see all settings.", cid))
        return
    _send_explain(cid, key)


@bot.message_handler(commands=['propose'])
def command_propose(m):
    """Generate setting proposals from measured context."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    _send_proposals(cid)


def _send_explain(cid, key):
    try:
        from research.settings_llm import explain
        from db.db_ops import get_setting_bool, get_setting
        lang = get_setting("llm_language") or "en"
        text = explain(key, lang, "100_to_1k")
        verdict_text = ""
        try:
            from trade.settings_rules import validate
            v = validate(key, get_setting(key) or "")
            if v.level != "ok":
                verdict_text = f"\n\n⚠️ Validator: {v.level.upper()} — {v.message}"
        except Exception:
            pass
        send_text_message_chunked(cid, f"💡 {key}\n{text}{verdict_text}")
    except ImportError:
        try:
            from trade.settings_schema import BY_KEY
            spec = BY_KEY.get(key)
            if spec:
                send_text_message_chunked(cid, f"💡 {key}\n{spec.short}\nType: {spec.type.__name__}, range: {spec.soft_min}–{spec.soft_max}\n(LLM helper not available)")
            else:
                bot.send_message(cid, translate(f"❌ Unknown setting: {key}. Use /list.", cid))
        except Exception:
            bot.send_message(cid, translate("❌ Settings helper not available.", cid))


def _send_proposals(cid):
    try:
        from research.settings_llm import propose
        import sqlite3, os as _os, json as _json
        # Build context summary from DB
        db_path = _os.path.join(_os.path.dirname(__file__), "data", "trading.db")
        conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
        sig_count = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
        trade_count = conn.execute("SELECT COUNT(*) as c FROM closed_trades").fetchone()["c"]
        recent_pnl = conn.execute("SELECT COALESCE(SUM(pnl_net),0) as p FROM closed_trades").fetchone()["p"]
        conn.close()
        ctx = f"Signals: {sig_count}, Trades: {trade_count}, Total PnL: ${recent_pnl:.2f}"
        if sig_count == 0:
            ctx += "\nNo measured data available — dry-run has not run."
        proposals = propose(ctx)
        if not proposals:
            bot.send_message(cid, translate("No proposals generated — configuration looks valid.", cid))
            return
        for p in proposals:
            conf_icon = {"measured": "🟢", "heuristic": "🟡", "no_basis": "⚪"}.get(p.confidence, "⚪")
            text = (
                f"{conf_icon} {p.key}: {p.current_value} → {p.proposed_value}\n"
                f"{p.reason}\nConfidence: {p.confidence}"
            )
            bot.send_message(cid, text)
    except ImportError:
        bot.send_message(cid, translate("❌ Settings helper not available.", cid))
    except Exception as e:
        bot.send_message(cid, f"❌ Error: {str(e)[:200]}")


def explain_prompt(m):
    """Show settings grouped for /explain selection."""
    cid = m.chat.id
    try:
        from trade.settings_schema import BY_KEY, GROUPS
    except ImportError:
        bot.send_message(cid, translate("❌ Settings schema not available.", cid))
        return
    markup = InlineKeyboardMarkup()
    for g in GROUPS:
        keys_in_group = [k for k, s in BY_KEY.items() if s.group == g]
        markup.row(InlineKeyboardButton(f"{g} ({len(keys_in_group)})", callback_data=f"ExplainGroup:{g}"))
    markup.row(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="List"))
    bot.send_message(cid, translate("Select a setting group:", cid), reply_markup=markup)


def propose_start(m):
    """Run propose and show results."""
    cid = m.chat.id
    bot.send_message(cid, translate("Analyzing settings...", cid))
    _send_proposals(cid)


def _show_group_keys(cid, group):
    """Show settings in a group as clickable buttons for /explain."""
    try:
        from trade.settings_schema import BY_KEY
    except ImportError:
        bot.send_message(cid, translate("❌ Settings schema not available.", cid))
        return
    markup = InlineKeyboardMarkup()
    keys = sorted([k for k, s in BY_KEY.items() if s.group == group])
    for k in keys:
        spec = BY_KEY[k]
        markup.row(InlineKeyboardButton(f"{k} ({spec.unit or '-'})", callback_data=f"ExplainKey:{k}"))
    markup.row(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="ExplainPrompt"))
    bot.send_message(cid, translate(f"Settings in {group}:", cid), reply_markup=markup)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.message.chat.type != 'private': return
    cid = call.message.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    # Good practice: Answer callback to stop loading animation
    try:
        bot.answer_callback_query(call.id)
    except:
        pass

    # Determine if we should remove buttons immediately (long tasks) or later (UI transitions)
    immediate_remove = False
    if call.data.startswith("exec_sig:") or call.data.startswith("exec_sig_dex:") or call.data.startswith("exec_sig_cex:"):
        immediate_remove = True
    if call.data.startswith("asset_select:") or call.data.startswith("asset_remove:") or call.data.startswith("bot_toggle:"):
        immediate_remove = True
    
    if immediate_remove:
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except:
            pass

    # Execute logic
    if call.data.startswith("exec_sig_dex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="dex")
    elif call.data.startswith("exec_sig_cex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="cex")
    elif call.data.startswith("exec_sig:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset)
    elif call.data == "asset_add_prompt":
        handle_asset_add_prompt(call.message)
    elif call.data.startswith("asset_select:"):
        asset = call.data.split(":", 1)[1]
        handle_asset_select(cid, asset)
        manage_assets(call.message)
    elif call.data.startswith("asset_remove:"):
        asset = call.data.split(":", 1)[1]
        handle_asset_remove(cid, asset)
        manage_assets(call.message)
    elif call.data.startswith("bot_toggle:"):
        _, exchange, mode = call.data.split(":")
        handle_bot_toggle(cid, exchange, mode)
        start_stop_menu(call.message)
    elif call.data.startswith("ExplainGroup:"):
        group = call.data.split(":", 1)[1]
        _show_group_keys(cid, group)
    elif call.data.startswith("ExplainKey:"):
        key = call.data.split(":", 1)[1]
        _send_explain(cid, key)
    else:
        options = {
            'List': command_list,
            'ListSettings': ListSettings,
            'ProcessSignal': pick_exchange_for_signal,
            'AnalyzeTradesPerforming': execute_trade_performance,
            'ManageAssets': manage_assets,
            'StartStop': start_stop_menu,
            'ExplainPrompt': explain_prompt,
            'ProposeStart': propose_start,
        }
        func = options.get(call.data)
        if func:
            func(call.message)

    # Delayed remove for UI transitions (gives time for new menu/message to appear)
    if not immediate_remove:
        time.sleep(0.5) 
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except:
            pass


# === Main Actions ===

def execute_trade_performance(m):
    if m.chat.type != 'private':
        return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        return

    import sqlite3, os as _os
    from datetime import datetime, timezone
    db_path = _os.path.join(_os.path.dirname(__file__), "data", "trading.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total_trades = conn.execute("SELECT COUNT(*) as c FROM closed_trades").fetchone()["c"]
    today_trades = conn.execute(
        "SELECT COUNT(*) as c FROM closed_trades WHERE date(datetime(closed_at, 'unixepoch')) = ?",
        (today,)
    ).fetchone()["c"]
    today_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0) as p FROM closed_trades WHERE date(datetime(closed_at, 'unixepoch')) = ?",
        (today,)
    ).fetchone()["p"]
    today_signals = conn.execute(
        "SELECT COUNT(*) as c FROM signals WHERE date(datetime(timestamp, 'unixepoch')) = ?",
        (today,)
    ).fetchone()["c"]
    entered = conn.execute(
        "SELECT COUNT(*) as c FROM signals WHERE date(datetime(timestamp, 'unixepoch')) = ? AND action='entered'",
        (today,)
    ).fetchone()["c"]
    skipped = today_signals - entered
    conn.close()

    summary = (
        f"📊 MockbaV4 Stats\\n"
        f"Total closed trades: {total_trades}\\n"
        f"Today: {today_trades} trades | PnL: ${today_pnl:.2f}\\n"
        f"Signals today: {today_signals} ({entered} entered, {skipped} skipped)"
    )
    send_text_message_chunked(cid, summary)

def pick_exchange_for_signal(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    assets = get_asset_list()
    asset = assets[0] if assets else None
    if not asset:
        bot.send_message(cid, translate("❌ No assets configured. Please add one first.", cid))
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🌐 DEX (Orderly Futures)", callback_data=f"exec_sig_dex:{asset}"),
        InlineKeyboardButton("💱 CEX (Binance Spot)", callback_data=f"exec_sig_cex:{asset}")
    )
    bot.send_message(cid, translate(f"Select exchange for {asset}:", cid), reply_markup=markup)


def execute_signal(m, asset=None, exchange=None):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if asset is None:
        assets = get_asset_list()
        asset = assets[0] if assets else None
        if not asset:
            bot.send_message(cid, translate("❌ No assets configured. Please add one first.", cid))
            return

    exchange_label = "DEX" if exchange == "dex" else "CEX" if exchange == "cex" else ""
    exchange_suffix = f" ({exchange_label})" if exchange_label else ""

    bot.send_message(cid, translate(f"Processing signal for {asset}{exchange_suffix} ...", cid))
    time.sleep(1)
    try:
        # Process signal via new scalper
        from trading_bot.spot_scalper import scalp_cycle as spot_cycle
        from trading_bot.executor import BinanceSpot
        from trade.regime import detect_regime
        exchange_obj = BinanceSpot()
        regime = detect_regime(asset, "binance")
        obi = 1.0  # placeholder — full signal processing needs OB fetch
        import requests
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={asset}USDT", timeout=5)
            price = float(r.json()["price"])
        except Exception:
            price = 0.0
        action = spot_cycle(asset, exchange_obj, regime, obi, price)
        result = f"Asset: {asset}\nRegime: {regime}\nAction: {action or 'no signal'}"
    except Exception as e:
        result = f"Error: {str(e)}"

    # Send plain text result using Telegram-safe chunking (no truncation)
    try:
        result_str = str(result)
        header = translate(f"Signal processed for {asset}. Result:", cid)
        full_message = f"{header}\n\n{result_str}"
        send_text_message_chunked(cid, full_message)
    except Exception as e:
        bot.send_message(cid, translate(f"Signal processed but error displaying result: {str(e)}", cid))
         

def manage_assets(m):
    """Show current asset list with options to add, remove or select."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    from db.db_ops import get_asset_list as _gal, get_setting as _gs
    assets = _gal()
    current = _gs("current_asset") or ""

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(translate("➕ Add Asset", cid), callback_data="asset_add_prompt"))

    if assets:
        for a in assets:
            label = f"{'✅ ' if a == current else ''}{a}"
            markup.row(InlineKeyboardButton(label, callback_data=f"asset_select:{a}"))
            markup.row(
                InlineKeyboardButton(translate("❌ Remove", cid), callback_data=f"asset_remove:{a}"),
            )

    markup.row(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="List"))
    bot.send_message(cid, translate(f"📦 Assets (active: {current or 'none'})", cid), reply_markup=markup)


def start_stop_menu(m):
    """Show current bot mode and start/stop toggles."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    dex_mode = "true" if get_setting_bool("auto_trade_orderly", False) else "False"
    cex_mode = "true" if get_setting_bool("auto_trade_binance", False) else "False"

    dex_label = "🟢 DEX: ON" if dex_mode != "False" else "🔴 DEX: OFF"
    cex_label = "🟢 CEX: ON" if cex_mode != "False" else "🔴 CEX: OFF"

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(
        f"{'⏹ Stop' if dex_mode != 'False' else '▶️ Start'} DEX",
        callback_data=f"bot_toggle:dex:{'False' if dex_mode != 'False' else 'Automatic'}"
    ))
    markup.row(InlineKeyboardButton(
        f"{'⏹ Stop' if cex_mode != 'False' else '▶️ Start'} CEX",
        callback_data=f"bot_toggle:cex:{'False' if cex_mode != 'False' else 'Automatic'}"
    ))
    markup.row(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="List"))

    msg = translate(f"▶️ Bot Control\n\n{dex_label}\n{cex_label}", cid)
    bot.send_message(cid, msg, reply_markup=markup)


def handle_asset_add_prompt(m):
    """Prompt user to send the asset name."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    msg = bot.send_message(cid, translate("📝 Send the asset name to add (e.g., PERP_NEAR_USDC):", cid),
                           reply_markup=telebot.types.ForceReply(selective=True))
    bot.register_next_step_handler(msg, handle_asset_add_reply)


def handle_asset_add_reply(m):
    """Process the asset name from user reply."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    asset = (m.text or "").strip()
    if not asset:
        bot.send_message(cid, translate("❌ Invalid asset name.", cid))
        return

    from db.db_ops import add_asset as _aa, get_asset_list as _gal
    _aa(asset)
    bot.send_message(cid, translate(f"✅ Asset '{asset}' added. Total: {len(_gal())}", cid))
    manage_assets(m)


def handle_asset_select(cid: int, asset: str):
    """Set the current active asset."""
    from db.db_ops import upsert_setting
    upsert_setting("current_asset", asset)
    bot.send_message(cid, translate(f"✅ Active asset set to: {asset}", cid))


def handle_asset_remove(cid: int, asset: str):
    """Remove an asset from the list."""
    from db.db_ops import remove_asset as _ra, get_asset_list as _gal, get_setting as _gs, upsert_setting
    _ra(asset)
    current = _gs("current_asset") or ""
    if current == asset:
        remaining = _gal()
        new_current = remaining[0] if remaining else ""
        upsert_setting("current_asset", new_current)
    bot.send_message(cid, translate(f"✅ Asset '{asset}' removed.", cid))


def handle_bot_toggle(cid: int, exchange: str, mode: str):
    """Toggle auto_trade for an exchange."""
    key = "auto_trade_dex" if exchange == "dex" else "auto_trade_cex"
    from db.db_ops import upsert_setting
    upsert_setting(key, mode)
    status = "ON" if mode != "False" else "OFF"
    ex_label = "DEX" if exchange == "dex" else "CEX"
    bot.send_message(cid, translate(f"✅ {ex_label} is now {status} (mode: {mode})", cid))
    if m.chat.type != 'private':
        return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        return
    
    settings = get_all_settings()
    
    # Add defaults for missing important settings
    if 'prompt_mode' not in settings:
        settings['prompt_mode'] = os.getenv('PROMPT_MODE', 'mixed')
    
    if not settings:
        bot.send_message(cid, "❌ No settings configured")
        return
    
    # Build plain text message (safe for long content + chunking)
    message_lines = []
    message_lines.append("⚙️ BOT SETTINGS\n")
    message_lines.append("═══════════════════\n\n")
    
    # Shared settings
    message_lines.append("📋 Shared Settings:\n")
    shared_keys = [
        ("🌟", "current_asset", "Current Asset"),
        ("⏱️", "interval", "Interval"),
        ("🎯", "take_profit", "Take Profit"),
        ("🧠", "ml_threshold", "ML Threshold"),
    ]
    for emoji, key, label in shared_keys:
        if key in settings:
            value = settings[key]
            if key == "take_profit":
                value = f"{value}%"
            message_lines.append(f"{emoji} {label}: {value}\n")

    dex_mode = settings.get("auto_trade_dex", "False")
    cex_mode = settings.get("auto_trade_cex", "False")
    message_lines.append(f"🤖 Auto Mode DEX: {dex_mode}\n")
    message_lines.append(f"🤖 Auto Mode CEX: {cex_mode}\n")
    
    # DEX-only settings
    message_lines.append("\n🌐 DEX Only (Orderly Futures):\n")
    dex_keys = [
        ("🛡️", "stop_loss", "Stop Loss"),
        ("⚠️", "risk_level", "Risk Level"),
        ("💰", "capital_usage", "Capital Usage"),
        ("⚖️", "leverage", "Leverage"),
    ]
    for emoji, key, label in dex_keys:
        if key in settings:
            value = settings[key]
            if key == "stop_loss":
                value = f"{value}% (min floor, ATR-adjusted)"
            elif key == "risk_level":
                value = f"{value}% of balance"
            elif key == "capital_usage":
                value = f"{value}% of buying power"
            elif key == "leverage":
                value = f"{value}x"
            message_lines.append(f"{emoji} {label}: {value}\n")
    
    # CEX-only settings
    message_lines.append("\n💱 CEX Only (Binance Spot):\n")
    message_lines.append("🟢 Mode: BUY only (long-only)\n")
    cex_capital = settings.get("cex_capital", "10")
    message_lines.append(f"💵 Capital per position: {cex_capital} USDT\n")

    # Grid scalper settings (CEX, RANGE regime)
    grid_keys = [
        ("📉", "grid_obi_buy", "Grid OBI Buy"),
        ("📈", "grid_obi_sell", "Grid OBI Sell"),
        ("🎯", "grid_tp_pct", "Grid TP"),
        ("⏱️", "grid_cooldown_sec", "Grid Cooldown"),
        ("📊", "grid_price_dip_pct", "Grid Price Dip %"),
        ("📦", "grid_max_positions", "Grid Max Positions"),
    ]
    has_grid = any(k in settings for _, k, _ in grid_keys)
    if has_grid:
        message_lines.append("\n📊 Grid Scalper (CEX RANGE regime):\n")
        for emoji, key, label in grid_keys:
            if key in settings:
                value = settings[key]
                if key in ("grid_tp_pct", "grid_price_dip_pct"):
                    value = f"{value}%"
                elif key == "grid_cooldown_sec":
                    value = f"{value}s"
                message_lines.append(f"{emoji} {label}: {value}\n")
    
    # Add timestamp
    from datetime import datetime
    timestamp = datetime.now().strftime("%H:%M:%S")
    message_lines.append(f"\n⏰ Updated: {timestamp} | Total: {len(settings)} settings")
    
    message = "".join(message_lines)
    send_text_message_chunked(cid, message)



# Start polling
if __name__ == "__main__":
    bot.polling()