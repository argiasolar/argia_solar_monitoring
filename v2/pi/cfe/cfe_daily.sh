#!/bin/bash
# ARGIA CFE daily job (Pi, cron 08:10 America/Mexico_City).
# 1) probe: scrape GDMTH for the current month (17 divisions) — proves
#    the WAF path works end to end and samples real values
# 2) monthly: between day 3 and 27, if this month's full CSV has not
#    been pushed yet, scrape all 10 tariffs and push it
# 3) heartbeat: push heartbeat.json to pio06 (rrsync-restricted key)
# Logs: ~/cfe/logs/daily_YYYYMMDD.log (30 days kept)
set -u
CFE=~/cfe
PY=$CFE/venv/bin/python
INBOX="argia-cfe@37.235.105.173"     # alias in ~/.ssh/config (rrsync)
mkdir -p $CFE/state $CFE/logs $CFE/outbox
LOG=$CFE/logs/daily_$(date +%Y%m%d).log
exec >>"$LOG" 2>&1
echo "=== cfe_daily $(date -Is) ==="
YM=$(date +%Y-%m)
DOM=$(date +%-d)

push() {  # push file to pio06 inbox; rrsync jail = /opt/argia/cfe_inbox
    rsync -t --timeout=60 "$1" "$INBOX:$(basename "$1")" \
        && echo "pushed $(basename "$1")" || echo "PUSH FAILED $1"
}

# --- 1) probe ---------------------------------------------------------
PROBE_STATUS=fail
if timeout 900 $PY $CFE/cfe_scrape.py --months "$YM" --tariffs GDMTH \
        --out $CFE/state/probe.csv; then
    PROBE_STATUS=ok
fi
PROBE_ROWS=$(($(wc -l < $CFE/state/probe.csv 2>/dev/null || echo 1)-1))
echo "probe: $PROBE_STATUS ($PROBE_ROWS rows)"

# --- 2) monthly full fetch -------------------------------------------
FULL=$CFE/outbox/cfe_${YM}_full.csv
MARK=$CFE/state/sent_$YM
if [ "$PROBE_STATUS" = ok ] && [ ! -f "$MARK" ] \
        && [ "$DOM" -ge 3 ] && [ "$DOM" -le 27 ]; then
    echo "monthly fetch for $YM starting"
    if timeout 7200 $PY $CFE/cfe_scrape.py --months "$YM" \
            --out "$FULL"; then
        push "$FULL" && push "$FULL.manifest.json" && touch "$MARK"
    else
        echo "monthly fetch FAILED (see manifest)"
        [ -f "$FULL.manifest.json" ] && push "$FULL.manifest.json"
    fi
fi

# --- 3) heartbeat -----------------------------------------------------
HB=$CFE/state/heartbeat.json
cat > "$HB" <<EOF
{"ts": "$(date -Is)",
 "probe_status": "$PROBE_STATUS",
 "probe_rows": $PROBE_ROWS,
 "sent_month": "$([ -f "$MARK" ] && echo "$YM" || echo "")",
 "disk_free_mb": $(df -m --output=avail ~ | tail -1 | tr -d ' '),
 "host": "$(hostname)"}
EOF
push "$HB"

# --- housekeeping -----------------------------------------------------
find $CFE/logs -name 'daily_*.log' -mtime +30 -delete
find $CFE/outbox -name '*.csv*' -mtime +90 -delete
echo "=== done $(date -Is) ==="
