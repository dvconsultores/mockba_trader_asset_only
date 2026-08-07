"""
DEX signal test — simulates futures_scalper.scalp_cycle for NEAR for ~10 minutes.
Uses live Binance price/OBI data (Orderly proxy). Reports every cycle.
"""
import time, sys, os
from collections import deque
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

ASSET = "NEAR"
VENUE = "orderly"
CYCLES = 20  # ~10 min at 30s intervals
SLEEP = 30

# ── Price memory (mirrors futures_scalper.py) ──
_price_mem: deque = deque(maxlen=40)
_peak = 0.0
_trough = float("inf")
_last_entry_log: dict[str, float] = {}

# ── Settings (from DB defaults) ──
from db.db_ops import get_setting_float, get_setting_bool, get_setting_int

adaptive = get_setting_bool("adaptive_enabled", True)
dk = get_setting_float("dip_k", 0.5); dm = get_setting_float("dip_min_pct", 0.15)
pk = get_setting_float("pump_k", 0.5); pm = get_setting_float("pump_min_pct", 0.15)
tk = get_setting_float("tp_k", 1.0); tm = get_setting_float("tp_min_pct", 0.8)
sk = get_setting_float("sl_k", 0.6); sm = get_setting_float("sl_min_pct", 0.5)
cs = get_setting_float("cooldown_sec", 60)
sp = get_setting_float("min_entry_spacing_pct", 0.3)

print(f"=== DEX Signal Test: {ASSET} ===\n")
print(f"Settings: dip_k={dk} dip_min={dm}% pump_k={pk} pump_min={pm}%")
print(f"          tp_k={tk} tp_min={tm}% sl_k={sk} sl_min={sm}%")
print(f"          cooldown={cs}s spacing={sp}% adaptive={adaptive}")
print(f"          cycles={CYCLES} sleep={SLEEP}s total≈{CYCLES*SLEEP//60}min\n")

# ── Fetch helpers ──
def get_price(asset):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": f"{asset}USDT"}, timeout=5)
        return float(r.json()["price"])
    except Exception as e:
        print(f"  ⚠️ price fetch failed: {e}")
        return None

def get_obi(asset):
    try:
        r = requests.get("https://api.binance.com/api/v3/depth",
                         params={"symbol": f"{asset}USDT", "limit": 10}, timeout=5)
        data = r.json()
        bids = sum(float(b[1]) for b in data.get("bids", []))
        asks = sum(float(a[1]) for a in data.get("asks", []))
        if asks == 0:
            return None
        return bids / asks
    except Exception as e:
        print(f"  ⚠️ OBI fetch failed: {e}")
        return None

def get_regime(asset):
    """Quick regime check from recent candles."""
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": f"{asset}USDT", "interval": "1h", "limit": 30},
                         timeout=10)
        closes = [float(k[4]) for k in r.json()]
        n = len(closes)
        if n < 2:
            return "UNKNOWN"
        x_mean = (n-1)/2; y_mean = sum(closes)/n
        num = sum((i-x_mean)*(closes[i]-y_mean) for i in range(n))
        den = sum((i-x_mean)**2 for i in range(n))
        if den == 0:
            return "RANGE"
        slope = (num/den) / y_mean
        threshold = get_setting_float("slope_threshold", 0.0012)
        if slope > threshold:
            return "TREND_UP"
        elif slope < -threshold:
            return "TREND_DOWN"
        return "RANGE"
    except Exception as e:
        return f"ERROR:{e}"

def get_atr(asset):
    """Simple ATR from 5m candles."""
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": f"{asset}USDT", "interval": "5m", "limit": 14},
                         timeout=10)
        candles = r.json()
        trs = []
        for i in range(1, len(candles)):
            h = float(candles[i][2]); l = float(candles[i][3])
            pc = float(candles[i-1][4])
            tr = max(h-l, abs(h-pc), abs(l-pc))
            trs.append(tr)
        if not trs:
            return 0.0
        avg_tr = sum(trs)/len(trs)
        price = float(candles[-1][4])
        return (avg_tr/price)*100 if price > 0 else 0.0
    except Exception:
        return 0.0


print(f"{'#':>3} {'time':<8} {'price':>9} {'peak':>9} {'trough':>9} {'ext%':>7} {'th%':>6} {'atr%':>6} {'OBI':>6} {'regime':<12} {'result':<25}")
print("-" * 120)

signals_fired = 0
skipped_reasons: dict[str, int] = {}

