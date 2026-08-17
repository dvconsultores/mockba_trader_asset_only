# Data Model: Spot Exit Hardening (006)

**Feature**: 006-spot-exit-hardening | **Date**: 2026-08-12

No DB schema change, no migration. This feature adds: two settings (defaults in
`get_setting_*` fallbacks), one new `closed_trades.exit_reason` value, one new
scan-summary key, and one in-memory cooldown-stamping rule. Details cross-ref
`research.md` (line references) and `contracts/exit-reasons.md`.

## §1 — New settings (registered in `trade/settings_schema.py`)

| Key | Type | Group | Default | Hard range | Soft range | Unit | `depends_on` |
|---|---|---|---|---|---|---|---|
| `universe_max_atr_pct` | float | universe | 1.5 | 0.1–20 | 0.5–5 | % | — |
| `max_loss_per_position_pct` | float | exit | 3.0 | 0.1–20 | 1–5 | % | `("sl_min_pct_spot",)` |

- **Defaults live in the `get_setting_float(key, default)` fallbacks**
  (`db/db_ops.py` line 102) — no DB migration, settings read fresh each cycle.
- Registered as `SettingSpec`s so the Amendment 002 deterministic validator
  (`trade/settings_rules.py`) and UI/Telegram pick them up automatically
  (`BY_KEY`/`GROUPS` derive from `ALL`).
- Cross-checks (in `trade/settings_rules.py` `validate`, see §4).

## §2 — `closed_trades.exit_reason` enum (+ `crash_guard`)

Consumed by `dashboard/main.py` `REASON_LABELS` (line 823) and written by
`spot_scalper._close` → `record_closed_trade` (`trade/pnl.py` line 44).
Full contract: `contracts/exit-reasons.md`.

| Value | Producer (spot) | Fill-price semantics | Cooldown stamps `_last_sl` |
|---|---|---|---|
| `tp` | TP order FILLED / real TP fill recovery | real fill via `_real_fill`/`get_order_fills` | no |
| `sl` | SL order FILLED / price ≤ `sl_price` market sell | stored `sl_price` (order-filled path) or market-sell fill | **yes** |
| `time_stop` | hold-time exceeded market sell | market-sell fill (dry-run: entry) | no |
| `orphan` | no-balance recovery, no TP fill | live price or entry | no |
| `crash_guard` **(new)** | floor breach market sell (fill-aware) | market-sell fill (dry-run: entry); real reason `tp`/`sl` when an order already filled | **yes** |

## §3 — Crash-guard floor (per-position runtime state)

- `floor = entry_price × (1 − max_loss_per_position_pct / 100)` — computed per
  position from the stored `open_positions.entry_price`; applies to **all**
  spot positions (with or without a stored `sl_price`).
- Setting read fresh each cycle: `max_loss_per_position_pct =
  get_setting_float("max_loss_per_position_pct", 3.0)`.
- **Trigger**: `live is not None and live < floor` (strictly below). `live is
  None` → no action (Constitution IV, position kept to retry).
- **Ordering** (guard-first, fill-aware): the floor check is the first
  per-position check in `manage_open_positions`; inside it, TP/SL fill status
  is verified before any cancel/market-sell. Already-FILLED → real fill with
  real reason (`tp`/`sl`), never a market sell. Only when neither order filled
  → cancel TP/SL, `market_sell`, `_close(..., rsn="crash_guard")`.
- **Re-entry cooldown**: `_close` stamps `_last_sl[f"binance:{asset}:{side}"]`
  for `rsn in ("sl", "crash_guard")`; `_cooldown_ok` then blocks re-entry for
  `cooldown_sec × SL_COOLDOWN_MULT` (~10 min), identical to an `sl` exit.
  Longer-horizon exclusion is handled by `universe_max_atr_pct` on the next
  scan (Constitution VII — minimal change).
- **No-balance / orphan recovery**: the crash-guard cancel+market-sell path
  reuses the exact recovery pattern of the existing `sl`/`time_stop` branches
  (`market_sell is None` → balance check → real TP fill or `orphan`), so a
  position already closed by the exchange is never phantom-sold.

## §4 — Scan summary contract (universe cap observability)

`scan_venue` returns a summary dict today with
`venue, ok, stored, reason, candidates, survivors_after_filters,
survivors_after_depth, stored_count, min_recovery_rate`. It gains one key:

| Key | Type | When set | Meaning |
|---|---|---|---|
| `dropped_by_max_atr` | int | binance venue only (venue-branch) | candidates removed because `atr_pct_median > universe_max_atr_pct` after Stage 4 |

`_scan_summary_message` (line 782) may optionally surface the count; the dict
key alone is the required observability (consumed by tests and any
notification formatter). Orderly scans do not set the key (futures universe
untouched).

## §5 — ATR source (pinned)

`atr_pct_median` from Stage-4 `replay_symbol` (stored as
`asset_universe.atr_pct_median`). Cap filter applied in `scan_venue` after the
replay loop, before `select_ranked`, under a `venue == "binance"` branch.
Rationale + calibration evidence (live DB: BICO 1.86 removed, MMT 0.87 / PUMP
0.60 / others kept at cap 1.5): `research.md` §1.4–1.5.
