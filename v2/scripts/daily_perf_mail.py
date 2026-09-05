"""Daily PPA performance email — 19:00 Mexico City (pio06 only).

Tomasz, 2026-09-02: "create email with PPA daily performance where we
summarize the day, we show performance, issues, etc use some modern
template, send it around 7pm so we can still act if something is not
ok even though the data are not yet finalized."

So: this mail is an EARLY heads-up, not the book of record. At 19:00
the nightly close (kpi_eod) has not run — today's figures come straight
from live telemetry (per-inverter max etoday counters) and today's
weather-expected does not exist yet. The mail says so plainly and
leans on what IS solid: yesterday's closed numbers and the
month-to-date vs weather expectation from daily_production.

Recipients: the 'daily' channel of mail_subscription (managed in
/setup/, portal users only). No plant scoping on this channel — it is
one fleet-wide PPA summary by design.

Fires from argia-dailyperf.timer (19:00 America/Mexico_City,
Persistent=true) through run_job.sh. Dry-run prints the text body and
writes the HTML next to /tmp for eyeballing, sends nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as _html
import logging
import os
import re as _re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.alerts import emailer, subscriptions
from argia.core.time_utils import MX_TZ, parse_pg_ts
from argia.store import pg_mirror

LOG = logging.getLogger("argia.daily_perf_mail")

STALE_MIN = 45          # same threshold the alert mailer uses


def display_name(customer):
    """Human name for the mail — never the plant code (Tomasz, v180:
    'use names in first place and the code like GTO1 as additional').
    'TAIGENE PPA roof (Leon, GTO)' -> 'Taigene'; short all-caps
    acronyms (SAG, SMS) survive. Kept byte-identical to the copy in
    server/monitoring_gen.py — a unit test compares the two. Pure."""
    s = str(customer or '').split('(')[0].split(',')[0]
    for cut in (' PPA', ' CAPEX', ' roof', ' land'):
        i = s.find(cut)
        if i > 0:
            s = s[:i]
    parts = s.strip().split()
    if len(parts) == 1 and len(parts[0]) <= 3 and parts[0].isupper():
        return parts[0]                          # SAG, SMS stay acronyms
    return ' '.join('-'.join(p[:1].upper() + p[1:].lower()
                             for p in w.split('-')) for w in parts)


def logo_png():
    """ARGIA SOLAR wordmark bytes for CID embedding, or None. Reads
    the official asset (server/bundle/argia_logo.py data URI) so mail
    and portal can never diverge; missing file degrades to text."""
    import base64
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "server", "bundle", "argia_logo.py")
    try:
        src = open(path, encoding="utf-8").read()
        m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", src)
        return base64.b64decode(m.group(1)) if m else None
    except OSError:
        return None

# ------------------------------------------------------------- gathering

def gather_plants():
    from argia.store.pgq import psql_rows
    return [(r[0], r[1], float(r[2])) for r in psql_rows(
        "SELECT plant_key, customer, kwp_dc FROM plant"
        " WHERE active AND portfolio = 'PPA' ORDER BY plant_key;")
        if len(r) >= 3]


def gather_today():
    """{plant: (kwh_today, minutes_since_last_sample, inverters_seen)}
    from live telemetry, MX 'today'. Energy = sum of per-inverter max
    etoday counters — same basis the plant pages use intraday."""
    from argia.store.pgq import psql_rows
    mx_day = ("(ts_utc AT TIME ZONE 'America/Mexico_City')::date"
              " = (now() AT TIME ZONE 'America/Mexico_City')::date")
    out: Dict[str, Tuple[float, Optional[float], int]] = {}
    for r in psql_rows(
            "SELECT plant_key, coalesce(sum(e),0),"
            " min(age_min), count(*) FROM ("
            " SELECT plant_key, inverter_sn, max(etoday_kwh) AS e,"
            "  extract(epoch FROM now() - max(ts_utc))/60 AS age_min"
            f" FROM telemetry WHERE {mx_day}"
            "  AND (etoday_kwh IS NOT NULL OR power_w IS NOT NULL)"
            " GROUP BY 1, 2) s GROUP BY 1;"):
        if len(r) >= 4:
            try:
                out[r[0]] = (float(r[1]), float(r[2]), int(r[3]))
            except ValueError:
                pass
    return out


def gather_inverter_counts():
    from argia.store.pgq import psql_rows
    return {r[0]: int(r[1]) for r in psql_rows(
        "SELECT plant_key, count(*) FROM inverter WHERE active"
        " GROUP BY 1;") if len(r) >= 2}


def gather_yesterday(today: dt.date):
    from argia.store.pgq import psql_rows
    d = (today - dt.timedelta(days=1)).isoformat()
    out = {}
    for r in psql_rows(
            "SELECT plant_key, coalesce(energy_kwh,0)"
            f" FROM daily_production WHERE prod_date = '{d}';"):
        if len(r) >= 2:
            try:
                out[r[0]] = float(r[1])
            except ValueError:
                pass
    return out


def gather_mtd(today: dt.date):
    """{plant: (mtd_kwh, paired_kwh, expected_kwh)} through yesterday,
    from the closed daily_production rows. Paired = production on days
    that HAVE an expected figure, so vs-expected never divides a full
    month by a partial expectation (the v174 lesson)."""
    from argia.store.pgq import psql_rows
    d0 = today.replace(day=1).isoformat()
    d1 = (today - dt.timedelta(days=1)).isoformat()
    out = {}
    if d1 < d0:                     # the 1st: no closed days yet
        return out
    for r in psql_rows(
            "SELECT plant_key, coalesce(sum(energy_kwh),0),"
            " coalesce(sum(CASE WHEN expected_kwh IS NOT NULL"
            "  THEN energy_kwh END),0), coalesce(sum(expected_kwh),0)"
            f" FROM daily_production WHERE prod_date BETWEEN '{d0}'"
            f" AND '{d1}' GROUP BY 1;"):
        if len(r) >= 4:
            try:
                out[r[0]] = (float(r[1]), float(r[2]), float(r[3]))
            except ValueError:
                pass
    return out


def job_name_from_execstart(value: str) -> str:
    """The run_job.sh job name out of a systemd ExecStart value. Pure.

    ``systemctl show -p ExecStart --value`` does NOT print a plain
    command line — it prints the structured form
    ``{ path=/.../run_job.sh ; argv[]=/.../run_job.sh telemetry x.py ... }``
    so a naive '\\S+' after run_job.sh captures the ';' of the path=
    field. Only a real job-name token counts, which skips it.
    """
    m = _re.search(r"run_job\.sh\s+([A-Za-z0-9_.-]+)", value or "")
    return m.group(1) if m else ""


def unit_log_path(unit: str) -> str:
    """The job log a systemd unit writes to, via its run_job.sh name.

    run_job.sh logs to $HOME/argia_logs/<name>.log.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ExecStart", "--value"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return ""
    name = job_name_from_execstart(out)
    return "/root/argia_logs/%s.log" % name if name else ""


