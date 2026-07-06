#!/usr/bin/env bash
# One wrapper for every scheduled job: venv + secrets + flock + logging.
# Usage (from cron): run_job.sh <name> <script.py> [args...]
#   e.g. run_job.sh telemetry telemetry_5m.py --apply
set -euo pipefail
NAME="$1"; SCRIPT="$2"; shift 2
REPO_DIR="${ARGIA_REPO:-$HOME/argia_solar_monitoring}"
LOG_DIR="${ARGIA_LOG_DIR:-$HOME/argia_logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$NAME.log"
# secrets: plain env file + credentials JSON exported as content
set -a; source "$HOME/.argia_env"; set +a
export GOOGLE_CREDENTIALS="$(cat "$GOOGLE_CREDENTIALS_FILE")"
cd "$REPO_DIR/v2"
# flock: a slow vendor poll must never overlap the next tick (the v1
# session-invalidation lesson, enforced by the OS)
exec /usr/bin/flock -n "/tmp/argia_$NAME.lock" \
  ./.venv/bin/python -u "scripts/$SCRIPT" "$@" \
  >> "$LOG" 2>&1
