"""Maintenance tickets — the operational record behind an alert (v226).

Tomasz, 2026-09-07: "log maintenance tickets, connect them to plants,
notify the people on the ticket, track progress, comments and
attachments, open → in progress → verification → resolved; and when a
warning already has an open ticket, share the progress instead of
repeating the warning."

Design (see docs/MAINTENANCE_TICKETS.md):

* Monitoring detects (alert ledger, alert_key). A ticket is the record
  of what ARGIA does about it; ``ticket_alert`` links the two so the
  morning mail shows the ticket's progress instead of the warning, and
  new occurrences of the alert land on the ticket's timeline.
* ONE timeline per ticket (``ticket_event``): creation, comments,
  status changes, assignments, attachments, alert occurrences, system
  notes — in order, with the actor. Comments are not a separate thing.
* Lifecycle NEW → IN_PROGRESS → WAITING → VERIFICATION → RESOLVED →
  CLOSED (plus REOPEN from VERIFICATION/RESOLVED/CLOSED back to
  IN_PROGRESS). RESOLVED and CLOSED are separate on purpose: resolved
  = the fix is confirmed by data; closed = the paperwork is done.
* Priority P1–P4 is independent of status and carries the SLA targets.
* Numbering TK-<PLANT>-<NNNN>: anyone reading "TK-NL1-0007" knows the
  plant.

Everything in this module is pure (no I/O) except the loaders at the
bottom, which the Flask app and the daily job call.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ------------------------------------------------------------ lifecycle
STATUSES: List[Tuple[str, str, str]] = [
    ("NEW", "New", "Nuevo"),
    ("IN_PROGRESS", "In progress", "En curso"),
    ("WAITING", "Waiting", "En espera"),
    ("VERIFICATION", "Verification", "Verificación"),
    ("RESOLVED", "Resolved", "Resuelto"),
    ("CLOSED", "Closed", "Cerrado"),
]
STATUS_LABEL = {k: en for k, en, _es in STATUSES}
STATUS_LABEL_ES = {k: es for k, _en, es in STATUSES}
OPEN_STATUSES = ("NEW", "IN_PROGRESS", "WAITING", "VERIFICATION")
"""A ticket in one of these is 'open': it appears on the dashboard, its
alert is 'in hand' in the morning mail."""

TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "NEW": ("IN_PROGRESS", "WAITING", "CLOSED"),
    "IN_PROGRESS": ("WAITING", "VERIFICATION", "RESOLVED"),
    "WAITING": ("IN_PROGRESS", "VERIFICATION"),
    "VERIFICATION": ("RESOLVED", "IN_PROGRESS"),
    "RESOLVED": ("CLOSED", "IN_PROGRESS"),
    "CLOSED": ("IN_PROGRESS",),
}
"""What a person may pick next; going back to IN_PROGRESS from
VERIFICATION / RESOLVED / CLOSED is the re-open."""

STATUS_STAMP = {"IN_PROGRESS": "started_at", "VERIFICATION": "verification_at",
                "RESOLVED": "resolved_at", "CLOSED": "closed_at"}
"""Timestamp column stamped on the first entry into a status."""


def can_transition(cur: str, new: str) -> bool:
    return new in TRANSITIONS.get(cur, ())


def is_open(status: str) -> bool:
    return status in OPEN_STATUSES


# ------------------------------------------------------------- priority
PRIORITIES: List[Tuple[str, str, str, int, int]] = [
    # code, label EN, label ES, acknowledge (min), resolve target (h)
    ("P1", "Critical", "Crítica", 15, 4),
    ("P2", "High", "Alta", 60, 24),
    ("P3", "Medium", "Media", 240, 72),
    ("P4", "Low", "Baja", 1440, 0),
]
PRIORITY_LABEL = {p[0]: p[1] for p in PRIORITIES}
SLA_ACK_MIN = {p[0]: p[3] for p in PRIORITIES}
SLA_RESOLVE_H = {p[0]: p[4] for p in PRIORITIES}     # 0 = planned, no target

PRIORITY_FOR_SEVERITY = {"CRITICAL": "P2", "WARNING": "P3", "INFO": "P4"}
"""Default priority when a ticket is opened from an alert: a CRITICAL
alert (energy lost / unit off) is P2 — P1 stays a human decision (plant
outage, safety)."""

# ------------------------------------------------------------ categories
CATEGORIES: List[Tuple[str, str, str]] = [
    ("inverter/offline", "Inverter · offline", "Inversor · fuera de servicio"),
    ("inverter/derating", "Inverter · derating / heat", "Inversor · derrateo / temperatura"),
    ("inverter/fault", "Inverter · fault code", "Inversor · código de falla"),
    ("inverter/comms", "Inverter · communication", "Inversor · comunicación"),
    ("array/string", "PV array · string", "Arreglo FV · string"),
    ("array/soiling", "PV array · soiling / cleaning", "Arreglo FV · suciedad / limpieza"),
    ("array/module", "PV array · module / connector", "Arreglo FV · módulo / conector"),
    ("electrical/protection", "Electrical · breaker / protection", "Eléctrico · interruptor / protección"),
    ("electrical/transformer", "Electrical · transformer / MV", "Eléctrico · transformador / MT"),
    ("metering", "Metering", "Medición"),
    ("comms/site", "Communication · site / gateway", "Comunicación · sitio / gateway"),
    ("comms/vendor", "Communication · vendor cloud", "Comunicación · nube del fabricante"),
    ("structural", "Structural / roof", "Estructura / techo"),
    ("grid", "Grid / CFE", "Red / CFE"),
    ("preventive", "Preventive maintenance", "Mantenimiento preventivo"),
    ("customer", "Customer request", "Solicitud del cliente"),
    ("other", "Other", "Otro"),
]
CATEGORY_LABEL = {c[0]: c[1] for c in CATEGORIES}

CATEGORY_FOR_METRIC = {
    "inverter_temp_high": "inverter/derating", "inverter_fault": "inverter/fault",
    "inverter_silent": "inverter/comms", "inverter_relative": "inverter/offline",
    "string_fault": "array/string", "plant_offline": "electrical/protection",
    "data_stale": "comms/site", "energy_daily_pct": "other", "plant_twin_yield": "other",
}

ROOT_CAUSES: List[Tuple[str, str]] = [
    ("equipment", "Equipment failure"), ("installation", "Installation issue"),
    ("manufacturer", "Manufacturer / warranty"), ("environment", "Environmental (heat, dust, weather)"),
    ("grid", "Grid / CFE"), ("customer", "Customer side"), ("comms", "Communication"),
    ("design", "Design issue"), ("unknown", "Unknown"),
]

ROOT_CAUSE_ES = {
    "equipment": "Falla de equipo", "installation": "Problema de instalación", "manufacturer": "Fabricante / garantía",
    "environment": "Ambiental (calor, polvo, clima)", "grid": "Red / CFE", "customer": "Lado del cliente",
    "comms": "Comunicación", "design": "Problema de diseño", "unknown": "Desconocida",
}

EVENT_KINDS = ("created", "comment", "status", "assign", "follow", "unfollow",
               "attachment", "alert", "priority", "system", "resolution")

# ------------------------------------------------------------ numbering
NUMBER_RE = re.compile(r"^TK-([A-Z0-9]{3,6})-(\d{4,})$")


def make_number(plant_key: str, seq: int) -> str:
    """TK-NL1-0007 — the plant is in the number."""
    return f"TK-{(plant_key or 'FLEET').upper()}-{int(seq):04d}"


def parse_number(number: str) -> Optional[Tuple[str, int]]:
    m = NUMBER_RE.match((number or "").strip().upper())
    return (m.group(1), int(m.group(2))) if m else None


# -------------------------------------------------------------- records
@dataclass
class Ticket:
    id: int
    number: str
    plant_key: str
    inverter_sn: str
    title: str
    description: str
    category: str
    priority: str
    status: str
    created_by: str
    assigned_to: str
    created_at: str
    updated_at: str
    started_at: str = ""
    verification_at: str = ""
    resolved_at: str = ""
    closed_at: str = ""
    root_cause: str = ""
    resolution: str = ""
    lost_kwh: Optional[float] = None
    followers: List[str] = field(default_factory=list)
    alert_keys: List[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return is_open(self.status)


@dataclass
class Event:
    id: int
    ticket_id: int
    ts: str
    actor: str
    kind: str
    body: str
    meta: dict = field(default_factory=dict)


# ------------------------------------------------------------ helpers
def parse_ts(s: str) -> Optional[dt.datetime]:
    """psql's '2026-09-06 14:00:00.12+00' (or ISO 'T' form) -> aware UTC
    datetime; None when unparseable. Python 3.10-safe (it rejects '+00')."""
    raw = (s or "").strip().replace("T", " ")
    if not raw:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$", raw)
    if not m:
        try:
            d = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    day, hms, off = m.groups()
    if not off or off == "Z":
        tz = dt.timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        hh, mm = int(digits[:2]), int(digits[2:4] or 0)
        tz = dt.timezone(sign * dt.timedelta(hours=hh, minutes=mm))
    return dt.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)


def participants(t: Ticket) -> List[str]:
    """Everyone on the ticket: creator, assignee, followers — deduplicated,
    order kept."""
    out: List[str] = []
    for u in [t.created_by, t.assigned_to] + list(t.followers):
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)
    return out


def recipients(t: Ticket, actor: str) -> List[str]:
    """Who gets the notification for an event: every participant except
    the person who did it."""
    return [u for u in participants(t) if u != (actor or "").strip()]


def age(t: Ticket, now: dt.datetime) -> dt.timedelta:
    opened = parse_ts(t.created_at)
    if opened is None:
        return dt.timedelta(0)
    return max(dt.timedelta(0), now - opened)


def fmt_age(d: dt.timedelta) -> str:
    m = int(d.total_seconds() // 60)
    if m < 60:
        return f"{m} min"
    h = m // 60
    if h < 24:
        return f"{h} h {m % 60:02d}"
    return f"{h // 24} d {h % 24} h"


def sla_state(t: Ticket, now: dt.datetime) -> Tuple[str, str]:
    """(state, text): 'ok' | 'due' | 'breached' | 'none' against the
    priority's resolve target; resolved/closed tickets report what
    happened."""
    target_h = SLA_RESOLVE_H.get(t.priority, 0)
    if not target_h:
        return "none", "planned work, no SLA clock"
    a = age(t, now)
    if not t.open:
        done, opened = parse_ts(t.resolved_at or t.closed_at or t.updated_at), parse_ts(t.created_at)
        took = (done - opened) if (done is not None and opened is not None) else a
        ok = took.total_seconds() <= target_h * 3600
        return ("ok" if ok else "breached"), f"resolved in {fmt_age(took)} (target {target_h} h)"
    left = target_h * 3600 - a.total_seconds()
    if left < 0:
        return "breached", f"{fmt_age(dt.timedelta(seconds=-left))} over the {target_h} h target"
    if left < target_h * 3600 * 0.25:
        return "due", f"{fmt_age(dt.timedelta(seconds=left))} left of {target_h} h"
    return "ok", f"{fmt_age(dt.timedelta(seconds=left))} left of {target_h} h"


def title_for_alert(metric: str, plant_name: str, inverter_label: str) -> str:
    from argia.alerts import naming
    who = f"{plant_name} · {inverter_label}" if inverter_label else plant_name
    return f"{who}: {naming.phrase(metric)}"


def progress_line(t: Ticket, last_comment: Optional[Event], now: dt.datetime) -> str:
    """One line for the morning mail: 'TK-NL1-0007 · In progress · Juan ·
    2 d 4 h · last update: Filters replaced, verifying tomorrow'."""
    parts = [t.number, STATUS_LABEL.get(t.status, t.status)]
    if t.assigned_to:
        parts.append(t.assigned_to)
    parts.append(fmt_age(age(t, now)))
    line = " · ".join(parts)
    if last_comment and last_comment.body:
        body = last_comment.body.strip().replace("\n", " ")
        line += f" — last update: {body[:160]}{'…' if len(body) > 160 else ''}"
    return line


