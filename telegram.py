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
)
from trade.main import process_signal as run_process_signal, autotrade
import json
from datetime import timedelta
# Load environment variables
load_dotenv()
initialize_database_tables()

# Bot init
API_TOKEN = os.getenv("API_TOKEN")
bot = telebot.TeleBot(API_TOKEN)
TELEGRAM_MAX_MESSAGE_LEN = 4096


def _load_analyze_trade_performance():
    module_path = os.path.join(
        os.path.dirname(__file__),
        "trade",
        "performance-llm.py",
    )
    spec = importlib.util.spec_from_file_location("performance_llm", module_path)
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "analyze_trade_performance", None)


analyze_trade_performance = _load_analyze_trade_performance()


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
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


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
    else:
        options = {
            'List': command_list,
            'ListSettings': ListSettings,
            'ProcessSignal': pick_exchange_for_signal,
            'AnalyzeTradesPerforming': execute_trade_performance,
            'ManageAssets': manage_assets,
            'StartStop': start_stop_menu,
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

    if analyze_trade_performance is None:
        bot.send_message(cid, translate("❌ Performance analyzer is not available.", cid))
        return

    bot.send_message(cid, translate("Analyzing trades performance with LLM...", cid))

    asset = get_setting("asset") or "PERP_NEAR_USDC"
    symbol_filter = asset.replace("PERP_", "")

    try:
        result = analyze_trade_performance(symbol_filter=symbol_filter)
        if not result.get("ok"):
            bot.send_message(cid, str(result.get("error", "Unknown error during trade performance analysis.")))
            return

        stats = result.get("trade_stats", {})
        llm_response = str(result.get("llm_response", ""))
        if llm_response.startswith("```"):
            llm_response = re.sub(r"^```(?:json)?\n?", "", llm_response, flags=re.IGNORECASE)
            llm_response = re.sub(r"\n?```$", "", llm_response)
        llm_response = llm_response.strip()

        parsed_llm = None
        try:
            parsed_llm = json.loads(llm_response)
        except Exception:
            parsed_llm = None

        if isinstance(parsed_llm, dict):
            improvements = parsed_llm.get("strategy_improvements", [])
            params = parsed_llm.get("regime_filter_parameter_recommendations", [])

            improvements_text = "\n".join(
                [f"- {item}" for item in improvements[:5] if item]
            ) or "- No specific improvements provided"

            params_lines = []
            for p in params[:5]:
                if not isinstance(p, dict):
                    continue
                name = p.get("parameter", "parameter")
                decision = str(p.get("decision", "keep")).upper()
                suggested = p.get("suggested_value", "-")
                params_lines.append(f"- {name}: {decision} -> {suggested}")
            params_text = "\n".join(params_lines) if params_lines else "- No parameter recommendations"

            summary = (
                f"📊 Trades Performance ({symbol_filter})\n"
                f"Total: {stats.get('total_trades', 0)}\n"
                f"Positive: {stats.get('positive_trades', 0)}\n"
                f"Negative: {stats.get('negative_trades', 0)}\n"
                f"Neutral: {stats.get('neutral_trades', 0)}\n"
                f"PnL Total: {stats.get('pnl_total', 0)}\n\n"
                f"🧠 Verdict: {str(parsed_llm.get('final_verdict', 'N/A')).upper()}\n"
                f"📌 Summary: {parsed_llm.get('summary', 'No summary')}\n\n"
                f"✅ Improvements:\n{improvements_text}\n\n"
                f"⚙️ Parameter Suggestions:\n{params_text}"
            )
        else:
            summary = (
                f"📊 Trades Performance ({symbol_filter})\n"
                f"Total: {stats.get('total_trades', 0)}\n"
                f"Positive: {stats.get('positive_trades', 0)}\n"
                f"Negative: {stats.get('negative_trades', 0)}\n"
                f"Neutral: {stats.get('neutral_trades', 0)}\n"
                f"PnL Total: {stats.get('pnl_total', 0)}\n\n"
                f"LLM Analysis:\n{llm_response}"
            )
        send_text_message_chunked(cid, summary)

        md_path = result.get("md_path")
        if md_path and os.path.isfile(md_path):
            with open(md_path, "rb") as f:
                bot.send_document(cid, f, caption="📄 performance_llm_analysis.md")
    except Exception as e:
        bot.send_message(cid, translate(f"Error analyzing trades performance: {str(e)}", cid))

def pick_exchange_for_signal(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    asset = get_setting("current_asset")
    if not asset:
        bot.send_message(cid, translate("❌ No current asset set. Please configure one first.", cid))
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
        asset = get_setting("current_asset")
        if not asset:
            bot.send_message(cid, translate("❌ No current asset set. Please configure one first.", cid))
            return

    interval = get_setting("interval")
    exchange_label = "DEX" if exchange == "dex" else "CEX" if exchange == "cex" else ""
    exchange_suffix = f" ({exchange_label})" if exchange_label else ""

    bot.send_message(cid, translate(f"Processing signal for {asset} interval {interval}{exchange_suffix} ...", cid))
    time.sleep(1)
    try:
        result = run_process_signal(asset_override=asset, exchange_override=exchange)
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

    dex_mode = get_setting("auto_trade_dex") or "False"
    cex_mode = get_setting("auto_trade_cex") or "False"

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
    # Start autotrade in a separate thread to avoid blocking the bot
    t = threading.Thread(target=autotrade, daemon=True)
    t.start()
    bot.polling()