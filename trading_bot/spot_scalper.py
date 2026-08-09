"""
MockbaV4 — Spot grid scalper (Binance) — Amendment 001.

- OBI demoted to reference (logged, not gated)
- Adaptive thresholds via ATR: dip/pump/TP/SL scale with volatility
- Toxicity checks (observe-only by default)
- Single entry condition: price extreme >= adaptive threshold
"""

from __future__ import annotations
import time, uuid
from collections import deque
from typing import Optional

from trading_bot.executor import BinanceSpot
from trading_bot.types import Fill
from logs.log_config import apolo_trader_logger as logger
from trade.pnl import record_closed_trade, is_entry_blocked, compute_slot_size
from trade.regime import get_atr_pct
from trade.toxicity import evaluate as tox_eval, record_observation
from trade.universe import compute_thresholds  # shared with universe replay (Amendment 003)
from db.db_ops import (
    get_setting_float, get_setting_int, get_setting_bool,
    save_position, load_all_positions, update_position, delete_position,
    get_db_connection,
)

_price_memory: dict[str, deque] = {}
_peak: dict[str, float] = {}
_trough: dict[str, float] = {}
WINDOW_SIZE = 40

def _ensure_memory(a: str):
    if a not in _price_memory:
        _price_memory[a] = deque(maxlen=WINDOW_SIZE)
        _peak[a] = 0.0; _trough[a] = float("inf")

def _update_price_memory(a: str, p: float):
    _ensure_memory(a)
    if p > 0:
        _price_memory[a].append(p)
        _peak[a] = max(_price_memory[a])
        _trough[a] = min(_price_memory[a])

def _is_dip(a: str, p: float, th: float) -> bool:
    return _peak.get(a,0)>0 and len(_price_memory.get(a,[]))>=10 and (_peak[a]-p)/_peak[a]*100>=th

def _is_pump(a: str, p: float, th: float) -> bool:
    t=_trough.get(a,float("inf"))
    return t!=float("inf") and len(_price_memory.get(a,[]))>=10 and (p-t)/t*100>=th

def _extreme_pct(a: str, p: float) -> float:
    pk=_peak.get(a,0); tr=_trough.get(a,float("inf"))
    d=(pk-p)/pk*100 if pk>0 else 0
    u=(p-tr)/tr*100 if tr!=float("inf") else 0
    return -d if d>u else u

_last_entry: dict[str,float]={}
_last_sl: dict[str,float]={}
# After a stop-loss, block re-entry into the same (asset, side) for this many × cooldown_sec
SL_COOLDOWN_MULT = 10

def _cooldown_ok(a: str, s: str, cs: float) -> bool:
    now=time.time()
    if now-_last_entry.get(f"binance:{a}:{s}",0)<cs: return False
    if now-_last_sl.get(f"binance:{a}:{s}",0)<cs*SL_COOLDOWN_MULT: return False
    return True

def _spacing_ok(a: str, p: float, sp: float) -> bool:
    for pos in load_all_positions(asset=a, venue="binance"):
        ep=float(pos.get("entry_price",0))
        if ep>0 and abs(p-ep)/ep*100<sp: return False
    return True

