"""The monthly O&M report of the Prologis platform, the design yield
table and the HSE register (v322).

MSA Schedule A, Data & Reporting: "Provide monthly report for (a) each
Project and (b) the entire portfolio ... within ten (10) days of the end of
each month", with tables and graphs of production actual, expected
(weather adjusted) and estimated (Helio/PVsyst), insolation; every outage
longer than three days with its duration, reason, estimated return to
service and corrective action; the log of warnings, alarms, anomalies and
O&M activity; and the Response Time of each work order. ARGIA's proposal
(4.4) adds availability, open corrective items, HSE events and delivery
"also as spreadsheet". Exhibit F: a written HSE incident report to
Prologis within 24 hours.

A report is a draft (computed from the data each time it is opened, seen
by ARGIA only) until an operator publishes it: publishing freezes the
numbers as JSON, so a published report never changes afterwards, and
records when it was published against the 10-day deadline.

The design (estimated) yield per site and month comes from the PVsyst /
Helioscope studies, loaded by CSV; a site without it shows "not provided"
- never a made-up number.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from argia.prologis import metering as M
from argia.prologis import sla as SLA

SCHEMA = """
CREATE TABLE IF NOT EXISTS design_yield (
  site_code TEXT NOT NULL, month INTEGER NOT NULL, kwh REAL NOT NULL, source TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '', PRIMARY KEY (site_code, month));
CREATE TABLE IF NOT EXISTS hse_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_utc TEXT NOT NULL, site_code TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, description TEXT NOT NULL, actions TEXT NOT NULL DEFAULT '', reported_utc TEXT NOT NULL DEFAULT '',
  mojo_ref TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monthly_reports (
  month TEXT PRIMARY KEY, published_utc TEXT NOT NULL, published_by TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL);
