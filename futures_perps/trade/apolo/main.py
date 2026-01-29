from datetime import timedelta
import json
import time
import requests
import os
import sys
from pydantic import BaseModel
# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from db.db_ops import  get_setting
from logs.log_config import apolo_trader_logger as logger
from futures_perps.trade.apolo.historical_data import get_historical_data_limit_apolo, get_orderbook, get_funding_rate_history, get_public_liquidations

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Import your executor
from trading_bot.futures_executor_apolo import place_futures_order, get_close_price, get_available_balance, ORDERLY_ACCOUNT_ID, ORDERLY_SECRET, ORDERLY_PUBLIC_KEY

from trading_bot.send_bot_message import send_bot_message

# Import your liquidity persistence monitor
from futures_perps.trade.apolo import liquidity_persistence_monitor as lpm


# Helper: Format orderbook as text (not CSV!)
def format_orderbook_as_text(ob: dict) -> str:
    lines = ["Top Bids (price, quantity):"]
    for price, qty in ob.get('bids', [])[:15]:
        lines.append(f"{price},{qty}")
    
    lines.append("\nTop Asks (price, quantity):")
    for price, qty in ob.get('asks', [])[:15]:
        lines.append(f"{price},{qty}")
    
    return "\n".join(lines)


def analyze_with_llm(signal_dict: dict) -> dict:
    """LLM analyzes full candle context; Python enforces rules ONLY if prompt_mode == 'mixed'."""
    from logs.log_config import apolo_trader_logger as logger

    # === 1. Fetch market data (80 candles) ===
    df = get_historical_data_limit_apolo(
        symbol=signal_dict['asset'],
        interval=signal_dict['interval'],
        limit=80,
        strategy=signal_dict.get('indicator')
    )

    # ALWAYS fetch 5m for exhaustion confirmation (critical for your edge)
    five_min_df = get_historical_data_limit_apolo(
        symbol=signal_dict['asset'],
        interval="5m",
        limit=80,  # last 15 candles = 75 mins of 5m data
        strategy=signal_dict.get('indicator')
    )

    if df is None or len(df) < 20:
        return {
            "approved": False,
            "analysis": "Insufficient historical data",
            "explanation_for_user": "❌ No se pudieron cargar suficientes datos históricos para analizar la señal."
        }
    
    if five_min_df is None or len(five_min_df) < 10:
        return {
            "approved": False,
            "analysis": "Insufficient 5m data for exhaustion confirmation",
            "explanation_for_user": "❌ No se pudieron cargar suficientes datos de 5m para confirmar agotamiento."
        }

    latest_close = float(df['close'].iloc[-1])
    
    # === Trim CSV to avoid LLM timeout ===
    csv_content = df.to_csv(index=False)
    csv_lines = csv_content.split('\n')
    if len(csv_lines) > 30:
        csv_content = '\n'.join(csv_lines[:20] + ["... (middle truncated) ..."] + csv_lines[-10:])


    # get the last 15 rows of five_min_df as csv
    csv_5min_content = five_min_df.to_csv(index=False)
    csv_5min_lines = csv_5min_content.split('\n')
    if len(csv_5min_lines) > 16:  # 1 header + 15 data rows
        csv_5min_content = '\n'.join(
            csv_5min_lines[:1] +  # header
            csv_5min_lines[1:6] +  # first 5 data rows
            ["... (middle truncated) ..."] +
            csv_5min_lines[-10:]   # last 10 data rows
        )


    # === Live price ===
    live_price = get_close_price(ORDERLY_ACCOUNT_ID, signal_dict['asset'])
    if live_price is None:
        live_price = latest_close
        logger.warning("Falling back to candle close price (WebSocket failed)")
    price_delta_pct = (live_price / latest_close - 1) * 100

    # === Orderbook ===
    orderbook = get_orderbook(signal_dict['asset'], limit=20)
    orderbook_content = format_orderbook_as_text(orderbook)

    # === Balance & funding ===
    balance = get_available_balance(ORDERLY_SECRET, ORDERLY_ACCOUNT_ID, ORDERLY_PUBLIC_KEY)
    funding_data = get_funding_rate_history(symbol=signal_dict['asset'], limit=50)
    current_funding = float(funding_data[0].get('funding_rate', 0)) if funding_data else 0.0

    liquidation_data = get_public_liquidations(symbol=signal_dict['asset'], lookback_hours=24)
    nearby_liquidations = 0
    if liquidation_data:
        current_price = latest_close
        price_range = current_price * 0.02
        for liq in liquidation_data:
            for pos in liq.get('positions_by_perp', []):
                if pos.get('symbol') == signal_dict['asset']:
                    mark = float(pos.get('mark_price', 0))
                    if abs(mark - current_price) <= price_range:
                        nearby_liquidations += 1

    # === Parse risk settings ===
    try:
        min_sl_pct = float(signal_dict['min_sl']) / 100
        min_tp_pct = float(signal_dict['min_tp']) / 100
        leverage = int(signal_dict['leverage'])
        risk_level = float(signal_dict['risk_level'])
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid risk settings: {e}")
        return {
            "approved": False,
            "analysis": f"Invalid settings: {e}",
            "explanation_for_user": "❌ Error en la configuración del riesgo (SL, TP, apalancamiento o saldo)."
        }
    
    orderbook_threshold = float(get_setting("order_book_threshold") or 1.6)

    # === Build prompt ===
    user_prompt = get_setting("prompt_text") or ""   

    market_context_full = (
        f"\n\n 📊 CONTEXTO DE MERCADO PARA ANÁLISIS DE SEÑAL 📊"
        f"Activo: {signal_dict['asset']}\n"
        f"Intérvalo: {signal_dict['interval']}\n"
        f"Precio de cierre de la última vela: {latest_close:.6f}\n"
        f"Precio en vivo (último trade): {live_price:.6f}\n"
        f"Diferencia intra-candle: {price_delta_pct:+.3f}%\n"
        f"Saldo disponible: {balance:.2f} USDC\n"
        f"Apalancamiento: {leverage}x\n"
        f"Nivel de riesgo: {risk_level}%\n"
        f"Valor Stop Loss: {min_sl_pct*100:.2f}%\n"
        f"Valor Take Profit: {min_tp_pct*100:.2f}%\n"
        f"Tasa de funding actual: {current_funding:.6f}\n"
        f"Liquidaciones cercanas (±2%): {nearby_liquidations}\n\n"
        f"LIBRO DE ÓRDENES (top 20):\n{orderbook_content}\n\n"
        f"Threshold de imbalance requerido: {orderbook_threshold}x\n\n"
        f"📉 ÚLTIMAS 15 VELAS (5m) — PARA CONFIRMACIÓN DE AGOTAMIENTO:(15 de {len(df)} filas):\n{csv_5min_content}\n\n"
        f"HISTORIAL DE VELAS (30 de {len(df)} filas):\n{csv_content}"
    )
    
    response_format = """{
        "side": "BUY" or "SELL" or "NONE",
        "approved": true or false,
        "entry": 0.0,
        "take_profit": 0.0,
        "stop_loss": 0.0,
        "resume_of_analysis": "Analisys result, explanation. Format this field EXCLUSIVELY for Telegram with:\\n• Emojis (✅❌🚨⚡💎🛡️💰📊)\\n• \\n line breaks between sections\\n• NO markdown, NO asterisks"
    }"""

    prompt_mode = get_setting("prompt_mode") or "user_only"
    
    if prompt_mode == "mixed":
        prompt = f"""{user_prompt}

        {market_context_full}

        Responde EXCLUSIVAMENTE en este formato JSON:
        {response_format}"""    
    else:        
        prompt = f"""{user_prompt}

        📋 INSTRUCCIÓN FINAL:
        Analiza la señal basándote en los datos de mercado proporcionados.

        Responde EXCLUSIVAMENTE en este formato JSON:
        {response_format}"""    

    if get_setting("show_prompt") == "True":
        send_bot_message(int(os.getenv("TELEGRAM_CHAT_ID")), f"📝 Prompt ({len(prompt)} chars):\n{prompt}...")

    # === Call LLM ===
    response = None
    used_model = None
    last_error = None
    model_name = get_setting("llm_model")
    timeout_sec = 30
    
    try:
        logger.info(f"Trying LLM model: {model_name} with timeout {timeout_sec}s")
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('DEEP_SEEK_API_KEY')}"},
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 3000,
                "stream": False
            },
            timeout=timeout_sec
        )
        if response.status_code == 200:
            used_model = model_name
            logger.info(f"✓ LLM model {model_name} succeeded")
        else:
            last_error = f"Status {response.status_code}: {response.text[:200]}"
    except Exception as e:
        last_error = str(e)
        logger.warning(f"✗ LLM error: {e}")
    
    if response is None or response.status_code != 200:
        return {
            "approved": False,
            "analysis": f"LLM service unavailable: {last_error}",
            "explanation_for_user": "⚠️ Servicio de análisis temporalmente no disponible. Intente en 1 minuto."
        }

    # === Parse LLM response ===
    try:
        content = response.json()['choices'][0]['message']['content']
        json_start = content.find('{')
        json_end = content.rfind('}') + 1
        if json_start == -1 or json_end == 0:
            llm_result = json.loads(content.strip())
        else:
            llm_result = json.loads(content[json_start:json_end])
        
        required = ["side", "approved", "resume_of_analysis"]
        for field in required:
            if field not in llm_result:
                raise ValueError(f"Missing field: {field}")
    except Exception as e:
        logger.error(f"LLM parse failed: {e}")
        content_lower = content.lower()
        if "buy" in content_lower and ("approved" in content_lower or "true" in content_lower):
            llm_result = {"side": "BUY", "approved": True, "resume_of_analysis": "Fallback: BUY approved"}
        elif "sell" in content_lower and ("approved" in content_lower or "true" in content_lower):
            llm_result = {"side": "SELL", "approved": True, "resume_of_analysis": "Fallback: SELL approved"}
        else:
            llm_result = {"side": "NONE", "approved": False, "resume_of_analysis": "Fallback: rejected"}

    llm_side = llm_result.get("side", "NONE")
    llm_approved = bool(llm_result.get("approved", False))
    llm_reason = llm_result.get("resume_of_analysis", "No analysis")
    logger.info(f"LLM Decision: {llm_side} (Approved: {llm_approved})")

    # === FINAL DECISION LOGIC ===
    prompt_mode = get_setting("prompt_mode") or "user_only"

    final_approved = False
    final_side = "NONE"
    entry = latest_close
    stop_loss = take_profit = entry
    explanation_for_user = ""
    rejection_reasons = []

    # === USER_ONLY MODE: TRUST LLM COMPLETELY ===
    if llm_side in ("BUY", "SELL") and llm_approved:
        final_approved = True
        final_side = llm_side
        # Try to use LLM-provided prices; fallback to simple risk levels
        try:
            entry = float(llm_result.get("entry", latest_close))
            take_profit = float(llm_result.get("take_profit", 0))
            stop_loss = float(llm_result.get("stop_loss", 0))
            # If LLM gave invalid TP/SL, compute defaults
            if take_profit == 0 or stop_loss == 0:
                sl_dist = entry * min_sl_pct
                tp_dist = entry * min_tp_pct
                if llm_side == "BUY":
                    stop_loss = entry - sl_dist
                    take_profit = entry + tp_dist
                else:
                    stop_loss = entry + sl_dist
                    take_profit = entry - tp_dist
        except:
            sl_dist = entry * min_sl_pct
            tp_dist = entry * min_tp_pct
            if llm_side == "BUY":
                stop_loss = entry - sl_dist
                take_profit = entry + tp_dist
            else:
                stop_loss = entry + sl_dist
                take_profit = entry - tp_dist

        explanation_for_user = f"✅ APROBADA ({final_side}) — modo user_only (confianza total en LLM)"
    else:
        explanation_for_user = "❌ RECHAZADA — LLM no aprobó (modo user_only)"


    logger.info(f"Prompt mode: {prompt_mode} | Approved: {final_approved}, Side: {final_side}")

    return {
        "approved": final_approved,
        "symbol": signal_dict['asset'],
        "side": final_side,
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "resume_of_analysis": llm_reason,
        "analysis": content[:1000] + "..." if len(content) > 1000 else content,
        "explanation_for_user": explanation_for_user,
        "llm_model_used": used_model,
        "rejection_reasons": rejection_reasons if not final_approved else [],
        "warning_reasons": []
    }


