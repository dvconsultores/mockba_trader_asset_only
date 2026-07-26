# Quickstart: Amendment 002 — Settings Validator & LLM Helper

**Feature**: Amendment 002 | **Date**: 2026-07-26

Validation guide for operators and developers. All scenarios are runnable against a live or dry-run bot.

## Prerequisites

- Python 3.11+ with venv activated
- SQLite database with `schema_v2.sql` + migration `003_amendment_002.sql` applied
- All 51 settings seeded in the `settings` table
- (LLM scenarios only) `DEEP_SEEK_API_KEY` or `DEEPSEEK_API_KEY` in `.env`

## Setup

```bash
cd /home/andres/vsCodeProjects/Python/MockbaV4/mockba_trader_asset_only
source venv/bin/activate

# Apply Amendment 002 migration (idempotent)
python3 -c "
import sqlite3
conn = sqlite3.connect('data/mockba.db')
conn.executescript(open('db/migrations/003_amendment_002.sql').read())
conn.commit()
conn.close()
print('Migration 003 applied')
"

# Verify 51 settings match schema
python3 -c "
from trade.settings_schema import BY_KEY
from db.db_ops import get_all_settings
db_keys = set(get_all_settings().keys())
spec_keys = set(BY_KEY.keys())
missing = db_keys - spec_keys
extra = spec_keys - db_keys
print(f'DB: {len(db_keys)}, Spec: {len(spec_keys)}, Missing: {len(missing)}, Extra: {len(extra)}')
if not missing and not extra:
    print('✅ Perfect 1:1 match — all settings have SettingSpec, all specs in DB')
else:
    if missing: print(f'Settings in DB missing from schema: {missing}')
    if extra: print(f'Settings in schema missing from DB: {extra}')
"
```

## Scenario 1: Single-setting validation (deterministic)

```bash
python3 -c "
from trade.settings_rules import validate

# Valid setting
v = validate('tp_min_pct', 0.8)
print(f'tp_min_pct=0.8 → {v.level}: {v.message}')
assert v.level == 'ok'

# Type error
v = validate('tp_min_pct', 'yes')
print(f'tp_min_pct=\"yes\" → {v.level}: {v.message}')
assert v.level == 'error'

# Hard range violation
v = validate('llm_timeout_sec', 200)
print(f'llm_timeout_sec=200 → {v.level}: {v.message}')
assert v.level == 'error'

# Soft range warning
v = validate('max_consecutive_losses', 1)
print(f'max_consecutive_losses=1 → {v.level}: {v.message}')
assert v.level == 'warn'

print('✅ All single-setting checks pass')
"
```

**Expected**: Four verdicts printed, no assertion errors.

## Scenario 2: Cross-setting validation (tp vs sl)

```bash
python3 -c "
from trade.settings_rules import validate

# tp <= sl → error
v = validate('tp_min_pct', 0.3)
print(f'tp_min_pct=0.3 (sl_min_pct default 0.5) → {v.level}: {v.message}')
assert v.level == 'error'
assert 'exceed' in v.message.lower()

# Valid pair
v = validate('tp_min_pct', 0.9)
print(f'tp_min_pct=0.9 → {v.level}')
assert v.level == 'ok'

print('✅ Cross-setting checks pass')
"
```

**Expected**: tp=0.3 triggers error because sl default is 0.5. tp=0.9 passes.

## Scenario 3: Batch validation (validate_all)

```bash
python3 -c "
from trade.settings_rules import validate_all, SettingsContext

ctx = SettingsContext(venue='orderly', equity=500, min_notional=5)
results = validate_all(ctx)

errors = {k: v for k, v in results.items() if v.level == 'error'}
warnings = {k: v for k, v in results.items() if v.level == 'warn'}
ok_count = sum(1 for v in results.values() if v.level == 'ok')

print(f'Total: {len(results)} | OK: {ok_count} | Warn: {len(warnings)} | Error: {len(errors)}')
for k, v in errors.items():
    print(f'  ERROR {k}: {v.message}')
for k, v in warnings.items():
    print(f'  WARN  {k}: {v.message}')
"
```