def unit_error(unit: str) -> str:
    """The newest ERROR line from a failed unit's own job log.

    This is the difference between 'unit-failed' and 'sheet write failed:
    ... above the limit of 10000000 cells' (the 2026-09-03 incident).
    """
    path = unit_log_path(unit)
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as fh:                 # tail ~64 KB
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return last_error_line(tail)


def inverter_labels():
    """{serial: 'Inverter 3'} so a silent-inverter alert names the unit
    an engineer can find on site, not only its serial."""
    from argia.store.pgq import psql_rows
    try:
        return {r[0]: r[1] for r in psql_rows(
            "SELECT DISTINCT ON (inverter_sn) inverter_sn, inverter_label"
            " FROM telemetry WHERE ts_utc > now() - interval '7 days'"
            " ORDER BY inverter_sn, ts_utc DESC;") if len(r) >= 2 and r[1]}
    except RuntimeError:
        return {}


def gather_issues():
    """Active alerts from alert_state (what the alert mailer tracks) —
    key, severity, since when, and whatever detail makes each one
    actionable — plus plants under a logged maintenance window."""
    from argia.store.pgq import psql_rows
    labels = inverter_labels()
    alerts = []
    for r in psql_rows(
            "SELECT key, coalesce(severity,'CRITICAL'), first_seen"
            " FROM alert_state WHERE active ORDER BY 2, 1;"):
        if len(r) < 2:
            continue
        key, sev = r[0], r[1]
        first_seen = None
        if len(r) > 2 and r[2]:
            try:
                first_seen = parse_pg_ts(r[2])
            except ValueError:
                first_seen = None
        head, _, rest = key.partition(":")
        extra = {}
        if head == "unit-failed" and rest:
            err = unit_error(rest)
            if err:
                extra["error"] = err
        elif head == "inverter-silent" and ":" in rest:
            lab = labels.get(rest.split(":", 1)[1])
            if lab:
                extra["label"] = lab
        alerts.append((key, sev, first_seen, extra or None))
    alerts.extend(ledger_issues(labels))
    try:
        maint = sorted({r[0] for r in psql_rows(
            "SELECT DISTINCT plant_key FROM maintenance_event"
            " WHERE start_ts <= now() AND (end_ts IS NULL"
            " OR end_ts >= now());") if r and r[0]})
    except RuntimeError:
        maint = []
    return alerts, maint


