"""Alarm engine, triage clock, e-mail notifications and the daily review
log of the Prologis platform (v321).

What the MSA (Schedule A, Monitoring) and ARGIA's proposal (4.1) ask for:

* "Maintain and manage the monitoring alarm notification schema" and
  "Setup email notifications ... to ensure notification of critical
  alarms are triggered during non-daylight hours": every run (every 5
  minutes) evaluates each operating site; a NEW critical alarm is mailed
  at once, at any hour; everything else goes into the 07:30 daily digest.
* DAS checks: communication loss, data logger (frozen readings), meter
  issue (meter and inverters disagree), weather sensor implausible.
* Production faults classed on the MSA response-time table (sla.py); the
  alarm's detection time is the ticket's detection time, so the response
  clock starts at the DAS alarm (MSA "Response Time").
* Proposal 4.1: "Alarm triage and classification within 4 business hours"
  - business hours Monday to Friday 09:00-18:00 Mexico City, the Ley
  Federal del Trabajo public holidays off (Tomasz, 10 Oct 2026).
* "On a daily basis, review and analyze system performance at the
  inverter level": one review entry per calendar day, missed days flagged
  (Tomasz chose the MSA wording over the proposal's business days).

Mail safety (Tomasz, 10 Oct 2026: dry-run until live): the mode setting
starts as ``dry_run`` - messages are written to the outbox table and
shown on the platform, never sent. Alarms computed from SAMPLE data are
never mailed, whatever the mode. Recipients: the ARGIA desk addresses
(setting) plus each platform user who opted in (``users.alarm_mail``:
'critical' or 'all').

The rules are pure functions of a ``Snapshot`` (tests drive them with
synthetic snapshots); ``sample_snapshot`` builds one from the SAMPLE
metering until the SolarEdge / Hark data is collected.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from argia.prologis import metering as M
from argia.prologis import sla as SLA

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  msa_class TEXT NOT NULL, kw_lost REAL, detail TEXT NOT NULL DEFAULT '', detected_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL, cleared_utc TEXT NOT NULL DEFAULT '', misses INTEGER NOT NULL DEFAULT 0,
  triage TEXT NOT NULL DEFAULT '', triaged_utc TEXT NOT NULL DEFAULT '', triaged_by TEXT NOT NULL DEFAULT '',
  triage_note TEXT NOT NULL DEFAULT '', ticket_id INTEGER, notified_utc TEXT NOT NULL DEFAULT '',
  sample INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS alarms_open ON alarms(site_code, kind) WHERE cleared_utc = '';
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
  to_addrs TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
  sent_utc TEXT NOT NULL DEFAULT '', sample INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS daily_review (
  day TEXT PRIMARY KEY, username TEXT NOT NULL, ts_utc TEXT NOT NULL, late INTEGER NOT NULL DEFAULT 0,
  sites_checked INTEGER NOT NULL DEFAULT 0, findings TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}');
"""
DEFAULTS = {"mail_mode": "dry_run", "desk_emails": "", "review_start": "", "last_digest_day": ""}
MAIL_MODES = ("dry_run", "live")
MAIL_PREFS = [("", "No alarm e-mails", "Sin correos de alarmas"),
              ("critical", "Critical alarms at once (any hour)", "Alarmas críticas al momento (a cualquier hora)"),
              ("all", "Critical at once + daily digest 07:30", "Críticas al momento + resumen diario 07:30")]

KINDS = [("production_loss", "Production loss", "Pérdida de producción"),
         ("comm_loss", "Communication loss", "Pérdida de comunicación"),
         ("meter_mismatch", "Meter issue: meter and inverters disagree", "Falla de medidor: medidor e inversores no coinciden"),
         ("sensor_implausible", "Weather sensor implausible", "Sensor meteorológico no plausible"),
         ("frozen_data", "Data logger: readings frozen", "Datalogger: lecturas congeladas"),
         ("underperformance", "Underperformance: day below 85% of expected", "Bajo desempeño: día bajo 85% de lo esperado")]