# ------------------------------------------------------------------ SQL
ENSURE_SQL = """CREATE TABLE IF NOT EXISTS ticket (
    id              serial PRIMARY KEY,
    number          text NOT NULL UNIQUE,
    plant_key       text NOT NULL,
    inverter_sn     text NOT NULL DEFAULT '',
    title           text NOT NULL,
    description     text NOT NULL DEFAULT '',
    category        text NOT NULL DEFAULT 'other',
    priority        text NOT NULL DEFAULT 'P3',
    status          text NOT NULL DEFAULT 'NEW',
    created_by      text NOT NULL,
    assigned_to     text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    verification_at timestamptz,
    resolved_at     timestamptz,
    closed_at       timestamptz,
    root_cause      text NOT NULL DEFAULT '',
    resolution      text NOT NULL DEFAULT '',
    lost_kwh        numeric(12,1)
);
CREATE INDEX IF NOT EXISTS idx_ticket_plant_status ON ticket (plant_key, status);
CREATE TABLE IF NOT EXISTS ticket_follower (
    ticket_id int NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    username  text NOT NULL,
    PRIMARY KEY (ticket_id, username)
);
CREATE TABLE IF NOT EXISTS ticket_event (
    id        serial PRIMARY KEY,
    ticket_id int NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    ts        timestamptz NOT NULL DEFAULT now(),
    actor     text NOT NULL DEFAULT '',
    kind      text NOT NULL,
    body      text NOT NULL DEFAULT '',
    meta      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_ticket_event_ticket ON ticket_event (ticket_id, ts);
CREATE TABLE IF NOT EXISTS ticket_attachment (
    id          serial PRIMARY KEY,
    ticket_id   int NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    event_id    int,
    filename    text NOT NULL,
    stored_as   text NOT NULL,
    bytes       int NOT NULL DEFAULT 0,
    mime        text NOT NULL DEFAULT '',
    uploaded_by text NOT NULL DEFAULT '',
    uploaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ticket_alert (
    ticket_id  int NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    alert_key  text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    occurrences int NOT NULL DEFAULT 1,
    PRIMARY KEY (ticket_id, alert_key)
);
CREATE INDEX IF NOT EXISTS idx_ticket_alert_key ON ticket_alert (alert_key);
"""