def ledger_issues(labels=None):
    """v203: the engine's OPEN alerts (alert_ledger — temperature, vendor
    faults, peer lag, silent inverter, string flags, plant offline) as
    issues, next to the mailer's infrastructure/freshness ones. Until now
    this mail only knew alert_state, so SLP2 read "OK" on 2026-09-04 with a
    CRITICAL vendor fault open in the ledger. Digest rows are a mail
    vehicle, not an issue."""
    from argia.store.pgq import psql_rows
    out = []
    try:
        rows = psql_rows(
            "SELECT metric, plant_key, coalesce(inverter_sn,''), severity,"
            " opened_utc, message FROM alert_ledger WHERE state = 'OPEN'"
            " AND metric <> 'daily_digest' ORDER BY plant_key, metric;")
    except RuntimeError:
        return out
    for r in rows:
        if len(r) < 6 or not r[0] or not r[1]:
            continue
        metric, plant, sn, sev, opened, msg = r[0], r[1], r[2], r[3] or "WARNING", r[4], r[5]
        try:
            first_seen = parse_pg_ts(opened.replace("T", " ")) if opened else None
        except (ValueError, AttributeError):
            first_seen = None
        extra = {"message": msg or ""}
        if sn and (labels or {}).get(sn):
            extra["label"] = labels[sn]
        out.append((f"{metric}:{plant}:{sn}" if sn else f"{metric}:{plant}", sev,
                    first_seen, extra))
    return out


# ------------------------------------------------------------------ pure

_ISSUE_PHRASE = {
    "inverter_temp_high": "inverter running hot",
    "inverter_fault": "inverter reports a fault code",
    "inverter_relative": "inverter below its peers",
    "inverter_silent": "inverter silent while the plant produces",
    "string_fault": "new string diagnostic flag",
    "energy_daily_pct": "production far below expected",
    "plant_offline": "plant produced nothing",
    "plant_twin_yield": "below its twin plant",
    "data_stale": "telemetry gap",
    "plant-dark": "no telemetry today",
    "plant-stale": "telemetry stale",
    "inverter-silent": "inverter silent",
    "recon-fail": "reconciliation FAIL",
    "satellite-drift": "irradiance sensor drift suspected",
    "unit-failed": "scheduled job failed",
    "cfe-probe": "CFE tariff probe warning",
    "cfe-coverage": "CFE tariff coverage gap",
}

# What the alert MEANS and what to do about it. "[CRITICAL] server:
# unit-failed" told Tomasz nothing on 2026-09-03 (v185) — an alert has
# to name the thing that broke and say what it implies.
_ISSUE_WHY = {
    "inverter_temp_high": "Internal temperature above 65 degC (critical from "
                          "75): the unit derates to protect itself and heat "
                          "shortens its life. Check fans, filters, heatsink, "
                          "shade on the enclosure.",
    "inverter_fault": "The inverter's own fault code — its diagnosis, not "
                      "an inference. Grid-side codes (Growatt 300-304) mean "
                      "the utility or a breaker, not the PV array.",
    "inverter_relative": "Much less energy per kW than its siblings under the "
                         "same sun: the problem is this unit (strings, "
                         "breaker, restarts), not the weather.",
    "inverter_silent": "Stopped sending while the others produce. When it "
                       "reappears its own counter tells comms gap (no energy "
                       "lost) from a unit that was off.",
    "string_fault": "A string diagnostic flag the unit never showed before — "
                    "a broken string, blown fuse or new mismatch on the DC side.",
    "energy_daily_pct": "The plant produced far less than the weather "
                        "allowed: outage, curtailment or a wrong expected.",
    "plant_offline": "Telemetry arrived but every inverter stayed at zero "
                     "all day.",
    "plant_twin_yield": "Its twin site under the same sky did much better.",
    "data_stale": "The engine saw a long gap in this plant's telemetry.",
    "plant-dark": "Nothing has arrived from this plant since midnight. "
                  "The inverters may still be producing — it is the data "
                  "path (datalogger, site internet, vendor portal) that "
                  "is down. Today's kWh for this plant cannot be trusted.",
    "plant-stale": "The newest reading is older than %d minutes during "
                   "daylight. Usually a datalogger or vendor-API hiccup "
                   "that clears itself; if it persists past sunset, treat "
                   "it as a dark plant." % STALE_MIN,
    "inverter-silent": "The plant is reporting but this inverter is not. "
                       "A single dead inverter is invisible in the plant "
                       "total — check the unit and its comms on site.",
    "recon-fail": "Metered energy and vendor energy disagree beyond "
                  "tolerance for that day. Settle the meter reading "
                  "before this day is invoiced.",
    "satellite-drift": "The on-site irradiance sensor and the satellite "
                       "estimate have disagreed for several days. Clean "
                       "or recalibrate the pyranometer — performance "
                       "ratios are computed from it.",
    "unit-failed": "A scheduled server job exited with an error. Whatever "
                   "that job produces is missing or stale until it runs "
                   "clean again.",
    "cfe-probe": "The CFE tariff scraper reported a problem. New tariffs "
                 "may be missing for the current month.",
    "cfe-coverage": "The CFE tariff database is missing tariffs or "
                    "divisions it expects to have by now.",
}

