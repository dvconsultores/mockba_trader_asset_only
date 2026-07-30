"""
MockbaV4 — Futures grid scalper (Orderly DEX) — Amendment 001.

- OBI demoted to reference (logged, not gated)
- Adaptive thresholds via ATR: dip/pump/TP/SL scale with volatility
- Toxicity checks (observe-only by default)
- Single entry condition: price extreme >= adaptive threshold
- Direction gate by regime: RANGE→long+short, TREND_UP→long only, TREND_DOWN→short only
- Mandatory SL, leverage support, per-direction cooldown
"""

from __future__ import annotations
import time, uuid
from collections import deque
from typing import Optional

from trading_bot.executor import OrderlyFutures
from trading_bot.types import Fill
from trade.pnl import record_closed_trade, is_entry_blocked, compute_slot_size
from trade.regime import get_atr_pct
from trade.toxicity import evaluate as tox_eval, record_observation
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

def _cooldown_ok(a: str, s: str, cs: float) -> bool:
    return time.time()-_last_entry.get(f"orderly:{a}:{s}",0)>=cs

def _spacing_ok(a: str, p: float, sp: float) -> bool:
    for pos in load_all_positions(asset=a, venue="orderly"):
        ep=float(pos.get("entry_price",0))
        if ep>0 and abs(p-ep)/ep*100<sp: return False
    return True

def manage_open_positions(asset: str, exchange: OrderlyFutures, current_regime: str):
    """Check TP/SL fills, time stops, regime exits."""
    positions=load_all_positions(asset=asset, venue="orderly")
    if not positions: return
    sym=f"PERP_{asset}_USDC"; mh=get_setting_int("max_hold_minutes_futures",240)*60; now=time.time()
    for pd in positions:
        pid=pd["id"]; tpid=pd.get("tp_order_id"); slid=pd.get("sl_order_id")
        ep=float(pd["entry_price"]); sp=float(pd["signal_price"]); q=float(pd["qty"])
        op=float(pd["opened_at"]); si=pd.get("signal_id"); side=pd["side"]

        # Check TP fill
        if tpid and exchange.get_order_status(tpid)=="FILLED":
            _close(asset,"orderly",side,ep,float(pd["tp_price"]),sp,q,0.0003,pid,si,"tp"); continue

        # Check SL fill
        if slid and exchange.get_order_status(slid)=="FILLED":
            _close(asset,"orderly",side,ep,float(pd["sl_price"]),sp,q,0.0003,pid,si,"sl"); continue

        # Time stop
        if (now-op)>mh:
            if tpid: exchange.cancel_order(sym,tpid)
            if slid: exchange.cancel_order(sym,slid)
            _close(asset,"orderly",side,ep,ep,sp,q,0.0003,pid,si,"time_stop"); continue

        # Regime exit: adverse regime → move TP to breakeven
        adverse = (side=="long" and current_regime=="TREND_DOWN") or \
                  (side=="short" and current_regime=="TREND_UP")
        if adverse and tpid:
            fee_pct=0.06
            new_tp=ep*(1+fee_pct/100) if side=="long" else ep*(1-fee_pct/100)
            update_position(pid, tp_price=new_tp)

def _close(a,v,s,ep,xp,sp,q,fr,pid,si,rsn):
    fee_ep=ep*q*fr; fee_xp=xp*q*fr
    record_closed_trade(asset=a,venue=v,side=s,entry_price=ep,exit_price=xp,signal_price=sp,qty=q,fee_entry=fee_ep,fee_exit=fee_xp,opened_at=0,closed_at=time.time(),exit_reason=rsn)
    delete_position(a,v,pid)

