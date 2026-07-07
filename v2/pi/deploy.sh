#!/usr/bin/env bash
# Pull-based deploy: GitHub is the source of truth, the Pi follows main.
# Runs from cron every 10 min. Does NOTHING unless origin/main moved.
set -euo pipefail
# v2's OWN clone. Never ~/argia_solar_monitoring: that is v1's live home,
# a dirty February clone carrying unpushed production edits (discovered
# 2026-07-06 during Phase 1 — the reset below would have destroyed them).
REPO_DIR="${ARGIA_REPO:-$HOME/argia_v2}"
LOG="${ARGIA_LOG_DIR:-$HOME/argia_logs}/deploy.log"
mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"
git fetch -q origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0
# SAFETY GUARD: never hard-reset a directory holding uncommitted work.
# GitHub is the only source of truth for v2, so a dirty tree here means
# either misconfiguration (pointed at v1's home) or manual edits — both
# must be looked at by a human, not erased by a cron job.
# --untracked-files=no (2026-07-07): reset --hard does NOT touch
# untracked files, so they are not at risk and must not block deploys.
# A dashpub build artifact in the tree stalled three pushes for hours.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "[$(date '+%F %T')] deploy REFUSED: uncommitted changes in $REPO_DIR" >> "$LOG"
  exit 1
fi
{
  echo "[$(date '+%F %T')] deploy: $LOCAL -> $REMOTE"
  git reset --hard -q origin/main
  ./v2/.venv/bin/pip install -q -r v2/requirements.txt
  echo "[$(date '+%F %T')] deploy done"
} >> "$LOG" 2>&1
