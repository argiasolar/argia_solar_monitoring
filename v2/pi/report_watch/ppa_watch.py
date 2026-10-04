"""Outage-mode PPA plant watch (runs on the Pi, cron every 30 min).

While pio06 is healthy it does ALL plant alerting (severity-aware,
from real telemetry) - and it must stay the only holder of the vendor
sessions, so this script normally exits without touching anything.

When the report-site watchdog has declared the server DOWN, nobody is
watching the plants - so this script takes over the bare-minimum
question: are the PPA plants still producing? It polls the vendor
clouds directly (no session conflict: the dead server is not polling)
and pushes an ntfy alert when a plant's today-energy counter stops
moving during daylight.

Detection is counter-based, the fleet's own doctrine: even under heavy
cloud the today-kWh counter creeps up; a counter frozen for >= ~55
minutes in the 08:00-19:00 window means the plant (or its monitoring)
is really down. One alert per plant per 2 h, plus a recovery note.

State: ~/report_watch/ppa_state.json ·  Log: ~/argia_logs/ppa_watch.log
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/argia_v2/v2"))

NTFY_TOPIC = "argia-reportwatch-x9k24fq7"
STATE_FILE = os.path.expanduser("~/report_watch/ppa_state.json")
PORTFOLIO_JSON = os.path.expanduser("~/report_watch/portfolio.json")   # v214
WATCH_STATE = os.path.expanduser("~/report_watch/state")
STALL_SEC = 55 * 60          # counter frozen this long => alert
REALERT_SEC = 2 * 3600
DAY_START, DAY_END = 8, 19   # Pi runs MX local time


def log(msg):
    print("%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def server_is_down() -> bool:
    try:
        txt = open(WATCH_STATE).read()
    except OSError:
        return False
    return "status=DOWN" in txt


def push_enabled(conf=None):
    """v305.2: phone pushes are switched in push.conf next to this file
    (PUSH=on|off); the ARGIA_PUSH environment variable overrides it."""
    env = os.environ.get("ARGIA_PUSH")
    if env:
        return env.strip().lower() == "on"
    conf = conf or os.path.join(os.path.dirname(os.path.abspath(__file__)), "push.conf")
    try:
        for ln in open(conf, encoding="utf-8"):
            if ln.strip().startswith("PUSH="):
                return ln.split("=", 1)[1].strip().lower() == "on"
    except OSError:
        pass
    return False


def push(title, msg):
    if not push_enabled():
        log("alert (push off): " + title)
        return
    try:
        subprocess.run(
            ["curl", "-sS", "-m", "15", "-H", "Title: " + title,
             "-H", "Priority: high", "-H", "Tags: rotating_light",
             "-d", msg, "https://ntfy.sh/" + NTFY_TOPIC],
            capture_output=True, timeout=20)
        log("alert sent: " + title)
    except Exception as e:                                  # noqa: BLE001
        log("alert FAILED: %r" % e)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:                                       # noqa: BLE001
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"))


def step(rec, val, now, today):
    """One plant, one check. Pure: returns (new record, action, seconds frozen).

    action: None (fine / still waiting), "frozen" (alert: NOT producing),
    "recovered" (alert: producing again), "unknown" (alert: vendor did not
    answer - that is NOT 'not producing').

    v275 fixes (2026-09-29, six plants pushed as 'NOT producing ... frozen for
    32910 min' - 23 days):
    * the record belongs to ONE day: yesterday's (or a September outage's)
      counter is never the baseline for today's - a new day starts a fresh one;
    * no answer from the vendor (val None) is 'unknown', never 'frozen';
    * frozen time is measured from the last time the counter moved TODAY."""
    rec = dict(rec or {})
    if rec.get("day") != today:
        rec = {"day": today}
    if val is None:
        if now - rec.get("last_unknown", 0) >= REALERT_SEC:
            rec["last_unknown"] = now
            return rec, "unknown", 0.0
        return rec, None, 0.0
    prev = rec.get("etoday")
    if prev is None or val > prev + 0.05:
        action = "recovered" if rec.get("alerted") else None
        rec.update(etoday=val, ts=now, alerted=False)
        return rec, action, 0.0
    stalled = now - rec.get("ts", now)
    if stalled >= STALL_SEC and now - rec.get("last_alert", 0) >= REALERT_SEC:
        rec.update(last_alert=now, alerted=True)
        return rec, "frozen", stalled
    return rec, None, stalled


def fetch_etoday(plants):
    """{plant_key: today_kwh or None} straight from the vendor clouds."""
    out = {}
    today = dt.date.today().isoformat()

    growatt = [p for p in plants if p.brand.upper() == "GROWATT"]
    if growatt:
        try:
            from argia.vendors.growatt_web import GrowattWebClient
            from argia.vendors.growatt_web_parser import parse_max_total_data
            c = GrowattWebClient(
                username=os.environ["GROWATT_USERNAME"],
                password=os.environ["GROWATT_PASSWORD"])
            c.login()
            for p in growatt:
                pid = str(p.site_id or "").strip()
                try:
                    d = parse_max_total_data(c.get_max_total_data(pid))
                    out[p.plant_key] = d.e_today_kwh if d else None
                except Exception as e:                      # noqa: BLE001
                    log("growatt %s: %r" % (p.plant_key, e))
                    out[p.plant_key] = None
                time.sleep(1)
        except Exception as e:                              # noqa: BLE001
            log("growatt login failed: %r" % e)

    hw = [p for p in plants if p.brand.upper() == "HUAWEI"]
    if hw:
        try:
            from argia.vendors.huawei import HuaweiClient
            hc = HuaweiClient(
                username=os.environ["HUAWEI_USERNAME"],
                password=os.environ["HUAWEI_PASSWORD"])
            for p in hw:
                try:
                    out[p.plant_key] = hc.fetch_day_kwh(p, today)
                except Exception as e:                      # noqa: BLE001
                    log("huawei %s: %r" % (p.plant_key, e))
                    out[p.plant_key] = None
        except Exception as e:                              # noqa: BLE001
            log("huawei client failed: %r" % e)
    return out


def main() -> int:
    hour = dt.datetime.now().hour
    if not server_is_down():
        return 0                    # server healthy: it does the alerting
    if not (DAY_START <= hour <= DAY_END):
        return 0                    # night: zero production is normal

    # v214: the workbook is retired - the plant list comes from the
    # portfolio snapshot pulled nightly with the backups (pull_backup.sh)
    from argia.core.config import portfolio_from_records
    with open(PORTFOLIO_JSON, encoding="utf-8") as fh:
        snap = json.load(fh)
    plants = [p for p in portfolio_from_records(
        snap.get("plants", []), snap.get("inverters", [])).active_plants()
              if p.portfolio == "PPA"]
    log("outage mode: checking %d PPA plant(s)" % len(plants))
    # v217: name first, code as the detail - never "NL2" alone
    from argia.alerts.naming import short_customer
    name = {p.plant_key: "%s (%s)" % (short_customer(p.customer), p.plant_key)
            if short_customer(p.customer) else p.plant_key for p in plants}

    now = time.time()
    today = dt.date.today().isoformat()
    values = fetch_etoday(plants)
    st = load_state()
    for pk, val in sorted(values.items()):
        rec, action, stalled = step(st.get(pk, {}), val, now, today)
        st[pk] = rec
        who = name.get(pk, pk)
        if action == "recovered":
            push("%s producing again" % who, "%s today-energy counter moves again (%.1f kWh)." % (who, val))
        elif action == "frozen":
            push("%s NOT producing" % who,
                 "%s today-energy frozen for %.0f min during daylight "
                 "(checked directly at the vendor - server outage mode)." % (who, stalled / 60))
        elif action == "unknown":
            push("%s: vendor not reachable" % who,
                 "%s could not be checked at the vendor (no answer) - server outage mode. "
                 "Not the same as 'not producing'." % who)
        log("%s %s etoday=%s frozen=%.0f min" % (pk, action or "ok", val, stalled / 60))
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