KIND_KEYS = [k for k, _, _ in KINDS]
CRITICAL_CLASSES = {"EMERGENCY", "OUT_500", "OUT_100_500", "COMM_1"}
TRIAGE_ACTIONS = [("ticket", "Confirmed: open a ticket", "Confirmada: abrir ticket"),
                  ("link", "Confirmed: belongs to an open ticket", "Confirmada: pertenece a un ticket abierto"),
                  ("dismiss", "Dismissed (false alarm, known, duplicate)", "Descartada (falsa, conocida, duplicada)")]
TRIAGE_HOURS = 4.0
CLEAR_AFTER_MISSES = 2              # two clean runs in a row (about 10 minutes)
SELF_CLEARED_MIN = 60               # cleared by itself within an hour before anyone triaged it
DIGEST_AT = dt.time(7, 30)
BIZ_START, BIZ_END = dt.time(9, 0), dt.time(18, 0)
REVIEW_GRACE_DAYS = 3


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    if have and "alarm_mail" not in have:
        c.execute("ALTER TABLE users ADD COLUMN alarm_mail TEXT NOT NULL DEFAULT ''")
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
    c.commit()


def _audit(c, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    from argia.prologis import store as S
    S.audit(c, user, ip, action, target, detail)


# ------------------------------------------------------------------ settings
def setting(c, key: str) -> str:
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else DEFAULTS.get(key, "")


_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_emails(text: str) -> List[str]:
    """Comma / semicolon / newline separated -> list; ValueError on a bad one."""
    out = []
    for x in re.split(r"[,;\s]+", text or ""):
        x = x.strip().lower()
        if not x:
            continue
        if not _EMAIL.match(x):
            raise ValueError(f"not an e-mail address: {x}")
        if x not in out:
            out.append(x)
    return out


def set_setting(c, key: str, value: str, by: str, ip: str = "") -> None:
    if key not in DEFAULTS:
        raise ValueError("unknown setting")
    if key == "mail_mode" and value not in MAIL_MODES:
        raise ValueError("mode must be dry_run or live")
    if key == "desk_emails":
        value = ", ".join(parse_emails(value))
    if key == "review_start" and value:
        dt.date.fromisoformat(value)
    old = setting(c, key)
    c.execute("INSERT OR REPLACE INTO settings (key, value, updated_by, updated_utc) VALUES (?,?,?,?)", (key, value, by, now_utc()))
    c.commit()
    if key != "last_digest_day":
        _audit(c, by, ip, "setting", key, f"{old!r} -> {value!r}")


def set_mail_pref(c, username: str, pref: str, ip: str = "") -> None:
    if pref not in [k for k, _, _ in MAIL_PREFS]:
        raise ValueError("unknown preference")
    c.execute("UPDATE users SET alarm_mail=? WHERE username=?", (pref, username))
    c.commit()
    _audit(c, username, ip, "alarm_mail_pref", username, pref or "none")


# ------------------------------------------------------------------ business hours (pure)
def _nth_monday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(0 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def mx_holidays(year: int) -> Set[dt.date]:
    """Mandatory rest days, Ley Federal del Trabajo art. 74 (election days
    aside): 1 Jan, first Monday of February, third Monday of March, 1 May,
    16 Sep, third Monday of November, 25 Dec, and 1 Oct of each
    presidential inauguration year (2024, 2030, ...)."""
    out = {dt.date(year, 1, 1), _nth_monday(year, 2, 1), _nth_monday(year, 3, 3), dt.date(year, 5, 1),
           dt.date(year, 9, 16), _nth_monday(year, 11, 3), dt.date(year, 12, 25)}
    if year >= 2024 and (year - 2024) % 6 == 0:
        out.add(dt.date(year, 10, 1))
    return out


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in mx_holidays(d.year)


def _next_open(t: dt.datetime) -> dt.datetime:
    """The first business moment at or after ``t`` (local time)."""
    while True:
        if is_business_day(t.date()):
            if t.time() < BIZ_START:
                return dt.datetime.combine(t.date(), BIZ_START)
            if t.time() < BIZ_END:
                return t
        t = dt.datetime.combine(t.date() + dt.timedelta(days=1), BIZ_START)


def add_business_hours(start: dt.datetime, hours: float) -> dt.datetime:
    """``start`` (MX local, naive) plus ``hours`` of business time."""
    left = hours * 3600.0
    t = _next_open(start)
    while True:
        end = dt.datetime.combine(t.date(), BIZ_END)
        avail = (end - t).total_seconds()
        if left <= avail:
            return t + dt.timedelta(seconds=left)
        left -= avail
        t = _next_open(end)


def business_hours_between(a: dt.datetime, b: dt.datetime) -> float:
    if b <= a:
        return 0.0
    total = 0.0
    t = _next_open(a)
    while t < b:
        end = min(dt.datetime.combine(t.date(), BIZ_END), b)
        total += max(0.0, (end - t).total_seconds())
        t = _next_open(dt.datetime.combine(t.date(), BIZ_END))
    return total / 3600.0


def to_local(ts_utc: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts_utc) + M.MX_OFFSET


def triage_due_local(detected_utc: str) -> dt.datetime:
    return add_business_hours(to_local(detected_utc), TRIAGE_HOURS)


def triage_state(a, now_utc_dt: dt.datetime) -> str:
    """'auto' (cleared by itself), 'met', 'breached' or 'running'."""
    if a["triage"] == "self_cleared":
        return "auto"
    due = triage_due_local(a["detected_utc"])
    if a["triaged_utc"]:
        return "met" if to_local(a["triaged_utc"]) <= due else "breached"
    return "breached" if (now_utc_dt + M.MX_OFFSET) > due else "running"


# ------------------------------------------------------------------ rules (pure)
@dataclass(frozen=True)
class Snapshot:
    """What the engine knows about one site at one moment.

    kw / kw_expected: AC now and what the site should make now. basis says
    where kw_expected comes from: 'irradiance' (on-site sensor) or 'sample'
    can be compared directly; 'clear_sky' cannot (clouds), so then only
    inverter-level status (inverters_down) raises a production alarm."""
    site_code: str
    kwp: float
    daylight: bool
    kw: Optional[float] = None
    kw_expected: Optional[float] = None
    basis: str = "clear_sky"
    data_age_min: Optional[float] = None       # None = never reported
    night_heartbeat: bool = False              # the DAS reports at night too (then comm loss is checked at night)
    meter_kw: Optional[float] = None
    irr_sensor: Optional[float] = None
    ghi_clear: Optional[float] = None
    frozen_n: int = 0                          # consecutive identical non-zero readings
    inverters_down: Tuple[Tuple[str, float], ...] = ()   # (tag, DC kW behind it)
    sample: bool = False


@dataclass
class Finding:
    kind: str
    msa_class: str
    kw_lost: Optional[float]
    detail: str

    @property
    def severity(self) -> str:
        return "critical" if self.msa_class in CRITICAL_CLASSES else "warning"


COMM_DAY_MIN, COMM_NIGHT_MIN = 30.0, 180.0


def evaluate(s: Snapshot) -> Tuple[List[Finding], Set[str]]:
    """(findings, kinds that could be evaluated). A kind that was
    evaluable and did not fire may clear its open alarm; a kind that could
    not be evaluated (night, no data) leaves the alarm as it is."""
    out: List[Finding] = []
    ev: Set[str] = set()
    # communication loss (DAS: modem / network)
    if s.daylight or s.night_heartbeat:
        ev.add("comm_loss")
        limit = COMM_DAY_MIN if s.daylight else COMM_NIGHT_MIN
        if s.data_age_min is None or s.data_age_min > limit:
            confirmed = s.meter_kw is not None and s.meter_kw > 0
            age = "never" if s.data_age_min is None else f"{s.data_age_min:.0f} min"
            out.append(Finding("comm_loss", "COMM_2" if confirmed else "COMM_1", None,
                               f"no data for {age}" + ("; the billing meter shows production" if confirmed else "; operation not confirmed")))
    fresh = s.data_age_min is not None and s.data_age_min <= COMM_DAY_MIN and s.kw is not None
    # production loss
    if s.daylight and fresh:
        lost = None
        why = ""
        if s.inverters_down:
            dc = sum(x for _, x in s.inverters_down)
            per_kwp = (s.kw_expected / s.kwp) if (s.kw_expected and s.kwp) else M.PR_TARGET * 0.8
            lost = round(dc * per_kwp, 1)
            why = "inverter(s) down: " + ", ".join(t for t, _ in s.inverters_down)
        elif s.basis in ("irradiance", "sample") and s.kw_expected is not None and s.kw_expected >= 0.05 * s.kwp:
            if s.kw < 0.6 * s.kw_expected:
                lost = round(s.kw_expected - s.kw, 1)
                why = f"{s.kw:.0f} kW against {s.kw_expected:.0f} kW expected"
        if s.inverters_down or (s.basis in ("irradiance", "sample") and s.kw_expected is not None):
            ev.add("production_loss")
        if lost is not None and lost >= 1.0:
            out.append(Finding("production_loss", SLA.suggest(lost), lost, why))
    # meter issue
    if s.meter_kw is not None and fresh and s.kw >= 0.1 * s.kwp * M.AC_RATIO:
        ev.add("meter_mismatch")
        if abs(s.meter_kw - s.kw) / s.kw > 0.10:
            out.append(Finding("meter_mismatch", "COMM_2", None, f"meter {s.meter_kw:.0f} kW, inverters {s.kw:.0f} kW"))
    # weather sensor
    if s.irr_sensor is not None and s.daylight and s.ghi_clear is not None and s.ghi_clear >= 300 and fresh:
        ev.add("sensor_implausible")
        dark = s.irr_sensor < 0.05 * s.ghi_clear and s.kw > 0.3 * s.kwp * M.AC_RATIO
        hot = s.irr_sensor > 1.4 * s.ghi_clear + 100
        if dark or hot:
            out.append(Finding("sensor_implausible", "OTHER", None,
                               f"sensor {s.irr_sensor:.0f} W/m2, clear sky {s.ghi_clear:.0f} W/m2, site {s.kw:.0f} kW"))
    # data logger frozen
    if s.daylight and fresh:
        ev.add("frozen_data")
        if s.frozen_n >= 6 and s.kw > 0:
            out.append(Finding("frozen_data", "COMM_2", None, f"the same reading {s.kw:.1f} kW {s.frozen_n} times in a row"))
    return out, ev


def evaluate_day(actual_kwh: float, expected_kwh: float, completeness: float) -> Optional[Finding]:
    """Yesterday below 85% of the weather-adjusted expectation, with at
    least 95% of the data present (missing data is never a loss)."""
    if completeness < 0.95 or expected_kwh <= 0:
        return None
    ratio = actual_kwh / expected_kwh
    if ratio < 0.85:
        return Finding("underperformance", "OTHER", None, f"{actual_kwh:.0f} kWh = {ratio * 100:.0f}% of {expected_kwh:.0f} kWh expected")
    return None


def sample_snapshot(site, now_local: dt.datetime, index: int = 0, n_sites: int = 1) -> Snapshot:
    """A Snapshot from the SAMPLE metering (until live data is collected)."""
    lv = M.live(site, now_local, index, n_sites)
    el = M.sun_elevation(site.lat, site.lon, now_local)
    prof = M._profile(site.code, site.lat, site.lon, site.kwp, now_local.date())
    hhmm = now_local.strftime("%H:%M")
    slot = [x for x in prof if x[0] <= hhmm]
    kw_exp = slot[-1][1] if slot else 0.0
    ghi = M.clear_sky_ghi(el)
    if lv.status == "comm_loss":
        last = lv.last_seen
        age = None
        if last:
            t = dt.datetime.combine(now_local.date(), dt.time(int(last[:2]), int(last[3:])))
            age = (now_local - t).total_seconds() / 60
        return Snapshot(site.code, site.kwp, el > 5, None, kw_exp, "sample", age, ghi_clear=ghi, sample=True)
    down: Tuple[Tuple[str, float], ...] = ()
    if lv.status == "inverter_fault":
        ev = M.event_for(site.code, now_local.date(), n_sites, index)
        down = (("INV (sample)", round(site.kwp * (ev.share if ev else 0.33), 1)),)
    return Snapshot(site.code, site.kwp, el > 5, lv.kw_now, kw_exp, "sample", 0.0, ghi_clear=ghi,
                    inverters_down=down, sample=True)


# ------------------------------------------------------------------ alarm store
def open_alarms(c, site: str = "") -> List[sqlite3.Row]:
    q, a = "SELECT * FROM alarms WHERE cleared_utc=''", []
    if site:
        q += " AND site_code=?"
        a.append(site)
    return c.execute(q + " ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, id DESC", a).fetchall()


def alarm(c, aid: int) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM alarms WHERE id=?", (aid,)).fetchone()


_RANK = {k: i for i, k in enumerate(["OTHER", "MICRO", "STRING_25", "COMM_2", "STRING_100", "COMM_1", "OUT_100_500", "OUT_500", "EMERGENCY"])}


@dataclass
class RunResult:
    opened: List[int] = field(default_factory=list)
    escalated: List[int] = field(default_factory=list)
    cleared: List[int] = field(default_factory=list)


def apply(c, snap: Snapshot, now: str, findings: Sequence[Finding], evaluable: Set[str], res: RunResult) -> None:
    """Upsert one site's findings: one open alarm per site and kind; a
    worse class escalates it (and is notified again if critical); an
    evaluable kind that stays clean CLEAR_AFTER_MISSES runs clears it."""
    cur = {r["kind"]: r for r in open_alarms(c, snap.site_code)}
    seen = set()
    for f in findings:
        seen.add(f.kind)
        old = cur.get(f.kind)
        if old is None:
            r = c.execute("INSERT INTO alarms (site_code, kind, severity, msa_class, kw_lost, detail, detected_utc, last_seen_utc, sample)"
                          " VALUES (?,?,?,?,?,?,?,?,?)", (snap.site_code, f.kind, f.severity, f.msa_class, f.kw_lost, f.detail[:500],
                                                          now, now, 1 if snap.sample else 0))
            res.opened.append(int(r.lastrowid))
            continue
        worse = _RANK.get(f.msa_class, 0) > _RANK.get(old["msa_class"], 0)
        c.execute("UPDATE alarms SET last_seen_utc=?, misses=0, detail=?, kw_lost=coalesce(?, kw_lost)"
                  + (", msa_class=?, severity=?" if worse else "") + " WHERE id=?",
                  (now, f.detail[:500], f.kw_lost, *((f.msa_class, f.severity) if worse else ()), old["id"]))
        if worse and f.severity == "critical" and old["severity"] != "critical":
            res.escalated.append(int(old["id"]))
    for kind, old in cur.items():
        if kind in seen or kind not in evaluable or kind == "underperformance":
            continue
        misses = old["misses"] + 1
        if misses < CLEAR_AFTER_MISSES:
            c.execute("UPDATE alarms SET misses=? WHERE id=?", (misses, old["id"]))
            continue
        c.execute("UPDATE alarms SET misses=?, cleared_utc=? WHERE id=?", (misses, now, old["id"]))
        dur = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(old["detected_utc"])).total_seconds() / 60
        if not old["triaged_utc"] and dur <= SELF_CLEARED_MIN:
            c.execute("UPDATE alarms SET triage='self_cleared', triaged_utc=?, triaged_by='system', "
                      "triage_note=? WHERE id=?", (now, f"cleared by itself after {dur:.0f} min", old["id"]))
        res.cleared.append(int(old["id"]))
    c.commit()


def apply_day(c, site_code: str, now: str, finding: Optional[Finding], sample: bool, res: RunResult) -> None:
    """The daily underperformance check: opens, keeps or clears one alarm."""
    old = next((r for r in open_alarms(c, site_code) if r["kind"] == "underperformance"), None)
    if finding and old is None:
        r = c.execute("INSERT INTO alarms (site_code, kind, severity, msa_class, detail, detected_utc, last_seen_utc, sample)"
                      " VALUES (?,?,?,?,?,?,?,?)", (site_code, finding.kind, finding.severity, finding.msa_class,
                                                    finding.detail, now, now, 1 if sample else 0))
        res.opened.append(int(r.lastrowid))
    elif finding and old is not None:
        c.execute("UPDATE alarms SET last_seen_utc=?, detail=? WHERE id=?", (now, finding.detail, old["id"]))
    elif old is not None:
        c.execute("UPDATE alarms SET cleared_utc=? WHERE id=?", (now, old["id"]))
        res.cleared.append(int(old["id"]))
    c.commit()


def triage(c, a: sqlite3.Row, action: str, by: str, note: str = "", ticket_number: str = "",
           title: str = "", ip: str = "") -> Optional[str]:
    """Classify an alarm. 'ticket' opens an incident whose detection time is
    the alarm's (the MSA response clock starts at the DAS alarm); 'link'
    attaches it to an open ticket; 'dismiss' needs a reason. Returns the
    ticket number when one is involved."""
    from argia.prologis import store as S
    if a["triaged_utc"]:
        raise ValueError("already triaged")
    if action not in [k for k, _, _ in TRIAGE_ACTIONS]:
        raise ValueError("unknown triage action")
    tid, num = None, None
    if action == "dismiss" and not note.strip():
        raise ValueError("a dismissal needs a reason")
    if action == "link":
        r = c.execute("SELECT id, number, status FROM tickets WHERE number=?", ((ticket_number or "").strip().upper(),)).fetchone()
        if not r:
            raise ValueError("no such ticket")
        if r["status"] not in S.OPEN:
            raise ValueError("that ticket is not open")
        tid, num = r["id"], r["number"]
    if action == "ticket":
        label = dict((k, en) for k, en, _ in KINDS).get(a["kind"], a["kind"])
        t = S.create_ticket(c, a["site_code"], (title or label).strip()[:160],
                            f"From alarm #{a['id']}: {a['detail']}\n{note}".strip(), a["msa_class"], by,
                            detected_utc=a["detected_utc"], kw_lost=a["kw_lost"], sample=bool(a["sample"]), ip=ip)
        tid, num = t["id"], t["number"]
    c.execute("UPDATE alarms SET triage=?, triaged_utc=?, triaged_by=?, triage_note=?, ticket_id=? WHERE id=?",
              (action, now_utc(), by, note.strip()[:1000], tid, a["id"]))
    c.commit()
    _audit(c, by, ip, "alarm_triage", f"alarm#{a['id']}", f"{action} {num or ''} {note[:200]}")
    return num


# ------------------------------------------------------------------ mail
def recipients(c, level: str) -> List[str]:
    """The ARGIA desk plus every enabled user who opted in: level
    'critical' -> users with 'critical' or 'all'; 'digest' -> 'all';
    'owner' (a denied warranty claim) -> Prologis managers who opted in."""
    try:
        out = parse_emails(setting(c, "desk_emails"))
    except ValueError:
        out = []
    if level == "critical":
        q = "SELECT email FROM users WHERE disabled=0 AND alarm_mail IN ('critical','all') AND email<>''"
    elif level == "digest":
        q = "SELECT email FROM users WHERE disabled=0 AND alarm_mail='all' AND email<>''"
    else:
        q = "SELECT email FROM users WHERE disabled=0 AND role='manager' AND alarm_mail IN ('critical','all') AND email<>''"
    for (e,) in c.execute(q):
        e = e.strip().lower()
        if e and e not in out:
            out.append(e)
    return out


def queue(c, kind: str, ref: str, subject: str, body: str, to: Sequence[str], sample: bool) -> int:
    """Write one message to the outbox. Its status says what happens to it:
    'no_recipient', 'sample' (never mailed), 'dry_run' (mode), or
    'pending' (live: deliver() sends it)."""
    mode = setting(c, "mail_mode")
    status = "no_recipient" if not to else ("sample" if sample else ("pending" if mode == "live" else "dry_run"))
    r = c.execute("INSERT INTO outbox (created_utc, kind, ref, to_addrs, subject, body, status, sample) VALUES (?,?,?,?,?,?,?,?)",
                  (now_utc(), kind, ref, ", ".join(to), subject[:300], body[:20000], status, 1 if sample else 0))
    c.commit()
    return int(r.lastrowid)


def deliver(c, cfg: Optional[Dict[str, str]], send=None) -> Tuple[int, int]:
    """Send every 'pending' message (live mode only). (sent, failed).
    Without SMTP config nothing is sent and the messages stay pending."""
    if setting(c, "mail_mode") != "live" or not cfg:
        return 0, 0
    from argia.alerts import emailer
    send = send or emailer.send
    ok = bad = 0
    for m in c.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id").fetchall():
        msg = emailer.build_email(m["subject"], m["body"], cfg.get("SMTP_FROM") or cfg["SMTP_USER"],
                                  [x.strip() for x in m["to_addrs"].split(",") if x.strip()])
        if send(msg, cfg):
            c.execute("UPDATE outbox SET status='sent', sent_utc=? WHERE id=?", (now_utc(), m["id"]))
            ok += 1
        else:
            c.execute("UPDATE outbox SET status='failed' WHERE id=?", (m["id"],))
            bad += 1
        c.commit()
    return ok, bad


def _kind_label(kind: str) -> str:
    return dict((k, en) for k, en, _ in KINDS).get(kind, kind)


def alarm_mail(a, site_name: str, base_url: str) -> Tuple[str, str]:
    cls = SLA.BY_CODE.get(a["msa_class"], SLA.BY_CODE["OTHER"])
    due = triage_due_local(a["detected_utc"])
    subj = f"[ARGIA for Prologis]{' [SAMPLE]' if a['sample'] else ''} CRITICAL {_kind_label(a['kind'])} - {site_name}"
    body = (f"{_kind_label(a['kind'])} at {site_name} ({a['site_code']}).\n"
            f"Detected: {to_local(a['detected_utc']):%d %b %Y %H:%M} Mexico City time.\n"
            f"MSA class: {cls.priority} {cls.en}.\n"
            + (f"Estimated loss: {a['kw_lost']:.0f} kW.\n" if a["kw_lost"] else "")
            + f"Detail: {a['detail']}\n"
            f"Triage due by: {due:%d %b %Y %H:%M} (4 business hours).\n\n"
            f"{base_url}/alarms/#a{a['id']}\n"
            + ("\nSAMPLE: computed from simulated data until the SolarEdge / Hark access is granted. Not a real event.\n" if a["sample"] else ""))
    return subj, body


REVIEW_WORDS = {"on_time": "done", "late": "done late", "missing": "MISSING", "today": "to do", "before_start": "log not started yet"}


def digest(c, rg, now_local: dt.datetime, base_url: str) -> Tuple[str, str, bool]:
    """The 07:30 digest: open alarms, triage overdue, the daily review."""
    nowu = now_local - M.MX_OFFSET
    op = open_alarms(c)
    lines = []
    sample = bool(op) and all(a["sample"] for a in op)
    for a in op:
        s = rg.site(a["site_code"])
        st = triage_state(a, nowu) if not a["triaged_utc"] else "triaged"
        lines.append(f"- {a['severity'].upper():8} {_kind_label(a['kind'])} - {s.name if s else a['site_code']}"
                     f" since {to_local(a['detected_utc']):%d %b %H:%M}"
                     + (" - TRIAGE OVERDUE" if st == "breached" else (" - to triage" if st == "running" else ""))
                     + (" [SAMPLE]" if a["sample"] else ""))
    yday = now_local.date() - dt.timedelta(days=1)
    rv = review_status(c, now_local.date(), days=7)
    missing = [d for d, st in rv if st == "missing"]
    body = (f"ARGIA for Prologis - daily alarm digest {now_local:%d %b %Y}\n\n"
            f"Open alarms: {len(op)} ({sum(1 for a in op if a['severity'] == 'critical')} critical)\n"
            + ("\n".join(lines) if lines else "None.") + "\n\n"
            f"Daily performance review {yday:%d %b}: {REVIEW_WORDS.get(dict(rv).get(yday.isoformat(), ''), 'n/a')}\n"
            + (f"Days without a review (last 7): {', '.join(missing)}\n" if missing else "")
            + f"\n{base_url}/alarms/\n")
    subj = f"[ARGIA for Prologis]{' [SAMPLE]' if sample else ''} Daily alarm digest {now_local:%d %b %Y} - {len(op)} open"
    return subj, body, sample


# ------------------------------------------------------------------ daily review
def review_status(c, today: dt.date, days: int = 30) -> List[Tuple[str, str]]:
    """[(day, 'on_time' | 'late' | 'missing' | 'today' | 'before_start')]
    for the last ``days`` days, newest first."""
    start = setting(c, "review_start")
    rows = {r["day"]: r for r in c.execute("SELECT * FROM daily_review")}
    out = []
    for i in range(days):
        d = today - dt.timedelta(days=i)
        k = d.isoformat()
        if k in rows:
            out.append((k, "late" if rows[k]["late"] else "on_time"))
        elif not start or k < start:
            out.append((k, "before_start"))
        elif d == today:
            out.append((k, "today"))
        else:
            out.append((k, "missing"))
    return out


def save_review(c, day: str, by: str, findings: str, sites_checked: int, evidence: dict,
                today: dt.date, ip: str = "") -> None:
    d = dt.date.fromisoformat(day)
    if d > today:
        raise ValueError("a review cannot be for a future day")
    if (today - d).days > REVIEW_GRACE_DAYS:
        raise ValueError(f"a review can be recorded up to {REVIEW_GRACE_DAYS} days late")
    if c.execute("SELECT 1 FROM daily_review WHERE day=?", (day,)).fetchone():
        raise ValueError("this day is already reviewed")
    if not findings.strip():
        raise ValueError("write what was found (or 'no findings')")
    c.execute("INSERT INTO daily_review (day, username, ts_utc, late, sites_checked, findings, evidence) VALUES (?,?,?,?,?,?,?)",
              (day, by, now_utc(), 1 if d < today else 0, sites_checked, findings.strip()[:5000], json.dumps(evidence)[:100000]))
    c.commit()
    _audit(c, by, ip, "daily_review", day, f"sites={sites_checked} late={d < today}")
    if not setting(c, "review_start"):
        set_setting(c, "review_start", day, by, ip)


def review_compliance(status: Iterable[Tuple[str, str]]) -> Optional[float]:
    s = [x for _, x in status if x in ("on_time", "late", "missing")]
    if not s:
        return None
    return sum(1 for x in s if x == "on_time") / len(s)


# ------------------------------------------------------------------ the run
def run(c, rg, now_local: dt.datetime, base_url: str = "https://prologis.argia.com.mx",
        snapshot=None, cfg: Optional[Dict[str, str]] = None, send=None) -> Dict[str, int]:
    """One engine pass (the 5-minute timer): evaluate every operating site,
    upsert alarms, queue a mail for each new or escalated critical alarm,
    the daily checks and digest once after 07:30, then deliver (live mode)."""
    snapshot = snapshot or sample_snapshot
    now = (now_local - M.MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
    res = RunResult()
    op = rg.operating
    for i, s in enumerate(op):
        snap = snapshot(s, now_local, i, len(op))
        f, ev = evaluate(snap)
        apply(c, snap, now, f, ev, res)
    queued = 0
    for aid in res.opened + res.escalated:
        a = alarm(c, aid)
        if a["severity"] != "critical":
            continue
        s = rg.site(a["site_code"])
        subj, body = alarm_mail(a, s.name if s else a["site_code"], base_url)
        queue(c, "alarm", f"alarm#{aid}", subj, body, recipients(c, "critical"), bool(a["sample"]))
        c.execute("UPDATE alarms SET notified_utc=? WHERE id=?", (now, aid))
        c.commit()
        queued += 1
    digest_sent = 0
    today = now_local.date().isoformat()
    if now_local.time() >= DIGEST_AT and setting(c, "last_digest_day") != today:
        yday = now_local.date() - dt.timedelta(days=1)
        for i, s in enumerate(op):
            r = M.day_result(s.code, s.lat, s.lon, s.kwp, yday, index=i, n_sites=len(op))
            comp = sum(1 for _, v in r.series if v is not None) / max(1, len(r.series))
            apply_day(c, s.code, now, evaluate_day(r.kwh, r.expected_kwh, comp), True, res)
        subj, body, sample = digest(c, rg, now_local, base_url)
        queue(c, "digest", today, subj, body, recipients(c, "digest"), sample)
        set_setting(c, "last_digest_day", today, "system")
        digest_sent = 1
    sent, failed = deliver(c, cfg, send)
    return {"sites": len(op), "opened": len(res.opened), "escalated": len(res.escalated), "cleared": len(res.cleared),
            "queued": queued + digest_sent, "sent": sent, "failed": failed}