"""
HSE_KINDS = [("incident", "Incident (injury or damage)", "Incidente (lesión o daño)"),
             ("first_aid", "First aid case", "Caso de primeros auxilios"),
             ("near_miss", "Near miss", "Casi accidente"),
             ("stop_work", "Stop work", "Paro de trabajo"),
             ("observation", "Safety observation", "Observación de seguridad")]
HSE_REPORTABLE = {"incident", "first_aid", "near_miss"}      # Exhibit F: written report within 24 h
OUTAGE_DAYS = 3.0
DEADLINE_DAY = 10
OUTAGE_CLASSES = {"EMERGENCY", "OUT_500", "OUT_100_500", "STRING_100", "STRING_25", "MICRO"}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(tickets)")}
    if have and "eta_date" not in have:
        c.execute("ALTER TABLE tickets ADD COLUMN eta_date TEXT NOT NULL DEFAULT ''")
    c.commit()


def _audit(c, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    from argia.prologis import store as S
    S.audit(c, user, ip, action, target, detail)


# ------------------------------------------------------------------ months and deadlines (pure)
def month_bounds(month: str) -> Tuple[dt.date, dt.date]:
    m = re.match(r"^(\d{4})-(\d{2})$", month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError("month must look like 2026-10")
    y, mo = int(m.group(1)), int(m.group(2))
    start = dt.date(y, mo, 1)
    end = (dt.date(y + 1, 1, 1) if mo == 12 else dt.date(y, mo + 1, 1)) - dt.timedelta(days=1)
    return start, end


def deadline(month: str) -> dt.date:
    """The 10th day after the month ends (MSA: within ten days)."""
    return month_bounds(month)[1] + dt.timedelta(days=DEADLINE_DAY)


def prev_month(d: dt.date) -> str:
    first = d.replace(day=1) - dt.timedelta(days=1)
    return f"{first.year}-{first.month:02d}"


def deadline_state(month: str, published_utc: str, today: dt.date) -> str:
    """'met' | 'late' (published after the deadline) | 'due' | 'overdue' | 'open' (month not over)."""
    start, end = month_bounds(month)
    dl = deadline(month)
    if published_utc:
        pub = (dt.datetime.fromisoformat(published_utc) + M.MX_OFFSET).date()
        return "met" if pub <= dl else "late"
    if today <= end:
        return "open"
    return "due" if today <= dl else "overdue"


# ------------------------------------------------------------------ response clock (shared with the app)
def ticket_clock(c, tk, now: dt.datetime) -> Tuple[SLA.SlaClass, Optional[float], str]:
    """(MSA class, hours on the clock, status) of one incident ticket."""
    from argia.prologis import store as S
    cls = SLA.BY_CODE.get(tk["sla_class"], SLA.BY_CODE["OTHER"])
    det = dt.datetime.fromisoformat(tk["detected_utc"])
    appr = dt.datetime.fromisoformat(tk["approved_utc"]) if tk["approved_utc"] else None
    start = SLA.clock_start(cls, det, appr if tk["approval"] in ("approved",) else (det if tk["approval"] == "not_required" else None))
    if start is None:
        return cls, None, "not_started"
    end = dt.datetime.fromisoformat(tk["responded_utc"]) if tk["responded_utc"] else now
    spans = [(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b) if b else None)
             for a, b in S.waiting_spans(S.ticket_events(c, tk["id"]))]
    used = SLA.elapsed_hours(start, end, spans)
    return cls, used, SLA.status(cls, used, bool(tk["responded_utc"]))


# ------------------------------------------------------------------ design yield
def parse_design(text: str, site_codes: Iterable[str]) -> Tuple[List[Tuple[str, int, float]], List[str]]:
    """CSV: site, m1..m12 (kWh per month, from PVsyst / Helioscope). All or
    nothing; empty cells are allowed (not provided). Pure."""
    codes = set(site_codes)
    rd = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    head = [h.strip().lower() for h in (rd.fieldnames or [])]
    want = ["site"] + [f"m{i}" for i in range(1, 13)]
    errs: List[str] = []
    if head != want:
        return [], ["header must be: " + ",".join(want)]
    rows: List[Tuple[str, int, float]] = []
    seen = set()
    for n, raw in enumerate(rd, start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        site = r["site"].upper()
        if not site:
            continue
        if site not in codes:
            errs.append(f"line {n}: unknown site {site}")
            continue
        if site in seen:
            errs.append(f"line {n}: site {site} twice")
        seen.add(site)
        for i in range(1, 13):
            v = r.get(f"m{i}", "").replace(",", "")
            if not v:
                continue
            try:
                f = float(v)
            except ValueError:
                errs.append(f"line {n}: m{i} is not a number")
                continue
            if not 0 < f < 10_000_000:
                errs.append(f"line {n}: m{i} out of range")
                continue
            rows.append((site, i, f))
    if not rows and not errs:
        errs.append("no values")
    return rows, errs


def save_design(c, rows: Sequence[Tuple[str, int, float]], source: str, by: str, ip: str = "") -> int:
    with c:
        for site, mo, kwh in rows:
            c.execute("INSERT OR REPLACE INTO design_yield (site_code, month, kwh, source, updated_by, updated_utc) VALUES (?,?,?,?,?,?)",
                      (site, mo, kwh, source[:120], by, now_utc()))
    _audit(c, by, ip, "design_yield_import", f"{len({r[0] for r in rows})} sites", f"{len(rows)} values {source[:100]}")
    return len(rows)


def design_map(c) -> Dict[Tuple[str, int], float]:
    return {(r["site_code"], r["month"]): r["kwh"] for r in c.execute("SELECT * FROM design_yield")}


# ------------------------------------------------------------------ HSE
def add_hse(c, occurred_local: str, site_code: str, kind: str, description: str, actions: str, by: str,
            reported_local: str = "", mojo_ref: str = "", ip: str = "", site_codes: Iterable[str] = (),
            now_local: Optional[dt.datetime] = None) -> int:
    if kind not in [k for k, _, _ in HSE_KINDS]:
        raise ValueError("unknown kind")
    if site_code and site_code.upper() not in set(site_codes):
        raise ValueError("unknown site")
    if not description.strip():
        raise ValueError("describe what happened")
    try:
        occ = dt.datetime.fromisoformat(occurred_local.replace("T", " "))
        rep = dt.datetime.fromisoformat(reported_local.replace("T", " ")) if reported_local else None
    except ValueError:
        raise ValueError("dates as YYYY-MM-DD HH:MM")
    now_local = now_local or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + M.MX_OFFSET)
    if occ > now_local or (rep and rep > now_local):
        raise ValueError("a date is in the future")
    if rep and rep < occ:
        raise ValueError("reported before it happened")
    to_u = lambda x: (x - M.MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")      # noqa: E731
    r = c.execute("INSERT INTO hse_events (occurred_utc, site_code, kind, description, actions, reported_utc, mojo_ref, created_by, created_utc)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", (to_u(occ), site_code.upper(), kind, description.strip()[:5000], actions.strip()[:5000],
                                                  to_u(rep) if rep else "", mojo_ref.strip()[:60], by, now_utc()))
    c.commit()
    _audit(c, by, ip, "hse_add", f"hse#{r.lastrowid}", f"{kind} {site_code} reported={bool(rep)}")
    return int(r.lastrowid)


def hse_on_time(e, now_utc_dt: Optional[dt.datetime] = None) -> Optional[bool]:
    """True/False: reported to Prologis within 24 h (Exhibit F); None when
    the kind does not need it. Not reported yet and 24 h not over -> True."""
    if e["kind"] not in HSE_REPORTABLE:
        return None
    occ = dt.datetime.fromisoformat(e["occurred_utc"])
    if e["reported_utc"]:
        return dt.datetime.fromisoformat(e["reported_utc"]) - occ <= dt.timedelta(hours=24)
    now_utc_dt = now_utc_dt or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return now_utc_dt - occ <= dt.timedelta(hours=24)


# ------------------------------------------------------------------ the report
def _site_days(rg, s, start: dt.date, last: dt.date) -> List[M.DayResult]:
    op = rg.operating
    i = op.index(s)
    if last < start:
        return []
    return M.history(s, last, (last - start).days + 1, i, len(op))


def build(c, rg, month: str, today: dt.date, now_utc_dt: Optional[dt.datetime] = None) -> Dict:
    """Everything the monthly report shows, JSON-serialisable. Days after
    yesterday are not counted (a running month is a partial report)."""
    from argia.prologis import store as S
    start, end = month_bounds(month)
    last = min(end, today - dt.timedelta(days=1))
    now_utc_dt = now_utc_dt or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    s_u = (dt.datetime.combine(start, dt.time()) - M.MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    e_u = (dt.datetime.combine(end + dt.timedelta(days=1), dt.time()) - M.MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    design = design_map(c)
    sites = []
    daily_tot: Dict[str, List[float]] = {}
    for s in rg.operating:
        days = _site_days(rg, s, start, last)
        kwh = sum(d.kwh for d in days)
        exp = sum(d.expected_kwh for d in days)
        irr = sum(d.irr_kwh_m2 for d in days)
        comp = (sum(sum(1 for _, v in d.series if v is not None) / max(1, len(d.series)) for d in days) / len(days)) if days else 0.0
        des_m = design.get((s.code, start.month))
        des = (des_m * len(days) / ((end - start).days + 1)) if des_m is not None and days else None
        for d in days:
            t = daily_tot.setdefault(d.day.isoformat(), [0.0, 0.0])
            t[0] += d.kwh
            t[1] += d.expected_kwh
        sites.append({"code": s.code, "name": s.name, "kwp": s.kwp, "kwh": round(kwh, 1), "expected_kwh": round(exp, 1),
                      "design_kwh": None if des is None else round(des, 1), "irr_kwh_m2": round(irr, 2),
                      "pr": round(kwh / (s.kwp * irr), 3) if irr else None,
                      "availability": round(sum(d.availability for d in days) / len(days), 4) if days else None,
                      "completeness": round(comp, 3), "source": M.source_for(s), "days": len(days),
                      "daily": [[d.day.isoformat(), d.kwh, d.expected_kwh, d.irr_kwh_m2] for d in days]})
    kwp = sum(x["kwp"] for x in sites)
    tot = lambda k: round(sum(x[k] for x in sites), 1)                      # noqa: E731
    have_design = [x for x in sites if x["design_kwh"] is not None]
    irr_w = sum(x["irr_kwh_m2"] * x["kwp"] for x in sites)
    port = {"sites": len(sites), "kwp": round(kwp, 1), "kwh": tot("kwh"), "expected_kwh": tot("expected_kwh"),
            "design_kwh": round(sum(x["design_kwh"] for x in have_design), 1) if have_design else None,
            "design_sites": len(have_design),
            "irr_kwh_m2": round(irr_w / kwp, 2) if kwp else 0.0, "pr": round(tot("kwh") / irr_w, 3) if irr_w else None,
            "availability": round(sum((x["availability"] or 0) * x["kwp"] for x in sites) / kwp, 4) if kwp and any(x["days"] for x in sites) else None,
            "completeness": round(sum(x["completeness"] * x["kwp"] for x in sites) / kwp, 3) if kwp else 0.0,
            "days": (last - start).days + 1 if last >= start else 0, "days_in_month": (end - start).days + 1}
    # alarms and O&M log
    log = []
    for a in c.execute("SELECT * FROM alarms WHERE detected_utc>=? AND detected_utc<? ORDER BY detected_utc", (s_u, e_u)):
        log.append({"ts": a["detected_utc"], "site": a["site_code"], "type": "alarm", "severity": a["severity"],
                    "text": f"{a['kind']}: {a['detail']}", "triage": a["triage"], "cleared": a["cleared_utc"], "sample": a["sample"]})
    for e in c.execute("SELECT e.*, t.number, t.site_code, t.kind AS tkind FROM ticket_events e JOIN tickets t ON t.id=e.ticket_id "
                       "WHERE e.ts_utc>=? AND e.ts_utc<? ORDER BY e.ts_utc", (s_u, e_u)):
        m = json.loads(e["meta"] or "{}")
        what = {"created": "opened", "status": f"{m.get('from', '')} > {m.get('to', '')}", "comment": "comment",
                "approval": "approval " + ("granted" if m.get("approved") else "refused"), "file": "file attached",
                "quote": "quote"}.get(e["kind"], e["kind"])
        log.append({"ts": e["ts_utc"], "site": e["site_code"], "type": "order" if e["tkind"] == "order" else "ticket",
                    "severity": "", "text": f"{e['number']} {what}" + (f": {e['body'][:300]}" if e["body"] else ""), "triage": "",
                    "cleared": "", "sample": 0})
    log.sort(key=lambda x: x["ts"])
    # work orders with their response time
    wos = []
    for tk in c.execute("SELECT * FROM tickets WHERE kind='incident' AND detected_utc<? AND (resolved_utc='' OR resolved_utc>=?) "
                        "ORDER BY detected_utc", (e_u, s_u)):
        cls, used, st = ticket_clock(c, tk, now_utc_dt)
        wos.append({"number": tk["number"], "site": tk["site_code"], "title": tk["title"], "class": cls.code, "priority": cls.priority,
                    "deadline_h": cls.hours, "detected": tk["detected_utc"], "responded": tk["responded_utc"],
                    "resolved": tk["resolved_utc"], "hours": None if used is None else round(used, 1), "clock": st,
                    "status": tk["status"], "sample": tk["sample"]})
    # outages longer than 3 days
    outages = []
    for tk in c.execute("SELECT * FROM tickets WHERE kind='incident' AND detected_utc<? AND (resolved_utc='' OR resolved_utc>=?)",
                        (e_u, s_u)):
        if tk["sla_class"] not in OUTAGE_CLASSES and not (tk["kw_lost"] or 0) > 0:
            continue
        a = dt.datetime.fromisoformat(tk["detected_utc"])
        b = dt.datetime.fromisoformat(tk["resolved_utc"]) if tk["resolved_utc"] else min(now_utc_dt, dt.datetime.fromisoformat(e_u))
        days = (b - a).total_seconds() / 86400
        if days <= OUTAGE_DAYS:
            continue
        evs = [e for e in S.ticket_events(c, tk["id"]) if e["body"]]
        outages.append({"number": tk["number"], "site": tk["site_code"], "title": tk["title"], "reason": tk["description"][:500],
                        "detected": tk["detected_utc"], "resolved": tk["resolved_utc"], "days": round(days, 1),
                        "kw_lost": tk["kw_lost"], "eta": tk["eta_date"], "status": tk["status"],
                        "action": evs[-1]["body"][:500] if evs else "", "sample": tk["sample"]})
    hse = []
    for e in c.execute("SELECT * FROM hse_events WHERE occurred_utc>=? AND occurred_utc<? ORDER BY occurred_utc", (s_u, e_u)):
        hse.append({"id": e["id"], "occurred": e["occurred_utc"], "site": e["site_code"], "kind": e["kind"], "description": e["description"],
                    "actions": e["actions"], "reported": e["reported_utc"], "on_time": hse_on_time(e, now_utc_dt), "mojo": e["mojo_ref"]})
    open_orders = [dict(number=t["number"], title=t["title"], status=t["status"], site=t["site_code"])
                   for t in c.execute("SELECT * FROM tickets WHERE kind='order' AND status IN ('ORDERED','CONFIRMED','SCHEDULED','IN_PROGRESS')")]
    open_inc = [dict(number=t["number"], title=t["title"], status=t["status"], site=t["site_code"])
                for t in c.execute("SELECT * FROM tickets WHERE kind='incident' AND status IN ('NEW','RESPONDED','IN_PROGRESS','WAITING_OWNER')")]
    closed_cl = [w for w in wos if w["clock"] in ("met", "breached")]
    from argia.prologis import alarms as AL
    return {"month": month, "first_day": start.isoformat(), "last_day": last.isoformat(), "generated_utc": now_utc_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "sample" if any(x["source"] == "sample" for x in sites) else "measured",
            "guarantee": float(AL.setting(c, "availability_guarantee") or 0.98),
            "portfolio": port, "sites": sites, "daily": [[k, round(v[0], 1), round(v[1], 1)] for k, v in sorted(daily_tot.items())],
            "log": log, "work_orders": wos, "response_met": sum(1 for w in closed_cl if w["clock"] == "met"),
            "response_closed": len(closed_cl), "outages": outages, "hse": hse,
            "open_items": {"incidents": open_inc, "orders": open_orders}}


def published(c, month: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM monthly_reports WHERE month=?", (month,)).fetchone()


def publish(c, month: str, data: Dict, notes: str, by: str, today: dt.date, ip: str = "") -> None:
    """Freeze a finished month's report. A month cannot be published before
    it is over, nor twice."""
    start, end = month_bounds(month)
    if today <= end:
        raise ValueError("the month is not over yet")
    if published(c, month):
        raise ValueError("already published")
    if not notes.strip():
        raise ValueError("write the summary for Prologis")
    data = dict(data, notes=notes.strip()[:10000])
    c.execute("INSERT INTO monthly_reports (month, published_utc, published_by, notes, data) VALUES (?,?,?,?,?)",
              (month, now_utc(), by, notes.strip()[:10000], json.dumps(data)))
    c.commit()
    _audit(c, by, ip, "monthly_report_publish", month, f"deadline {deadline(month)}")


def report_data(c, rg, month: str, today: dt.date) -> Tuple[Dict, bool]:
    """(data, frozen): the published snapshot, or a fresh draft."""
    p = published(c, month)
    if p:
        return json.loads(p["data"]), True
    return build(c, rg, month, today), False


def workbook_sheets(d: Dict, names: Dict[str, str]) -> List[Tuple[str, List[List]]]:
    """The report as spreadsheet sheets (proposal 4.4: also as spreadsheet)."""
    p = d["portfolio"]
    port = [["metric", "value"], ["month", d["month"]], ["days counted", p["days"]], ["data", d["source"]],
            ["sites", p["sites"]], ["kWp", p["kwp"]], ["actual kWh", p["kwh"]], ["expected kWh (weather adjusted)", p["expected_kwh"]],
            ["design kWh (PVsyst / Helioscope)", "" if p["design_kwh"] is None else p["design_kwh"]],
            ["sites with a design value", p["design_sites"]], ["insolation kWh/m2", p["irr_kwh_m2"]], ["PR", p["pr"]],
            ["availability", p["availability"]], ["availability guarantee", d["guarantee"]], ["data completeness", p["completeness"]],
            ["work orders with response measured", d["response_closed"]], ["response within the MSA deadline", d["response_met"]]]
    sites = [["site", "name", "kWp", "actual kWh", "expected kWh", "design kWh", "actual / expected", "insolation kWh/m2", "PR",
              "availability", "data completeness", "source"]]
    for x in d["sites"]:
        sites.append([x["code"], names.get(x["code"], x["name"]), x["kwp"], x["kwh"], x["expected_kwh"], x["design_kwh"],
                      round(x["kwh"] / x["expected_kwh"], 3) if x["expected_kwh"] else None, x["irr_kwh_m2"], x["pr"], x["availability"],
                      x["completeness"], x["source"]])
    daily = [["site", "day", "actual kWh", "expected kWh", "insolation kWh/m2"]]
    for x in d["sites"]:
        for day, kwh, exp, irr in x["daily"]:
            daily.append([x["code"], day, kwh, exp, irr])
    outs = [["ticket", "site", "title", "reason", "detected (UTC)", "resolved (UTC)", "days", "kW lost", "estimated return", "status", "last action"]]
    outs += [[o["number"], o["site"], o["title"], o["reason"], o["detected"], o["resolved"], o["days"], o["kw_lost"], o["eta"], o["status"], o["action"]]
             for o in d["outages"]]
    log = [["time (UTC)", "site", "type", "severity", "text", "triage", "cleared (UTC)", "sample"]]
    log += [[x["ts"], x["site"], x["type"], x["severity"], x["text"], x["triage"], x["cleared"], "yes" if x["sample"] else ""] for x in d["log"]]
    wo = [["ticket", "site", "title", "MSA class", "deadline h", "detected (UTC)", "responded (UTC)", "resolved (UTC)", "response h", "clock", "status"]]
    wo += [[w["number"], w["site"], w["title"], w["class"], w["deadline_h"], w["detected"], w["responded"], w["resolved"], w["hours"], w["clock"], w["status"]]
           for w in d["work_orders"]]
    hse = [["occurred (UTC)", "site", "kind", "description", "actions", "reported to Prologis (UTC)", "within 24 h", "Safety Mojo"]]
    hse += [[h["occurred"], h["site"], h["kind"], h["description"], h["actions"], h["reported"],
             "" if h["on_time"] is None else ("yes" if h["on_time"] else "NO"), h["mojo"]] for h in d["hse"]]
    return [("Portfolio", port), ("Sites", sites), ("Daily", daily), ("Outages over 3 days", outs), ("Alarms and O&M log", log),
            ("Work orders", wo), ("HSE", hse)]
