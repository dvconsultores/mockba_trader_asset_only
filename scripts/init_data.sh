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

echo "Data bootstrap complete."