def _txt(s) -> str:
    return "'" + str(s if s is not None else "").replace("'", "''") + "'"


def _ts(s) -> str:
    return "NULL" if not s else _txt(s)


TICKET_COLS = ("id", "number", "plant_key", "inverter_sn", "title", "description", "category",
               "priority", "status", "created_by", "assigned_to", "created_at", "updated_at",
               "started_at", "verification_at", "resolved_at", "closed_at", "root_cause",
               "resolution", "lost_kwh")
SELECT_TICKETS = ("SELECT " + ", ".join(f"{c}::text" if c in ("created_at", "updated_at", "started_at", "verification_at",
                                                              "resolved_at", "closed_at") else c for c in TICKET_COLS)
                  + ", (SELECT string_agg(username, ',' ORDER BY username) FROM ticket_follower f WHERE f.ticket_id = t.id) AS followers"
                  + ", (SELECT string_agg(alert_key, ',' ORDER BY alert_key) FROM ticket_alert a WHERE a.ticket_id = t.id) AS alert_keys"
                  + " FROM ticket t")


def next_number_sql(plant_key: str) -> str:
    """The next TK-<plant>-NNNN: one SELECT, the app retries on a unique
    clash (two people opening a ticket in the same second)."""
    pk = (plant_key or "FLEET").upper()
    return (f"SELECT coalesce(max(substring(number from '\\d+$')::int), 0) + 1 FROM ticket"
            f" WHERE number LIKE {_txt('TK-' + pk + '-%')};")


