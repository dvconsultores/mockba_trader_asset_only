#!/bin/bash
# Start dashboard API and Vite dev server for local testing
set -e

PROJ="/home/andres/vsCodeProjects/Python/MockbaV4/mockba_trader_asset_only"
VENV="$PROJ/venv/bin/python"

export DB_PATH="$PROJ/data/trading.db"
export LOG_PATH="/tmp/mockba.log"
export MODEL_PATH="$PROJ/data/signal_model.json"
export API_TOKEN="8353557705:AAEsFFyyHs1a7TJF7QiGVeEDCkJXCbIQK0w"

echo "=== Starting dashboard API on :8080 ==="
$VENV -m uvicorn dashboard.main:app --host 0.0.0.0 --port 8080 &
API_PID=$!
sleep 2

echo "=== Starting Vite dev server on :5173 ==="
cd "$PROJ/dashboard-ui"
npx vite --host 0.0.0.0 --port 5173 &
VITE_PID=$!
sleep 3

echo ""
echo "=== SERVERS RUNNING ==="
echo "  Frontend: http://localhost:5173/"
echo "  Backend:  http://localhost:8080/"
echo "  API PID: $API_PID   Vite PID: $VITE_PID"
echo ""
echo "Press Ctrl+C to stop both."
wait
