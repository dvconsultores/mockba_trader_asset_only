#!/usr/bin/env python3
"""
Demo / Dry-Run Script for the Refactored Arbitrage System.

Runs every component automatically with 1s pauses between steps.
No user input needed. No real trades placed.

Usage:
    python trade/demo_arbitrage.py
"""

import os
import sys
import time
from pathlib import Path

# ── Resolve project root regardless of where script is run from ──────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)  # ensure CWD is project root
sys.path.insert(0, str(PROJECT_ROOT))

# ── Setup ────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from db.db_ops import (
    initialize_database_tables,
    initialize_capital_allocation,
    insert_inventory_snapshot,
    get_latest_inventory,
    get_current_capital_allocation,
    record_capital_change,
    get_arbitrage_run_state,
    set_arbitrage_run_state,
)
from trade.spread_llm_analyzer import (
    calculate_break_even_threshold,
    executable_spread,
    get_binance_book_tickers_bulk,
    get_bitget_book_tickers_bulk,
    get_binance_quote_volume_bulk,
    is_tradable,
    is_observed,
    is_spread_sane,
    TRADABLE_QUOTE_ASSETS,
    OBSERVE_QUOTE_ASSETS,
    MAX_SPREAD_PCT,
    MIN_TOP_BOOK_NOTIONAL_USDT,
    MIN_LIQUIDITY_24H_USDT,
)

# ── Startup banner ───────────────────────────────────────────────────────────
print("⚡ Arbitrage Demo — loading...", flush=True)


