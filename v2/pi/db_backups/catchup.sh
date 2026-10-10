#!/usr/bin/env bash
# v309: catch-up for the nightly off-site backup pull (runs on the office Pi,
# called by report_watch.sh every 5 minutes).
#
# Why: from 5 Sep to 4 Oct every 22:00 pull died with "Permission denied"
# (the live crontab calls pull_backup.sh without bash and the file was not
# executable) and nothing retried - one missed night became 29. Now, when
# the newest off-site dump is older than MAX_AGE_H, the pull is run (with
# bash, so the file mode no longer matters) - at most once per RETRY_SEC.
# pull_backup.sh holds a lock, so this never overlaps the 22:00 run.
#
# Prints one line when it acts; silent otherwise. Exit 0 always (the caller
# is a watchdog). Paths can be overridden by the environment (unit tests).
set -u
BK_DIR="${BK_DIR:-$HOME/db_backups/daily}"
KEY="${KEY:-$HOME/.ssh/argia_backup_pull}"
STAMP="${STAMP:-$HOME/report_watch/backup_catchup_at}"
PULL="${PULL:-$(dirname "$0")/pull_backup.sh}"
LOG="${LOG:-$HOME/argia_logs/db_backup_pull.log}"
NOW="${NOW:-$(date +%s)}"
MAX_AGE_H="${MAX_AGE_H:-26}"      # nightly pull at 22:00 -> a fresh dump is never older than 24 h
RETRY_SEC="${RETRY_SEC:-3600}"

[ -f "$KEY" ] || exit 0           # not the office Pi (or not set up)

# v319: sealed (.dump.age) or plain
newest=$(ls -1t "$BK_DIR"/argia_mont_*.dump* 2>/dev/null | head -1)
if [ -n "$newest" ]; then
  age=$(( NOW - $(stat -c %Y "$newest") ))
else
  age=$(( MAX_AGE_H * 3600 + 1 ))  # no dump at all = overdue
fi
[ "$age" -gt $(( MAX_AGE_H * 3600 )) ] || exit 0
last=$(cat "$STAMP" 2>/dev/null || echo 0)
[ $(( NOW - last )) -ge "$RETRY_SEC" ] || exit 0

mkdir -p "$(dirname "$STAMP")" "$(dirname "$LOG")"
echo "$NOW" > "$STAMP"
echo "$(date '+%Y-%m-%d %H:%M:%S') backup catch-up: newest off-site dump is $(( age / 3600 )) h old - pulling now"
if bash "$PULL" >> "$LOG" 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') backup catch-up: pull OK"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') backup catch-up: pull FAILED (see $LOG) - next try in $(( RETRY_SEC / 60 )) min"
fi
exit 0