def insert_ticket_sql(number: str, plant_key: str, inverter_sn: str, title: str, description: str,
                      category: str, priority: str, created_by: str, assigned_to: str) -> str:
    return ("INSERT INTO ticket (number, plant_key, inverter_sn, title, description, category, priority,"
            " status, created_by, assigned_to) VALUES ("
            f"{_txt(number)}, {_txt(plant_key.upper())}, {_txt(inverter_sn)}, {_txt(title)}, {_txt(description)},"
            f" {_txt(category)}, {_txt(priority)}, 'NEW', {_txt(created_by)}, {_txt(assigned_to)}) RETURNING id;")


def event_sql(ticket_id: int, actor: str, kind: str, body: str = "", meta: Optional[dict] = None) -> str:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    m = json.dumps(meta or {}, ensure_ascii=False)
    return (f"INSERT INTO ticket_event (ticket_id, actor, kind, body, meta) VALUES"
            f" ({int(ticket_id)}, {_txt(actor)}, {_txt(kind)}, {_txt(body)}, {_txt(m)}::jsonb) RETURNING id;"
            f"\nUPDATE ticket SET updated_at = now() WHERE id = {int(ticket_id)};")


def status_sql(ticket_id: int, new_status: str) -> str:
    stamp = STATUS_STAMP.get(new_status)
    extra = f", {stamp} = coalesce({stamp}, now())" if stamp else ""
    return f"UPDATE ticket SET status = {_txt(new_status)}, updated_at = now(){extra} WHERE id = {int(ticket_id)};"


