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
    add_asset, remove_asset, get_asset_list,
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
gp1 = ""  # global setting key
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
    if call.data.startswith("exec_sig:") or call.data.startswith("exec_sig_dex:") or call.data.startswith("exec_sig_cex:"):
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
    elif call.data.startswith("exec_sig_dex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="dex")
    elif call.data.startswith("exec_sig_cex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="cex")
    elif call.data.startswith("exec_sig:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset)
    elif call.data.startswith("set_val:"):
        _, key, val = call.data.split(":", 2)
        upsert_setting(key, val)

        # Map key to its parent menu so user stays in context after a change
        menu_for_key = {
            "exchange": set_exchange,
            "current_asset": set_current_asset,
            "risk_level": set_risk,
            "capital_usage": set_capital_usage,
            "leverage": set_leverage,
            "ml_threshold": set_ml_threshold,
            "grid_obi_buy": set_grid_obi_buy,
            "grid_obi_sell": set_grid_obi_sell,
            "grid_tp_pct": set_grid_tp_pct,
            "grid_cooldown_sec": set_grid_cooldown_sec,
            "grid_price_dip_pct": set_grid_price_dip_pct,
            "grid_max_positions": set_grid_max_positions,
            "show_prompt": set_show_prompt,
            "prompt_mode": set_prompt_mode,
        }
        parent_menu = menu_for_key.get(key, settings)
        parent_menu(call.message)
    elif call.data.startswith("set_mode:"):
        _, exchange, mode = call.data.split(":", 2)
        if exchange not in ("dex", "cex") or mode not in ("False", "Signal", "Automatic"):
            bot.send_message(cid, translate("❌ Invalid auto-trade mode selection.", cid))
            return

        mode_key = "auto_trade_dex" if exchange == "dex" else "auto_trade_cex"
        upsert_setting(mode_key, mode)

        exchange_label = "🌐 DEX" if exchange == "dex" else "💱 CEX"
        bot.send_message(cid, translate(f"✅ {exchange_label} mode set to: {mode}", cid))
        set_auto_trade(call.message)
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
            'set_take_profit': set_take_profit,
            # Backward-compatible callback aliases
            'set_min_tp': set_take_profit,
            'set_auto_trade': set_auto_trade,
            'set_cex_capital': set_cex_capital,
            'set_risk': set_risk,
            'set_capital_usage': set_capital_usage,
            'set_leverage': set_leverage,
            'set_ml_threshold': set_ml_threshold,
            'grid_obi_buy': set_grid_obi_buy,
            'grid_obi_sell': set_grid_obi_sell,
            'grid_tp_pct': set_grid_tp_pct,
            'grid_cooldown_sec': set_grid_cooldown_sec,
            'grid_price_dip_pct': set_grid_price_dip_pct,
            'grid_max_positions': set_grid_max_positions,
            'ListSettings': ListSettings,
            'ProcessSignal': pick_exchange_for_signal,
            'AnalyzeTradesPerforming': execute_trade_performance,
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
        # Row 1 — General
        "manage_assets": "💰 Manage Assets",
        "set_current_asset": "🎯 Current Asset",
        # Row 2 — Trading
        "set_auto_trade": "🤖 Auto Trade",
        "set_take_profit": "📈 Take Profit",
        # Row 3 — CEX
        "set_cex_capital": "💵 CEX Capital",
        "set_ml_threshold": "🧠 ML Threshold",
        # Row 4 — DEX
        "set_risk": "⚠️ Risk Level",
        "set_capital_usage": "💰 Capital Usage",
        # Row 5 — DEX
        "set_leverage": "⚖️ Leverage",
        # Row 6 — Grid
        "grid_obi_buy": "📉 Grid OBI Buy",
        "grid_obi_sell": "📈 Grid OBI Sell",
        # Row 7 — Grid
        "grid_tp_pct": "🎯 Grid TP %",
        "grid_cooldown_sec": "⏱️ Grid Cooldown",
        "grid_price_dip_pct": "📊 Grid Price Dip %",
        "grid_max_positions": "📦 Grid Max Positions",
    }

    markup = InlineKeyboardMarkup()
    keys = list(labels.keys())
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(translate(labels[keys[i]], cid), callback_data=keys[i])]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(translate(labels[keys[i + 1]], cid), callback_data=keys[i + 1]))
        markup.row(*row)
        
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
    elif gp1 == "take_profit":
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, "Take Profit must be positive"
    elif gp1 == "leverage":
        if not is_integer(valor) or not (1 <= int(valor) <= 50):
            valid, error_msg = False, "Leverage must be integer 1–50"
    elif gp1 == "cex_capital":
        if not is_float(valor) or float(valor) <= 0:
            valid, error_msg = False, "CEX capital must be a positive number in USDT"
    elif gp1 == "ml_threshold":
        if not is_float(valor) or not (0 < float(valor) <= 1.0):
            valid, error_msg = False, "ML threshold must be between 0.01 and 1.00"
    elif gp1 == "auto_trade":
        if valor not in ("True", "False"):
            valid, error_msg = False, "Auto Trade must be 'True' or 'False'"
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
               InlineKeyboardButton(translate("Next: Take Profit ➡️", cid), callback_data="set_take_profit"))
    
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
               InlineKeyboardButton(translate("Next: Take Profit ➡️", cid), callback_data="set_take_profit"))
    
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
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    current = get_setting("risk_level") or "1.0"

    levels = [
        ("🟢 Low (1%)",     "1.0",  "Safe — small positions, low drawdown"),
        ("🟡 Medium (2.5%)", "2.5",  "Balanced — moderate exposure"),
        ("🟠 High (5%)",     "5.0",  "Aggressive — larger positions"),
        ("🔴 Max (6.5%)",    "6.5",  "Maximum — hits margin cap, high risk"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val, desc in levels:
        check = "✅" if current == val else "  "
        markup.add(InlineKeyboardButton(
            f"{check} {label}",
            callback_data=f"set_val:risk_level:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Capital Usage ➡️", cid), callback_data="set_capital_usage"))

    bot.send_message(cid, translate("Select Risk Level (DEX only — % of balance risked per trade):", cid), reply_markup=markup)

def set_capital_usage(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    current = get_setting("capital_usage") or "50"

    levels = [
        ("🟢 25%",  "25",  "Conservative — leaves 75% buffer"),
        ("🟡 50%",  "50",  "Balanced — default, half buying power"),
        ("🟠 75%",  "75",  "Aggressive — uses most capital"),
        ("🔴 100%", "100", "Max — full buying power, no buffer"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val, desc in levels:
        check = "✅" if current == val else "  "
        markup.add(InlineKeyboardButton(
            f"{check} {label}",
            callback_data=f"set_val:capital_usage:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Leverage ➡️", cid), callback_data="set_leverage"))

    bot.send_message(cid, translate("Select Capital Usage (DEX only — % of buying power deployed):", cid), reply_markup=markup)

def set_take_profit(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "take_profit"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Auto Trade ➡️", cid), callback_data="set_auto_trade"))
               
    bot.send_message(cid, translate("Enter take profit % (e.g., 0.3)", cid), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_auto_trade(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    
    dex_mode = get_setting("auto_trade_dex") or "False"
    cex_mode = get_setting("auto_trade_cex") or "False"

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(f"🌐 DEX OFF {'✅' if dex_mode == 'False' else ''}", callback_data="set_mode:dex:False"),
        InlineKeyboardButton(f"📡 DEX Signal {'✅' if dex_mode == 'Signal' else ''}", callback_data="set_mode:dex:Signal"),
        InlineKeyboardButton(f"🤖 DEX Auto {'✅' if dex_mode == 'Automatic' else ''}", callback_data="set_mode:dex:Automatic")
    )
    markup.row(
        InlineKeyboardButton(f"💱 CEX OFF {'✅' if cex_mode == 'False' else ''}", callback_data="set_mode:cex:False"),
        InlineKeyboardButton(f"📡 CEX Signal {'✅' if cex_mode == 'Signal' else ''}", callback_data="set_mode:cex:Signal"),
        InlineKeyboardButton(f"🤖 CEX Auto {'✅' if cex_mode == 'Automatic' else ''}", callback_data="set_mode:cex:Automatic")
    )
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: CEX Capital ➡️", cid), callback_data="set_cex_capital"))
    
    bot.send_message(cid, translate("Select Auto Trade mode for each exchange:", cid), reply_markup=markup)

def set_cex_capital(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "cex_capital"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    current = get_setting("cex_capital") or "10"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Risk Level ➡️", cid), callback_data="set_risk"))

    bot.send_message(
        cid,
        translate(f"Enter CEX capital in USDT per Binance position. Current: {current}", cid),
        reply_markup=markup
    )
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

def set_leverage(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    current = get_setting("leverage") or "3"

    levels = [
        ("🟢 2x",  "2",  "Safe — low liquidation risk"),
        ("🟡 3x",  "3",  "Balanced — moderate leverage"),
        ("🟠 5x",  "5",  "Aggressive — higher exposure"),
        ("🔴 10x", "10", "Max — high risk, tight liquidation"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val, desc in levels:
        check = "✅" if current == val else "  "
        markup.add(InlineKeyboardButton(
            f"{check} {label}",
            callback_data=f"set_val:leverage:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: ML Threshold ➡️", cid), callback_data="set_ml_threshold"))

    bot.send_message(cid, translate("Select Leverage (DEX futures multiplier):", cid), reply_markup=markup)

def set_ml_threshold(m):
    if m.chat.type != 'private': return
    global gp1; gp1 = "ml_threshold"
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    current = get_setting("ml_threshold") or "0.80"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))

    bot.send_message(
        cid,
        translate(f"Enter ML Threshold (0.01–1.00). Current: {current}", cid),
        reply_markup=markup
    )
    bot.register_next_step_handler_by_chat_id(cid, upsert_assets)

# ── Grid Scalper Settings ─────────────────────────────────────────────────────

def _grid_capital() -> float:
    """Return current CEX capital in USDT for recommendation logic."""
    try:
        val = get_setting("cex_capital")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return 100.0


def _grid_recommend(capital: float, key: str) -> str:
    """Return recommended value for a grid setting based on capital tier."""
    if key == "grid_obi_buy":
        return "0.94" if capital > 500 else "0.95" if capital > 200 else "0.96"
    if key == "grid_obi_sell":
        return "1.30" if capital > 500 else "1.22" if capital > 200 else "1.18"
    if key == "grid_tp_pct":
        return "0.75" if capital > 500 else "0.5" if capital > 100 else "0.3"
    if key == "grid_cooldown_sec":
        return "900" if capital > 500 else "600" if capital > 200 else "300"
    if key == "grid_price_dip_pct":
        return "0.5" if capital > 500 else "0.4" if capital > 200 else "0.3"
    if key == "grid_max_positions":
        return "2" if capital > 500 else "2" if capital > 200 else "1"
    return ""


def set_grid_obi_buy(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_obi_buy") or "0.96"
    rec = _grid_recommend(capital, "grid_obi_buy")

    levels = [
        ("🟢 0.92 — Aggressive (more entries)", "0.92"),
        ("🟡 0.94 — Balanced", "0.94"),
        ("🟠 0.96 — Moderate (recommended)", "0.96"),
        ("🔴 0.98 — Conservative (fewer entries)", "0.98"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_obi_buy:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Grid OBI Sell ➡️", cid), callback_data="grid_obi_sell"))

    bot.send_message(cid, translate(
        f"📉 Grid OBI Buy — OBI below → BUY in RANGE\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec} recommended\n"
        f"Current: {current}", cid), reply_markup=markup)


def set_grid_obi_sell(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_obi_sell") or "1.22"
    rec = _grid_recommend(capital, "grid_obi_sell")

    levels = [
        ("🟢 1.10 — Slight pump", "1.10"),
        ("🟡 1.18 — Moderate bullish", "1.18"),
        ("🟠 1.22 — Strong bullish", "1.22"),
        ("🔴 1.30 — Euphoria", "1.30"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_obi_sell:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Grid TP % ➡️", cid), callback_data="grid_tp_pct"))

    bot.send_message(cid, translate(
        f"📈 Grid OBI Sell — OBI above → signal in RANGE\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec} recommended\n"
        f"Current: {current}", cid), reply_markup=markup)


def set_grid_tp_pct(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_tp_pct") or "0.5"
    rec = _grid_recommend(capital, "grid_tp_pct")
    profit = capital * float(rec) / 100

    levels = [
        ("🟢 0.3% — Scalp",  "0.3"),
        ("🟡 0.5% — Balanced", "0.5"),
        ("🟠 0.75% — Swing",  "0.75"),
        ("🔴 1.0% — Wide",   "1.0"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_tp_pct:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Next: Grid Cooldown ➡️", cid), callback_data="grid_cooldown_sec"))

    bot.send_message(cid, translate(
        f"🎯 Grid TP % — profit target above fill\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec}% = ~${profit:.2f}/trade\n"
        f"Current: {current}%", cid), reply_markup=markup)


def set_grid_cooldown_sec(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_cooldown_sec") or "300"
    rec = _grid_recommend(capital, "grid_cooldown_sec")

    levels = [
        ("⚡ 120s — Fast",   "120"),
        ("🟡 300s — Normal",  "300"),
        ("🟠 600s — Patient", "600"),
        ("🐢 900s — Slow",   "900"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_cooldown_sec:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))

    bot.send_message(cid, translate(
        f"⏱️ Grid Cooldown — seconds between entries\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec}s recommended\n"
        f"Current: {current}s", cid), reply_markup=markup)


def set_grid_price_dip_pct(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_price_dip_pct") or "0.4"
    rec = _grid_recommend(capital, "grid_price_dip_pct")

    levels = [
        ("⚡ 0.2% — Aggressive", "0.2"),
        ("🟡 0.3% — Active",     "0.3"),
        ("🟠 0.4% — Moderate",   "0.4"),
        ("🐢 0.6% — Conservative","0.6"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_price_dip_pct:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))

    bot.send_message(cid, translate(
        f"📊 Grid Price Dip % — buy when price drops this far below recent peak\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec}% recommended\n"
        f"Current: {current}%", cid), reply_markup=markup)


def set_grid_max_positions(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    capital = _grid_capital()
    current = get_setting("grid_max_positions") or "1"
    rec = _grid_recommend(capital, "grid_max_positions")

    levels = [
        ("1️⃣  1 — Single (safe)",     "1"),
        ("2️⃣  2 — Double stack",      "2"),
        ("3️⃣  3 — Triple stack",      "3"),
        ("5️⃣  5 — Aggressive",        "5"),
    ]

    markup = InlineKeyboardMarkup()
    for label, val in levels:
        marks = []
        if current == val:
            marks.append("✅")
        if val == rec:
            marks.append("⭐")
        prefix = " ".join(marks) + " " if marks else "   "
        markup.add(InlineKeyboardButton(
            f"{prefix}{label}",
            callback_data=f"set_val:grid_max_positions:{val}"
        ))

    markup.add(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="Settings"),
               InlineKeyboardButton(translate("Finish ✅", cid), callback_data="Settings"))

    bot.send_message(cid, translate(
        f"📦 Grid Max Positions — concurrent buys allowed\n"
        f"💰 Capital: ${capital:.0f} → ⭐ {rec} recommended\n"
        f"Current: {current}", cid), reply_markup=markup)


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