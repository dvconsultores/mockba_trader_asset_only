# Quickstart: Dynamic Asset Universe & Capital View

**Feature**: 003-dynamic-asset-universe-capital | **Date**: 2026-08-04

Run these scenarios to validate the feature end-to-end. All commands run from the
project root with the venv Python.

## Prerequisites

- Bot running in dry-run (`dry_run=true`)
- Exchange API credentials in `.env`
- Dashboard API accessible at `http://localhost:8080`
- Telegram bot token configured

---

## Scenario 1: Apply Migrations

**Goal**: Verify the new tables and settings exist.

```bash
./venv/bin/python -c "from db.db_ops import initialize_database_tables; initialize_database_tables()"

./venv/bin/python -c "
from db.db_ops import get_db_connection
with get_db_connection() as c:
    print([r['name'] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('asset_universe','venue_state')\")])
    print([r['key'] for r in c.execute(\"SELECT key FROM settings WHERE key LIKE 'universe_%' ORDER BY key\")])
"
```

**Expected**: `asset_universe` and `venue_state` present; all 12 `universe_*`
settings seeded (plus `capital_*`, `max_slots_*`).

---

## Scenario 2: Run the Scanner

**Goal**: Populate the universe. Start `bot.py` (the scanner thread scans on
startup if the stored scan is missing) or trigger directly:

```bash
./venv/bin/python -c "
from db.db_ops import get_db_connection
from trade.universe import scan_venue
print(scan_venue('binance'))
with get_db_connection() as c:
    rows = c.execute('SELECT asset, rank, recovery_rate FROM asset_universe WHERE venue=? ORDER BY rank LIMIT 5', ('binance',)).fetchall()
    print([dict(r) for r in rows])
"
```

**Expected**: `stored_count > 0` (network permitting); a ranked list with
`recovery_rate` between 0 and 1 and `signals_count >= 20`.

---

## Scenario 3: Blacklist Survives a Rescan

**Goal**: Operator override persists.

```bash
./venv/bin/python -c "
from db.db_ops import get_db_connection, set_blacklist, get_universe
import trade.universe as u
row = get_universe('binance')[0]
set_blacklist('binance', row['asset'], True)
u.scan_venue('binance')  # rescan
print([r['asset'] for r in get_universe('binance', include_blacklisted=True) if r['blacklisted']])
"
```

**Expected**: the blacklisted asset still has `blacklisted=1` after the rescan.

---

## Scenario 4: Capital & Universe via Telegram

- `/capital` — both venues: declared vs live equity, slot size, deployed, free.
- `/universe cex` — CEX list with rank, recovery, signals, spread, scan age.
- `/blacklist add NEAR` / `/blacklist remove NEAR` — toggle the override.

---

## Scenario 5: Dashboard Capital View

Open the Mini App → **Capital** tab. Expected:
- Two venue panels with declared vs live equity and a divergence warning if they
  differ by more than 25%.
- Editable slot %, max slots, declared capital (via settings writes).
- Read-only Universe panels per venue with scan age; a **STALE** highlight when
  `scan_age_hours > universe_max_age_hours`.
- Blacklist toggles only — no way to add assets manually.

---

## Scenario 6: Unit Tests

```bash
./venv/bin/python -m pytest tests/test_amendment003.py --basetemp=.pytest_tmp -q
```

**Expected**: 17 passed (shared-threshold binding, replay recovery, breakeven
`auto`, hard filters, min-signals exclusion, DEX short-store, blacklist
carry-forward, budget-exhaustion abort, live-equity sizing, per-venue net edge,
validator cross-checks).
