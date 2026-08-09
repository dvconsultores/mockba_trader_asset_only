#!/usr/bin/env bash
#
# push-db.sh — Upload the trading database from this machine to the server.
#
#   Local DB  : ${LOCADB_LOCATION}/${DBNAME}
#   Server DB : ${SRVUSER}@${SRVHOST}:${SRVDB_LOCATION}/${DBNAME}
#
# Reads SRVUSER / SRVPASS / SRVHOST / SRVDB_LOCATION / LOCADB_LOCATION / DBNAME
# from .env (see "## Server config" section). Uses sshpass for password auth
# when available; otherwise falls back to plain scp (SSH keys or prompt).
#
# NOTE: the local DB file is gitignored (data/ in .gitignore).
#
# ⚠️  Pushing over a live database (bot running on the server) can corrupt it.
#     Stop the bot (or its DB writes) before pushing, and ensure the server
#     target directory exists (default ${SRVDB_LOCATION:-/opt/Mockba/data}).
#
# Re-exec under bash if invoked via `sh` (script uses bash-isms: [[ ]], ${var:-}).
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ── Load config from .env ─────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    echo "❌ .env not found at $ENV_FILE" >&2
    exit 1
fi

env_val() {
    local key="$1" line
    line="$(grep -E "^${key}=" "$ENV_FILE" | head -1 || true)"
    [[ -z "$line" ]] && { echo ""; return; }
    echo "$line" | cut -d= -f2- \
        | sed -e 's/[[:space:]]*#.*$//' \
              -e 's/^[[:space:]]*//' \
              -e 's/[[:space:]]*$//' \
              -e 's/^"//' \
              -e 's/"$//'
}

SRVUSER="$(env_val SRVUSER)"
SRVPASS="$(env_val SRVPASS)"
SRVHOST="$(env_val SRVHOST)"
SRVDB_LOCATION="$(env_val SRVDB_LOCATION)"
LOCADB_LOCATION="$(env_val LOCADB_LOCATION)"
DBNAME="$(env_val DBNAME)"

if [[ -z "$SRVUSER" || -z "$SRVPASS" || -z "$SRVHOST" ]]; then
    echo "❌ SRVUSER / SRVPASS / SRVHOST missing in $ENV_FILE" >&2
    exit 1
fi

# Paths from .env, with sensible defaults when unset (trailing "/" stripped)
LOCAL_DIR="${LOCADB_LOCATION:-$SCRIPT_DIR/data}"
SERVER_DB_DIR="${SRVDB_LOCATION:-/opt/Mockba/data}"
REMOTE_DB="${DBNAME:-trading.db}"
LOCAL_DIR="${LOCAL_DIR%/}"
SERVER_DB_DIR="${SERVER_DB_DIR%/}"

# ── scp helper (password auth via sshpass when available) ─────────────────
push() {
    local local="$1" remote="$2"
    local opts=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "$SRVPASS" scp "${opts[@]}" "$local" "${SRVUSER}@${SRVHOST}:$remote"
    else
        echo "⚠️  sshpass not installed — falling back to plain scp (SSH keys or prompt)." >&2
        scp "${opts[@]}" "$local" "${SRVUSER}@${SRVHOST}:$remote"
    fi
}

# ── Safety: warn before overwriting the server DB ─────────────────────────
echo "🔌 Connecting to ${SRVUSER}@${SRVHOST} ..."
echo "📤 Uploading ${LOCAL_DIR}/${REMOTE_DB} → ${SERVER_DB_DIR}/${REMOTE_DB}"
echo "⚠️  This OVERWRITES the server database. Ctrl+C within 3s to abort..."
sleep 3

# ── Push the DB (plus WAL/journal sidecars if present) ────────────────────
if [[ ! -f "${LOCAL_DIR}/${REMOTE_DB}" ]]; then
    echo "❌ Local DB not found: ${LOCAL_DIR}/${REMOTE_DB}" >&2
    exit 1
fi

push "${LOCAL_DIR}/${REMOTE_DB}" "${SERVER_DB_DIR}/${REMOTE_DB}"

for ext in -wal -journal -shm; do
    if [[ -f "${LOCAL_DIR}/${REMOTE_DB}${ext}" ]]; then
        if push "${LOCAL_DIR}/${REMOTE_DB}${ext}" "${SERVER_DB_DIR}/${REMOTE_DB}${ext}"; then
            echo "   ✓ uploaded ${REMOTE_DB}${ext}"
        fi
    fi
done

echo "✅ Success. Database uploaded to: ${SRVUSER}@${SRVHOST}:${SERVER_DB_DIR}/${REMOTE_DB}"
