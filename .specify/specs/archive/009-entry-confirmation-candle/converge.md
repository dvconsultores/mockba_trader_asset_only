# Converge: Entry Confirmation Candle (009)

**Date**: 2026-08-15 | **Status**: Implemented — 14/14 tasks, 88/88 tests green
**Constitution**: v1.1.0 (Principle II amended 2026-08-15, ratified in this session)

## Acceptance criteria — assessment

| AC | Status | Evidence |
|---|---|---|
| 1 Helper correctness | ✅ | `test_helper_reads_last_closed_bar` — up/down/**flat ⇒ False**, with an opposite in-progress `[-1]` bar proving `[-2]` is read |
| 2 Indeterminate is None | ✅ | `test_helper_indeterminate` — empty cache and 1-bar cache |
| 3 Observe mode is a no-op | ✅ | `test_observe_mode_never_blocks` — down bar, entry still fires, no `entry_not_confirmed` row |
| 4 Observe mode records | ✅ *(gap closed during converge — see below)* | `test_observe_mode_records`; all six post-evaluation `_log` calls now carry the verdict |
| 5 Enforce blocks unconfirmed | ✅ | `test_enforce_blocks_unconfirmed` — asserts the fake exchange received **no** `place_entry` |
| 6 Enforce passes confirmed | ✅ | `test_enforce_passes_confirmed` |
| 7 Fail closed on None | ✅ | `test_enforce_none_fails_closed` |
| 8 Futures symmetry | ✅ *(evidence-free)* | `test_futures_direction_symmetry` — synthetic series only; the study covers spot longs, DEX is off |
| 9 Zero new API calls | ✅ *(scoped — see below)* | `test_no_additional_api_calls` — warm cache ⇒ 0 `_fetch_ohlcv` calls |
| 10 Setting registered | ✅ | `test_setting_registered` — bool/`entry`, validator `ok`, default `False` |
| 11 Migration idempotent | ✅ | `test_migration_idempotent` — double init, one column, pre-existing row stays `NULL` |
| 12 Tests | ✅ | 12 new + 76 existing = **88 passed** |
| 13 Docs | ✅ | `docs/CURRENT_STATE.md` §0, `docs/CHANGELOG.md` 2026-08-15 |
| 14 Measurability | ✅ | `test_ab_query` |

## Deviations from plan

1. **AC4 gap found and closed during converge.** The plan specified `ec=ec` only
   on the `signaled` and `entered` calls. But confirmation is evaluated *before*
   the cooldown / spacing / qty-too-small checks, so those three rows per scalper
   were writing `NULL` for a verdict that was already known — which does not
   "reflect the evaluated state" as AC4 requires. Fixed: all six calls now pass
   `ec=ec`. Rows written *before* the evaluation point (`below_threshold`,
   `toxicity`, `tp_eff below cost+edge`) correctly stay `NULL`.
   **Consequence**: the A/B now also covers entries the confirmation would have
   allowed but a later gate rejected — a cleaner denominator.

2. **AC9 is scoped, not absolute.** "Zero additional API calls" holds for the
   live configuration (`adaptive_enabled=true`), where `get_atr_pct` has already
   warmed the cache in the same cycle. With `adaptive_enabled=false` the helper's
   delegation costs at most one 5m fetch per asset per `candle_cache_sec` (60s).
   Pinned in plan M1, documented in CURRENT_STATE, and asserted only for the warm
   path in the test.

3. **No `research.md` / `data-model.md` / `contracts/`.** The 008 layout was not
   copied where it would be empty ceremony: the decisions live in the spec's
   Clarifications, the data model is one nullable column, and the helper has one
   signature with two call sites — no cross-module contract exists.

## Remaining work

### Operator actions (not code)

- **T015 — Deploy, code first.** Commit → push `main` → GitHub Actions builds →
  Watchtower pulls; the migration runs at container start via
  `initialize_database_tables`. **Order matters**: the four DB values from
  2026-08-15 (`sl_k_spot=2.0`, `sl_min_pct_spot=1.5`, `tp_k=1.2`,
  `max_loss_per_position_pct=3.0`) are still **local only** — pushing them to a
  server running the pre-`662837d` image would halt entries on the old `te<=se`
  guard. Deploy the image, confirm the container restarted, then `push-db.sh`
  (which warns to stop DB writes first). `entry_confirm_candle` needs no DB row.
- **T016 — Enforcement decision gate.** Leave `entry_confirm_candle` unset and
  let observe-mode accumulate to **300+ entries** (spec Assumptions: the study's
  n=41 confirmed arm is thin), then decide from the AC14 query. Do not enable on
  the study alone.

### Follow-up specs identified by this feature

- **012 — Frequency recovery.** Enforcement cuts entries ~64%; slot capacity sits
  at 19% (42-min average hold, 6.5 of ~34 trades/day/slot). Levers:
  `max_concurrent_positions` 2→4-5 with `cex_slot_pct` 40→15-20,
  `max_active_pairs`/`universe_size` 10→20. Constitution VIII makes this the
  natural companion to enforcing 009.
- **013 — Loop latency / whole-exchange snapshot.** Measured ~2.16s of REST per
  asset ⇒ ~26s serial for 12 assets on top of `time.sleep(30)`, so the effective
  cadence is ~60s and assets are evaluated on data up to 26s apart. One
  `bookTicker` call (1.5s, all symbols) replaces the per-asset price fetches and
  is already implemented as `universe._fetch_binance_book_ticker`.
- **014 — Constitution VII re-baseline.** The 1,500-line hot-path budget is at
  **2,495** after this feature and has been exceeded for a long time. Either
  re-baseline the number against reality or split `executor.py` (623 lines).
  Currently every plan must carry a justification paragraph for a budget nobody
  intends to meet.

### Defects from the 2026-08-15 audit, still open (no spec yet)

Ranked; none are touched by 009.

| # | Defect | Severity |
|---|---|---|
| 1 | `futures_scalper.manage_open_positions` time-stop cancels TP/SL and deletes the DB row **without closing the position** — leaks a live leveraged position, and records a fabricated exit at entry price | Critical *(latent: DEX off)* |
| 2 | Futures regime-exit "move TP to breakeven" only writes `tp_price` to the DB; the exchange order is never amended, then the fabricated price is recorded as the exit | High *(latent)* |
| 3 | `bot.py` venue-failure escalation counts failures **within one cycle** (dict re-created each iteration) yet logs "consecutive" and permanently writes `auto_trade_{venue}=false` | High |
| 4 | `BinanceSpot.get_equity` swallows exceptions and returns `0.0`, which silently disables the percentage daily-loss limit (`limit = equity × pct` ⇒ 0) and prevents the venue-failure counter from ever firing | High |
| 5 | Toxicity spread/depth checks are fed hardcoded constants (`tox_eval(..., 0.05, 1000, ...)`) — two of four filters can never fire, and `signals.spread_pct`/`depth_top10` are always `NULL` | Medium |
| 6 | `closed_trades.opened_at` is hardcoded `0` in both `_close` implementations — hold-time analytics impossible (this blocked the 009 study, which had to rebuild entry times from `signals`) | Medium |
| 7 | Spot exchange-SL exits record the theoretical `sl_price` with `fee_exit=0` instead of the real fill (the TP path uses `_real_fill`) — losses understated | Medium |
| 8 | `universe._fetch_binance_exchange_info` reads only `MIN_NOTIONAL`; Binance spot uses `NOTIONAL`, so `min_notional` is 0 and the fundability filter never rejects | Medium |
| 9 | `universe_replay_days` silently caps at ~3.5 days (`limit = days × 288` vs Binance's 1000-kline max) | Low |
| 10 | SQLite has no WAL mode and opens a connection per setting read, across four processes | Low |
| 11 | Dashboard: `allow_origins=["*"]` with `allow_credentials=True`; all read endpoints unauthenticated; `secure=False` cookie; `initData` `auth_date` never checked | Low *(SameSite=Lax mitigates)* |

**Recommendation**: spec #1, #2 and the futures PnL fabrication together as `010-futures-exit-integrity` before
DEX is ever armed, and #3/#4 as `015-kill-switch-integrity` — both are safety
defects in the live path, unlike the throughput work above.
