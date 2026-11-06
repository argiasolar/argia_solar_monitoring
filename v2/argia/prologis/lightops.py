"""Lighting operations in the portal (v356): warranty claims opened by Prologis, and the lighting calendar
(audits, installations, handovers, warranty visits) - Tomasz, 5 Nov 2026:

* "let prologis employees open warranty ticket inside the portal"
* "keep track of 5 years warranty period from handover"
* "lets add calendar for scheduling audit, and showing any projects work onsite where Prologis can see what is
  happening"

Tables live in the platform's SQLite file (created by store.connect()). Pure helpers + small SQL functions; no Flask.
"""
from __future__ import annotations

import calendar as _cal
import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS light_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE, code TEXT NOT NULL, customer TEXT NOT NULL DEFAULT '',
  fixtures INTEGER, area TEXT NOT NULL DEFAULT '', symptom TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  contact TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'NEW', w_ticket TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL, closed_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS light_claim_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL, ts_utc TEXT NOT NULL, username TEXT NOT NULL,
  kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS light_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL, slot TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', requested_by TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL, created_utc TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS light_events_dates ON light_events (start_date, end_date);
CREATE TABLE IF NOT EXISTS light_pins (
  project_id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, username TEXT NOT NULL, ts_utc TEXT NOT NULL);