def process_signal(asset_override=None):
    """
    Main entry point for signal processing.
    Called by Telegram bot. Must return a string.
    """
    try:
        # --- Fetch required settings ---
        asset = asset_override if asset_override else get_setting("asset")
        interval = get_setting("interval")
        min_tp = get_setting("min_tp")
        min_sl = get_setting("min_sl")
        #
        min_tp = float(min_tp)
        min_sl = float(min_sl)

        leverage = get_setting("leverage")
        risk_level = get_setting("risk_level")
        indicator = get_setting("indicator")

        # --- Validate settings ---
        missing = []
        if not asset: missing.append("asset")
        if not interval: missing.append("interval")
        if not min_tp: missing.append("min_tp")
        if not min_sl: missing.append("min_sl")
        if not leverage: missing.append("leverage")
        if not risk_level: missing.append("risk_level")

        if missing:
            return f"❌ Missing settings: {', '.join(missing)}. Please configure them via /list."

        # --- Convert types ---
        try:
            min_tp = float(min_tp)
            min_sl = float(min_sl)
            leverage = int(leverage)
            risk_level = float(risk_level)
        except (ValueError, TypeError) as e:
            return f"❌ Invalid setting format: {str(e)}"

        # --- Build signal dict ---
        signal_dict = {
            "asset": asset,
            "interval": interval,
            "min_tp": min_tp,
            "min_sl": min_sl,
            "leverage": leverage,
            "risk_level": risk_level,
            "indicator": indicator or "Trend-Following",
        }

        # --- Call LLM analyzer ---
        llm_result = analyze_with_llm(signal_dict)

        # --- Format response ---
        if isinstance(llm_result, dict) and llm_result.get("approved"):
            try:
                # the signal was approved, if the auto_trade setting is true, place the order
                # and create the dict required to place the order, the values are
                # symbol, side, take_profit, stop_loss, leverage
                auto_trade_val = get_setting("auto_trade")
                if auto_trade_val == "True" or auto_trade_val == "Automatic":
                    signal_dict = {
                        "symbol": llm_result['symbol'],
                        "side": llm_result['side'],
                        "entry": float(llm_result['entry']),   
                        "take_profit": float(llm_result['take_profit']),
                        "stop_loss": float(llm_result['stop_loss']),
                        "leverage": leverage
                    }
                    place_futures_order(signal_dict)  
                return (
                    f"✅ TRADE APPROVED\n"
                    f"• Symbol: {llm_result['symbol']}\n"
                    f"• Side: {llm_result['side']}\n"
                    f"• Entry: {float(llm_result['entry']):.6f}\n"
                    f"• TP: {float(llm_result['take_profit']):.6f}\n"
                    f"• SL: {float(llm_result['stop_loss']):.6f}\n"
                    f"• Reason: {llm_result.get('resume_of_analysis', 'N/A')}"
                )

            except (KeyError, ValueError, TypeError) as e:
                return f"⚠️ Trade approved but malformed output: {str(e)}"            
        else:
            if isinstance(llm_result, dict):
                # Prefer the clean analysis summary
                reason = llm_result.get("resume_of_analysis") or llm_result.get("analysis", "No reason provided.")
            else:
                reason = str(llm_result)

            # Clean up if reason starts with JSON (fallback)
            reason = str(reason).strip()
            if reason.startswith("{"):
                # Try to extract resume_of_analysis from raw JSON string
                try:
                    raw_json_start = reason.find('{')
                    raw_json_end = reason.rfind('}') + 1
                    raw_json_str = reason[raw_json_start:raw_json_end]
                    fallback = json.loads(raw_json_str)
                    reason = fallback.get("resume_of_analysis", "Trade rejected by LLM.")
                except:
                    reason = "Trade rejected due to failing hard rules (see analysis)."
            
            logger.info(f"Trade rejected. Reason: {reason}")

            return f"Trade rejected\n• Reason: {reason}"  # Allow slightly more for clarity

    except Exception as e:
        logger.exception("Error in process_signal")
        return f"🔥 Internal error: {str(e)}"

