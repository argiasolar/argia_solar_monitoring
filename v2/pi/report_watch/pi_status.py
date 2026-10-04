"""The office Pi reports on itself to the server (v305.2) - hourly, called
by report_watch.sh.

Tomasz, 2026-10-04: "make sure Pi is working as our watchdog". Nobody can
log in to the Pi from outside, so until now the only proof that it works
was indirect (SFTP logins in the server's sshd log) - and that showed no
nightly backup pull for a week. This script writes pi_status.json - what
cron runs, when each Pi job last wrote its log and what it said, the newest
off-site backup and its age, the CFE heartbeat, disk, the checkout's git
commit - and pushes it (as pi_status_<UTC stamp>.json, v308) into the server's CFE inbox with the CFE job's own
write-only rrsync key (ssh alias "argia-cfe"). The server's health job
(alert_mailer.py) reads it: a silent Pi or a stale backup becomes a
problem in health.json (and, after 6 h, the last-resort mail).

Standard library only; ``build`` is pure (unit-tested).
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
LOGS = os.path.join(HOME, "argia_logs")
JOB_LOGS = {"deploy": "deploy_cron.log", "report_watch": "report_watch.log",
            "ppa_watch": "ppa_watch.log", "backup_pull": "db_backup_pull.log"}
BACKUPS = os.path.join(HOME, "db_backups")
CFE = os.path.join(HOME, "cfe")
INBOX = "argia-cfe"           # ssh-config alias of the CFE job (write-only rrsync jail)


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _last_line(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4096))
            lines = [ln.strip() for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-1][:200] if lines else ""
    except OSError:
        return None


def _tail(path, n=12):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            lines = [ln.rstrip()[:200] for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-n:]
    except OSError:
        return []


def build(now, files, crontab, git_head, disk_free_mb, hostname):
    """The status document. ``files``: {name: (path, mtime or None, last
    line or None)} for the job logs; plus 'dumps' / 'weekly' lists of
    (name, mtime) and 'cfe_heartbeat' (mtime, writable). Pure."""
    dumps = sorted(files.get("dumps", []), key=lambda x: x[1])
    newest = dumps[-1] if dumps else None
    hb = files.get("cfe_heartbeat")
    return {
        "ts": _iso(now), "host": hostname, "git_head": git_head, "disk_free_mb": disk_free_mb,
        "crontab": crontab,
        "jobs": {k: {"log_mtime": _iso(v[1]), "last": v[2]} for k, v in sorted(files.get("logs", {}).items())},
        "backup": {"newest": newest[0] if newest else None,
                   "age_h": round((now - newest[1]) / 3600.0, 1) if newest else None,
                   "daily_count": len(dumps), "weekly_count": len(files.get("weekly", []))},
        "cfe_heartbeat": ({"age_h": round((now - hb[0]) / 3600.0, 1) if hb[0] else None, "writable": hb[1]}
                          if hb else None),
        "tails": files.get("tails", {}),          # the last lines of the CFE and backup logs, for diagnosis
    }


def push_name(now):
    """v308: a new name for every push. The server's rsync 3.5.0 (Debian
    security update, 29 Sep 2026) refuses to replace an existing file in the
    write-only jail, so a fixed pi_status.json landed once and never again;
    the server reads the newest pi_status_*.json and removes the older ones."""
    return "pi_status_%s.json" % dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def gather(now):
    logs = {}
    for k, name in JOB_LOGS.items():
        p = os.path.join(LOGS, name)
        logs[k] = (p, os.path.getmtime(p) if os.path.exists(p) else None, _last_line(p))
    cfe_logs = sorted(glob.glob(os.path.join(CFE, "logs", "daily_*.log")))
    if cfe_logs:
        logs["cfe_daily"] = (cfe_logs[-1], os.path.getmtime(cfe_logs[-1]), _last_line(cfe_logs[-1]))
    dumps = [(os.path.basename(p), os.path.getmtime(p)) for p in glob.glob(os.path.join(BACKUPS, "daily", "argia_mont_*.dump"))]
    weekly = [(os.path.basename(p), os.path.getmtime(p)) for p in glob.glob(os.path.join(BACKUPS, "weekly", "argia_mont_*.dump"))]
    hb = os.path.join(CFE, "state", "heartbeat.json")
    hb_info = ((os.path.getmtime(hb) if os.path.exists(hb) else None),
               os.access(hb, os.W_OK) if os.path.exists(hb) else os.access(os.path.dirname(hb), os.W_OK))
    try:
        crontab = [ln for ln in subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
                   .stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    except Exception:  # noqa: BLE001
        crontab = None
    try:
        head = subprocess.run(["git", "-C", os.path.join(HOME, "argia_v2"), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip() or None
    except Exception:  # noqa: BLE001
        head = None
    try:
        st = os.statvfs(HOME)
        free = int(st.f_bavail * st.f_frsize / 1048576)
    except OSError:
        free = None
    tails = {k: _tail(v[0]) for k, v in logs.items() if k in ("cfe_daily", "backup_pull")}
    return build(now, {"logs": logs, "dumps": dumps, "weekly": weekly, "cfe_heartbeat": hb_info, "tails": tails},
                 crontab, head, free, os.uname().nodename)


def main():
    if not os.path.exists(os.path.join(HOME, ".ssh", "config")):
        return 0                    # not the office Pi
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    doc = gather(now)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "pi_status.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        r = subprocess.run(["rsync", "--timeout=60", p, INBOX + ":" + push_name(now)], capture_output=True, text=True, timeout=90)
    print("%s pi_status %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "pushed" if r.returncode == 0 else "PUSH FAILED: " + r.stderr.strip()[:160]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
