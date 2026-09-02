"""Outage-mode PPA plant watch (runs on the Pi, cron every 30 min).

While pio06 is healthy it does ALL plant alerting (severity-aware,
from real telemetry) — and it must stay the only holder of the vendor
sessions, so this script normally exits without touching anything.

When the report-site watchdog has declared the server DOWN, nobody is
watching the plants — so this script takes over the bare-minimum
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


def push(title, msg):
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

    from argia.core.config import load_portfolio
    from argia.core.sheets import SheetsClient
    sheets = SheetsClient(
        sheet_id=os.environ.get("GOOGLE_SHEET_ID_V2", "").strip())
    plants = [p for p in load_portfolio(sheets).active_plants()
              if p.portfolio == "PPA"]
    log("outage mode: checking %d PPA plant(s)" % len(plants))

    now = time.time()
    values = fetch_etoday(plants)
    st = load_state()
    for pk, val in sorted(values.items()):
        rec = st.get(pk, {})
        prev, prev_ts = rec.get("etoday"), rec.get("ts", now)
        last_alert = rec.get("last_alert", 0)
        moved = (val is not None and (prev is None or val > prev + 0.05))
        if moved:
            if rec.get("alerted"):
                push("Plant %s producing again" % pk,
                     "%s today-energy counter moves again (%.1f kWh)."
                     % (pk, val))
            st[pk] = {"etoday": val, "ts": now}
            log("%s OK etoday=%.1f" % (pk, val))
            continue
        stalled = now - prev_ts
        log("%s STALLED %.0f min (etoday=%s)" % (pk, stalled / 60, val))
        if stalled >= STALL_SEC and now - last_alert >= REALERT_SEC:
            push("PPA plant %s NOT producing" % pk,
                 "%s today-energy frozen for %.0f min during daylight "
                 "(checked directly at the vendor - server outage mode)."
                 % (pk, stalled / 60))
            rec.update(last_alert=now, alerted=True)
        rec.setdefault("etoday", prev)
        rec.setdefault("ts", prev_ts)
        st[pk] = rec
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