def autotrade():
    logger.info("Starting autotrade loop...")
    while True:
        try:
            if get_setting("auto_trade") == "Automatic":
                # Map interval string to timedelta
                interval_str = get_setting("interval")
                interval_map = {
                    '5m': timedelta(minutes=5),
                    '15m': timedelta(minutes=15),
                    '30m': timedelta(minutes=30),
                    '1h': timedelta(hours=1),
                    '4h': timedelta(hours=4),
                    '1d': timedelta(days=1)
                }
                trade_interval = interval_map.get(interval_str, timedelta(hours=1))
                
                automated_assets = get_setting("automated_assets")
                if automated_assets:
                    asset_list = [a.strip() for a in automated_assets.split(',') if a.strip()]
                    logger.info(f"Processing automated assets: {asset_list}")
                    for asset in asset_list:
                        try:
                            logger.info(f"Processing autotrade for each interval {interval_str} asset: {asset}")
                            process_signal(asset_override=asset)
                        except Exception as e:
                            logger.exception(f"Error processing automated asset {asset}: {e}")
                        time.sleep(10)
                else:
                    logger.info("Auto trade is Automatic but no assets configured.")
                
                # Sleep for the interval
                time.sleep(trade_interval.total_seconds())
            else:
                # Not automatic, sleep and check again later
                time.sleep(60)
        except Exception as e:
            logger.error(f"Error in autotrade loop: {e}")
            time.sleep(60)        
            
# if __name__ == "__main__":
#     asset = "PERP_BTC_USDC"
#     five_min_df = get_historical_data_limit_apolo(
#         symbol=asset,
#         interval="5m",
#         limit=50,  # last 15 candles = 75 mins of 5m data
#         strategy="Trend-Following"
#     )
#     # check len before printing
#     if five_min_df is None: 
#         print("No data returned")
#     else:
#         # print last 5 rows of dataframe
#         print(len(five_min_df))