def resolution_sql(ticket_id: int, root_cause: str, resolution: str, lost_kwh: Optional[float]) -> str:
    lk = "NULL" if lost_kwh is None else repr(round(float(lost_kwh), 1))
    return (f"UPDATE ticket SET root_cause = {_txt(root_cause)}, resolution = {_txt(resolution)},"
            f" lost_kwh = {lk}, updated_at = now() WHERE id = {int(ticket_id)};")


def assign_sql(ticket_id: int, username: str) -> str:
    return f"UPDATE ticket SET assigned_to = {_txt(username)}, updated_at = now() WHERE id = {int(ticket_id)};"


def priority_sql(ticket_id: int, priority: str) -> str:
    return f"UPDATE ticket SET priority = {_txt(priority)}, updated_at = now() WHERE id = {int(ticket_id)};"


def follow_sql(ticket_id: int, username: str, follow: bool = True) -> str:
    if follow:
        return (f"INSERT INTO ticket_follower (ticket_id, username) VALUES ({int(ticket_id)}, {_txt(username)})"
                " ON CONFLICT DO NOTHING;")
    return f"DELETE FROM ticket_follower WHERE ticket_id = {int(ticket_id)} AND username = {_txt(username)};"


def link_alert_sql(ticket_id: int, alert_key: str) -> str:
    return (f"INSERT INTO ticket_alert (ticket_id, alert_key) VALUES ({int(ticket_id)}, {_txt(alert_key)})"
            " ON CONFLICT (ticket_id, alert_key) DO UPDATE SET last_seen = now(),"
            " occurrences = ticket_alert.occurrences + 1;")


def attachment_sql(ticket_id: int, event_id: Optional[int], filename: str, stored_as: str,
                   nbytes: int, mime: str, uploaded_by: str) -> str:
    ev = "NULL" if event_id is None else str(int(event_id))
    return (f"INSERT INTO ticket_attachment (ticket_id, event_id, filename, stored_as, bytes, mime, uploaded_by)"
            f" VALUES ({int(ticket_id)}, {ev}, {_txt(filename)}, {_txt(stored_as)}, {int(nbytes)}, {_txt(mime)},"
            f" {_txt(uploaded_by)}) RETURNING id;")


# --------------------------------------------------------------- parsing
def rows_from_csv(text: str) -> List[Dict[str, str]]:
    """psql --csv output -> dicts (free text with newlines survives)."""
    return list(csv.DictReader(io.StringIO(text)))