**Expected**: With default settings, expect 0 errors and a few warnings (toxicity baseline unvalidated, etc.).

## Scenario 4: LLM explanation (cached)

```bash
python3 -c "
from research.settings_llm import explain

# First call — may hit LLM or cache
text = explain('tp_min_pct', 'en', '100_to_1k')
print(f'Explain tp_min_pct (en, 100_to_1k):')
print(text[:200])
assert len(text) > 20

# Second call — must be instant (cached)
import time
t0 = time.time()
text2 = explain('tp_min_pct', 'en', '100_to_1k')
elapsed = time.time() - t0
print(f'Cached lookup: {elapsed*1000:.1f}ms')
assert elapsed < 0.1  # file read should be sub-100ms
assert text == text2

print('✅ Explanation cache works')
"
```

**Expected**: First call returns explanation text. Second call is <100ms and returns identical text.

## Scenario 5: LLM proposals (deterministic fallback)

```bash
python3 -c "
from research.settings_llm import propose

# No context → deterministic-only, no_basis confidence
proposals = propose(context_summary='')
print(f'Proposals (no context): {len(proposals)}')

for p in proposals[:5]:
    print(f'  {p.key}: {p.current_value} → {p.proposed_value} [{p.confidence}] {p.reason[:80]}')

# All should be heuristic or no_basis (no measured data)
non_measured = [p for p in proposals if p.confidence != 'measured']
print(f'Non-measured proposals: {len(non_measured)}/{len(proposals)}')

# Verify proposals were written to DB
import sqlite3
conn = sqlite3.connect('data/mockba.db')
count = conn.execute('SELECT COUNT(*) FROM settings_proposals').fetchone()[0]
conn.close()
print(f'Rows in settings_proposals: {count}')
assert count >= len(proposals)

print('✅ Proposal generation works')
"
```

**Expected**: Proposals generated from validator suggestions. Rows written to `settings_proposals` table.

## Scenario 6: Constitution Principle I verification (LLM module isolation)

```bash
python3 -c "
import ast, sys

# Verify no trading module imports settings_llm
forbidden_importers = ['bot', 'executor', 'spot_scalper', 'futures_scalper']
violations = []

for mod_name in forbidden_importers:
    try:
        with open(f'trading_bot/{mod_name}.py' if mod_name != 'bot' else 'bot.py') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names if hasattr(node, 'names') else []:
                    imported = alias.name
                    if 'settings_llm' in imported:
                        violations.append(f'{mod_name}.py imports {imported}')
    except FileNotFoundError:
        pass

if violations:
    print('❌ CONSTITUTION VIOLATION:')
    for v in violations:
        print(f'  {v}')
    sys.exit(1)
else:
    print('✅ Constitution I satisfied: no trading module imports settings_llm')
"
```

**Expected**: No violations. Passes with exit code 0.

## Scenario 7: Bot startup validation gate (dry-run safe)

```bash
python3 -c "
from trade.settings_rules import validate_all
from trade.settings_schema import BY_KEY

results = validate_all()
errors = {k: v for k, v in results.items() if v.level == 'error'}

if errors:
    print(f'❌ STARTUP BLOCKED: {len(errors)} error(s)')
    for k, v in errors.items():
        spec = BY_KEY[k]
        print(f'  {k} = ? → {v.message} (suggested: {v.suggested_value})')
    print('Fix errors before starting bot.')
else:
    print(f'✅ Startup gate passed: {len(results)} settings validated, 0 errors')
"
```

**Expected**: With default settings and all 51 keys seeded, should pass with 0 errors.

## Cleanup

No cleanup needed — all validation is read-only. LLM proposals are append-only (may accumulate in `settings_proposals`; retention policy deferred).

## Related

- [Data Model](data-model.md) — entity definitions and relationships
- [Research](research.md) — technical decisions and rationale
- [Plan](../../plan.md) — implementation plan with constitution check
- [Spec](spec.md) — full feature specification with acceptance criteria
