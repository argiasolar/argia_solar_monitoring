#!/bin/bash
# ARGIA report site watchdog (runs on the Pi, cron */5).
#
# Probes https://report.argia.com.mx/login (public page, no auth) and
# alerts when the site stops answering. The Pi is the right vantage
# point: it is OUTSIDE the server, so it keeps working when pio06 dies
# (2026-09-01 outage: SSH, ping and TLS all dead while cron on the
# server could alert nobody).
#
# Alert rules (state kept in ~/report_watch/state):
#   * DOWN alert after 2 consecutive failures (>=10 min down) — one
#     flaky probe never pages anyone
#   * while down, repeat the alert every 60 min, not every 5
#   * one UP (recovery) alert on the first success after a DOWN
#
# Channels:
#   * ntfy.sh push (topic below) — works with no local secrets;
#     subscribe to the topic in the ntfy app to receive alerts
#   * email — automatically used IF ~/report_watch/send_mail_hook.sh
#     exists (wired to the ARGIA mailer credentials once copied from
#     the server; absent = silently skipped)

URL="https://report.argia.com.mx/login"
MARKER="ARGIA"
NTFY_TOPIC="argia-reportwatch-x9k24fq7"
STATE_DIR="$HOME/report_watch"
STATE="$STATE_DIR/state"
REALERT_SEC=3600
mkdir -p "$STATE_DIR"

now=$(date +%s)
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# ---- probe ----
body=$(curl -sS -m 20 --retry 1 "$URL" 2>/tmp/report_watch_err)
rc=$?
ok=0
if [ $rc -eq 0 ] && echo "$body" | grep -q "$MARKER"; then
  ok=1
fi

# ---- state ----
fails=0; status=OK; last_alert=0
[ -f "$STATE" ] && . "$STATE"

alert() {  # $1 = title, $2 = message
  curl -sS -m 15 -H "Title: $1" -H "Priority: high" -H "Tags: warning" \
       -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 \
    && echo "$(stamp) alert sent (ntfy): $1" \
    || echo "$(stamp) alert FAILED to send (ntfy): $1"
  if [ -x "$STATE_DIR/send_mail_hook.sh" ]; then
    "$STATE_DIR/send_mail_hook.sh" "$1" "$2" \
      && echo "$(stamp) alert sent (mail): $1" \
      || echo "$(stamp) alert FAILED to send (mail): $1"
  fi
}

if [ $ok -eq 1 ]; then
  if [ "$status" = DOWN ]; then
    alert "ARGIA report site is BACK UP" \
          "report.argia.com.mx answers again at $(stamp) (office view from the Pi)."
  fi
  echo "$(stamp) OK"
  printf 'fails=0\nstatus=OK\nlast_alert=0\n' > "$STATE"
else
  fails=$((fails + 1))
  err=$(head -c 160 /tmp/report_watch_err 2>/dev/null)
  echo "$(stamp) FAIL #$fails (curl rc=$rc) $err"
  new_status=$status
  if [ $fails -ge 2 ]; then
    new_status=DOWN
    if [ $((now - last_alert)) -ge $REALERT_SEC ]; then
      alert "ARGIA report site is DOWN" \
            "report.argia.com.mx not loading since >= $((fails * 5)) min (probe from the office Pi, curl rc=$rc $err). Check pio06 / hosting."
      last_alert=$now
    fi
  fi
  printf 'fails=%s\nstatus=%s\nlast_alert=%s\n' \
         "$fails" "$new_status" "$last_alert" > "$STATE"
fi