def ticket_from_row(r: Dict[str, str]) -> Ticket:
    return Ticket(
        id=int(r["id"]), number=r["number"], plant_key=r["plant_key"], inverter_sn=r.get("inverter_sn") or "",
        title=r["title"], description=r.get("description") or "", category=r.get("category") or "other",
        priority=r.get("priority") or "P3", status=r.get("status") or "NEW", created_by=r.get("created_by") or "",
        assigned_to=r.get("assigned_to") or "", created_at=r.get("created_at") or "", updated_at=r.get("updated_at") or "",
        started_at=r.get("started_at") or "", verification_at=r.get("verification_at") or "",
        resolved_at=r.get("resolved_at") or "", closed_at=r.get("closed_at") or "",
        root_cause=r.get("root_cause") or "", resolution=r.get("resolution") or "",
        lost_kwh=float(r["lost_kwh"]) if r.get("lost_kwh") else None,
        followers=[u for u in (r.get("followers") or "").split(",") if u],
        alert_keys=[k for k in (r.get("alert_keys") or "").split(",") if k],
    )


def event_from_row(r: Dict[str, str]) -> Event:
    try:
        meta = json.loads(r.get("meta") or "{}")
    except ValueError:
        meta = {}
    return Event(id=int(r["id"]), ticket_id=int(r["ticket_id"]), ts=r.get("ts") or "", actor=r.get("actor") or "",
                 kind=r.get("kind") or "system", body=r.get("body") or "", meta=meta if isinstance(meta, dict) else {})


# ------------------------------------------------- alert <-> ticket bridge
@dataclass(frozen=True)
class TicketBrief:
    """What the mails and pages need to know about a ticket behind an
    alert key."""
    number: str
    status: str
    priority: str
    assigned_to: str
    created_at: str
    last_update: str        # newest comment / resolution text, '' when none
    open: bool


def briefs_by_alert(tickets: Iterable[Ticket], last_comment: Dict[int, str]) -> Dict[str, TicketBrief]:
    """{alert_key: TicketBrief} for OPEN tickets (an alert with a closed
    ticket is a fresh problem, not 'in hand')."""
    out: Dict[str, TicketBrief] = {}
    for t in tickets:
        if not t.open:
            continue
        b = TicketBrief(t.number, t.status, t.priority, t.assigned_to, t.created_at,
                        last_comment.get(t.id, ""), True)
        for k in t.alert_keys:
            out.setdefault(k, b)
    return out


# --------------------------------------------------------------- loaders
def load_open_briefs(rows_csv=None) -> Dict[str, TicketBrief]:
    """{alert_key: brief} from PostgreSQL; {} on any failure (a mail
    without ticket lines beats no mail). The daily job and the page
    generators call this."""
    from argia.store.pgq import psql_csv
    rows_csv = rows_csv or psql_csv
    try:
        tks = [ticket_from_row(r) for r in rows_from_csv(rows_csv(
            SELECT_TICKETS + " WHERE status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION');"))]
        last = {}
        for r in rows_from_csv(rows_csv(
                "SELECT DISTINCT ON (ticket_id) ticket_id, body FROM ticket_event"
                " WHERE kind IN ('comment','resolution') AND body <> '' ORDER BY ticket_id, ts DESC;")):
            last[int(r["ticket_id"])] = r["body"]
        return briefs_by_alert(tks, last)
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------- v227 additions
def is_email(identity: str) -> bool:
    """A participant is a portal username or, since v227, a bare e-mail
    address (an external technician, a customer contact)."""
    s = (identity or "").strip()
    return "@" in s and " " not in s and "." in s.split("@", 1)[1]


def valid_email(s: str) -> str:
    s = (s or "").strip().lower()
    return s if is_email(s) and len(s) <= 120 else ""


VERIFY_QUIET_DAYS = 2
"""A ticket in VERIFICATION is RESOLVED by the data when none of its
linked alerts has been open or touched for this many days."""


