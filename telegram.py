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
    get_setting_float, get_setting_bool, get_setting_int,
    load_all_positions,
    get_universe, get_universe_scan_age, get_venue_equity,
    set_blacklist, get_capital_pool, get_tradeable_universe,
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


# === Reset to Defaults ===

RECOMMENDED_DEFAULTS = {
    'tp_min_pct': '0.8', 'sl_min_pct': '0.5',
    'dip_min_pct': '0.15', 'pump_min_pct': '0.15',
    'tp_k': '1.0', 'sl_k': '0.6',
    'dip_k': '0.5', 'pump_k': '0.5',
    'cooldown_sec': '60', 'min_entry_spacing_pct': '0.3',
    'leverage': '3', 'max_leverage': '3',
    'daily_loss_limit_pct': '5', 'max_consecutive_losses': '4',
    'daily_loss_limit': '0',
    'adaptive_enabled': 'true', 'trading_enabled': '1',
    'max_hold_minutes_spot': '120', 'max_hold_minutes_futures': '240',
    'atr_period': '14', 'atr_interval': '5m', 'candle_cache_sec': '60',
    'dex_round_trip_fee_pct': '0.06', 'cex_round_trip_fee_pct': '0.20',
    'assumed_slippage_pct': '0.03', 'min_net_edge_pct': '0.30',
    'regime_cache_sec': '300', 'slope_threshold': '0.0012',
    'max_active_pairs': '6', 'max_concurrent_positions': '9', 'binance_blocklist': '',
    'global_daily_loss_limit': '0', 'global_daily_loss_limit_pct': '0',
    'tox_window': '120', 'velocity_window': '3',
    'tox_velocity_enforce': 'false', 'tox_spread_enforce': 'false',
    'tox_depth_enforce': 'false', 'tox_obi_enforce': 'false',
    'max_extreme_velocity_pct': '0.25',
    'spread_z_max': '2.5', 'depth_ratio_min': '0.5', 'obi_z_max': '2.5',
}

def reset_to_defaults(message):
    """Reset all settings to recommended defaults."""
    cid = message.chat.id
    try:
        from db.db_ops import get_db_connection
        with get_db_connection() as conn:
            for key, value in RECOMMENDED_DEFAULTS.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value)
                )
            conn.commit()
        bot.send_message(cid, f"✅ {len(RECOMMENDED_DEFAULTS)} settings reset to recommended defaults.\n\nKey values:\nTP={RECOMMENDED_DEFAULTS['tp_min_pct']}%  SL={RECOMMENDED_DEFAULTS['sl_min_pct']}%  Lev={RECOMMENDED_DEFAULTS['leverage']}x\nAdaptive={'ON' if RECOMMENDED_DEFAULTS['adaptive_enabled']=='true' else 'OFF'}")
    except Exception as e:
        bot.send_message(cid, f"❌ Error: {str(e)[:200]}")


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
    markup.row(InlineKeyboardButton(translate("� Capital", cid), callback_data="Capital"))
    markup.row(InlineKeyboardButton(translate("🛰️ Universe", cid), callback_data="Universe"))
    markup.row(InlineKeyboardButton(translate("�📖 Explain Settings", cid), callback_data="ExplainAll"))
    markup.row(InlineKeyboardButton(translate("🤖 Propose Changes", cid), callback_data="ProposeStart"))
    markup.row(InlineKeyboardButton(translate("🔄 Reset to Defaults", cid), callback_data="ResetDefaults"))
    bot.send_message(cid, translate("Available options.", cid), reply_markup=markup)


@bot.message_handler(commands=['explain'])
def command_explain(m):
    """Explain a setting using LLM (cached). Usage: /explain <key>  or  /explain all"""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    key = m.text.replace('/explain', '').strip()
    if not key or key.startswith('@'):
        bot.send_message(cid, translate(
            "Usage:\n/explain <setting_key> — explain one setting\n"
            "/explain all — LLM analysis of all settings\n"
            "Use /list to browse settings.", cid))
        return
    if key.lower() == 'all':
        _send_explain_all(cid)
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