def manage_open_positions(asset: str, exchange: BinanceSpot):
    positions=load_all_positions(asset=asset, venue="binance")
    if not positions: return
    sym=f"{asset}USDT"; mh=get_setting_int("max_hold_minutes_spot",120)*60; now=time.time()
    # Live price for price-based SL check — at/below sl_price: cancel TP limit + market sell
    live = exchange.get_price(asset) if any(pd.get("sl_price") for pd in positions) else None
    for pd in positions:
        pid=pd["id"]; tpid=pd.get("tp_order_id"); slid=pd.get("sl_order_id")
        ep=float(pd["entry_price"]); sp=float(pd["signal_price"]); q=float(pd["qty"])
        op=float(pd["opened_at"]); si=pd.get("signal_id")
        fee_ep = float(pd.get("fee_entry") or 0.0)
        slp = float(pd["sl_price"]) if pd.get("sl_price") else 0.0
        # Exchange-side fills first: if TP/SL already filled the coins are gone, so
        # close the position instead of attempting a phantom market sell below.
        if tpid and exchange.get_order_status(sym,tpid)=="FILLED":
            xp, fee_xp = _real_fill(exchange, sym, tpid, float(pd["tp_price"]))
            _close(asset,"binance","long",ep,xp,sp,q,pid,si,"tp",fee_ep,fee_xp); continue
        if slid and exchange.get_order_status(sym,slid)=="FILLED":
            _close(asset,"binance","long",ep,float(pd.get("sl_price",ep)),sp,q,pid,si,"sl",fee_ep,0.0); continue
        if slp > 0 and live is not None and live <= slp:
            # Free the coins held by the open TP/SL orders first; only sell once the
            # balance is actually free, otherwise the market sell gets -2010.
            tp_cancel_ok = (not tpid) or exchange.cancel_order(sym, tpid)
            if slid: exchange.cancel_order(sym, slid)
            if not tp_cancel_ok:
                logger.error(f"[EXIT] {asset} sl: TP cancel failed — cannot free balance, keeping position to retry")
                continue
            sell = exchange.market_sell(asset, q)
            if sell is None:
                # Coins likely already gone (TP filled between checks) — close as TP if so.
                if tpid and exchange.get_order_status(sym,tpid)=="FILLED":
                    xp, fee_xp = _real_fill(exchange, sym, tpid, float(pd["tp_price"]))
                    _close(asset,"binance","long",ep,xp,sp,q,pid,si,"tp",fee_ep,fee_xp)
                else:
                    bal = exchange.get_asset_balance(asset)
                    if bal is not None and bal < q:
                        # Coins are not held on the exchange — position already gone.
                        # Recover the real exit from the TP order if it filled, else orphan.
                        if tpid:
                            fill_info = exchange.get_order_fills(sym, tpid)
                            if fill_info:
                                xp, fee_xp = fill_info
                                logger.warning(f"[EXIT] {asset} sl: no balance ({bal}) — closing as TP via real fill {xp}")
                                _close(asset,"binance","long",ep,xp,sp,q,pid,si,"tp",fee_ep,fee_xp)
                                continue
                        xp = live if live else ep
                        logger.warning(f"[EXIT] {asset} sl: no balance ({bal}) — closing orphan position at {xp}")
                        _close(asset,"binance","long",ep,xp,sp,q,pid,si,"orphan",fee_ep,0.0)
                    else:
                        logger.error(f"[EXIT] {asset} sl: market sell failed — keeping position to retry")
                continue
            xp = sell.fill_price if sell.fill_price > 0 else slp
            _close(asset,"binance","long",ep,xp,sp,q,pid,si,"sl",fee_ep,sell.fee_amount)
            continue
        if (now-op)>mh:
            if tpid: exchange.cancel_order(sym,tpid)
            if slid: exchange.cancel_order(sym,slid)
            sell = exchange.market_sell(asset, q)
            if sell is None:
                bal = exchange.get_asset_balance(asset)
                if bal is not None and bal < q:
                    # Coins are not held on the exchange — position already gone.
                    # Recover the real exit from the TP order if it filled, else orphan.
                    if tpid:
                        fill_info = exchange.get_order_fills(sym, tpid)
                        if fill_info:
                            xp, fee_xp = fill_info
                            logger.warning(f"[EXIT] {asset} time_stop: no balance ({bal}) — closing as TP via real fill {xp}")
                            _close(asset,"binance","long",ep,xp,sp,q,pid,si,"tp",fee_ep,fee_xp)
                            continue
                    xp = live if live else ep
                    logger.warning(f"[EXIT] {asset} time_stop: no balance ({bal}) — closing orphan position at {xp}")
                    _close(asset,"binance","long",ep,xp,sp,q,pid,si,"orphan",fee_ep,0.0)
                else:
                    logger.error(f"[EXIT] {asset} time_stop: market sell failed — keeping position to retry")
                continue
            xp = sell.fill_price if sell.fill_price > 0 else ep
            _close(asset,"binance","long",ep,xp,sp,q,pid,si,"time_stop",fee_ep,sell.fee_amount)

def _real_fill(exchange, sym, order_id, default_price):
    """Real exit price + commission for a filled order, else (default_price, 0)."""
    try:
        res = exchange.get_order_fills(sym, order_id)
        if res:
            return res
    except Exception:
        pass
    return float(default_price), 0.0

def _close(a,v,s,ep,xp,sp,q,pid,si,rsn,fee_ep=0.0,fee_xp=0.0):
    if rsn == "sl":
        _last_sl[f"{v}:{a}:{s}"]=time.time()
    if fee_ep <= 0: fee_ep = ep*q*0.001
    if fee_xp <= 0: fee_xp = xp*q*0.001
    record_closed_trade(asset=a,venue=v,side=s,entry_price=ep,exit_price=xp,signal_price=sp,qty=q,fee_entry=fee_ep,fee_exit=fee_xp,opened_at=0,closed_at=time.time(),exit_reason=rsn)
    delete_position(a,v,pid)