def verify_decision(t: Ticket, ledger_open_keys: Iterable[str], touched_keys: Iterable[str],
                    last_seen: Dict[str, dt.datetime], now: dt.datetime,
                    quiet_days: int = VERIFY_QUIET_DAYS) -> Tuple[Optional[str], str]:
    """The data's verdict on a ticket (pure):

    * VERIFICATION + a linked alert recurred today → ("IN_PROGRESS",
      reason) — the fix did not hold;
    * VERIFICATION + no linked alert open and none seen for
      ``quiet_days`` → ("RESOLVED", reason);
    * VERIFICATION + linked alerts still open / recently seen → (None,
      "waiting: …") — keep watching;
    * any other status, or no linked alerts → (None, "") — nothing for
      the data to say (a ticket without alerts is resolved by people).
    """
    if t.status != "VERIFICATION" or not t.alert_keys:
        return None, ""
    open_keys = set(ledger_open_keys)
    touched = set(touched_keys)
    hit = [k for k in t.alert_keys if k in touched]
    if hit:
        return "IN_PROGRESS", f"monitoring saw the alert again ({', '.join(hit)}) — back to In progress"
    still = [k for k in t.alert_keys if k in open_keys]
    if still:
        return None, f"waiting: {len(still)} linked alert(s) still open in the ledger"
    recent = []
    for k in t.alert_keys:
        seen = last_seen.get(k)
        if seen is not None and (now - seen) < dt.timedelta(days=quiet_days):
            recent.append(k)
    if recent:
        return None, f"waiting: last occurrence less than {quiet_days} days ago"
    return "RESOLVED", f"no linked alert open or seen for {quiet_days} days — resolved by the data"


def matching_ticket(open_tickets: Iterable[Ticket], plant_key: str, inverter_sn: str) -> Optional[Ticket]:
    """The open ticket on the same asset (plant + inverter, or the plant
    itself for a plant-level alert), newest first. Pure."""
    cands = [t for t in open_tickets if t.plant_key.upper() == (plant_key or "").upper()
             and (t.inverter_sn or "") == (inverter_sn or "")]
    if not cands:
        return None
    return sorted(cands, key=lambda t: t.created_at, reverse=True)[0]


# --------------------------------------------------------- reply by mail
REPLY_NUMBER_RE = re.compile(r"\[(TK-[A-Z0-9]{3,6}-\d{4,})\]")
_QUOTE_HEAD = re.compile(r"^(On .+wrote:|El .+escribi[oó]:|-{3,}\s*Original Message\s*-{3,}|From: .+|De: .+|_{5,})\s*$", re.I)


def reply_ticket_number(subject: str) -> Optional[str]:
    m = REPLY_NUMBER_RE.search(subject or "")
    return m.group(1) if m else None


def strip_reply(body: str) -> str:
    """The person's own words: everything before the quoted mail, minus
    '>' lines and signature separators. Pure."""
    out: List[str] = []
    for ln in (body or "").replace("\r", "").split("\n"):
        if _QUOTE_HEAD.match(ln.strip()) or ln.rstrip() in ("--", "-- "):
            break
        if ln.lstrip().startswith(">"):
            continue
        out.append(ln.rstrip())
    return "\n".join(out).strip()


# ------------------------------------------------------------- statistics
def status_timeline(tickets: Sequence[Ticket], status_events: Sequence[Event],
                    days: int, now: dt.datetime) -> List[Tuple[dt.date, Dict[str, int]]]:
    """Open tickets by status at the end of each of the last ``days``
    days, replayed from the status events (kind='status', meta from/to)
    and the creation dates. Pure."""
    parse = parse_ts
    created = {t.id: parse(t.created_at) for t in tickets}
    changes: Dict[int, List[Tuple[dt.datetime, str]]] = {}
    for ev in status_events:
        ts = parse(ev.ts)
        if ts is not None and ev.kind == "status" and ev.meta.get("to"):
            changes.setdefault(ev.ticket_id, []).append((ts, ev.meta["to"]))
    out = []
    for back in range(days - 1, -1, -1):
        day = (now - dt.timedelta(days=back)).date()
        end = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
        counts: Dict[str, int] = {}
        for t in tickets:
            c = created.get(t.id)
            if c is None or c > end:
                continue
            status = "NEW"
            for ts, to in sorted(changes.get(t.id, [])):
                if ts <= end:
                    status = to
            if is_open(status):
                counts[status] = counts.get(status, 0) + 1
        out.append((day, counts))
    return out