# ═════════════════════════════════════════════════════════════════════════════
# Amendment 003 — Capital / Universe / Blacklist commands
# ═════════════════════════════════════════════════════════════════════════════

@bot.message_handler(commands=['capital'])
def command_capital(m):
    """Capital view — per-venue pools: declared vs live equity, slot, deployed."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    show_capital(m)


@bot.message_handler(commands=['universe'])
def command_universe(m):
    """Show the current universe for a venue. Usage: /universe [cex|dex]"""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    arg = (m.text.replace('/universe', '').strip() or "").lower()
    if arg in ("cex", "binance"):
        venues = ["binance"]
    elif arg in ("dex", "orderly"):
        venues = ["orderly"]
    else:
        venues = ["binance", "orderly"]
    for venue in venues:
        _send_universe(cid, venue)


@bot.message_handler(commands=['blacklist'])
def command_blacklist(m):
    """Operator blacklist override. Usage: /blacklist add|remove <ASSET>"""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid):
        bot.send_message(cid, translate("🔍 Not authorized", cid))
        return
    parts = (m.text.replace('/blacklist', '').strip() or "").split()
    if len(parts) < 2:
        bot.send_message(cid, translate(
            "Usage: /blacklist add|remove <ASSET>\n"
            "Examples:\n/blacklist add NEAR\n/blacklist remove NEAR", cid))
        return
    action = parts[0].lower()
    asset = parts[1].upper()
    if action not in ("add", "remove"):
        bot.send_message(cid, translate("Action must be 'add' or 'remove'.", cid))
        return
    target = (action == "add")
    results = []
    for venue in ("binance", "orderly"):
        if set_blacklist(venue, asset, target):
            results.append(f"{venue}: {'🚫 blacklisted' if target else '✅ unblacklisted'}")
    if not results:
        bot.send_message(cid, translate(f"❌ '{asset}' is not in any stored universe — nothing to toggle.", cid))
        return
    bot.send_message(cid, translate(f"✅ {asset}: " + "; ".join(results), cid))


def show_capital(m):
    """Capital view — per-venue pools: declared vs live equity, slot size, deployed, free."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    lines = ["💰 Capital (per venue)"]
    for venue, vlabel, pct_key, slots_key in (
        ("binance", "CEX — Binance spot", "cex_slot_pct", "max_slots_cex"),
        ("orderly", "DEX — Orderly perps", "dex_slot_pct", "max_slots_dex"),
    ):
        pool = get_capital_pool(venue)
        st = get_venue_equity(venue)
        equity = float(st["equity"]) if st else 0.0
        eq_age = st["updated_at"] if st else None
        slot_pct = get_setting_float(pct_key, 10.0)
        slot = equity * slot_pct / 100 if equity > 0 else 0.0
        max_slots = get_setting_int(slots_key, 9)
        deployed = sum(
            float(p.get("qty", 0) or 0) * float(p.get("entry_price", 0) or 0)
            for p in load_all_positions(venue=venue)
        )
        free = max(0.0, equity - deployed)
        warn = ""
        if equity > 0 and pool > 0 and abs(pool - equity) / equity > 0.25:
            warn = f"\n  ⚠️ Declared ${pool:,.0f} diverges from live ${equity:,.0f} — exchange wins, sizing unchanged"
        age_txt = f"  (as of {time.strftime('%H:%M UTC', time.gmtime(eq_age))})" if eq_age else ""
        lines.append(
            f"\n▫️ {vlabel}"
            f"\n  Declared: ${pool:,.0f}   Live equity: ${equity:,.0f}{age_txt}{warn}"
            f"\n  Slot: {slot_pct:.1f}% → ${slot:,.0f}   Max slots: {max_slots}"
            f"\n  Deployed: ${deployed:,.0f}   Free: ${free:,.0f}"
        )
    send_text_message_chunked(cid, "\n".join(lines))