# systemd unit -> what it actually does, in words an operator can use.
_UNIT_ROLE = {
    "argia-telemetry": "Telemetry collector (Growatt + Huawei)",
    "argia-telemetry-se": "Telemetry collector (SolarEdge)",
    "argia-kpi": "Nightly KPI close",
    "argia-kpimirror": "KPI mirror to Postgres",
    "argia-recon": "Meter reconciliation",
    "argia-report-am": "Morning report build",
    "argia-report-pm": "Evening report build",
    "argia-alerts-snap": "Alert snapshot",
    "argia-alerts-daily": "Daily alert digest",
    "argia-mailer": "Alert mailer",
    "argia-dashboard": "Dashboard build",
    "argia-dash-update": "Dashboard refresh",
    "argia-client-pages": "Client report pages",
    "argia-finreport": "Financial report build",
    "argia-cfe-ingest": "CFE tariff ingest",
    "argia-cfe-push": "CFE tariff push to the Engine app",
    "argia-monitoring-gen": "Portal page generator",
    "argia-archive": "Telemetry archive to Drive",
    "argia-strings": "String-level collector",
    "argia-satcheck": "Satellite irradiance check",
    "argia-dbdump": "Database backup",
    "argia-sync": "Sheet sync",
}


def friendly_unit(unit: str) -> str:
    """'argia-telemetry-se.service' -> 'Telemetry collector (SolarEdge)'.

    Pure. Unknown units keep their own name rather than vanishing —
    a name we do not recognise is still more use than 'server'.
    """
    base = (unit or "").strip()
    for suffix in (".service", ".timer"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return _UNIT_ROLE.get(base) or base or "server"


def humanize_since(first_seen, now=None) -> str:
    """How long an alert has been open, e.g. '3 days' / '2 h' / '25 min'.

    Pure. ``first_seen`` may be a datetime or None; None gives "".
    """
    if first_seen is None:
        return ""
    now = now or dt.datetime.now(first_seen.tzinfo)
    secs = (now - first_seen).total_seconds()
    if secs < 0:
        return ""
    if secs < 3600:
        return "%d min" % max(1, int(secs // 60))
    if secs < 86400:
        return "%d h" % int(secs // 3600)
    days = int(secs // 86400)
    return "1 day" if days == 1 else "%d days" % days


def last_error_line(text: str, limit: int = 180) -> str:
    """The newest ERROR line of a job log, compacted to one readable line.

    Pure. Strips the timestamp/logger prefix and truncates, so the mail
    can carry the real reason ("sheet write failed: ... above the limit
    of 10000000 cells") instead of a bare "unit-failed".
    """
    for line in reversed((text or "").splitlines()):
        if " ERROR " not in line and not line.startswith("ERROR"):
            continue
        msg = line.split(" ERROR ", 1)[-1].strip()
        msg = msg.split(": ", 1)[-1].strip() if msg.startswith("argia.")             else msg
        msg = " ".join(msg.split())
        return msg[: limit - 1] + "\u2026" if len(msg) > limit else msg
    return ""


def issue_who(key: str, labels=None) -> str:
    """Who the alert is about: the customer name (and code) for a plant,
    the job's role for a server unit. Pure."""
    head, _, rest = key.partition(":")
    plant = subscriptions.alert_plant(key)
    if plant:
        name = (labels or {}).get(plant)
        return f"{name} ({plant})" if name else plant
    if head == "unit-failed" and rest:
        return friendly_unit(rest)
    return "Server"


def issue_detail(key: str, extra=None) -> str:
    """The troubleshooting handle: inverter SN, unit name, date. Pure."""
    head, _, rest = key.partition(":")
    bits = []
    if (extra or {}).get("message"):
        # ledger issue: the engine's own sentence (value, threshold, what
        # the counter said) is the detail
        sn = rest.split(":", 1)[1] if ":" in rest else ""
        lab = (extra or {}).get("label")
        if sn:
            bits.append(f"{lab} \u00b7 SN {sn}" if lab else f"SN {sn}")
        bits.append(extra["message"])
        return " \u00b7 ".join(b for b in bits if b)
    if head == "inverter-silent" and ":" in rest:
        sn = rest.split(":", 1)[1]
        lab = (extra or {}).get("label")
        bits.append(f"{lab} \u00b7 SN {sn}" if lab else f"SN {sn}")
    elif head == "recon-fail" and ":" in rest:
        bits.append("for %s" % rest.split(":", 1)[1])
    elif head == "unit-failed" and rest:
        bits.append(rest)
    err = (extra or {}).get("error")
    if err:
        bits.append("last error: %s" % err)
    return " \u00b7 ".join(b for b in bits if b)


def issue_record(key: str, severity: str, first_seen=None, extra=None,
                 labels=None, now=None) -> dict:
    """One open issue, in the shape both templates render. Pure."""
    head = key.partition(":")[0]
    return {
        "key": key,
        "sev": severity,
        "who": issue_who(key, labels),
        "what": _ISSUE_PHRASE.get(head, head),
        "detail": issue_detail(key, extra),
        "why": _ISSUE_WHY.get(head, ""),
        "since": humanize_since(first_seen, now),
    }


def describe_issue(key: str, labels=None) -> str:
    """One-line summary: 'GTO1: telemetry stale'. Pure."""
    head, _, rest = key.partition(":")
    r = issue_record(key, "", labels=labels)
    plant = subscriptions.alert_plant(key)
    who = r["who"] if (plant or head == "unit-failed") else "server"
    if plant and head == "inverter-silent" and ":" in rest:
        return f"{who}: {r['what']} ({rest.split(':', 1)[1]})"
    return f"{who}: {r['what']}"


def summarize(plants, today_map, inv_counts, yday_map, mtd_map,
              alerts, maint, today: dt.date, now_hm: str, now=None) -> dict:
    """Assemble everything the templates need. Pure, unit-tested."""
    ppa_keys = {k for k, _, _ in plants}
    rows = []
    for key, customer, kwp in plants:
        kwh, age, seen = today_map.get(key, (0.0, None, 0))
        inv_total = inv_counts.get(key, 0)
        mtd_kwh, paired, exp = mtd_map.get(key, (0.0, 0.0, 0.0))
        vs_exp = 100.0 * paired / exp if exp > 0 else None
        plant_alerts = [(a[0], a[1]) for a in alerts
                        if subscriptions.alert_plant(a[0]) == key]
        if key in maint:
            status, cls = "maintenance", "warn"
        elif kwh <= 0.0:
            status, cls = "dark", "bad"
        elif age is not None and age > STALE_MIN:
            status, cls = f"stale {age:.0f} min", "warn"
        elif any(s == "CRITICAL" for _, s in plant_alerts):
            status, cls = "issues", "bad"
        elif plant_alerts:
            status, cls = "check", "warn"
        else:
            status, cls = "OK", "ok"
        rows.append({
            "key": key, "customer": customer, "kwp": kwp,
            "label": display_name(customer),
            "desc": f"{key} · {customer} · {kwp:,.0f} kWp",
            "kwh_today": kwh,
            "yield": kwh / kwp if kwp > 0 else 0.0,
            "inv": f"{seen}/{inv_total}" if inv_total else str(seen),
            "kwh_yday": yday_map.get(key),
            "mtd_kwh": mtd_kwh, "vs_exp": vs_exp,
            "status": status, "cls": cls,
        })
    tot_today = sum(r["kwh_today"] for r in rows)
    tot_yday = sum(v for v in (r["kwh_yday"] for r in rows)
                   if v is not None)
    tot_mtd = sum(r["mtd_kwh"] for r in rows)
    tot_paired = sum(mtd_map.get(r["key"], (0, 0, 0))[1] for r in rows)
    tot_exp = sum(mtd_map.get(r["key"], (0, 0, 0))[2] for r in rows)
    labels = {r["key"]: r["label"] for r in rows}
    issues = [issue_record(a[0], a[1],
                           first_seen=a[2] if len(a) > 2 else None,
                           extra=a[3] if len(a) > 3 else None,
                           labels=labels, now=now)
              for a in alerts
              if subscriptions.alert_plant(a[0]) in ppa_keys
              or subscriptions.alert_plant(a[0]) is None]
    tot_kwp = sum(r["kwp"] for r in rows)
    inv_seen = sum(today_map.get(r["key"], (0, None, 0))[2] for r in rows)
    inv_all = sum(inv_counts.get(r["key"], 0) for r in rows)
    return {
        "date": today.isoformat(), "time": now_hm, "rows": rows,
        "tot_today": tot_today, "tot_yday": tot_yday,
        "tot_mtd": tot_mtd,
        "tot_vs_exp": (100.0 * tot_paired / tot_exp
                       if tot_exp > 0 else None),
        "tot_kwp": tot_kwp,
        "tot_yield": tot_today / tot_kwp if tot_kwp > 0 else 0.0,
        "tot_inv": f"{inv_seen}/{inv_all}" if inv_all else str(inv_seen),
        "issues": issues, "maint": list(maint),
        "n_bad": sum(1 for r in rows if r["cls"] == "bad"),
        "n_warn": sum(1 for r in rows if r["cls"] == "warn"),
    }


def mail_subject(data: dict) -> str:
    n = data["n_bad"] + data["n_warn"]
    flag = f" — {n} plant(s) need attention" if n else " — all OK"
    return ("[ARGIA] Daily PPA performance %s: %s kWh%s"
            % (data["date"], f"{data['tot_today']:,.0f}", flag))


def _pct(v) -> str:
    return f"{v:,.1f}%" if v is not None else "—"


def _num(v, dec=0) -> str:
    return f"{v:,.{dec}f}" if v is not None else "—"


def render_text(data: dict) -> str:
    """Plain-text alternative — grep-able archive copy. Pure."""
    L = [f"ARGIA — Daily PPA performance, {data['date']} "
         f"(as of {data['time']} MX)", "",
         f"Fleet today:   {_num(data['tot_today'])} kWh (live, preliminary)",
         f"Yesterday:     {_num(data['tot_yday'])} kWh (closed)",
         f"Month to date: {_num(data['tot_mtd'])} kWh"
         f" — {_pct(data['tot_vs_exp'])} of weather expectation", ""]
    for r in data["rows"]:
        L.append("  %-20s %8s kWh  %5.2f kWh/kWp  inv %s  [%s]"
                 % (r["label"][:20], _num(r["kwh_today"]),
                    r["yield"], r["inv"], r["status"]))
        L.append("    %s" % r["desc"])
    L.append("  %-20s %8s kWh  %5.2f kWh/kWp  inv %s"
             % ("TOTAL", _num(data["tot_today"]), data["tot_yield"],
                data["tot_inv"]))
    L.append("    %d plants · %s kWp · MTD %s kWh · vs exp %s"
             % (len(data["rows"]), _num(data["tot_kwp"]),
                _num(data["tot_mtd"]), _pct(data["tot_vs_exp"])))
    L.append("")
    if data["issues"]:
        L.append("Open issues:")
        for i in data["issues"]:
            age = f" · open {i['since']}" if i["since"] else ""
            L.append(f"  • [{i['sev']}] {i['who']} — {i['what']}{age}")
            if i["detail"]:
                L.append(f"      {i['detail']}")
            if i["why"]:
                L.append(f"      {i['why']}")
    else:
        L.append("Open issues: none")
    L += ["",
          "Today's figures are live telemetry, not yet reconciled — the"
          " nightly close finalizes them. Weather-expected for today"
          " lands with the close as well.",
          "", "Portal: https://monitoring.argia.com.mx",
          "Manage subscriptions: https://report.argia.com.mx/setup/"]
    return "\n".join(L)


_PILL = {"ok": ("#e7f4e8", "#1d7a2c", "OK"),
         "warn": ("#fdf3d7", "#8a6d1a", None),
         "bad": ("#fdeaea", "#b3261e", None)}


def render_html(data: dict) -> str:
    """Modern, email-safe HTML: inline CSS, tables, dark text on light
    ground (house readability rule), no external assets. Pure."""
    e = _html.escape
    tile = ('<td style="background:#ffffff;border:1px solid #e3e8ee;'
            'border-radius:10px;padding:14px 16px;text-align:center">'
            '<div style="font-size:11px;letter-spacing:.06em;'
            'text-transform:uppercase;color:#5f6b7a">%s</div>'
            '<div style="font-size:22px;font-weight:700;color:#16324f;'
            'padding-top:4px">%s</div>'
            '<div style="font-size:11px;color:#8a94a1">%s</div></td>')
    tiles = "".join([
        tile % ("Today (live)", e(_num(data["tot_today"])) + " kWh",
                "preliminary"),
        '<td style="width:10px"></td>',
        tile % ("Yesterday", e(_num(data["tot_yday"])) + " kWh",
                "closed"),
        '<td style="width:10px"></td>',
        tile % ("Month to date", e(_num(data["tot_mtd"])) + " kWh",
                "through yesterday"),
        '<td style="width:10px"></td>',
        tile % ("MTD vs expected", e(_pct(data["tot_vs_exp"])),
                "weather-based"),
    ])
    trs = []
    for r in data["rows"]:
        bg, fg, label = _PILL[r["cls"]]
        pill = ('<span style="background:%s;color:%s;border-radius:99px;'
                'padding:2px 10px;font-size:11px;font-weight:600">%s'
                '</span>' % (bg, fg, e(label or r["status"])))
        trs.append(
            '<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4">'
            f'<b style="color:#16324f;font-size:13.5px">{e(r["label"])}</b>'
            f'<br><span style="color:#8a94a1;font-size:11px">'
            f'{e(r["desc"])}</span></td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:center">{pill}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["kwh_today"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{r["yield"]:.2f}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:center">{e(r["inv"])}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["kwh_yday"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_num(r["mtd_kwh"]))}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef1f4;'
            f'text-align:right">{e(_pct(r["vs_exp"]))}</td></tr>')
    # summary line at the table bottom — always, even with the tiles
    # above (Tomasz, v180)
    tb = 'padding:8px 10px;border-top:2px solid #16324f;font-weight:700'
    trs.append(
        '<tr>'
        f'<td style="{tb}">TOTAL'
        f'<br><span style="color:#8a94a1;font-size:11px;font-weight:400">'
        f'{len(data["rows"])} plants · {e(_num(data["tot_kwp"]))} kWp'
        '</span></td>'
        f'<td style="{tb}"></td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_today"]))}</td>'
        f'<td style="{tb};text-align:right">{data["tot_yield"]:.2f}</td>'
        f'<td style="{tb};text-align:center">{e(data["tot_inv"])}</td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_yday"]))}</td>'
        f'<td style="{tb};text-align:right">{e(_num(data["tot_mtd"]))}</td>'
        f'<td style="{tb};text-align:right">{e(_pct(data["tot_vs_exp"]))}'
        '</td></tr>')
    if data["issues"]:
        blocks = []
        for i in data["issues"]:
            crit = i["sev"] == "CRITICAL"
            col = "#b3261e" if crit else "#8a6d1a"
            bg = "#fdf4f3" if crit else "#fffdf4"
            age = ('<span style="float:right;color:#8a94a1;font-size:11px;'
                   'font-weight:400">open %s</span>' % e(i["since"])
                   if i["since"] else "")
            detail = ('<div style="margin-top:3px;font-size:12px;'
                      'color:#3b4658;font-family:Consolas,Menlo,monospace">'
                      '%s</div>' % e(i["detail"])) if i["detail"] else ""
            why = ('<div style="margin-top:4px;font-size:12px;'
                   'color:#6b7686;line-height:1.45">%s</div>' % e(i["why"])
                   ) if i["why"] else ""
            blocks.append(
                '<div style="margin:8px 0 0;padding:9px 12px;background:%s;'
                'border:1px solid #e7dfd8;border-left:4px solid %s;'
                'border-radius:6px">'
                '<div style="font-size:13px;color:#243041">%s'
                '<b style="color:%s;font-size:11px;letter-spacing:.04em">'
                '%s</b> &nbsp;<b style="color:#16324f">%s</b>'
                ' &mdash; %s</div>%s%s</div>'
                % (bg, col, age, col, e(i["sev"]), e(i["who"]),
                   e(i["what"]), detail, why))
        issues_html = "".join(blocks)
    else:
        issues_html = ('<p style="margin:6px 0 0;color:#1d7a2c">'
                       'No open issues.</p>')
    return f'''<!doctype html><html><body style="margin:0;padding:0;
background:#f2f5f8;font-family:'Segoe UI',Roboto,Arial,sans-serif;
color:#243041">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:22px 10px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
 style="max-width:640px;width:100%">
<tr><td style="background:#ffffff;border:1px solid #e3e8ee;
border-bottom:3px solid #16324f;border-radius:12px 12px 0 0;
padding:16px 22px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="font-size:17px;font-weight:600;letter-spacing:2.5px;
color:#16324f;white-space:nowrap;vertical-align:middle">DAILY&nbsp;PPA
&nbsp;PERFORMANCE</td>
<td align="right" style="vertical-align:middle">
<img src="cid:argialogo" alt="ARGIA SOLAR" height="19"
 style="height:19px;display:block"></td>
</tr>
<tr>
<td style="padding-top:5px;color:#8a94a1;font-size:12.5px">
Live telemetry &#183; not yet reconciled</td>
<td align="right" style="padding-top:5px;font-size:14px;font-weight:600;
color:#16324f;white-space:nowrap">{data["date"]} &#183;
{data["time"]} MX</td>
</tr></table>
</td></tr>
<tr><td style="background:#fbfcfe;border:1px solid #e3e8ee;
border-top:none;padding:18px 22px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>{tiles}</tr></table>
<h3 style="margin:20px 0 6px;font-size:13px;letter-spacing:.05em;
text-transform:uppercase;color:#5f6b7a">Plants</h3>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="font-size:13px;border-collapse:collapse">
<tr style="color:#5f6b7a;font-size:11px;text-transform:uppercase">
<td style="padding:4px 10px">Plant</td>
<td style="padding:4px 10px;text-align:center">Status</td>
<td style="padding:4px 10px;text-align:right">Today kWh</td>
<td style="padding:4px 10px;text-align:right">kWh/kWp</td>
<td style="padding:4px 10px;text-align:center">Inv</td>
<td style="padding:4px 10px;text-align:right">Yday kWh</td>
<td style="padding:4px 10px;text-align:right">MTD kWh</td>
<td style="padding:4px 10px;text-align:right">vs exp</td></tr>
{"".join(trs)}</table>
<h3 style="margin:20px 0 0;font-size:13px;letter-spacing:.05em;
text-transform:uppercase;color:#5f6b7a">Open issues</h3>
{issues_html}
<p style="margin:18px 0 0;padding:10px 12px;background:#fffdf4;
border:1px solid #e7dfc2;border-radius:8px;font-size:12px;
color:#6b5d2a">Today's figures are <b>live telemetry, not yet
reconciled</b> — the nightly close finalizes them and adds today's
weather expectation. This mail goes out at 19:00 so there is still
time to act on anything red.</p>
<p style="margin:14px 0 0;font-size:12px;color:#8a94a1">
<a href="https://monitoring.argia.com.mx" style="color:#2b6cb0">
Open the portal</a> ·
<a href="https://report.argia.com.mx/setup/" style="color:#2b6cb0">
Manage subscriptions</a></p>
</td></tr></table></td></tr></table></body></html>'''


# ------------------------------------------------------------------ main

def recipients():
    return [e for e, _ in subscriptions.only_portal(
        subscriptions.recipients_for("daily"),
        subscriptions.portal_emails())]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="daily PPA performance mail")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    from argia.store.pgq import psql_exec
    psql_exec(subscriptions.ENSURE_SQL)

    now_mx = dt.datetime.now(MX_TZ)
    today = now_mx.date()
    alerts, maint = gather_issues()
    data = summarize(gather_plants(), gather_today(),
                     gather_inverter_counts(), gather_yesterday(today),
                     gather_mtd(today), alerts, maint, today,
                     now_mx.strftime("%H:%M"))
    if not data["rows"]:
        LOG.error("no active PPA plants found — nothing to report")
        return 1
    text = render_text(data)
    LOG.info("summary: today=%.0f kWh, %d plant(s), %d issue(s)",
             data["tot_today"], len(data["rows"]), len(data["issues"]))

    if args.dry_run:
        print(text)
        out = "/tmp/argia_daily_perf_preview.html"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render_html(data))
        LOG.info("dry-run: HTML preview at %s — nothing sent", out)
        return 0

    rcpt = recipients()
    if not rcpt:
        LOG.warning("no enabled 'daily' subscribers — nothing sent")
        return 0
    cfg = emailer.load_smtp()
    if not cfg:
        LOG.error("no SMTP config (/root/.argia_mail) — not sending")
        return 1
    logo = logo_png()
    msg = emailer.build_html_email(
        mail_subject(data), text, render_html(data), cfg["SMTP_USER"],
        rcpt, images={"argialogo": (logo, "png")} if logo else None)
    if not emailer.send(msg, cfg):
        LOG.error("send FAILED")
        return 1
    LOG.info("sent to %d recipient(s)", len(rcpt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