def scalp_cycle(asset: str, exchange: BinanceSpot, regime: str, obi: float, live_price: float, signal_only: bool = False) -> Optional[str]:
    venue="binance"
    if not signal_only:
        if regime=="TREND_DOWN": _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped","regime=TREND_DOWN"); return None
        if regime not in ("RANGE","TREND_UP"): _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped",f"regime={regime}"); return None

    if not signal_only:
        equity=exchange.get_equity()
        blocked,reason=is_entry_blocked(venue,equity)
        if blocked: _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped",reason); return None
        if len(load_all_positions(asset=asset,venue=venue))>=get_setting_int("max_slots_cex",9):
            _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped","max_slots_cex"); return None

    _update_price_memory(asset,live_price)

    adaptive=get_setting_bool("adaptive_enabled",True)
    atr=get_atr_pct(asset,venue) if adaptive else None
    dk=get_setting_float("dip_k",0.5); dm=get_setting_float("dip_min_pct",0.15)
    pk=get_setting_float("pump_k",0.5); pm=get_setting_float("pump_min_pct",0.15)
    tk=get_setting_float("tp_k",1.0); tm=get_setting_float("tp_min_pct",0.8)
    sk=get_setting_float("sl_k",0.6); sm=get_setting_float("sl_min_pct",0.5)

    dn, pn, te, se = compute_thresholds(atr, dk, dm, pk, pm, tk, tm, sk, sm)

    if te<=se: _log(asset,venue,regime,None,live_price,0,dn,atr or 0,obi,0,None,{},"skipped","tp_eff<=sl_eff"); return None

    cs=get_setting_float("cooldown_sec",60); sp=get_setting_float("min_entry_spacing_pct",0.3)
    dip=_is_dip(asset,live_price,dn); pump=_is_pump(asset,live_price,pn)
    ext=_extreme_pct(asset,live_price)

    tox=tox_eval(asset,venue,0.05,1000,obi,ext)
    record_observation(asset,venue,0.05,1000,obi,ext)
    tbl=bool(tox.get("tox_enforced",0))

    direction="long" if dip else None
    # Compute TP/SL prices for logging (always available after direction is set)
    tp_price = live_price * (1 + te/100) if direction == "long" else (live_price * (1 - te/100) if direction == "short" else 0.0)
    sl_price = live_price * (1 - se/100) if direction == "long" else (live_price * (1 + se/100) if direction == "short" else 0.0)
    if direction is None:
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","below_threshold"); return None
    if tbl:
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","toxicity",tp=tp_price,sl=sl_price); return None
    if not _cooldown_ok(asset,direction,cs):
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","cooldown",tp=tp_price,sl=sl_price); return None
    if not _spacing_ok(asset,live_price,sp):
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","spacing",tp=tp_price,sl=sl_price); return None

    if signal_only:
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"signaled",f"{direction} {abs(ext):.2f}%",tp=tp_price,sl=sl_price)
        _last_entry[f"{venue}:{asset}:{direction}"]=time.time()
        return {"direction": "buy" if direction=="long" else "sell", "tp": tp_price, "sl": sl_price, "tp_pct": te, "sl_pct": se}

    info=exchange.get_symbol_info(asset)
    if info is None: return None
    slot=compute_slot_size(venue,equity,info.min_notional)
    qty=slot/live_price; qty=qty-(qty%info.base_tick) if info.base_tick>0 else qty
    if qty<info.min_qty or (qty*live_price)<info.min_notional:
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","qty too small",tp=tp_price,sl=sl_price); return None

    pid=str(uuid.uuid4())
    fill=exchange.place_entry(asset,direction,qty,live_price,te,pid,se)
    if fill is None: return None
    si=_log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"entered",f"{direction} {abs(ext):.2f}%",tp=tp_price,sl=sl_price)
    _save_open(asset,venue,direction,fill,live_price,te,se,pid,si)
    _last_entry[f"{venue}:{asset}:{direction}"]=time.time()
    return {"direction": "buy" if direction=="long" else "sell", "tp": tp_price, "sl": sl_price, "tp_pct": te, "sl_pct": se}

def _save_open(a,v,s,fill,sp,tp,sl,pid,si):
    tpp=fill.fill_price*(1+tp/100) if s=="long" else fill.fill_price*(1-tp/100)
    slp=fill.fill_price*(1-sl/100) if s=="long" else fill.fill_price*(1+sl/100)
    save_position({"id":pid,"asset":a,"venue":v,"side":s,"qty":fill.sellable_qty,"entry_price":fill.fill_price,"signal_price":sp,"tp_price":tpp,"sl_price":slp if sl>0 else None,"tp_order_id":fill.tp_order_id,"sl_order_id":fill.sl_order_id,"opened_at":time.time(),"signal_id":si,"fee_entry":fill.fee_amount})

def _log(a,v,r,d,p,ex,th,at,ob,vl,dr,tx,act,rsn,tp=0.0,sl=0.0):
    try:
        with get_db_connection() as conn:
            cur=conn.cursor()
            cur.execute("""INSERT INTO signals (ts,asset,venue,regime,direction,price,extreme_pct,threshold_pct,atr_pct,velocity_pct,obi,obi_z,spread_pct,spread_z,depth_top10,depth_ratio,tox_velocity,tox_spread,tox_depth,tox_obi,tox_any,tox_enforced,action,reason,tp_price,sl_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(),a,v,r,d,p,ex,th,at,vl,ob,tx.get("obi_z"),None,tx.get("spread_z"),None,dr,tx.get("tox_velocity"),tx.get("tox_spread"),tx.get("tox_depth"),tx.get("tox_obi"),tx.get("tox_any"),tx.get("tox_enforced",0),act,rsn,tp,sl))
            conn.commit(); return cur.lastrowid
    except Exception:
        from logs.log_config import apolo_trader_logger
        apolo_trader_logger.error(f"[DB] failed to insert signal: {a} {v} {act} {rsn}", exc_info=True)
        return None
