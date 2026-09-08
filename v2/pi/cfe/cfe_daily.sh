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
# v240: the scraper runs FROM THE REPO CHECKOUT (deploy.sh keeps it
# current — the v217.1 rule); ~/cfe keeps only venv, divmap, state,
# outbox, logs. The old copy is a fallback for a Pi without a checkout.
SCRAPER=$HOME/argia_v2/v2/pi/cfe/cfe_scrape.py
[ -f "$SCRAPER" ] || SCRAPER=$CFE/cfe_scrape.py
# Bare ssh-config alias, NOT user@host: rsync only consults the Host
# block in ~/.ssh/config when the target has no explicit user@, and the
# alias is what carries the IdentityFile for the rrsync-restricted key.
# Written as "argia-cfe@37.235.105.173" this resolved to a literal user
# named argia-cfe and every push died with "Permission denied".
INBOX="argia-cfe"                    # Host block in ~/.ssh/config
mkdir -p $CFE/state $CFE/logs $CFE/outbox
LOG=$CFE/logs/daily_$(date +%Y%m%d).log
exec >>"$LOG" 2>&1
echo "=== cfe_daily $(date -Is) ==="
YM=$(date +%Y-%m)
DOM=$(date +%-d)

push() {  # push file to pio06 inbox; rrsync jail = /opt/argia/cfe_inbox
    # Must return rsync's own status: the caller chains
    # push "$FULL" && ... && touch "$MARK", so a push() that always
    # succeeded marked the month as sent while nothing had arrived.
    if rsync -t --timeout=60 "$1" "$INBOX:$(basename "$1")"; then
        echo "pushed $(basename "$1")"
        return 0
    fi
    echo "PUSH FAILED $1"
    return 1
}

# --- 1) probe ---------------------------------------------------------
PROBE_STATUS=fail
if timeout 900 $PY $SCRAPER --months "$YM" --tariffs GDMTH \
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
    if [ -s "$FULL" ]; then
        # Scraped on an earlier day but the push failed.  Re-send the
        # file we already have — the scrape costs two hours, the push
        # costs seconds, and the two fail for unrelated reasons.
        echo "monthly CSV for $YM already scraped — retrying the push"
        push "$FULL" && push "$FULL.manifest.json" && touch "$MARK"
    else
        echo "monthly fetch for $YM starting"
        if timeout 7200 $PY $SCRAPER --months "$YM" \
                --out "$FULL"; then
            push "$FULL" && push "$FULL.manifest.json" && touch "$MARK"
        else
            echo "monthly fetch FAILED (see manifest)"
            [ -f "$FULL.manifest.json" ] && push "$FULL.manifest.json"
        fi
    fi
fi

# --- 2b) gap-fill (v240) ----------------------------------------------
# A cell the portal failed to serve on fetch day used to stay missing
# for good. Retry the errored cells of the last months (from the
# manifests in outbox/), at most 5 attempts per cell, and push the
# small CSV; the loader upserts it like any other.
if [ "$PROBE_STATUS" = ok ]; then
    GAP=$CFE/outbox/cfe_gapfill_$(date +%Y%m%d).csv
    if timeout 1800 $PY $SCRAPER --gapfill $CFE/outbox \
            --state $CFE/state/gaps.json --out "$GAP"; then
        if [ -s "$GAP" ] && [ "$(wc -l < "$GAP")" -gt 1 ]; then
            push "$GAP" && push "$GAP.manifest.json"
        else
            rm -f "$GAP" "$GAP.manifest.json"
        fi
    else
        echo "gap-fill: some cells still missing (see $GAP.manifest.json)"
        [ -s "$GAP" ] && [ "$(wc -l < "$GAP")" -gt 1 ] && push "$GAP" && push "$GAP.manifest.json"
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