def stats(tickets: Sequence[Ticket], now: dt.datetime, weeks: int = 8) -> dict:
    """Opened / resolved per week, MTTR of resolved tickets, counts by
    plant, category, priority, over-SLA. Pure."""
    parse = parse_ts
    week_of = lambda d: (d - dt.timedelta(days=d.weekday())).date()  # noqa: E731
    weeks_list = [week_of(now - dt.timedelta(weeks=w)) for w in range(weeks - 1, -1, -1)]
    opened = {w: 0 for w in weeks_list}
    resolved = {w: 0 for w in weeks_list}
    mttr: List[float] = []
    by_plant: Dict[str, int] = {}
    by_cat: Dict[str, int] = {}
    by_prio: Dict[str, int] = {}
    over = 0
    for t in tickets:
        c = parse(t.created_at)
        if c is not None and week_of(c) in opened:
            opened[week_of(c)] += 1
        r = parse(t.resolved_at) if t.resolved_at else None
        if r is not None:
            if week_of(r) in resolved:
                resolved[week_of(r)] += 1
            if c is not None:
                mttr.append((r - c).total_seconds() / 3600.0)
        if t.open:
            by_plant[t.plant_key] = by_plant.get(t.plant_key, 0) + 1
            by_cat[t.category] = by_cat.get(t.category, 0) + 1
            by_prio[t.priority] = by_prio.get(t.priority, 0) + 1
            if sla_state(t, now)[0] == "breached":
                over += 1
    return {"weeks": [(w, opened[w], resolved[w]) for w in weeks_list],
            "mttr_h": round(sum(mttr) / len(mttr), 1) if mttr else None, "n_resolved": len(mttr),
            "by_plant": sorted(by_plant.items(), key=lambda kv: -kv[1]),
            "by_category": sorted(by_cat.items(), key=lambda kv: -kv[1]),
            "by_priority": sorted(by_prio.items()), "over_sla": over,
            "n_open": sum(1 for t in tickets if t.open), "n_total": len(tickets)}


STATUS_COLOR = {"NEW": "#c2554e", "IN_PROGRESS": "#f0a83b", "WAITING": "#9aa3ad", "VERIFICATION": "#05b1a9"}


def status_chart_svg(series: Sequence[Tuple[dt.date, Dict[str, int]]], width: int = 720, height: int = 180) -> str:
    """Stacked area of open tickets by status over time — inline SVG, no
    library. Pure."""
    if not series:
        return ""
    order = ["NEW", "IN_PROGRESS", "WAITING", "VERIFICATION"]
    top = max(1, max(sum(c.values()) for _d, c in series))
    pad_l, pad_r, pad_t, pad_b = 28, 8, 8, 22
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(series)
    x = lambda i: pad_l + (w * i / max(1, n - 1))  # noqa: E731
    y = lambda v: pad_t + h - h * v / top  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" aria-label="open tickets by status">']
    for g in range(0, top + 1, max(1, top // 4)):
        parts.append(f'<line x1="{pad_l}" y1="{y(g):.1f}" x2="{width - pad_r}" y2="{y(g):.1f}" stroke="#eceef0"/>'
                     f'<text x="{pad_l - 4}" y="{y(g) + 4:.1f}" font-size="10" text-anchor="end" fill="#6b7480">{g}</text>')
    base = [0] * n
    for st in order:
        tops = [base[i] + series[i][1].get(st, 0) for i in range(n)]
        if not any(tops[i] - base[i] for i in range(n)):
            base = tops
            continue
        pts = " ".join(f"{x(i):.1f},{y(tops[i]):.1f}" for i in range(n)) + " " + " ".join(f"{x(i):.1f},{y(base[i]):.1f}" for i in range(n - 1, -1, -1))
        parts.append(f'<polygon points="{pts}" fill="{STATUS_COLOR[st]}" fill-opacity=".75"><title>{STATUS_LABEL[st]}</title></polygon>')
        base = tops
    step = max(1, n // 6)
    for i in range(0, n, step):
        parts.append(f'<text x="{x(i):.1f}" y="{height - 6}" font-size="10" text-anchor="middle" fill="#6b7480">{series[i][0].strftime("%d %b")}</text>')
    parts.append("</svg>")
    return "".join(parts)
