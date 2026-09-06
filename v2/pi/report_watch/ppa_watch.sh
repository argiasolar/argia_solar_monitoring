#!/bin/bash
# Outage-mode PPA watch wrapper (Pi cron, every 30 min 08-19 MX): the
# vendor credentials from ~/.argia_env, the repo's venv, the repo's script.
# v217.1: lives in the repo so `deploy.sh` keeps it current — the copy in
# ~/report_watch never received v214/v217.
set -a; source "$HOME/.argia_env"; set +a
if [ -n "${GOOGLE_CREDENTIALS_FILE:-}" ] && [ -f "$GOOGLE_CREDENTIALS_FILE" ]; then
  export GOOGLE_CREDENTIALS="$(cat "$GOOGLE_CREDENTIALS_FILE")"
fi
exec "$HOME/argia_v2/v2/.venv/bin/python" "$HOME/argia_v2/v2/pi/report_watch/ppa_watch.py"