"""

SYMPTOMS = [("off", "Fixture does not turn on", "La luminaria no enciende"),
            ("intermittent", "Turns on and off / flickers", "Enciende y apaga / parpadea"),
            ("partial", "Part of the fixture is off", "Una parte de la luminaria está apagada"),
            ("sensor", "Sensor or controls do not work", "El sensor o los controles no funcionan"),
            ("damage", "Physical damage", "Daño físico"),
            ("other", "Other", "Otro")]
SYMPTOM = {k: (en, es) for k, en, es in SYMPTOMS}
CLAIM_STATUSES = [("NEW", "Reported", "Reportado", "#e0a100"),
                  ("RECEIVED", "Received by ARGIA", "Recibido por ARGIA", "#2CB5E5"),
                  ("PARTS", "Replacement ordered", "Reemplazo pedido", "#2CB5E5"),
                  ("SCHEDULED", "Visit scheduled", "Visita programada", "#71BE45"),
                  ("CLOSED", "Fixed", "Resuelto", "#23B2A9"),
                  ("NOT_COVERED", "Not covered by the warranty", "No cubierto por la garantía", "#9aa4ad")]
CLAIM_STATUS = {k: (en, es, col) for k, en, es, col in CLAIM_STATUSES}
CLAIM_OPEN = ("NEW", "RECEIVED", "PARTS", "SCHEDULED")
CLAIM_NEXT = {"NEW": ("RECEIVED", "NOT_COVERED"), "RECEIVED": ("PARTS", "SCHEDULED", "CLOSED", "NOT_COVERED"),
              "PARTS": ("SCHEDULED", "CLOSED"), "SCHEDULED": ("CLOSED", "PARTS"), "CLOSED": ("RECEIVED",),
              "NOT_COVERED": ("RECEIVED",)}

EVENT_KINDS = [("audit", "Audit", "Auditoría", "#2CB5E5"), ("installation", "Installation", "Instalación", "#1B4D4A"),
               ("handover", "Handover", "Entrega", "#71BE45"), ("warranty", "Warranty visit", "Visita de garantía", "#e0a100"),
               ("other", "Other work on site", "Otro trabajo en sitio", "#9aa4ad")]
EVENT_KIND = {k: (en, es, col) for k, en, es, col in EVENT_KINDS}
EVENT_STATUSES = [("requested", "Requested by Prologis", "Solicitado por Prologis"), ("confirmed", "Confirmed", "Confirmado"),
                  ("done", "Done", "Realizado"), ("cancelled", "Cancelled", "Cancelado")]
EVENT_STATUS = {k: (en, es) for k, en, es in EVENT_STATUSES}

CODE_RE = re.compile(r"^[A-Z]{3}\d{5}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
W_RE = re.compile(r"^W-\d{1,5}-\d{2}$")
MAX_DAYS = 120                                     # one calendar entry spans at most four months


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


def _audit(c, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    c.execute("INSERT INTO audit (ts_utc, username, ip, action, target, detail) VALUES (?,?,?,?,?,?)",
              (now_utc(), user, ip, action, target, detail[:500]))


def parse_date(s: str) -> Optional[dt.date]:
    s = (s or "").strip()
    if not DATE_RE.match(s):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ warranty period
@dataclass(frozen=True)
class Coverage:
    code: Optional[str]
    project_id: int
    handover: str
    until: str
    days_left: int
    state: str                                       # in | ending (within 12 months) | ended


def coverage(projects: Iterable, today: dt.date, years: int = 5, soon_days: int = 365) -> List[Coverage]:
    """The warranty period of every delivered project with a real (not planned) handover date."""
    from argia.prologis.warranty import warranty_until
    out = []
    for p in projects:
        if p.status != "delivered" or not p.handover_date or getattr(p, "handover_planned", False):
            continue
        until = warranty_until(p.handover_date, years)
        if not until:
            continue
        left = (dt.date.fromisoformat(until) - today).days
        state = "ended" if left < 0 else ("ending" if left <= soon_days else "in")
        out.append(Coverage(p.yardi, p.id, p.handover_date, until, left, state))
    return sorted(out, key=lambda x: x.until)


# ------------------------------------------------------------------ warranty claims opened in the portal
class Invalid(ValueError):
    pass


def _next_number(c, year: int) -> str:
    n = c.execute("SELECT COUNT(*) FROM light_claims WHERE number LIKE ?", (f"LW-{year}-%",)).fetchone()[0]
    return f"LW-{year}-{n + 1:04d}"


def create_claim(c, code: str, customer: str, fixtures, area: str, symptom: str, description: str, contact: str,
                 by: str, ip: str = "", today: Optional[dt.date] = None) -> sqlite3.Row:
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        raise Invalid("building code")
    if symptom not in SYMPTOM:
        raise Invalid("symptom")
    try:
        n = int(str(fixtures).strip()) if str(fixtures or "").strip() else None
    except ValueError:
        raise Invalid("fixtures")
    if n is not None and not 0 < n < 5000:
        raise Invalid("fixtures")
    if not (description or "").strip() and symptom == "other":
        raise Invalid("description")
    year = (today or dt.date.today()).year
    now = now_utc()
    num = _next_number(c, year)
    cur = c.execute("INSERT INTO light_claims (number, code, customer, fixtures, area, symptom, description, contact, status,"
                    " created_by, created_utc, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (num, code, (customer or "")[:120], n, (area or "")[:160], symptom, (description or "")[:4000],
                     (contact or "")[:200], "NEW", by, now, now))
    add_claim_event(c, int(cur.lastrowid), by, "created", "")
    _audit(c, by, ip, "light_claim_new", num, code)
    c.commit()
    return claim(c, num)


def claim(c, number: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM light_claims WHERE number=?", (number,)).fetchone()


def claims(c, code: str = "", open_only: bool = False) -> List[sqlite3.Row]:
    q, a = "SELECT * FROM light_claims WHERE 1=1", []
    if code:
        q += " AND code=?"
        a.append(code)
    if open_only:
        q += f" AND status IN ({','.join('?' * len(CLAIM_OPEN))})"
        a += list(CLAIM_OPEN)
    return c.execute(q + " ORDER BY id DESC", a).fetchall()


def add_claim_event(c, claim_id: int, by: str, kind: str, body: str = "", meta: Optional[dict] = None) -> None:
    c.execute("INSERT INTO light_claim_events (claim_id, ts_utc, username, kind, body, meta) VALUES (?,?,?,?,?,?)",
              (claim_id, now_utc(), by, kind, (body or "")[:4000], json.dumps(meta or {})))
    c.execute("UPDATE light_claims SET updated_utc=? WHERE id=?", (now_utc(), claim_id))
    c.commit()


def claim_events(c, claim_id: int) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM light_claim_events WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()


def set_claim_status(c, number: str, to: str, by: str, note: str = "", w_ticket: str = "", ip: str = "") -> sqlite3.Row:
    cl = claim(c, number)
    if cl is None:
        raise Invalid("claim")
    if to not in CLAIM_NEXT.get(cl["status"], ()):
        raise Invalid("status")
    w = (w_ticket or "").strip().upper()
    if w and not W_RE.match(w):
        raise Invalid("W- ticket")
    c.execute("UPDATE light_claims SET status=?, w_ticket=CASE WHEN ?<>'' THEN ? ELSE w_ticket END, closed_utc=? WHERE id=?",
              (to, w, w, now_utc() if to in ("CLOSED", "NOT_COVERED") else "", cl["id"]))
    add_claim_event(c, cl["id"], by, "status", note, {"from": cl["status"], "to": to, **({"w": w} if w else {})})
    _audit(c, by, ip, "light_claim_status", number, f"{cl['status']}->{to}")
    c.commit()
    return claim(c, number)


def claim_mail(cl: sqlite3.Row, building: str, base_url: str, who: str) -> Tuple[str, str]:
    sym = SYMPTOM.get(cl["symptom"], (cl["symptom"],))[0]
    subject = f"[Prologis LED warranty] {cl['number']} - {cl['code']} - {sym}"
    body = (f"A warranty claim was opened in the Prologis portal by {who}.\n\n"
            f"Claim: {cl['number']}\nBuilding: {cl['code']} {building}\nCustomer: {cl['customer'] or '-'}\n"
            f"Fixtures affected: {cl['fixtures'] or '-'}\nWhere: {cl['area'] or '-'}\nWhat happens: {sym}\n"
            f"Contact on site: {cl['contact'] or '-'}\n\n{cl['description']}\n\n{base_url}/lighting/warranty/c/{cl['number']}/\n")
    return subject, body


# ------------------------------------------------------------------ calendar
def add_event(c, kind: str, code: str, title: str, start: str, end: str, slot: str, status: str, notes: str,
              by: str, ref: str = "", requested_by: str = "", ip: str = "") -> int:
    if kind not in EVENT_KIND:
        raise Invalid("kind")
    if status not in EVENT_STATUS:
        raise Invalid("status")
    code = (code or "").strip().upper()
    if code and not CODE_RE.match(code):
        raise Invalid("building code")
    d0, d1 = parse_date(start), parse_date(end or start)
    if not d0 or not d1 or d1 < d0 or (d1 - d0).days > MAX_DAYS:
        raise Invalid("dates")
    title = (title or "").strip() or EVENT_KIND[kind][0]
    now = now_utc()
    cur = c.execute("INSERT INTO light_events (kind, code, title, start_date, end_date, slot, status, notes, ref, requested_by,"
                    " created_by, created_utc, updated_by, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, code, title[:160], d0.isoformat(), d1.isoformat(), (slot or "")[:40], status, (notes or "")[:2000],
                     ref[:40], requested_by, by, now, by, now))
    _audit(c, by, ip, "light_event_new", f"{kind}/{code}", f"{d0}..{d1} {status}")
    c.commit()
    return int(cur.lastrowid)


def event(c, eid: int) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM light_events WHERE id=?", (eid,)).fetchone()


def update_event(c, eid: int, by: str, status: Optional[str] = None, start: str = "", end: str = "", slot: Optional[str] = None,
                 ip: str = "") -> sqlite3.Row:
    ev = event(c, eid)
    if ev is None:
        raise Invalid("event")
    st = status or ev["status"]
    if st not in EVENT_STATUS:
        raise Invalid("status")
    d0 = parse_date(start) if start else dt.date.fromisoformat(ev["start_date"])
    d1 = parse_date(end) if end else (d0 if start else dt.date.fromisoformat(ev["end_date"]))
    if not d0 or not d1 or d1 < d0 or (d1 - d0).days > MAX_DAYS:
        raise Invalid("dates")
    c.execute("UPDATE light_events SET status=?, start_date=?, end_date=?, slot=?, updated_by=?, updated_utc=? WHERE id=?",
              (st, d0.isoformat(), d1.isoformat(), ev["slot"] if slot is None else slot[:40], by, now_utc(), eid))
    _audit(c, by, ip, "light_event_update", str(eid), f"{ev['status']}->{st} {d0}..{d1}")
    c.commit()
    return event(c, eid)


def events_between(c, first: dt.date, last: dt.date, include_cancelled: bool = False) -> List[sqlite3.Row]:
    q = "SELECT * FROM light_events WHERE start_date<=? AND end_date>=?" + ("" if include_cancelled else " AND status<>'cancelled'")
    return c.execute(q + " ORDER BY start_date, id", (last.isoformat(), first.isoformat())).fetchall()


def month_grid(year: int, month: int) -> List[List[dt.date]]:
    """Weeks (Monday first) covering the month, with the days of the neighbouring months that fill the weeks."""
    return [list(w) for w in _cal.Calendar(firstweekday=0).monthdatescalendar(year, month)]


def month_of(s: str, today: dt.date) -> Tuple[int, int]:
    m = re.match(r"^(\d{4})-(\d{2})$", s or "")
    if m and 2017 <= int(m.group(1)) <= 2100 and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    return today.year, today.month


def shift_month(y: int, m: int, k: int) -> Tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


@dataclass(frozen=True)
class Entry:
    """One item on the calendar: a stored event, or something derived from the data (a planned handover)."""
    kind: str
    title: str
    code: str
    start: dt.date
    end: dt.date
    status: str
    slot: str = ""
    eid: Optional[int] = None
    href: str = ""


def entries(c, first: dt.date, last: dt.date, projects: Sequence = ()) -> List[Entry]:
    out = [Entry(r["kind"], r["title"], r["code"], dt.date.fromisoformat(r["start_date"]), dt.date.fromisoformat(r["end_date"]),
                 r["status"], r["slot"], r["id"], f"/lighting/calendar/e/{r['id']}/") for r in events_between(c, first, last)]
    stored = {(e.kind, e.code, e.start) for e in out}
    for p in projects:                                         # planned handovers of projects under way
        if p.status == "in_progress" and p.handover_date and getattr(p, "handover_planned", False):
            d = parse_date(p.handover_date)
            if d and first <= d <= last and ("handover", p.yardi or "", d) not in stored:
                out.append(Entry("handover", f"{p.customer} - planned handover", p.yardi or "", d, d, "confirmed",
                                 href=f"/lighting/p/{p.id}"))
    return sorted(out, key=lambda e: (e.start, e.kind, e.title))


# ------------------------------------------------------------------ v360 pins moved by hand
# Tomasz, 5 Nov 2026: "i want this map to be also interactive so I can zoom in and out and I can edit the pin location,
# because in many cases the pin is showing something completely out of nowhere". A pin moved in the portal is kept in
# light_pins and laid over lighting.json at load time (rebuilding lighting.json does not lose it); every move and every
# reset is in the audit log with the old and the new position.
MX_BOX = (14.0, 33.5, -118.6, -86.5)               # lat min/max, lon min/max: Mexico
MAX_MOVE_KM = 60.0                                  # from the data file's point: a slip of the mouse, not another city


def _km(a: float, b: float, c: float, d: float) -> float:
    import math
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(x))


def pins(c: sqlite3.Connection) -> Dict[int, sqlite3.Row]:
    return {r["project_id"]: r for r in c.execute("SELECT * FROM light_pins")}


def set_pin(c: sqlite3.Connection, project_id: int, lat, lon, ref: Optional[Tuple[float, float]], user: str, ip: str = "") -> Tuple[float, float]:
    """Store the plant's position for a project; `ref` is the data file's point (None when only the city is known)."""
    try:
        lat, lon = round(float(lat), 6), round(float(lon), 6)
    except (TypeError, ValueError):
        raise Invalid("position missing")
    if not (MX_BOX[0] <= lat <= MX_BOX[1] and MX_BOX[2] <= lon <= MX_BOX[3]):
        raise Invalid("the position is not in Mexico")
    if ref is not None and _km(ref[0], ref[1], lat, lon) > MAX_MOVE_KM:
        raise Invalid(f"the pin moved more than {MAX_MOVE_KM:.0f} km from the recorded park")
    old = c.execute("SELECT lat, lon FROM light_pins WHERE project_id=?", (project_id,)).fetchone()
    was = f"{old['lat']},{old['lon']}" if old else (f"{ref[0]},{ref[1]} (data file)" if ref else "none")
    c.execute("INSERT INTO light_pins (project_id, lat, lon, username, ts_utc) VALUES (?,?,?,?,?) "
              "ON CONFLICT(project_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, username=excluded.username, ts_utc=excluded.ts_utc",
              (project_id, lat, lon, user, now_utc()))
    _audit(c, user, ip, "light_pin", f"P{project_id}", f"{lat},{lon} was {was}")
    c.commit()
    return lat, lon


def clear_pin(c: sqlite3.Connection, project_id: int, user: str, ip: str = "") -> bool:
    old = c.execute("SELECT lat, lon FROM light_pins WHERE project_id=?", (project_id,)).fetchone()
    if not old:
        return False
    c.execute("DELETE FROM light_pins WHERE project_id=?", (project_id,))
    _audit(c, user, ip, "light_pin_reset", f"P{project_id}", f"was {old['lat']},{old['lon']}")
    c.commit()
    return True


def apply_pins(ds, moved: Dict[int, sqlite3.Row]):
    """The dataset with the hand-placed positions (geo = building); the same object when nothing was moved."""
    if not moved:
        return ds
    from dataclasses import replace
    ps = [replace(p, lat=moved[p.id]["lat"], lon=moved[p.id]["lon"], geo="building") if p.id in moved else p for p in ds.projects]
    return replace(ds, projects=ps)
