"""Preventive maintenance and cleaning plan of the Prologis platform (v333).

Tomasz, 28 Oct 2026: "calendar for preventive maintenance and cleaning".

* A plan task is a recurring job (module cleaning, thermography, visual and
  mechanical inspection, electrical tests, inverter service, roof and
  drainage check, other) for one site or for every operating site, every N
  months from a first due date.
* Occurrences are generated, never stored: due = first_due + k * every
  months, per site. Only what people DO is stored, one row per occurrence
  (pm_visits): a planned date, the done date, the linked ticket, a note. So
  changing a task's rhythm never leaves orphan rows behind, and the history
  of what was done stays.
* Status of an occurrence: done (done date set), planned (planned date set),
  overdue (due date passed, not done), due (in the future).
* The calendar, the year planner and the iCalendar export all read the same
  ``occurrences`` list.

Pure functions plus three small table functions; rows live in the
server-only prologis.db. Every change writes an audit row (store.audit,
done by the caller).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
  title TEXT NOT NULL, every_months INTEGER NOT NULL, first_due TEXT NOT NULL,
  duration_days INTEGER NOT NULL DEFAULT 1, notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pm_visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, site_code TEXT NOT NULL, due TEXT NOT NULL,
  planned TEXT NOT NULL DEFAULT '', done TEXT NOT NULL DEFAULT '', ticket_id INTEGER, note TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '',
  UNIQUE (task_id, site_code, due));
"""

# kind -> (English, Spanish, colour). Colours are not green / amber / red:
# those mean status on the platform.
KINDS = {
    "cleaning": ("Module cleaning", "Limpieza de módulos", "#2f6fdb"),
    "thermography": ("Thermography", "Termografía", "#9b59d0"),
    "inspection": ("Visual and mechanical inspection", "Inspección visual y mecánica", "#13a3b5"),
    "electrical": ("Electrical tests (IEC 62446-1)", "Pruebas eléctricas (IEC 62446-1)", "#a07a3c"),
    "inverter": ("Inverter service", "Servicio de inversores", "#e05aa8"),
    "roof": ("Roof and drainage check", "Revisión de techo y drenaje", "#4a5d73"),
    "other": ("Other", "Otro", "#6b7480"),
}
FREQUENCIES = (1, 2, 3, 4, 6, 12, 24)
DONE, PLANNED, OVERDUE, DUE = "done", "planned", "overdue", "due"
STATUS_TEXT = {DONE: ("done", "hecho"), PLANNED: ("planned", "programado"),
               OVERDUE: ("overdue", "vencido"), DUE: ("due", "pendiente")}


