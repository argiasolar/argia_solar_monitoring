#!/bin/bash
# portal.argia.com.mx watchdog (runs on the Pi, cron */5).
#
# Probes https://portal.argia.com.mx/login (public page, no auth) and
# alerts when the site stops answering. The Pi is the right vantage
# point: it is OUTSIDE the server, so it keeps working when pio06 dies
# (2026-09-01 outage: SSH, ping and TLS all dead while cron on the
# server could alert nobody).
#
# Alert rules (state kept in ~/report_watch/state):
#   * DOWN alert after 2 consecutive failures (>=10 min down) - one
#     flaky probe never pages anyone
#   * while down, repeat the alert every 60 min, not every 5
#   * one UP (recovery) alert on the first success after a DOWN
#
# Channels:
#   * ntfy.sh push (topic below) - works with no local secrets;
#     subscribe to the topic in the ntfy app to receive alerts
#   * email - automatically used IF ~/report_watch/send_mail_hook.sh
#     exists (wired to the ARGIA mailer credentials once copied from
#     the server; absent = silently skipped)

URL="https://portal.argia.com.mx/login"
HOST="portal.argia.com.mx"
# v275: the server's address, for the second probe when the Pi's own DNS
# fails. 2026-09-29: 11 "portal DOWN" pushes in one afternoon were all
# "curl rc=6 Could not resolve host" on the office Pi while Google and
# Cloudflare DNS answered 37.235.105.173 and the portal served every page.
# If the server ever moves, this probe simply fails and the old behaviour
# (DOWN alert) returns - it can hide nothing.
PORTAL_IP="37.235.105.173"
MARKER="ARGIA"
NTFY_TOPIC="argia-reportwatch-x9k24fq7"
STATE_DIR="$HOME/report_watch"
STATE="$STATE_DIR/state"
REALERT_SEC=3600
DNS_REALERT_SEC=21600        # the Pi's own DNS trouble: one low-priority note per 6 h
mkdir -p "$STATE_DIR"

now=$(date +%s)
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# ---- probe ----
# v217: the HTTP status travels with the alert ("portal.argia.com.mx is
# DOWN - HTTP 502" tells a different story than "curl rc=28 timeout")
body=$(curl -sS -m 20 --retry 1 -w '\n__HTTP__%{http_code}' "$URL" 2>/tmp/report_watch_err)
rc=$?
http=$(printf '%s' "$body" | sed -n 's/^__HTTP__//p' | tail -1)
body=$(printf '%s' "$body" | sed '/^__HTTP__/d')
[ "$http" = 000 ] && http="no response"
ok=0
# the portal answers the public /login page with 401 + the login body
if [ $rc -eq 0 ] && echo "$body" | grep -q "$MARKER"; then
  ok=1
fi

# ---- state ----
fails=0; status=OK; last_alert=0; last_dns_note=0
[ -f "$STATE" ] && . "$STATE"

# ---- v275: the Pi could not resolve the name - is the PORTAL down, or the Pi's DNS? ----
dns_only=0
if [ $ok -eq 0 ] && [ $rc -eq 6 ]; then
  body2=$(curl -sS -m 20 --resolve "$HOST:443:$PORTAL_IP" "$URL" 2>/dev/null)
  if [ $? -eq 0 ] && echo "$body2" | grep -q "$MARKER"; then
    dns_only=1
  fi
fi

# v305.2: phone pushes are switched in push.conf (ARGIA_PUSH overrides, for tests)
PUSH=off
[ -f "$(dirname "$0")/push.conf" ] && . "$(dirname "$0")/push.conf"
PUSH="${ARGIA_PUSH:-$PUSH}"

alert() {  # $1 = title, $2 = message, $3 = ntfy priority (default high)
  if [ "$PUSH" = on ]; then
    curl -sS -m 15 -H "Title: $1" -H "Priority: ${3:-high}" -H "Tags: warning" \
         -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 \
      && echo "$(stamp) alert sent (ntfy): $1" \
      || echo "$(stamp) alert FAILED to send (ntfy): $1"
  else
    echo "$(stamp) alert (push off): $1"
  fi
  if [ -x "$STATE_DIR/send_mail_hook.sh" ]; then
    "$STATE_DIR/send_mail_hook.sh" "$1" "$2" \
      && echo "$(stamp) alert sent (mail): $1" \
      || echo "$(stamp) alert FAILED to send (mail): $1"
  fi
}

