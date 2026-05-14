"""
Data Verification Test - Check ATR, Slopes, Data Ordering, and Regime Logic

Run this to verify:
1. Data is ordered correctly (oldest → newest)
2. ATR calculation matches expected values
3. Slopes are calculating correctly
4. Regime logic is not forcing TREND_UP during downtrends
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trade.historical_data import (
    get_historical_data_limit_apolo,
    get_orderbook,
)
from trade.main import ReversalScalper
from db.db_ops import get_setting, initialize_database_tables
from logs.log_config import apolo_trader_logger as logger


def test_data_ordering_and_atr():
    """Test if data is ordered correctly and ATR matches expected values."""
    
    print("\n" + "="*60)
    print("🔍 DATA VERIFICATION TEST")
    print("="*60)
    
    initialize_database_tables()
    
    asset = "PERP_NEAR_USDC"
    
    # === 1. FETCH 5m DATA ===
    print(f"\n1️⃣  Fetching 5m data for {asset}...")
    df_5m = get_historical_data_limit_apolo(symbol=asset, interval='5m', limit=100)
    
    if df_5m is None or len(df_5m) < 30:
        print("❌ Failed to fetch data")
        return False
    
    print(f"✅ Fetched {len(df_5m)} candles")
    print(f"Columns: {df_5m.columns.tolist()}")
    
    # === 2. CHECK DATA ORDERING ===
    print("\n2️⃣  Checking data ordering...")
    first_ts = df_5m['start_timestamp'].iloc[0]
    last_ts = df_5m['start_timestamp'].iloc[-1]
    
    print(f"First timestamp: {first_ts} (should be oldest)")
    print(f"Last timestamp:  {last_ts} (should be newest)")
    
    if first_ts > last_ts:
        print("❌ Data is REVERSED! (newest first) - This breaks ATR and slope calculations")
        print("   SOLUTION: Data needs to be sorted oldest → newest")
        return False
    else:
        print("✅ Data ordering is CORRECT (oldest → newest)")
    
    # === 3. CALCULATE ATR ===
    print("\n3️⃣  Calculating ATR...")
    scalper = ReversalScalper()
    atr = scalper._calculate_atr(df_5m, period=14)
    last_close = df_5m['close'].iloc[-1]
    atr_pct = (atr / last_close) * 100
    
    print(f"Last close:            {last_close:.6f}")
    print(f"ATR (14):              {atr:.6f}")
    print(f"ATR as % of price:     {atr_pct:.3f}%")
    
    # Manual True Range calculation for verification
    print("\nManual ATR verification (last 5 candles):")
    highs = df_5m['high'].values
    lows = df_5m['low'].values
    closes = df_5m['close'].values
    
    tr_list = []
    for i in range(len(df_5m)):
        if i == 0:
            tr_val = highs[i] - lows[i]
        else:
            tr_val = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
        tr_list.append(tr_val)
    
    atr_manual = np.mean(tr_list[-14:])
    print(f"Manual ATR from last 14: {atr_manual:.6f}")
    print(f"Difference:             {abs(atr - atr_manual):.8f} (should be ~0)")
    
    # === 4. CALCULATE SLOPES ===
    print("\n4️⃣  Calculating slopes...")
    scalper_with_data = ReversalScalper()
    
    # 5m slope
    closes_5m = df_5m['close'].values[-10:]  # Last 10 candles
    x_5m = np.arange(len(closes_5m))
    slope_5m_manual = np.polyfit(x_5m, closes_5m, 1)[0]
    slope_5m_pct = (slope_5m_manual / last_close) * 100
    
    print(f"5m slope (last 10):    {slope_5m_manual:.6f} ({slope_5m_pct:+.3f}%)")
    if slope_5m_manual < -0.0005:
        print("   → CLEARLY DOWNTREND (slope < -0.0005)")
    elif slope_5m_manual > 0.0005:
        print("   → CLEARLY UPTREND (slope > 0.0005)")
    else:
        print("   → CHOPPY/RANGE market (slope near-zero)")
    
    # 1h slope
    df_1h = get_historical_data_limit_apolo(symbol=asset, interval='1h', limit=100)
    if df_1h is not None and len(df_1h) > 10:
        closes_1h = df_1h['close'].values[-10:]
        x_1h = np.arange(len(closes_1h))
        slope_1h_manual = np.polyfit(x_1h, closes_1h, 1)[0]
        slope_1h_pct = (slope_1h_manual / df_1h['close'].iloc[-1]) * 100
        print(f"1h slope (last 10):    {slope_1h_manual:.6f} ({slope_1h_pct:+.3f}%)")
    
    # === 5. TEST REGIME DETECTION ===
    print("\n5️⃣  Testing regime detection...")
    
    # Get current orderbook for OBI
    ob = get_orderbook(asset, limit=10)
    if ob:
        bids = float(ob['bids'][0][0]) if ob.get('bids') else 0
        asks = float(ob['asks'][0][0]) if ob.get('asks') else 0
        obi = bids / asks if asks > 0 else 1.0
        print(f"Current OBI:           {obi:.4f}")
        
        detection_result = scalper._detect_regime(df_5m, df_1h if df_1h is not None else None, ob)
        regime = detection_result['regime']
        obi_boosted = detection_result.get('obi_boosted', False)
        
        print(f"Detected regime:       {regime}")
        print(f"OBI boosted:           {obi_boosted}")
        
        # Check if TREND_UP is appropriate
        if regime == 'TREND_UP' and slope_5m_manual < -0.0005:
            print("❌ ERROR: Regime is TREND_UP but 5m slope is clearly DOWN!")
            print("   This is the OBI override bug - it shouldn't force TREND_UP during downtrends")
        elif regime == 'TREND_UP' and slope_5m_manual < 0.0001:
            print("⚠️  WARNING: Regime is TREND_UP but slope is near-zero")
            print("   Make sure this is not a false OBI signal")
        else:
            print("✅ Regime detection looks OK")
    
    # === 6. PATTERN DETECTION ===
    print("\n6️⃣  Testing pattern detection...")
    if len(df_5m) >= 10:
        # Use the last close as live price for testing
        live_price = df_5m['close'].iloc[-1]
        pattern = scalper._detect_reversal_pattern(df_5m, live_price)
        
        if pattern:
            print(f"✅ Pattern detected: {pattern}")
        else:
            print("ℹ️  No pattern detected in current data (may be normal)")
    
    print("\n" + "="*60)
    print("✅ Data verification complete!")
    print("="*60 + "\n")
    
    return True


if __name__ == "__main__":
    try:
        test_data_ordering_and_atr()
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
