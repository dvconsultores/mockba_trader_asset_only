#!/usr/bin/env bash
set -e

# Resolve project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/data"
DB_FILE="$DATA_DIR/trading.db"

mkdir -p "$DATA_DIR"

if [[ ! -f "$DB_FILE" ]]; then
  touch "$DB_FILE"
  echo "Created: $DB_FILE"
else
  echo "Exists:  $DB_FILE"
fi

# Keep directory/file writable for current user
chmod u+rwX "$DATA_DIR" "$DB_FILE"

# Apply DB migration for signal_history.exchange when available
if command -v python >/dev/null 2>&1; then
  (
    cd "$PROJECT_ROOT" && \
    python -c "from db.db_ops import initialize_database_tables; initialize_database_tables()" && \
    python -m db.migrations.003_add_signal_history_exchange && \
    python -m db.migrations.004_add_cex_capital_setting || true
  )
fi

echo "Data bootstrap complete."