class PlanError(ValueError):
    pass


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_months(d: dt.date, n: int) -> dt.date:
    """Same day n months later; the 31st becomes the month's last day. Pure."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(d.day, last))


def _date(s: str, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(s).strip())
    except ValueError as e:
        raise PlanError(f"{what}: not a date (YYYY-MM-DD)") from e


@dataclass
class Occurrence:
    task_id: int
    kind: str
    title: str
    site_code: str
    due: dt.date
    duration_days: int
    planned: Optional[dt.date] = None
    done: Optional[dt.date] = None
    ticket_id: Optional[int] = None
    note: str = ""
    status: str = DUE

    @property
    def day(self) -> dt.date:
        """The date the calendar shows: done, else planned, else due."""
        return self.done or self.planned or self.due

    @property
    def key(self) -> str:
        return f"{self.task_id}|{self.site_code}|{self.due.isoformat()}"


def status_of(due: dt.date, planned: Optional[dt.date], done: Optional[dt.date], today: dt.date) -> str:
    """Pure."""
    if done:
        return DONE
    if planned:
        return OVERDUE if planned < today and due < today else PLANNED
    return OVERDUE if due < today else DUE


def occurrences(tasks: Iterable[dict], visits: Iterable[dict], sites: Sequence[str],
                start: dt.date, end: dt.date, today: dt.date) -> List[Occurrence]:
    """Every occurrence whose shown day or due date falls in [start, end].
    tasks: rows of pm_tasks (active ones are used); visits: rows of
    pm_visits; sites: the operating site codes (a task for '' = every
    one of them). Pure."""
    vis = {(int(v["task_id"]), v["site_code"], v["due"]): v for v in visits}
    out = []
    for t in tasks:
        if not int(t.get("active", 1)):
            continue
        first = _date(t["first_due"], "first due")
        every = int(t["every_months"])
        targets = [t["site_code"]] if t.get("site_code") else list(sites)
        k = 0
        while True:
            due = add_months(first, every * k)
            k += 1
            if due > end + dt.timedelta(days=62):     # a planned date may move an occurrence into the window
                break
            for sc in targets:
                v = vis.get((int(t["id"]), sc, due.isoformat())) or {}
                planned = _date(v["planned"], "planned") if v.get("planned") else None
                done = _date(v["done"], "done") if v.get("done") else None
                o = Occurrence(int(t["id"]), t["kind"], t["title"], sc, due, int(t.get("duration_days") or 1),
                               planned, done, v.get("ticket_id"), v.get("note") or "")
                o.status = status_of(due, planned, done, today)
                if start <= o.day <= end or (o.status == OVERDUE and o.due <= end):
                    out.append(o)
    out.sort(key=lambda o: (o.day, o.site_code, o.kind, o.task_id))
    return out


def summary(occ: Sequence[Occurrence], today: dt.date) -> Dict[str, int]:
    """Tiles: overdue, next 30 days, done this year, planned. Pure."""
    soon = today + dt.timedelta(days=30)
    return {
        "overdue": sum(1 for o in occ if o.status == OVERDUE),
        "next30": sum(1 for o in occ if o.status in (DUE, PLANNED) and today <= o.day <= soon),
        "done_year": sum(1 for o in occ if o.status == DONE and o.done and o.done.year == today.year),
        "planned": sum(1 for o in occ if o.status == PLANNED),
    }


def month_grid(year: int, month: int) -> List[List[Optional[dt.date]]]:
    """Weeks (Monday first) of a month, None outside it. Pure."""
    first = dt.date(year, month, 1)
    nxt = add_months(first, 1)
    days = [first + dt.timedelta(days=i) for i in range((nxt - first).days)]
    weeks, week = [], [None] * first.weekday()
    for d in days:
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return weeks


def title_of(title: str, kind: str, lang: str = "en") -> str:
    """The task's own title, or the name of its kind in the language. Pure."""
    if (title or "").strip():
        return title.strip()
    en, es, _c = KINDS.get(kind, KINDS["other"])
    return es if lang == "es" else en


def ics(occ: Sequence[Occurrence], site_names: Dict[str, str], host: str, lang: str = "en") -> str:
    """iCalendar (RFC 5545) of the occurrences: all-day events, stable UIDs,
    so a calendar app updates instead of duplicating. Pure."""
    def esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ARGIA//Prologis maintenance plan//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:" + esc("Maintenance plan" if lang == "en" else "Plan de mantenimiento")]
    for o in occ:
        if o.status == DONE:
            continue
        kind = KINDS.get(o.kind, KINDS["other"])
        st = STATUS_TEXT[o.status][0 if lang == "en" else 1]
        start = o.planned or o.due
        lines += ["BEGIN:VEVENT", f"UID:pm-{o.task_id}-{o.site_code}-{o.due.strftime('%Y%m%d')}@{host}",
                  f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(start + dt.timedelta(days=max(1, o.duration_days))).strftime('%Y%m%d')}",
                  "SUMMARY:" + esc(f"{title_of(o.title, o.kind, lang)} - {site_names.get(o.site_code, o.site_code)}"),
                  "DESCRIPTION:" + esc(f"{kind[0] if lang == 'en' else kind[1]} · {st} · due {o.due.isoformat()}"),
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------------ store
def tasks(c: sqlite3.Connection, active_only: bool = False) -> List[dict]:
    ensure(c)
    q = "SELECT * FROM pm_tasks" + (" WHERE active=1" if active_only else "") + " ORDER BY kind, site_code, id"
    return [dict(r) for r in c.execute(q)]