def scalp_cycle(asset: str, exchange: OrderlyFutures, regime: str, obi: float, live_price: float, signal_only: bool = False) -> Optional[str]:
    venue="orderly"

    # Direction gate by regime (skip early return for signal-only mode)
    long_ok=regime in ("RANGE","TREND_UP")
    short_ok=regime in ("RANGE","TREND_DOWN")
    if not signal_only and not long_ok and not short_ok:
        _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped",f"regime={regime} blocks all directions"); return None

    if not signal_only:
        equity=exchange.get_equity()
        blocked,reason=is_entry_blocked(venue,equity)
        if blocked: _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped",reason); return None
        if len(load_all_positions(asset=asset,venue=venue))>=get_setting_int("max_slots",1):
            _log(asset,venue,regime,None,live_price,0,0,0,obi,0,None,{},"skipped","max_slots"); return None

    _update_price_memory(asset,live_price)

    adaptive=get_setting_bool("adaptive_enabled",True)
    atr=get_atr_pct(asset,venue) if adaptive else None
    dk=get_setting_float("dip_k",0.5); dm=get_setting_float("dip_min_pct",0.15)
    pk=get_setting_float("pump_k",0.5); pm=get_setting_float("pump_min_pct",0.15)
    tk=get_setting_float("tp_k",1.0); tm=get_setting_float("tp_min_pct",0.8)
    sk=get_setting_float("sl_k",0.6); sm=get_setting_float("sl_min_pct",0.5)

    if atr and atr>0:
        dn=max(dk*atr,dm); pn=max(pk*atr,pm); te=max(tk*atr,tm); se=max(sk*atr,sm)
    else:
        dn=dm; pn=pm; te=tm; se=sm

    if te<=se: _log(asset,venue,regime,None,live_price,0,dn,atr or 0,obi,0,None,{},"skipped","tp_eff<=sl_eff"); return None

    cs=get_setting_float("cooldown_sec",60); sp=get_setting_float("min_entry_spacing_pct",0.3)
    dip=_is_dip(asset,live_price,dn); pump=_is_pump(asset,live_price,pn)
    ext=_extreme_pct(asset,live_price)

    tox=tox_eval(asset,venue,0.05,1000,obi,ext)
    record_observation(asset,venue,0.05,1000,obi,ext)
    tbl=bool(tox.get("tox_enforced",0))

    direction="long" if (dip and long_ok) else ("short" if (pump and short_ok) else None)
    # Compute TP/SL prices for logging
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
        return {"direction": "buy" if direction=="long" else "sell", "tp": tp_price, "sl": sl_price}

    info=exchange.get_symbol_info(asset)
    if info is None: return None
    slot=compute_slot_size(venue,equity,info.min_notional)
    qty=slot/live_price; qty=qty-(qty%info.base_tick) if info.base_tick>0 else qty
    if qty<info.min_qty or (qty*live_price)<info.min_notional:
        _log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"skipped","qty too small",tp=tp_price,sl=sl_price); return None

    leverage=min(get_setting_int("leverage",3),get_setting_int("max_leverage",3))
    pid=str(uuid.uuid4())
    fill=exchange.place_entry(asset,direction,qty,live_price,te,se,leverage,pid)
    if fill is None: return None
    si=_log(asset,venue,regime,direction,live_price,ext,dn,atr or 0,obi,tox.get("velocity_pct",0),tox.get("depth_ratio"),tox,"entered",f"{direction} {abs(ext):.2f}%",tp=tp_price,sl=sl_price)
    _save_open(asset,venue,direction,fill,live_price,te,se,pid,si)
    _last_entry[f"{venue}:{asset}:{direction}"]=time.time()
    return "buy" if direction=="long" else "sell"

def _save_open(a,v,s,fill,sp,tp,sl,pid,si):
    tpp=fill.fill_price*(1+tp/100) if s=="long" else fill.fill_price*(1-tp/100)
    slp=fill.fill_price*(1-sl/100) if s=="long" else fill.fill_price*(1+sl/100)
    save_position({"id":pid,"asset":a,"venue":v,"side":s,"qty":fill.filled_qty,"entry_price":fill.fill_price,"signal_price":sp,"tp_price":tpp,"sl_price":slp,"tp_order_id":fill.order_id,"sl_order_id":None,"opened_at":time.time(),"signal_id":si})

def _log(a,v,r,d,p,ex,th,at,ob,vl,dr,tx,act,rsn,tp=0.0,sl=0.0):
    try:
        with get_db_connection() as conn:
            cur=conn.cursor()
            cur.execute("""INSERT INTO signals (ts,asset,venue,regime,direction,price,extreme_pct,threshold_pct,atr_pct,velocity_pct,obi,obi_z,spread_pct,spread_z,depth_top10,depth_ratio,tox_velocity,tox_spread,tox_depth,tox_obi,tox_any,tox_enforced,action,reason,tp_price,sl_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(),a,v,r,d,p,ex,th,at,vl,ob,tx.get("obi_z"),None,tx.get("spread_z"),None,dr,tx.get("tox_velocity"),tx.get("tox_spread"),tx.get("tox_depth"),tx.get("tox_obi"),tx.get("tox_any"),tx.get("tox_enforced",0),act,rsn,tp,sl))
            conn.commit(); return cur.lastrowid
    except: return None
