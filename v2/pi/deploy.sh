#!/usr/bin/env bash
# Pull-based deploy: GitHub is the source of truth, the Pi follows main.
# Runs from cron every 10 min. Does NOTHING unless origin/main moved.
set -euo pipefail
REPO_DIR="${ARGIA_REPO:-$HOME/argia_solar_monitoring}"
LOG="${ARGIA_LOG_DIR:-$HOME/argia_logs}/deploy.log"
mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"
git fetch -q origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0
{
  echo "[$(date '+%F %T')] deploy: $LOCAL -> $REMOTE"
  git reset --hard -q origin/main
  ./v2/.venv/bin/pip install -q -r v2/requirements.txt
  echo "[$(date '+%F %T')] deploy done"
} >> "$LOG" 2>&1