def main():
    MOCK_USDT = 200.0

    print(f"Capital: ${MOCK_USDT:.0f}/exchange  |  Mode: SIMULATION\n", flush=True)

    # Init DB & clean previous demo data
    initialize_database_tables()
    import sqlite3
    from db.db_ops import DB_PATH as _DB_PATH
    with sqlite3.connect(_DB_PATH) as _conn:
        _conn.execute("DELETE FROM arbitrage_capital_allocation WHERE reason LIKE 'demo_%' OR reason = 'initial_allocation'")
        _conn.commit()
    initialize_capital_allocation("binance", MOCK_USDT)
    initialize_capital_allocation("bitget", MOCK_USDT)
    bn_alloc = get_current_capital_allocation("binance")
    bg_alloc = get_current_capital_allocation("bitget")
    if bn_alloc != MOCK_USDT:
        record_capital_change("binance", MOCK_USDT, MOCK_USDT - bn_alloc, "demo_reset")
    if bg_alloc != MOCK_USDT:
        record_capital_change("bitget", MOCK_USDT, MOCK_USDT - bg_alloc, "demo_reset")
    bn_alloc = get_current_capital_allocation("binance")
    bg_alloc = get_current_capital_allocation("bitget")
    total_capital = bn_alloc + bg_alloc

    be = calculate_break_even_threshold()
    print(f"Break-even: {be:.4f}%  |  Tradable: {', '.join(TRADABLE_QUOTE_ASSETS)}", flush=True)
    if OBSERVE_QUOTE_ASSETS:
        print(f"Observe-only: {', '.join(OBSERVE_QUOTE_ASSETS)}", flush=True)
    print(flush=True)

    # ── Fetch live data ──────────────────────────────────────────────────────
    print("Fetching live order books from Binance & Bitget...", flush=True)
    try:
        binance_books = get_binance_book_tickers_bulk()
        bitget_books = get_bitget_book_tickers_bulk()
        binance_volumes = get_binance_quote_volume_bulk()
        common = set(binance_books.keys()) & set(bitget_books.keys())
        # Only keep symbols matching our configured quote assets
        common = {s for s in common if is_observed(s)}
        print(f"  {len(binance_books)} Binance + {len(bitget_books)} Bitget = {len(common)} matching symbols", flush=True)
    except Exception as e:
        print(f"  ⚠ API error: {e}", flush=True)
        return

    # ── Rank by composite score (spread × depth, with sanity & liquidity filters)
    print(f"  Filters: max_spread={MAX_SPREAD_PCT}% | min_notional=${MIN_TOP_BOOK_NOTIONAL_USDT:.0f} | min_vol=${MIN_LIQUIDITY_24H_USDT:.0f}\n", flush=True)

    rejected_sanity = 0
    rejected_liquidity = 0
    ranked = []

    for symbol in common:
        bn = binance_books.get(symbol, {})
        bg = bitget_books.get(symbol, {})
        bn_volume = binance_volumes.get(symbol, 0.0)
        bg_volume = bg.get("usdt_volume", 0.0)
        bn_ask = bn.get("ask", 0) or 0; bn_ask_qty = bn.get("ask_qty", 0) or 0
        bn_bid = bn.get("bid", 0) or 0; bn_bid_qty = bn.get("bid_qty", 0) or 0
        bg_ask = bg.get("ask", 0) or 0; bg_ask_qty = bg.get("ask_qty", 0) or 0
        bg_bid = bg.get("bid", 0) or 0; bg_bid_qty = bg.get("bid_qty", 0) or 0

        bn_notional = min(bn_bid * bn_bid_qty, bn_ask * bn_ask_qty)
        bg_notional = min(bg_bid * bg_bid_qty, bg_ask * bg_ask_qty)

        if (bn_volume < MIN_LIQUIDITY_24H_USDT or bg_volume < MIN_LIQUIDITY_24H_USDT
                or bn_notional < MIN_TOP_BOOK_NOTIONAL_USDT or bg_notional < MIN_TOP_BOOK_NOTIONAL_USDT):
            rejected_liquidity += 1
            continue

        s = executable_spread(bn, bg)
        if not is_spread_sane(s):
            rejected_sanity += 1
            continue

        best = max(s.get("spread_b2b") or -999, s.get("spread_btog") or -999)
        min_notional = min(bn_notional, bg_notional)
        depth_factor = min(1.0, min_notional / (MOCK_USDT * 10))
        if (s.get("spread_b2b") or 0) > (s.get("spread_btog") or 0):
            executable_depth = min(bn_ask * bn_ask_qty, bg_bid * bg_bid_qty)
        else:
            executable_depth = min(bg_ask * bg_ask_qty, bn_bid * bn_bid_qty)
        score = best * depth_factor * 100
        ranked.append((symbol, best, s, is_tradable(symbol), score, min_notional, executable_depth))

    ranked.sort(key=lambda x: x[4], reverse=True)
    tradable_ranking = [(sym, best, s, sc, depth, exec_d) for sym, best, s, t, sc, depth, exec_d in ranked if t]
    observe_ranking  = [(sym, best, s, sc, depth, exec_d) for sym, best, s, t, sc, depth, exec_d in ranked if not t]

    print(f"  Rejected: {rejected_liquidity} liquidity, {rejected_sanity} sanity", flush=True)
    print(f"  Ranked: {len(tradable_ranking)} tradable, {len(observe_ranking)} observe-only\n", flush=True)

    # ── Tier 1: tradable top 5 ───────────────────────────────────────────────
    print("── Tier 1 — Tradable (top 5 by composite score) ──", flush=True)
    print(f"  {'#':<3} {'Symbol':<14} {'Spread%':>10} {'Score':>8} {'Depth$':>10}  {'vs BE':>8}", flush=True)
    print(f"  {'-'*3} {'-'*14} {'-'*10} {'-'*8} {'-'*10}  {'-'*8}", flush=True)
    for i, (sym, best, s, score, depth, exec_d) in enumerate(tradable_ranking[:5], 1):
        vs = "ABOVE" if best > be else "below"
        print(f"  {i:<3} {sym:<14} {best:>+10.4f} {score:>8.2f} ${depth:>9.0f}  {vs:>8}", flush=True)

    # ── Tier 2: observe-only ─────────────────────────────────────────────────
    if observe_ranking:
        print(f"\n── Tier 2 — Observe-Only (top 3, not traded) ──", flush=True)
        print(f"  {'#':<3} {'Symbol':<14} {'Spread%':>10}  {'Depth$':>10}  Note", flush=True)
        print(f"  {'-'*3} {'-'*14} {'-'*10}  {'-'*10}  {'-'*20}", flush=True)
        for i, (sym, best, s, score, depth, exec_d) in enumerate(observe_ranking[:3], 1):
            vs = "⚠ above BE!" if best > be else "tracking"
            print(f"  {i:<3} {sym:<14} {best:>+10.4f}  ${depth:>9.0f}  {vs}", flush=True)

    # ── Simulate best tradable trade ─────────────────────────────────────────
    if not tradable_ranking:
        print("\n⚠ No tradable candidates passed all filters.", flush=True)
        print("  This is the system working correctly — boring = safe.", flush=True)
        return

    # ── Build quote → USD rate map ───────────────────────────────────────
    # USDT/USDC = 1.0. Generic: any QUOTEUSDT pair in the books gives the rate.
    quote_usd_rate: dict[str, float] = {"USDT": 1.0, "USDC": 1.0}
    for qa in TRADABLE_QUOTE_ASSETS:
        if qa in quote_usd_rate:
            continue
        pair = f"{qa}USDT"
        ref = binance_books.get(pair) or bitget_books.get(pair)
        if ref:
            mid = ((ref.get("bid", 0) or 0) + (ref.get("ask", 0) or 0)) / 2
            if mid > 0:
                quote_usd_rate[qa] = 1.0 / mid

    # Debug: show rates for non-USD quotes
    non_usd = {k: v for k, v in quote_usd_rate.items() if v != 1.0}
    if non_usd:
        print(f"  Quote→USD rates: {', '.join(f'{k}=${v:.4f}' for k, v in non_usd.items())}", flush=True)

    best_sym, best_spread, best_s, best_score, best_depth, best_exec_depth = tradable_ranking[0]
    b2b = best_s.get("spread_b2b") or 0
    btg = best_s.get("spread_btog") or 0
    direction = "binance_to_bitget" if b2b > btg else "bitget_to_binance"
    exec_spread = max(b2b, btg)

    # Determine quote asset and USD rate
    quote_asset = best_sym
    for qa in sorted(quote_usd_rate.keys(), key=len, reverse=True):
        if best_sym.endswith(qa):
            quote_asset = qa
            break
    usd_rate = quote_usd_rate.get(quote_asset, 1.0)
    if usd_rate <= 0:
        print(f"  ⚠ Cannot derive USD rate for {quote_asset} — skipping", flush=True)
        return

    if direction == "binance_to_bitget":
        buy_ex, sell_ex = "binance", "bitget"
        buy_price = best_s.get("binance_ask") or 0
        sell_price = best_s.get("bitget_bid") or 0
        buy_depth_qty = (binance_books.get(best_sym, {}).get("ask_qty") or 0)
        sell_depth_qty = (bitget_books.get(best_sym, {}).get("bid_qty") or 0)
    else:
        buy_ex, sell_ex = "bitget", "binance"
        buy_price = best_s.get("bitget_ask") or 0
        sell_price = best_s.get("binance_bid") or 0
        buy_depth_qty = (bitget_books.get(best_sym, {}).get("ask_qty") or 0)
        sell_depth_qty = (binance_books.get(best_sym, {}).get("bid_qty") or 0)

    print(f"\n═══ SIMULATION: {best_sym} ({direction}) ═══", flush=True)

    if buy_price <= 0 or sell_price <= 0:
        print(f"  ⚠ Invalid book prices — cannot simulate", flush=True)
        return

    # ── USD→quote conversion ────────────────────────────────────────────
    capital_usd = MOCK_USDT
    capital_quote = capital_usd / usd_rate  # e.g. $200 / 0.194 = 1032 BRL
    fee_pct = 0.001

    # Depth-cap in quote units
    max_buy_quote = buy_price * buy_depth_qty       # max quote units buyable at ask
    max_sell_quote = sell_price * sell_depth_qty     # max quote units sellable at bid
    effective_quote = min(capital_quote, max_buy_quote, max_sell_quote)
    depth_capped = effective_quote < capital_quote * 0.99

    buy_qty = effective_quote / buy_price
    buy_fee_quote = effective_quote * fee_pct
    sell_gross_quote = buy_qty * sell_price
    sell_fee_quote = sell_gross_quote * fee_pct
    net_gain_quote = sell_gross_quote - effective_quote - buy_fee_quote - sell_fee_quote

    # Convert back to USD for reporting & compounding (FR-13 requires USD)
    effective_usd = effective_quote * usd_rate
    buy_fee_usd = buy_fee_quote * usd_rate
    sell_fee_usd = sell_fee_quote * usd_rate
    net_gain_usd = net_gain_quote * usd_rate

    print(f"  Quote: {quote_asset} (1 {quote_asset} = ${usd_rate:.4f} USD)", flush=True)
    print(f"  Capital: ${capital_usd:.0f} USD = {capital_quote:.2f} {quote_asset}", flush=True)
    print(f"  Top-of-book depth: buy {buy_depth_qty:.4f}, sell {sell_depth_qty:.4f} units", flush=True)
    if depth_capped:
        print(f"  ⚠ DEPTH-CAPPED: ${effective_usd:.0f} USD ({effective_quote:.2f} {quote_asset})", flush=True)
    print(f"  Buy  on {buy_ex:>7}: {buy_qty:.6f} @ {buy_price:.6f} {quote_asset}  (fee ${buy_fee_usd:.4f})", flush=True)
    print(f"  Sell on {sell_ex:>7}: {buy_qty:.6f} @ {sell_price:.6f} {quote_asset}  (fee ${sell_fee_usd:.4f})", flush=True)
    print(f"  ─────────────────────────────────────────────", flush=True)
    print(f"  Executable spread: {exec_spread:+.4f}%  (break-even: {be:.4f}%)", flush=True)

    if exec_spread > be:
        print(f"  ✅ NET GAIN: ${net_gain_usd:+.4f} USD  ({net_gain_usd/effective_usd*100:+.3f}% on ${effective_usd:.0f})", flush=True)
        new_bn = bn_alloc + (net_gain_usd / 2)
        new_bg = bg_alloc + (net_gain_usd / 2)
        new_total = total_capital + net_gain_usd
        print(f"\n  ── Capital Compounding (USD) ──", flush=True)
        print(f"  binance: ${bn_alloc:.2f} → ${new_bn:.2f}  ({net_gain_usd/2:+.4f})", flush=True)
        print(f"  bitget:  ${bg_alloc:.2f} → ${new_bg:.2f}  ({net_gain_usd/2:+.4f})", flush=True)
        print(f"  TOTAL:   ${total_capital:.2f} → ${new_total:.2f}  ({net_gain_usd:+.4f})", flush=True)
        record_capital_change("binance", new_bn, net_gain_usd / 2, "demo_simulated_trade")
        record_capital_change("bitget", new_bg, net_gain_usd / 2, "demo_simulated_trade")
    else:
        print(f"  ❌ Spread below break-even — trade would be SKIPPED", flush=True)

    print(f"\n── Done. No real orders placed. ──", flush=True)


if __name__ == "__main__":
    while True:
        main()
        time.sleep(120)  #120s pause between runs
