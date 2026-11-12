#!/usr/bin/env bash
# v374: publish (freeze) one month of the PPA monthly performance report - setup app's "Publish" button (pio06).
#   ppa_publish.sh YYYY-MM USER
# Waits for a running daily generation (same lock as the argia-ppa-monthly job), then freezes the month:
# numbers, PDFs and Excel go to /opt/argia/ppa_report/published/YYYY-MM/ and never change afterwards.
set -uo pipefail
YM="${1:-}"; WHO="${2:-}"
[[ "$YM" =~ ^20[0-9]{2}-(0[1-9]|1[0-2])$ ]] || { echo "bad month: $YM"; exit 2; }
[[ "$WHO" =~ ^[a-z0-9.@_-]{2,64}$ ]] || { echo "bad user: $WHO"; exit 2; }
R="${ARGIA_REPO:-/root/argia_v2}"
LOG="${ARGIA_LOG_DIR:-/root/argia_logs}/ppamonthly.log"
mkdir -p "$(dirname "$LOG")"
export PYTHONPATH="$R/v2" PGOPTIONS='-c statement_timeout=120000'
cd "$R/v2" || exit 2
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') publish $YM by $WHO"
  /usr/bin/flock -w 300 /tmp/argia_ppamonthly.lock ./.venv/bin/python -u scripts/ppa_monthly_gen.py \
    /www/hosting/portal.argia.com.mx/www --publish "$YM" --by "$WHO" 2>&1; rc=$?
  echo "=== done rc=$rc"
  exit $rc
} 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
