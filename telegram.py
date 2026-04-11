import os
import re
import sys
import time
import threading
import importlib.util
import re
import html
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'machine_learning')))
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.db_ops import (
    upsert_setting, get_all_settings, initialize_database_tables, get_setting,
    add_asset, remove_asset, get_asset_list
)
from futures_perps.trade.apolo.main import process_signal as run_process_signal, autotrade
import json
from datetime import timedelta
# Load environment variables
load_dotenv()
initialize_database_tables()

# Bot init
API_TOKEN = os.getenv("API_TOKEN")
bot = telebot.TeleBot(API_TOKEN)
gp1 = ""  # global setting key
TELEGRAM_MAX_MESSAGE_LEN = 4096


def _load_analyze_trade_performance():
    module_path = os.path.join(
        os.path.dirname(__file__),
        "futures_perps",
        "trade",
        "apolo",
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
    markup.row(InlineKeyboardButton(translate("⚙️ Settings", cid), callback_data="Settings"))
    markup.row(InlineKeyboardButton(translate("📡 Process Signal", cid), callback_data="ProcessSignal"))
    markup.row(InlineKeyboardButton(translate(" List All Settings", cid), callback_data="ListSettings"))
    
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
    if call.data.startswith("exec_sig:"):
        immediate_remove = True
    
    if immediate_remove:
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except:
            pass

    # Execute logic
    if call.data.startswith("rm_asset:"):
        asset = call.data.split(":", 1)[1]
        confirm_remove_asset(call.message, asset)
    elif call.data.startswith("exec_sig:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset)
    elif call.data.startswith("set_val:"):
        _, key, val = call.data.split(":", 2)
        upsert_setting(key, val)

        bot.send_message(cid, translate(f"✅ {key} set to {val}.", cid))
    elif call.data == "auto_trade_auto":
        upsert_setting("auto_trade", "Automatic")
        bot.send_message(cid, translate("✅ Auto Trade set to Automatic.", cid))
        bot.send_message(cid, translate(f"📊 Running on: {get_setting('asset')}", cid))
    else:
        options = {
            'List': command_list,
            'Settings': settings,
            'manage_assets': set_asset,
            'set_current_asset': set_current_asset,
            'set_asset': set_asset,
            'asset_add': ask_add_asset,
            'asset_remove': ask_remove_asset,
            'set_exchange': set_exchange,
            'set_interval': set_interval,
            'set_take_profit': set_take_profit,
            'set_stop_loss': set_stop_loss,
            # Backward-compatible callback aliases
            'set_min_tp': set_take_profit,
            'set_min_sl': set_stop_loss,
            'set_auto_trade': set_auto_trade,
            'set_leverage': set_leverage,
            'ListSettings': ListSettings,
            'ProcessSignal': execute_signal,
            'AnalyzeTradesPerforming': execute_trade_performance
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


# === Helper UI Functions ===
def settings(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    labels = {
        "set_exchange": "🔄 Exchange (DEX/CEX)",
        "manage_assets": "💰 Manage Assets",
        "set_current_asset": "🎯 Current Asset",
        "set_interval": "⏱️ Interval",
        "set_take_profit": "📈 Take Profit",
        "set_stop_loss": "📉 Stop Loss (DEX only)",
        "set_auto_trade": "🤖 Auto Trade",
        "set_leverage": "⚖️ Leverage (DEX only)"
    }
    
    markup = InlineKeyboardMarkup()
    for key, label in labels.items():
        markup.row(InlineKeyboardButton(translate(label, cid), callback_data=key))
        
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


# === Validation & Input Handling ===

def upsert_assets(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    valor = m.text.strip()
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if valor.upper() == "CANCEL":
        bot.send_message(cid, translate("Operation cancelled.", cid))
        return

    global gp1
    valid, error_msg = True, ""

    if gp1 == "current_asset":
        assets = get_asset_list()
        if valor not in assets:
            valid, error_msg = False, f"Asset not in list. Available: {', '.join(assets)}"
    elif gp1 == "risk_level":
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, "Risk must be a positive number (e.g., 1.5)"
    elif gp1 in ("take_profit", "stop_loss"):
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, f"{'Take Profit' if gp1 == 'take_profit' else 'Stop Loss'} must be positive"
    elif gp1 == "leverage":
        if not is_integer(valor) or not (1 <= int(valor) <= 50):
            valid, error_msg = False, "Leverage must be integer 1–50"
    elif gp1 == "auto_trade":
        if valor not in ("True", "False"):
            valid, error_msg = False, "Auto Trade must be 'True' or 'False'"
    elif gp1 == "interval":
        if not re.match(r"^\d+[mhd]$", valor.lower()) and not valor.lower() in ("5m", "15m", "30m", "1h", "4h", "1d"):
            valid, error_msg = False, "Invalid interval (e.g., 15m, 1h)"
    if not valid:
        bot.send_message(cid, translate(f"❌ {error_msg}. Try again:", cid))
        bot.register_next_step_handler_by_chat_id(cid, upsert_assets)
        return

    upsert_setting(gp1, valor)

    bot.send_message(cid, translate(f"✅ {gp1} set to {valor}.", cid))


# === Setting Entry Points ===

def set_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("➕ Add Asset", cid), callback_data="asset_add"))
    markup.add(InlineKeyboardButton(translate("➖ Remove Asset", cid), callback_data="asset_remove"))
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Interval ➡️", cid), callback_data="set_interval"))
    
    current_assets = get_asset_list()
    msg = translate("Manage Assets:", cid) + "\n" + ", ".join(current_assets)
    bot.send_message(cid, msg, reply_markup=markup)

def ask_add_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    bot.send_message(cid, translate("Enter asset to ADD (format: PERP_BTC_USDC):", cid))
    bot.register_next_step_handler_by_chat_id(cid, confirm_add_asset)

def confirm_add_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    valor = m.text.strip()
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if valor.upper() == "CANCEL":
        bot.send_message(cid, translate("Operation cancelled.", cid))
        return

    if not re.match(r"^PERP_[A-Z0-9]+_USDC$", valor):
        bot.send_message(cid, translate("❌ Invalid format. Use: PERP_BTC_USDC. Try again:", cid))
        bot.register_next_step_handler_by_chat_id(cid, confirm_add_asset)
        return

    add_asset(valor)
    bot.send_message(cid, translate(f"✅ Asset {valor} added.", cid))
    set_asset(m) # Show menu again

def set_current_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    assets = get_asset_list()
    if not assets:
        bot.send_message(cid, translate("No assets available. Add one first.", cid))
        return

    markup = InlineKeyboardMarkup()
    current = get_setting("current_asset")
    for asset in assets:
        status = "✅" if asset == current else "  "
        markup.add(InlineKeyboardButton(f"{status} {asset}", callback_data=f"set_val:current_asset:{asset}"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Interval ➡️", cid), callback_data="set_interval"))
    
    bot.send_message(cid, translate("Select Current Asset:", cid), reply_markup=markup)

def set_exchange(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    current = get_setting("exchange") or "dex"
    markup = InlineKeyboardMarkup()
    dex_status = "✅" if current == "dex" else "  "
    cex_status = "✅" if current == "cex" else "  "
    markup.add(
        InlineKeyboardButton(f"{dex_status} 🌐 DEX (Orderly Futures)", callback_data="set_val:exchange:dex"),
        InlineKeyboardButton(f"{cex_status} 💱 CEX (Binance Spot)", callback_data="set_val:exchange:cex")
    )
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Assets ➡️", cid), callback_data="manage_assets"))
    
    msg = translate("Select Exchange:", cid)
    if current == "dex":
        msg += "\n🌐 DEX: Orderly futures (leverage, SL/TP)"
    else:
        msg += "\n💱 CEX: Binance spot (BUY only, no SL, no leverage)"
    bot.send_message(cid, msg, reply_markup=markup)

def ask_remove_asset(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    assets = get_asset_list()
    if not assets:
        bot.send_message(cid, translate("No assets to remove.", cid))
        return

    markup = InlineKeyboardMarkup()
    for asset in assets:
        markup.add(InlineKeyboardButton(f"❌ {asset}", callback_data=f"rm_asset:{asset}"))
    
    bot.send_message(cid, translate("Select asset to REMOVE:", cid), reply_markup=markup)

def confirm_remove_asset(m, asset):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    remove_asset(asset)
    bot.send_message(cid, translate(f"✅ Asset {asset} removed.", cid))
    set_asset(m) # Show menu again


def set_risk(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "risk_level"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Interval ➡️", cid), callback_data="set_interval"))
               
    bot.send_message(cid, translate("Enter risk level (e.g., 1.5 for 1.5%)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_interval(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    options = ['5m', '15m', '30m', '1h', '4h', '1d']
    buttons = [InlineKeyboardButton(opt, callback_data=f"set_val:interval:{opt}") for opt in options]
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Take Profit ➡️", cid), callback_data="set_take_profit"))
        
    bot.send_message(cid, translate("Select Interval:", cid), reply_markup=markup)

def set_take_profit(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "take_profit"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Stop Loss ➡️", cid), callback_data="set_stop_loss"))
               
    bot.send_message(cid, translate("Enter take profit % (e.g., 0.3)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_stop_loss(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "stop_loss"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Auto Trade ➡️", cid), callback_data="set_auto_trade"))
               
    bot.send_message(cid, translate("Enter stop loss % (e.g., 1.5)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_auto_trade(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ OFF", callback_data="set_val:auto_trade:False"),
               InlineKeyboardButton("🤖 Automatic", callback_data="auto_trade_auto"))
    markup.add(InlineKeyboardButton("📡 Signal Mode", callback_data="set_val:auto_trade:Signal"))
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Leverage ➡️", cid), callback_data="set_leverage"))
    
    bot.send_message(cid, translate("Select Auto Trade:", cid), reply_markup=markup)

def set_leverage(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "leverage"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))
               
    bot.send_message(cid, translate("Enter leverage (e.g., 5)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_prompt(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "prompt_text"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Show Prompt ➡️", cid), callback_data="set_show_prompt"))
               
    bot.send_message(cid, translate("Enter prompt text:", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_show_prompt(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("True", callback_data="set_val:show_prompt:True"),
               InlineKeyboardButton("False", callback_data="set_val:show_prompt:False"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Prompt Mode ➡️", cid), callback_data="set_prompt_mode"))
               
    bot.send_message(cid, translate("Select Show Prompt:", cid), reply_markup=markup)

def set_prompt_mode(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("mixed", callback_data="set_val:prompt_mode:mixed"),
               InlineKeyboardButton("user_only", callback_data="set_val:prompt_mode:user_only"))
    
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Order Book Threshold ➡️", cid), callback_data="set_order_book_threshold"))
               
    bot.send_message(cid, translate("Select Prompt Mode:", cid), reply_markup=markup)

def set_order_book_threshold(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "order_book_threshold"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))
               
    bot.send_message(cid, translate("Enter Order Book Threshold (e.g., 1.6)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)


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

def execute_signal(m, asset=None):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if asset is None:
        asset = get_setting("current_asset")
        if not asset:
            bot.send_message(cid, translate("❌ No current asset set. Please configure one first.", cid))
            return

    interval = get_setting("interval")

    bot.send_message(cid, translate(f"Processing signal for {asset} interval {interval} ...", cid))
    time.sleep(1)
    try:
        result = run_process_signal(asset_override=asset)  # Pass the selected asset
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
         

def ListSettings(m):
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
    
    # Show exchange first
    exchange = settings.get("exchange", "dex")
    exchange_label = "🌐 DEX (Orderly Futures)" if exchange == "dex" else "💱 CEX (Binance Spot)"
    message_lines.append(f"🔄 Exchange: {exchange_label}\n\n")
    
    message_lines.append("⏰ Trading:\n")
    trading_keys = [
        ("🌟", "current_asset", "Current Asset"),
        ("⏱️", "interval", "Interval"),
        ("🎯", "take_profit", "Take Profit"),
        ("🛡️", "stop_loss", "Stop Loss"),
        ("⚖️", "leverage", "Leverage"),
    ]
    
    for emoji, key, label in trading_keys:
        if key in settings:
            value = settings[key]
            if key in ["take_profit", "stop_loss"]:
                value = f"{value}%"
                if key == "stop_loss" and exchange == "cex":
                    value += " (N/A for spot)"
            elif key == "leverage":
                value = f"{value}x"
                if exchange == "cex":
                    value += " (N/A for spot)"
            message_lines.append(f"{emoji} {label}: {value}\n")
    
    message_lines.append("\n⚙️ Configuration:\n")
    config_keys = [
        ("🤖", "auto_trade", "Auto Trade")
    ]
    
    for emoji, key, label in config_keys:
        if key in settings:
            value = settings[key]
            if key == "auto_trade": # add for automatic option
                if value == "Automatic":
                    value = "🤖 Automatic (30s auto-trade)"
                elif value == "Signal":
                    value = "📡 Signal Mode (30s alerts)"
                else:
                    value = "❌ OFF"
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