def _send_universe(cid: int, venue: str):
    """Send the current universe list with metrics and scan age (read-only)."""
    rows = get_universe(venue, include_blacklisted=True)
    age = get_universe_scan_age(venue)
    label = "CEX" if venue == "binance" else "DEX"
    if age is None:
        bot.send_message(cid, translate(f"🛰️ {label} universe: no scan stored yet.", cid))
        return
    hours = (time.time() - age) / 3600
    max_age = get_setting_float("universe_max_age_hours", 36)
    stale = hours > max_age
    head = f"🛰️ {label} universe — scan {hours:.1f}h ago{'  ⚠️ STALE' if stale else ''}"
    if not rows:
        bot.send_message(cid, translate(f"{head}\n(empty)", cid))
        return
    lines = [head]
    for r in rows:
        rec = r.get("recovery_rate")
        rec_txt = f"{rec * 100:.0f}%" if rec is not None else "—"
        sig = r.get("signals_count")
        sig_txt = str(sig) if sig is not None else "—"
        spread = r.get("spread_pct")
        spread_txt = f"{spread:.3f}%" if spread is not None else "—"
        vol = r.get("quote_volume_24h") or 0
        flag = "  🚫" if r.get("blacklisted") else ""
        lines.append(
            f"#{r['rank']} {r['asset']}  rec={rec_txt} sig={sig_txt} "
            f"spread={spread_txt} vol=${vol / 1e6:.1f}M{flag}"
        )
    send_text_message_chunked(cid, "\n".join(lines))


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


def _send_explain_all(cid):
    """Analyze ALL settings via LLM in one call. Falls back to structured validator summary."""
    bot.send_message(cid, translate("📖 Analyzing all settings via LLM, please wait…", cid))
    try:
        from research.settings_llm import explain_all
        from db.db_ops import get_setting
        lang = get_setting("llm_language") or "en"
        text = explain_all(lang, "100_to_1k")
        send_text_message_chunked(cid, text)
    except ImportError:
        _send_explain_all_fallback(cid)
    except Exception as e:
        # If LLM call fails (timeout, etc), use structured fallback
        bot.send_message(cid, translate(f"⚠️ LLM unavailable, showing validator analysis…", cid))
        _send_explain_all_fallback(cid)


def _send_explain_all_fallback(cid):
    """Structured, friendly manual-style settings overview."""
    from db.db_ops import get_all_settings
    from research.settings_llm import _format_settings_grouped

    current = get_all_settings()
    text = _format_settings_grouped(current, "")
    send_text_message_chunked(cid, text)


