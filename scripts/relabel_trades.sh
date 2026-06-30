#!/bin/bash
# =============================================================================
# Deploy Script: Relabel All Trade Signals
#
# Run this after every deploy to:
#   1. Download fresh trades from Binance + Orderly
#   2. Correctly compute PnL via fixed FIFO matching
#   3. Label all unlabeled signal_history rows (win/loss/breakeven)
#   4. Auto-retrain ML model if enough new labels
#
# Usage:
#   bash scripts/relabel_trades.sh           # full sync, write to DB
#   bash scripts/relabel_trades.sh --dry-run  # preview only, no writes
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "============================================"
echo " MockbaV4 — Trade Labeler (Deploy Script)"
echo "============================================"
echo ""

# Activate virtual environment if it exists
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
elif [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

# Check for .env file
if [ ! -f .env ]; then
    echo "⚠️  WARNING: .env file not found — API keys may be missing"
fi

ARGS="--full"
if [[ "${1:-}" == "--dry-run" ]]; then
    ARGS="--full --dry-run"
    echo "🔍 DRY RUN MODE — preview only, no database writes"
else
    echo "📝 LIVE MODE — will write outcomes to database"
fi

echo ""
echo "Running labeler..."
python3 -m trade.signal_agent.labeler $ARGS

echo ""
echo "✅ Labeler complete."
echo ""
echo "To verify results, run:"
echo "  python3 -c \"import sqlite3; conn=sqlite3.connect('data/trading.db');"
echo "  rows=conn.execute(\\\"SELECT trade_outcome, COUNT(*) FROM signal_history"
echo "  WHERE trade_outcome IS NOT NULL GROUP BY trade_outcome\\\").fetchall();"
echo "  [print(f'{r[0]}: {r[1]}') for r in rows]; conn.close()\""
