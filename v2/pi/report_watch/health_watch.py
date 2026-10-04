"""Server health watch (runs on the office Pi, called by report_watch.sh
every 5 minutes) - v305.

Tomasz, 2026-10-04: "make sure we have our server monitored, either from
the Pi or GitHub ... mails should be last resort".

The server's monitoring job (alert_mailer.py, every 30 min) writes
/root/argia_backups/health.json; this script fetches it with the SAME
read-only SFTP key the nightly backup pull uses (no new secret, nothing
public) and pushes to the ntfy topic already on Tomasz's phone:

* a CRITICAL monitoring problem (a monitoring job failed, telemetry
  collection stopped, PostgreSQL down, disk >= 90 %) - at once, again
  every REPUSH_H hours while it lasts, one low-priority note when it
  clears;
* the health file older than STALE_MIN minutes - the monitoring job or
  the server is dead (a portal outage is report_watch's own push, so
  nothing is pushed twice while the portal is down);
* the file unreadable twice in a row (SFTP refused, server unreachable).

WARNINGs and the "other" list are in the file, never pushed. Standard
library only; the decision is the pure ``decide`` (unit-tested).

State: ~/report_watch/health_state.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

NTFY_TOPIC = "argia-reportwatch-x9k24fq7"
SRV = "root@37.235.105.173"
KEY = os.path.expanduser("~/.ssh/argia_backup_pull")
STATE_FILE = os.path.expanduser("~/report_watch/health_state.json")
WATCH_STATE = os.path.expanduser("~/report_watch/state")
STALE_MIN = 45            # the server writes every 30 min
REPUSH_H = 12
UNREAD_AFTER = 2          # consecutive failed fetches (10 min) before a push


def _ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def decide(state, health, fetch_ok, now, portal_down=False):
    """(pushes [(priority, title, message)], new_state). Pure.

    ``state``: {"fails": int, "pushed": {key: epoch}, "titles": {key: title},
    "stale_at": epoch, "unread_at": epoch}; ``health``: the parsed file or None."""
    st = {"fails": int(state.get("fails", 0)), "pushed": dict(state.get("pushed", {})),
          "titles": dict(state.get("titles", {})), "stale_at": float(state.get("stale_at", 0)),
          "unread_at": float(state.get("unread_at", 0))}
    pushes = []
    again = REPUSH_H * 3600
    if not fetch_ok or not isinstance(health, dict):
        st["fails"] += 1
        if st["fails"] >= UNREAD_AFTER and not portal_down and now - st["unread_at"] >= again:
            pushes.append(("high", "ARGIA server: health file unreadable",
                           "The office Pi could not read the server's health file %d times in a row"
                           " (SFTP refused or server unreachable)." % st["fails"]))
            st["unread_at"] = now
        return pushes, st
    st["fails"], st["unread_at"] = 0, 0.0
    try:
        age_min = (now - _ts(health["generated_utc"])) / 60.0
    except (KeyError, ValueError, TypeError):
        age_min = None
    if age_min is None or age_min > STALE_MIN:
        if not portal_down and now - st["stale_at"] >= again:
            pushes.append(("high", "ARGIA server: monitoring stopped",
                           "The server's health file is %s - the monitoring job or the server is not"
                           " running; alerts are not being evaluated." %
                           ("unreadable" if age_min is None else "%.0f min old" % age_min)))
            st["stale_at"] = now
        return pushes, st
    if st["stale_at"]:
        pushes.append(("low", "ARGIA server: monitoring running again", "The health file is fresh again."))
        st["stale_at"] = 0.0
    crit = {p["key"]: p for p in health.get("problems", []) if p.get("severity") == "CRITICAL"}
    for key, p in sorted(crit.items()):
        if now - st["pushed"].get(key, 0) >= again:
            pushes.append(("high", "ARGIA: " + p.get("title", key),
                           "%s - since %s UTC (server health check)." % (p.get("title", key),
                                                                         p.get("since_utc", "?").replace("T", " ")[:16])))
            st["pushed"][key] = now
            st["titles"][key] = p.get("title", key)
    for key in sorted(set(st["pushed"]) - set(crit)):
        pushes.append(("low", "ARGIA: resolved - " + st["titles"].get(key, key), "No longer reported by the server."))
        st["pushed"].pop(key, None)
        st["titles"].pop(key, None)
    return pushes, st


# ------------------------------------------------------------------ I/O
def fetch():
    """The health file via SFTP, or None."""
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "health.json")
        r = subprocess.run(["sftp", "-q", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                            "-o", "ConnectTimeout=20", SRV], input="get health.json %s\n" % dst,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(dst):
            return None
        try:
            with open(dst, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None


def push(prio, title, msg):
    subprocess.run(["curl", "-sS", "-m", "15", "-H", "Title: " + title, "-H", "Priority: " + prio,
                    "-H", "Tags: " + ("rotating_light" if prio == "high" else "white_check_mark"),
                    "-d", msg, "https://ntfy.sh/" + NTFY_TOPIC], capture_output=True, timeout=20)


def main():
    if not os.path.exists(KEY):
        return 0                    # not the office Pi (or not set up): nothing to watch from here
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    try:
        portal_down = "status=DOWN" in open(WATCH_STATE, encoding="utf-8").read()
    except OSError:
        portal_down = False
    health = fetch()
    pushes, state = decide(state, health, health is not None, dt.datetime.now(dt.timezone.utc).timestamp(),
                           portal_down)
    for prio, title, msg in pushes:
        push(prio, title, msg)
        print("%s health push (%s): %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), prio, title))
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    if health is not None and not pushes:
        print("%s health %s, %d problem(s)" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                               health.get("status"), len(health.get("problems", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