def visits(c: sqlite3.Connection) -> List[dict]:
    ensure(c)
    return [dict(r) for r in c.execute("SELECT * FROM pm_visits")]


def add_task(c: sqlite3.Connection, site_code: str, kind: str, title: str, every_months: int, first_due: str,
             duration_days: int, notes: str, user: str, site_codes: Sequence[str]) -> int:
    ensure(c)
    site_code = (site_code or "").strip()
    if site_code and site_code not in site_codes:
        raise PlanError("unknown site")
    if kind not in KINDS:
        raise PlanError("unknown kind")
    title = (title or "").strip()                   # empty = the kind's name, in the reader's language
    if int(every_months) not in FREQUENCIES:
        raise PlanError("frequency must be one of " + ", ".join(map(str, FREQUENCIES)) + " months")
    first = _date(first_due, "first due")
    days = int(duration_days or 1)
    if not 1 <= days <= 30:
        raise PlanError("duration must be 1 to 30 days")
    cur = c.execute("INSERT INTO pm_tasks (site_code, kind, title, every_months, first_due, duration_days, notes, created_by, created_utc)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (site_code, kind, title[:120], int(every_months), first.isoformat(), days,
                                                    (notes or "")[:500], user, now_utc()))
    c.commit()
    return int(cur.lastrowid)


def set_active(c: sqlite3.Connection, task_id: int, active: bool) -> None:
    ensure(c)
    if c.execute("UPDATE pm_tasks SET active=? WHERE id=?", (1 if active else 0, int(task_id))).rowcount != 1:
        raise PlanError("no such task")
    c.commit()


def record(c: sqlite3.Connection, task_id: int, site_code: str, due: str, user: str, *,
           planned: Optional[str] = None, done: Optional[str] = None, ticket_id: Optional[int] = None,
           note: Optional[str] = None, today: Optional[dt.date] = None) -> None:
    """Set the planned date, the done date, the ticket or the note of one
    occurrence (None = leave as is, '' = clear). A done date in the future
    is refused; the occurrence must belong to the task."""
    ensure(c)
    t = c.execute("SELECT * FROM pm_tasks WHERE id=?", (int(task_id),)).fetchone()
    if t is None:
        raise PlanError("no such task")
    d = _date(due, "due")
    first, every = _date(t["first_due"], "first due"), int(t["every_months"])
    k, ok = 0, False
    while True:
        x = add_months(first, every * k)
        if x == d:
            ok = True
        if x >= d:
            break
        k += 1
    if not ok:
        raise PlanError("not a due date of this task")
    if t["site_code"] and t["site_code"] != site_code:
        raise PlanError("not a site of this task")
    today = today or dt.date.today()
    if planned:
        _date(planned, "planned")
    if done:
        if _date(done, "done") > today:
            raise PlanError("a done date cannot be in the future")
    c.execute("INSERT OR IGNORE INTO pm_visits (task_id, site_code, due) VALUES (?,?,?)", (int(task_id), site_code, d.isoformat()))
    sets, vals = [], []
    for col, v in (("planned", planned), ("done", done), ("note", note)):
        if v is not None:
            sets.append(f"{col}=?")
            vals.append(str(v)[:500])
    if ticket_id is not None:
        sets.append("ticket_id=?")
        vals.append(int(ticket_id) if ticket_id else None)
    sets += ["updated_by=?", "updated_utc=?"]
    vals += [user, now_utc()]
    c.execute(f"UPDATE pm_visits SET {', '.join(sets)} WHERE task_id=? AND site_code=? AND due=?",
              vals + [int(task_id), site_code, d.isoformat()])
    c.commit()
