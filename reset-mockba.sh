#!/bin/bash
set -e

COMPOSE_FILE="mockba.yml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔══════════════════════════════════════════╗"
echo "║   Mockba Docker Reset & Rebuild         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Step 1: Down ───────────────────────────────────────────────
echo "📦 [1/5] Stopping containers..."
docker compose -f "$SCRIPT_DIR/$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
echo "   ✅ Containers stopped"

# ── Step 2: Remove containers (just in case) ────────────────────
echo ""
echo "🗑️  [2/5] Removing mockba containers..."
docker rm -f micro-mockba-asset-futures-bot mockba-dashboard-api mockba-dashboard-ui watchtower-apolo-asset 2>/dev/null || true
echo "   ✅ Containers removed"

# ── Step 3: Remove images ──────────────────────────────────────
echo ""
echo "🖼️  [3/5] Removing mockba images..."
docker rmi -f andresdom2004/micro-mockba-asset-futures-bot:latest 2>/dev/null || true
docker rmi -f andresdom2004/mockba-dashboard:latest 2>/dev/null || true
docker rmi -f andresdom2004/mockba-dashboard-ui:latest 2>/dev/null || true
echo "   ✅ Images removed"

# ── Step 4: Prune dangling ─────────────────────────────────────
echo ""
echo "🧹 [4/5] Pruning dangling images & build cache..."
docker image prune -f 2>/dev/null || true
docker builder prune -f 2>/dev/null || true
echo "   ✅ Cache cleaned"

# ── Step 5: Up (rebuild + start) ───────────────────────────────
echo ""
echo "🚀 [5/5] Building & starting..."
docker compose -f "$SCRIPT_DIR/$COMPOSE_FILE" up -d --build

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✅ Reset complete — all services up   ║"
echo "╚══════════════════════════════════════════╝"
docker compose -f "$SCRIPT_DIR/$COMPOSE_FILE" ps
