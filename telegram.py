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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from db.db_ops import (
    upsert_setting, initialize_database_tables,
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


def translate(text, chat_id):
    lang = os.getenv("BOT_LANGUAGE", "en").lower()
    try:
        translated = GoogleTranslator(source='auto', target=lang).translate(text)
        return translated
    except Exception as e:
        print(f"Translation error: {e}")
        return text


# === Message Handlers ===

@bot.message_handler(commands=['start'])
def command_start(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    nom = m.chat.first_name
    text = translate("Welcome to Mockba! With this bot, you trade against Apolo Dex.", cid)
    bot.send_message(cid, f"{text}. {nom} - {cid}")
    command_list(m)


@bot.message_handler(commands=['list'])
def command_list(m):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    mini_app_url = os.getenv("MINI_APP_URL", "")
    if not mini_app_url:
        bot.send_message(cid, translate("Mini app not configured (MINI_APP_URL missing).", cid))
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(translate("🚀 Open Mini App", cid), web_app=WebAppInfo(url=mini_app_url)))
    markup.add(InlineKeyboardButton(translate("Market check", cid), callback_data="market"))
    bot.send_message(cid, translate("Open the Mockba mini app:", cid), reply_markup=markup)


@bot.message_handler(commands=['market'])
def command_market(m):
    """Manual market-health report (feature 005): live-snapshot mode of the
    shared check. Operator escape hatch / override view — same verdict shape
    the automatic gate uses, on demand."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    from trade.market_check import check_venue_live, format_report
    labels = {k: translate(v, cid) for k, v in {
        "market": "Market", "verdict": "Verdict", "reasons": "Reasons",
        "scan": "Scan", "liquidity": "Liquidity",
        "thresholds": "Thresholds (diag)",
        "regime_mix": "regime mix", "assets_pass": "assets pass",
    }.items()}
    parts = []
    for venue in ("binance", "orderly"):
        try:
            parts.append(format_report(check_venue_live(venue), labels))
        except Exception as e:
            parts.append(f"[{venue}] market check failed: {str(e)[:200]}")
    text = "\n\n".join(parts)
    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN):
        bot.send_message(cid, text[i:i + TELEGRAM_MAX_MESSAGE_LEN])


def _dispatch_callback(call, cid):
    """Route callback data to the appropriate handler. Extracted for clean error boundaries."""
    immediate_remove = False
    if call.data.startswith("asset_toggle:") or call.data.startswith("asset_remove:") or call.data.startswith("asset_venuetoggle:"):
        immediate_remove = True

    if immediate_remove:
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except Exception:
            pass

    options = {
        'List': command_list,
        'market': command_market,
    }
    func = options.get(call.data)
    if func:
        func(call.message)

    if not immediate_remove:
        time.sleep(0.5)
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    # Always answer the callback to stop the loading spinner, even on errors
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if call.message is None:
        try:
            bot.send_message(call.from_user.id, translate("⚠️ Could not retrieve the original message. Please use /list again.", call.from_user.id))
        except Exception:
            pass
        return

    cid = call.message.chat.id

    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return

    try:
        _dispatch_callback(call, cid)
    except Exception as e:
        try:
            bot.send_message(cid, f"❌ Error processing request: {str(e)[:300]}")
        except Exception:
            pass


# Start polling
if __name__ == "__main__":
    bot.polling()