for i in range(1, CYCLES + 1):
    t = datetime.now().strftime("%H:%M:%S")
    price = get_price(ASSET)
    obi = get_obi(ASSET)

    if price is None or obi is None:
        print(f"{i:>3} {t:<8} {'N/A':>9} {'—':>9} {'—':>9} {'—':>7} {'—':>6} {'—':>6} {'—':>6} {'—':<12} {'DATA MISSING':<25}")
        time.sleep(SLEEP)
        continue

    if price > 0:
        _price_mem.append(price)
        _peak = max(_price_mem) if _price_mem else price
        _trough = min(_price_mem) if _price_mem else price

    # Regime (cached per run)
    if i == 1:
        regime = get_regime(ASSET)
        atr = get_atr(ASSET)
        print(f"  → Initial regime: {regime}, ATR(14,5m): {atr:.4f}%")
    elif i % 5 == 0:
        # Refresh regime every 5 cycles
        regime = get_regime(ASSET)
        atr = get_atr(ASSET)
    # (regime & atr stay from last fetch)

    # Calculate thresholds
    if adaptive and atr and atr > 0:
        dn = max(dk*atr, dm); pn = max(pk*atr, pm)
        te = max(tk*atr, tm); se = max(sk*atr, sm)
    else:
        dn = dm; pn = pm; te = tm; se = sm

    # Dip/pump detection
    dip = _peak > 0 and len(_price_mem) >= 10 and (_peak - price) / _peak * 100 >= dn
    pump = _trough != float("inf") and len(_price_mem) >= 10 and (price - _trough) / _trough * 100 >= pn

    # Extreme %
    if _peak > 0 and _trough != float("inf"):
        d_pct = (_peak - price) / _peak * 100
        u_pct = (price - _trough) / _trough * 100
        ext = -d_pct if d_pct > u_pct else u_pct
    else:
        ext = 0.0
        d_pct = 0.0
        u_pct = 0.0

    # Direction gate
    long_ok = regime in ("RANGE", "TREND_UP")
    short_ok = regime in ("RANGE", "TREND_DOWN")
    direction = "long" if (dip and long_ok) else ("short" if (pump and short_ok) else None)

    # Determine result
    if te <= se:
        result = "SKIP: tp_eff<=sl_eff"
    elif direction is None:
        result = "SKIP: below_threshold"
    else:
        # Cooldown check
        cd_key = f"{VENUE}:{ASSET}:{direction}"
        if time.time() - _last_entry_log.get(cd_key, 0) < cs:
            result = f"SKIP: cooldown ({direction})"
        else:
            tp_price = price * (1 + te/100) if direction == "long" else price * (1 - te/100)
            sl_price = price * (1 - se/100) if direction == "long" else price * (1 + se/100)
            result = f"SIGNAL: {direction} TP={tp_price:.4f} SL={sl_price:.4f}"
            _last_entry_log[cd_key] = time.time()
            signals_fired += 1

    # Track skip reasons
    if result.startswith("SKIP:"):
        reason = result.split(": ", 1)[1]
        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    # Print cycle
    peak_str = f"{_peak:.4f}" if _peak > 0 else "—"
    trough_str = f"{_trough:.4f}" if _trough != float("inf") else "—"
    ext_str = f"{ext:+.4f}" if ext != 0 else "0"
    th_str = f"{dn:.3f}" if direction == "long" or (dip and not long_ok) else (f"{pn:.3f}" if direction == "short" or (pump and not short_ok) else f"{dn:.3f}")
    atr_str = f"{atr:.4f}" if atr else "—"
    obi_str = f"{obi:.3f}" if obi else "—"

    print(f"{i:>3} {t:<8} {price:>9.4f} {peak_str:>9} {trough_str:>9} {ext_str:>7} {th_str:>6} {atr_str:>6} {obi_str:>6} {regime:<12} {result:<25}")

    if i < CYCLES:
        time.sleep(SLEEP)

print(f"\n{'='*60}")
print(f"Test complete. {CYCLES} cycles, {signals_fired} signals fired.")
if skipped_reasons:
    print("Skip reasons:")
    for reason, count in sorted(skipped_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
print(f"Price memory size: {len(_price_mem)}")
if _price_mem:
    print(f"Price range: {min(_price_mem):.4f} — {max(_price_mem):.4f}")
    print(f"Peak-to-trough spread: {(_peak - _trough) / _trough * 100:.4f}%")
