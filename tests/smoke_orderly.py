"""Live smoke test: Orderly connectivity, klines, fallback, circuit breaker.

Run anywhere; the definitive run is INSIDE the server container, on the
network the bot actually uses:

    docker exec <container> python tests/smoke_orderly.py

Local runs can't authenticate to Orderly (keys are IP-whitelisted to the
server — correct security), so the native-kline check reports SKIP locally
and must PASS on the server. Everything else must PASS everywhere.
"""
import os
import sys
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import requests

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str):
    results.append((name, status, detail))
    print(f"  {status:4}  {name} — {detail}")


def main():
    print("[SMOKE] Orderly chain — reachability, klines, fallback, breaker\n")

    # 1. Orderly public endpoint reachable + listings correct
    try:
        t0 = time.time()
        r = requests.get("https://api.orderly.org/v1/public/info", timeout=10)
        r.raise_for_status()
        symbols = {row["symbol"] for row in r.json()["data"]["rows"]}
        ms = (time.time() - t0) * 1000
        missing = [a for a in ("NEAR", "SOL", "ARB", "INJ")
                   if f"PERP_{a}_USDC" not in symbols]
        if missing:
            report("orderly reachability", FAIL, f"listed symbols missing: {missing}")
        else:
            gram = "GRAM absent as expected" if "PERP_GRAM_USDC" not in symbols \
                else "GRAM now LISTED (revisit blacklist!)"
            report("orderly reachability", PASS,
                   f"{len(symbols)} symbols in {ms:.0f}ms; {gram}")
    except Exception as e:
        report("orderly reachability", FAIL, f"cannot reach api.orderly.org: {e}")

    # 2. Orderly native klines (authed — server-only)
    from trading_bot.executor import OrderlyFutures, BinanceSpot
    orderly = OrderlyFutures()
    rows = orderly.get_klines("NEAR", "1h", 10)
    if rows:
        report("orderly native klines", PASS,
               f"NEAR 1h: {len(rows)} candles, last close {rows[-1]['close']}")
    else:
        in_container = os.path.exists("/.dockerenv")
        status = FAIL if in_container else SKIP
        report("orderly native klines", status,
               "no rows — expected off-server (IP-whitelisted keys); "
               "must PASS inside the container")

    # 3. Binance fallback klines (public — must work everywhere)
    binance = BinanceSpot()
    rows = binance.get_klines("NEAR", "1h", 10)
    if rows:
        report("binance fallback klines", PASS,
               f"NEAR 1h: {len(rows)} candles, last close {rows[-1]['close']}")
    else:
        report("binance fallback klines", FAIL, "no rows from Binance public API")

    # 4. End-to-end _fetch: orderly venue must yield candles no matter what
    import bot
    bot._orderly_fail_streak, bot._orderly_skip_until = 0, 0.0
    candles = bot._fetch(binance, orderly, "orderly", "NEAR", "1h", 120)
    if candles:
        report("_fetch end-to-end", PASS,
               f"orderly:NEAR returned {len(candles)} candles (native or fallback)")
    else:
        report("_fetch end-to-end", FAIL, "no candles from either venue")

    # 5. Circuit breaker (deterministic, mocked failures)
    bot._orderly_fail_streak, bot._orderly_skip_until = 0, 0.0
    dead = mock.Mock()
    dead.get_klines.return_value = None
    ok_binance = mock.Mock()
    ok_binance.get_klines.return_value = [{"ts": 1, "open": 1, "high": 1,
                                          "low": 1, "close": 1, "volume": 1}]
    for _ in range(bot.ORDERLY_BREAK_AFTER):
        bot._fetch(ok_binance, dead, "orderly", "NEAR", "1h", 120)
    tripped = bot._orderly_skip_until > time.time()
    bot._fetch(ok_binance, dead, "orderly", "SOL", "1h", 120)
    skipped = dead.get_klines.call_count == bot.ORDERLY_BREAK_AFTER
    if tripped and skipped:
        report("circuit breaker", PASS,
               f"tripped after {bot.ORDERLY_BREAK_AFTER} failures, "
               f"orderly skipped during cooldown")
    else:
        report("circuit breaker", FAIL,
               f"tripped={tripped} skipped_during_cooldown={skipped}")
    bot._orderly_fail_streak, bot._orderly_skip_until = 0, 0.0

    print()
    fails = [r for r in results if r[1] == FAIL]
    if fails:
        print(f"[SMOKE] {len(fails)} FAILED: {', '.join(r[0] for r in fails)}")
        return 1
    skips = [r for r in results if r[1] == SKIP]
    note = f" ({len(skips)} skipped — rerun inside the container)" if skips else ""
    print(f"[SMOKE] all checks passed{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
