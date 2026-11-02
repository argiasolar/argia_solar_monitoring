#!/usr/bin/env bash
# v343: invoice re-issue for the PPA calendar's "Recalculate" button (setup app, pio06).
#   reissue.sh YYYY-MM dry     preview, nothing written
#   reissue.sh YYYY-MM apply   daily table + register + annexes/PDFs/Drive/index, then every invoice rule
# One run at a time (flock). Everything goes to /root/argia_logs/reissue.log as well as stdout.
set -uo pipefail
YM="${1:-}"; MODE="${2:-}"
[[ "$YM" =~ ^20[0-9]{2}-(0[1-9]|1[0-2])$ ]] || { echo "bad month: $YM"; exit 2; }
[[ "$MODE" == dry || "$MODE" == apply ]] || { echo "bad mode: $MODE"; exit 2; }
R="${ARGIA_REPO:-/root/argia_v2}"
LOG="${ARGIA_LOG_DIR:-/root/argia_logs}/reissue.log"
mkdir -p "$(dirname "$LOG")"
set -a; source /root/.argia_env; set +a
export GOOGLE_CREDENTIALS="$(cat "$GOOGLE_CREDENTIALS_FILE")" PYTHONPATH="$R/v2" PGOPTIONS='-c statement_timeout=60000'
cd "$R/v2" || exit 2
ARGS=(--month "$YM"); [[ "$MODE" == apply ]] && ARGS+=(--apply)
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') reissue $YM $MODE by ${ARGIA_ACTOR:-console}"
  /usr/bin/flock -n /tmp/argia_reissue.lock ./.venv/bin/python -u scripts/invoice_reissue.py "${ARGS[@]}" 2>&1 \
    | grep -vE " INFO argia\.(maintenance|report|core|store|finance\.annex)" ; rc=${PIPESTATUS[0]}
  [[ $rc == 1 ]] && echo "another recalculation is running - try again in a minute"
  if [[ "$MODE" == apply && $rc == 0 ]]; then
    systemctl start argia-portal-gen.service && echo "portal page regenerated"
    ./.venv/bin/python scripts/billing_invariants.py --months "$YM" 2>&1
  fi
  echo "=== done rc=$rc"
  exit $rc
} 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