def _send_proposals(cid):
    try:
        from research.settings_llm import propose
        import sqlite3, os as _os, json as _json
        # Build rich performance context from DB
        db_path = _os.path.join(_os.path.dirname(__file__), "data", "trading.db")
        conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row

        sig_count = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
        signaled_count = conn.execute("SELECT COUNT(*) as c FROM signals WHERE action='signaled'").fetchone()["c"]
        entered_count = conn.execute("SELECT COUNT(*) as c FROM signals WHERE action='entered'").fetchone()["c"]
        trade_count = conn.execute("SELECT COUNT(*) as c FROM closed_trades").fetchone()["c"]
        wins = conn.execute("SELECT COUNT(*) as c FROM closed_trades WHERE pnl_net > 0").fetchone()["c"]
        losses = conn.execute("SELECT COUNT(*) as c FROM closed_trades WHERE pnl_net <= 0").fetchone()["c"]
        total_pnl = conn.execute("SELECT COALESCE(SUM(pnl_net),0) as p FROM closed_trades").fetchone()["p"]
        avg_pnl = conn.execute("SELECT COALESCE(AVG(pnl_net),0) as p FROM closed_trades").fetchone()["p"]

        # Win rate by regime
        regime_stats = []
        for row in conn.execute("SELECT regime, COUNT(*) as cnt, SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END) as w FROM closed_trades GROUP BY regime").fetchall():
            wr = row["w"]/row["cnt"]*100 if row["cnt"] > 0 else 0
            regime_stats.append(f"{row['regime']}: {row['cnt']} trades, {wr:.0f}% WR")

        # Win rate by venue
        venue_stats = []
        for row in conn.execute("SELECT venue, COUNT(*) as cnt, SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END) as w FROM closed_trades GROUP BY venue").fetchall():
            wr = row["w"]/row["cnt"]*100 if row["cnt"] > 0 else 0
            venue_stats.append(f"{row['venue']}: {row['cnt']} trades, {wr:.0f}% WR")

        # Recent PnL (last 7 days)
        recent_pnl = conn.execute("SELECT COALESCE(SUM(pnl_net),0) as p FROM closed_trades WHERE closed_at > unixepoch('now', '-7 days')").fetchone()["p"]

        # Current validation issues
        from trade.settings_rules import validate_all
        val_results = validate_all()
        val_issues = [f"{k}: {v.message}" for k, v in val_results.items() if v.level in ("error", "warn")]

        conn.close()

        wr = wins/trade_count*100 if trade_count > 0 else 0
        ctx_lines = [
            f"Trades: {trade_count} total ({wins}W/{losses}L, {wr:.0f}% WR)",
            f"Avg PnL per trade: ${avg_pnl:.2f}, Total PnL: ${total_pnl:.2f}",
            f"Recent 7d PnL: ${recent_pnl:.2f}",
            f"Signals: {sig_count} total ({signaled_count} signaled, {entered_count} entered)",
            f"Conversion rate: {entered_count/signaled_count*100:.0f}%" if signaled_count > 0 else "Conversion rate: N/A",
        ]
        if regime_stats:
            ctx_lines.append("By regime: " + " | ".join(regime_stats))
        if venue_stats:
            ctx_lines.append("By venue: " + " | ".join(venue_stats))
        if val_issues:
            ctx_lines.append("Validation issues: " + "; ".join(val_issues[:5]))

        ctx = "\n".join(ctx_lines)
        if sig_count == 0 and trade_count == 0:
            ctx += "\nNo measured data available — bot has not traded yet."
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
    markup.row(InlineKeyboardButton(translate("� Explain All Settings", cid), callback_data="ExplainAll"))
    markup.row(InlineKeyboardButton(translate("🔙 Back", cid), callback_data="List"))
    bot.send_message(cid, translate("Select a setting group or explain all:", cid), reply_markup=markup)


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