save() { printf 'fails=%s\nstatus=%s\nlast_alert=%s\nlast_dns_note=%s\n' "$1" "$2" "$3" "$last_dns_note" > "$STATE"; }

if [ $dns_only -eq 1 ]; then
  # the portal is UP (it answered at its address); only the Pi cannot look names up
  echo "$(stamp) OK via $PORTAL_IP - the Pi's DNS failed (curl rc=6), the portal did not"
  if [ $((now - last_dns_note)) -ge $DNS_REALERT_SEC ]; then
    alert "Office Pi: DNS lookups failing (portal is UP)" \
          "The office Pi could not resolve $HOST at $(stamp), but the portal answered at $PORTAL_IP. Not a portal outage: the Pi's network / DNS server (office router or ISP) is failing. Its other jobs (nightly backup pull, CFE) may fail too." low
    last_dns_note=$now
  fi
  if [ "$status" = DOWN ]; then
    alert "portal.argia.com.mx is BACK UP" \
          "portal.argia.com.mx answers (at $PORTAL_IP) at $(stamp) - probe from the office Pi."
  fi
  save 0 OK 0
elif [ $ok -eq 1 ]; then
  if [ "$status" = DOWN ]; then
    alert "portal.argia.com.mx is BACK UP" \
          "portal.argia.com.mx answers again (HTTP ${http:-?}) at $(stamp) - probe from the office Pi."
  fi
  echo "$(stamp) OK (HTTP ${http:-?})"
  save 0 OK 0
else
  fails=$((fails + 1))
  err=$(head -c 160 /tmp/report_watch_err 2>/dev/null)
  echo "$(stamp) FAIL #$fails (HTTP ${http:-none}, curl rc=$rc) $err"
  new_status=$status
  if [ $fails -ge 2 ]; then
    new_status=DOWN
    if [ $((now - last_alert)) -ge $REALERT_SEC ]; then
      alert "portal.argia.com.mx is DOWN" \
            "portal.argia.com.mx not loading since >= $((fails * 5)) min - HTTP ${http:-none}, curl rc=$rc $err (probe from the office Pi). Check the server / hosting."
      last_alert=$now
    fi
  fi
  save "$fails" "$new_status" "$last_alert"
fi

# v305: the server's own health (monitoring jobs, telemetry, database, disk) -
# health_watch.py reads /root/argia_backups/health.json over the backup SFTP
# key and pushes CRITICAL problems; it does nothing where that key is absent
python3 "$(dirname "$0")/health_watch.py" 2>&1 || echo "$(stamp) health_watch failed"

# v309: a missed nightly backup pull is retried once an hour until the newest
# off-site dump is fresh again (5 Sep - 4 Oct: 29 nights lost, nothing retried);
# after a catch-up the status report goes out at once, so the server sees it
CU_OUT=$(bash "$(dirname "$0")/../db_backups/catchup.sh" 2>&1)
[ -n "$CU_OUT" ] && echo "$CU_OUT"
case "$CU_OUT" in *"pull OK"*) rm -f "$STATE_DIR/pi_status_at" ;; esac

# v319: the Pi holds the key of the encrypted ARGIA for Prologis volume; when
# prologis.argia.com.mx stops answering (e.g. after a server reboot) it sends
# the key to the server's unlock-only forced command
UL_OUT=$(bash "$(dirname "$0")/../prologis_vault/unlock.sh" 2>&1)
[ -n "$UL_OUT" ] && echo "$UL_OUT"

# v305.2: the Pi reports on itself to the server once an hour (pi_status.py:
# cron, job logs, newest off-site backup, CFE heartbeat) - the server's health
# job turns a silent Pi or a stale backup into a problem
PS_STAMP="$STATE_DIR/pi_status_at"
if [ $((now - $(cat "$PS_STAMP" 2>/dev/null || echo 0))) -ge 3300 ]; then
  python3 "$(dirname "$0")/pi_status.py" 2>&1 || echo "$(stamp) pi_status failed"
  echo "$now" > "$PS_STAMP"
fi
