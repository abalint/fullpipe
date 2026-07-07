#!/usr/bin/env bash
#
# Daily off-site backup of the fullPipe ledger.
#
# The ledger (~/immersion/ledger.db) is a WAL-mode SQLite database that is
# expensive to lose and cannot be safely captured with a raw `cp` while the
# server is writing. This script takes a *consistent* snapshot with
# `VACUUM INTO` (also defragments it), verifies integrity, compresses it,
# keeps a local copy, and uploads to a cloud remote via rclone. Old copies
# (local and remote) beyond the retention window are pruned.
#
# Scheduled daily by ~/Library/LaunchAgents/app.fullpipe.backup.plist.
# Run by hand any time:  tools/backup_ledger.sh
#
# One-time setup (interactive, done once by a human):
#   rclone config     # create a Google Drive remote named 'japanese' (REMOTE_NAME below)
#
set -euo pipefail

# --- Config (override via environment if needed) -----------------------------
# Ledger path is read from config.json's `ledger_db` when present, else default.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_JSON="${CONFIG_JSON:-$REPO_DIR/config.json}"

default_db="$HOME/immersion/ledger.db"
if [[ -z "${LEDGER_DB:-}" && -f "$CONFIG_JSON" ]] && command -v python3 >/dev/null; then
  LEDGER_DB="$(python3 - "$CONFIG_JSON" <<'PY' 2>/dev/null || true
import json, os, sys
try:
    p = json.load(open(sys.argv[1])).get("ledger_db", "")
    print(os.path.expanduser(p)) if p else None
except Exception:
    pass
PY
)"
fi
LEDGER_DB="${LEDGER_DB:-$default_db}"

REMOTE_NAME="${REMOTE_NAME:-japanese}"        # rclone remote (from `rclone config`)
REMOTE_PATH="${REMOTE_PATH:-fullpipe-backups}" # folder within that remote
LOCAL_DIR="${LOCAL_DIR:-$HOME/immersion/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
RCLONE="${RCLONE:-$(command -v rclone || echo /opt/homebrew/bin/rclone)}"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# --- Preflight ---------------------------------------------------------------
[[ -f "$LEDGER_DB" ]] || { log "ERROR: ledger not found at $LEDGER_DB"; exit 1; }
command -v sqlite3 >/dev/null || { log "ERROR: sqlite3 not on PATH"; exit 1; }

STAMP="$(date '+%Y-%m-%d')"
NAME="ledger-$STAMP.db"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
SNAP="$STAGE/$NAME"

# --- Consistent snapshot + integrity guard -----------------------------------
log "Snapshotting $LEDGER_DB -> $NAME"
sqlite3 "$LEDGER_DB" "VACUUM INTO '$SNAP'"

check="$(sqlite3 "$SNAP" 'PRAGMA integrity_check;' | head -1)"
if [[ "$check" != "ok" ]]; then
  log "ERROR: snapshot failed integrity_check ($check) — NOT uploading"
  exit 1
fi
gzip -f "$SNAP"
SNAP_GZ="$SNAP.gz"
log "Snapshot ok ($(du -h "$SNAP_GZ" | cut -f1) compressed)"

# --- Local retained copy (the "2" in 3-2-1) ----------------------------------
mkdir -p "$LOCAL_DIR"
cp "$SNAP_GZ" "$LOCAL_DIR/$NAME.gz"
find "$LOCAL_DIR" -name 'ledger-*.db.gz' -type f -mtime +"$KEEP_DAYS" -delete
log "Local copy in $LOCAL_DIR (pruned > ${KEEP_DAYS}d)"

# --- Off-site upload + prune --------------------------------------------------
if [[ -x "$RCLONE" ]] && "$RCLONE" listremotes 2>/dev/null | grep -q "^${REMOTE_NAME}:"; then
  DEST="${REMOTE_NAME}:${REMOTE_PATH}"
  log "Uploading to $DEST"
  "$RCLONE" copy "$SNAP_GZ" "$DEST/" --log-level NOTICE
  "$RCLONE" delete --min-age "${KEEP_DAYS}d" --include 'ledger-*.db.gz' "$DEST/" --log-level NOTICE || true
  log "Off-site upload complete (pruned > ${KEEP_DAYS}d)"
else
  log "WARNING: rclone remote '${REMOTE_NAME}:' not configured — kept LOCAL backup only."
  log "         Run 'rclone config' to create it, then this will upload automatically."
  exit 2
fi

log "Backup complete."