def _dispatch_callback(call, cid):
    """Route callback data to the appropriate handler. Extracted for clean error boundaries."""
    immediate_remove = False
    if call.data.startswith("exec_sig"):
        immediate_remove = True
    if call.data.startswith("asset_toggle:") or call.data.startswith("asset_remove:") or call.data.startswith("asset_venuetoggle:"):
        immediate_remove = True

    if immediate_remove:
        try:
            bot.edit_message_reply_markup(chat_id=cid, message_id=call.message.message_id, reply_markup=None)
        except Exception:
            pass

    if call.data == "exec_sig_dex_pick":
        pick_asset_for_signal(call.message, "dex")
    elif call.data == "exec_sig_cex_pick":
        pick_asset_for_signal(call.message, "cex")
    elif call.data.startswith("exec_sig_dex_asset:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="dex")
    elif call.data.startswith("exec_sig_cex_asset:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="cex")
    elif call.data.startswith("exec_sig_dex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="dex")
    elif call.data.startswith("exec_sig_cex:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset, exchange="cex")
    elif call.data.startswith("exec_sig:"):
        asset = call.data.split(":", 1)[1]
        execute_signal(call.message, asset=asset)
    elif call.data.startswith("ExplainGroup:"):
        group = call.data.split(":", 1)[1]
        _show_group_keys(cid, group)
    elif call.data.startswith("ExplainKey:"):
        key = call.data.split(":", 1)[1]
        _send_explain(cid, key)
    elif call.data == "ExplainAll":
        _send_explain_all(cid)
    elif call.data == "ResetDefaults":
        reset_to_defaults(call.message)
    else:
        options = {
            'List': command_list,
            'ProcessSignal': pick_exchange_for_signal,
            'AnalyzeTradesPerforming': execute_trade_performance,
            'Capital': show_capital,
            'Universe': show_universe,
            'ExplainPrompt': explain_prompt,
            'ProposeStart': propose_start,
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

    if not get_tradeable_universe("binance") and not get_tradeable_universe("orderly"):
        bot.send_message(cid, translate("❌ No universe assets available. Run the scanner first.", cid))
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🌐 DEX (Orderly Futures)", callback_data="exec_sig_dex_pick"),
        InlineKeyboardButton("💱 CEX (Binance Spot)", callback_data="exec_sig_cex_pick")
    )
    bot.send_message(cid, translate("Select exchange:", cid), reply_markup=markup)


def pick_asset_for_signal(m, exchange):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    venue = "orderly" if exchange == "dex" else "binance"
    rows = get_tradeable_universe(venue)
    if not rows:
        bot.send_message(cid, translate(f"❌ No universe assets for {exchange}. Run the scanner first.", cid))
        return

    exchange_label = "DEX" if exchange == "dex" else "CEX"
    markup = InlineKeyboardMarkup()
    for r in rows:
        sym = r["asset"]
        markup.add(InlineKeyboardButton(sym, callback_data=f"exec_sig_{exchange}_asset:{sym}"))
    bot.send_message(cid, translate(f"Select asset for {exchange_label} signal:", cid), reply_markup=markup)


def execute_signal(m, asset=None, exchange=None):
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return

    if asset is None:
        venue_default = "orderly" if exchange == "dex" else "binance"
        rows = get_tradeable_universe(venue_default)
        asset = rows[0]["asset"] if rows else None
        if not asset:
            bot.send_message(cid, translate("❌ No universe assets available. Run the scanner first.", cid))
            return

    if exchange is None:
        exchange = "cex"

    exchange_label = "DEX" if exchange == "dex" else "CEX"
    venue = "orderly" if exchange == "dex" else "binance"

    bot.send_message(cid, translate(f"Processing signal for {asset} ({exchange_label}) ...", cid))
    time.sleep(1)
    try:
        from trade.regime import detect_regime
        import requests
        regime = detect_regime(asset, venue)
        obi = 1.0  # placeholder — full signal processing needs OB fetch
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={asset}USDT", timeout=5)
            price = float(r.json()["price"])
        except Exception:
            price = 0.0

        if exchange == "dex":
            from trading_bot.futures_scalper import scalp_cycle as futures_cycle
            from trading_bot.executor import OrderlyFutures
            exchange_obj = OrderlyFutures()
            action = futures_cycle(asset, exchange_obj, regime, obi, price)
        else:
            from trading_bot.spot_scalper import scalp_cycle as spot_cycle
            from trading_bot.executor import BinanceSpot
            exchange_obj = BinanceSpot()
            action = spot_cycle(asset, exchange_obj, regime, obi, price)

        result = f"Asset: {asset}\nExchange: {exchange_label}\nRegime: {regime}\nAction: {action or 'no signal'}"
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
         

def show_universe(m):
    """Show the current universe for both venues (read-only)."""
    if m.chat.type != 'private': return
    cid = m.chat.id
    if str(os.getenv("TELEGRAM_CHAT_ID")) != str(cid): return
    _send_universe(cid, "binance")
    _send_universe(cid, "orderly")


# Start polling
if __name__ == "__main__":
    bot.polling()