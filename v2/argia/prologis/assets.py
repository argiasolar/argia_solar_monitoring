"""Equipment register, warranty claims and spare-parts inventory of the
Prologis platform (v320).

What the Prologis O&M MSA (Schedule A) and ARGIA's proposal ask for:

* Data & Reporting: "Update and manage key data and documentation related
  to the Project's equipment ... Serial numbers / Flash test information /
  Warranties / EPC Spare inventories". Meter and sensor recalibration is
  scheduled (Monitoring). The availability formula weighs every component
  by the DC nameplate behind it, so each row carries ``dc_kw``.
* Maintenance & Repairs: "Track all warranty claims and responses for
  Owner visibility; notify Owner immediately of any warranty claims that
  are denied" and pass through any compensation received. A denied claim
  stays flagged for Prologis until a Prologis manager acknowledges it.
* Onboarding: the EPC spares move to ARGIA's warehouse after inspection;
  damage is reported. Proposal 4.6: a serialized, tracked inventory and a
  "quarterly inventory report with movements and restocking
  recommendations".

Customer data: this module only defines tables and rules; the rows live in
the server-only prologis.db (encrypted volume since v319). Everything that
changes data writes an audit row (store.audit).

Stock is never stored as a number: the balance of a part at a location is
the sum of its movements (in minus out), so the quarterly report and the
live stock always agree and every unit has a history.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL, category TEXT NOT NULL,
  tag TEXT NOT NULL DEFAULT '', make TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  serial TEXT NOT NULL DEFAULT '', qty INTEGER NOT NULL DEFAULT 1, dc_kw REAL, ac_kw REAL,
  installed TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'in_service',
  warranty_by TEXT NOT NULL DEFAULT '', warranty_until TEXT NOT NULL DEFAULT '',
  calib_due TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', replaced_by INTEGER,
  created_by TEXT NOT NULL, created_utc TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '',
  updated_utc TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS equipment_serial ON equipment(make, serial) WHERE serial <> '';
CREATE TABLE IF NOT EXISTS warranty_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE NOT NULL, site_code TEXT NOT NULL,
  equipment_id INTEGER, ticket_id INTEGER, supplier TEXT NOT NULL, supplier_ref TEXT NOT NULL DEFAULT '',
  fault TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'DRAFT', opened_utc TEXT NOT NULL,
  submitted_utc TEXT NOT NULL DEFAULT '', decided_utc TEXT NOT NULL DEFAULT '', closed_utc TEXT NOT NULL DEFAULT '',
  compensation_mxn REAL, passed_through TEXT NOT NULL DEFAULT '', owner_ack_utc TEXT NOT NULL DEFAULT '',
  owner_ack_by TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL, ts_utc TEXT NOT NULL,
  username TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS parts (
  code TEXT PRIMARY KEY, name_en TEXT NOT NULL, name_es TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,
  make TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'pc',
  serialized INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT 'PROLOGIS', active INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS stock_locations (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, transit INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock_min (
  part_code TEXT NOT NULL, loc TEXT NOT NULL, min_qty REAL NOT NULL, PRIMARY KEY (part_code, loc));
CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, day TEXT NOT NULL, part_code TEXT NOT NULL,
  kind TEXT NOT NULL, qty REAL NOT NULL, from_loc TEXT NOT NULL DEFAULT '', to_loc TEXT NOT NULL DEFAULT '',
  serial TEXT NOT NULL DEFAULT '', site_code TEXT NOT NULL DEFAULT '', ticket_id INTEGER, equipment_id INTEGER,
  condition TEXT NOT NULL DEFAULT 'ok', note TEXT NOT NULL DEFAULT '', username TEXT NOT NULL);
"""
DEFAULT_LOCATIONS = [("WH-CDMX", "ARGIA warehouse, Estado de Mexico", 0), ("TRANSIT", "In transit", 1)]

CATEGORIES = [("inverter", "Inverter", "Inversor"), ("optimizer", "Optimizer", "Optimizador"),
              ("module", "PV modules", "Módulos FV"), ("meter", "Meter", "Medidor"),
              ("logger", "Data logger / gateway", "Datalogger / gateway"),
              ("sensor", "Weather sensor", "Sensor meteorológico"), ("modem", "Modem / router", "Módem / router"),
              ("protection", "Protection (SPD, fuses, breakers)", "Protecciones (DPS, fusibles, interruptores)"),
              ("racking", "Racking", "Estructura"), ("other", "Other", "Otro")]
CAT_KEYS = [k for k, _, _ in CATEGORIES]
CALIBRATED = {"meter", "sensor"}              # recalibration is scheduled (MSA Monitoring)
EQ_STATUSES = [("in_service", "In service", "En servicio"), ("fault", "Out of service (fault)", "Fuera de servicio (falla)"),
               ("removed", "Removed / replaced", "Retirado / reemplazado")]
EQ_STATUS_KEYS = [k for k, _, _ in EQ_STATUSES]
WARN_WARRANTY_DAYS = 90
WARN_CALIB_DAYS = 30

CLAIM_STATUSES = [("DRAFT", "Being prepared", "En preparación"), ("SUBMITTED", "Submitted to supplier", "Enviado al proveedor"),
                  ("APPROVED", "Approved", "Aprobado"), ("DENIED", "Denied", "Rechazado"), ("CLOSED", "Closed", "Cerrado")]
CLAIM_TRANSITIONS = {"DRAFT": ("SUBMITTED", "CLOSED"), "SUBMITTED": ("APPROVED", "DENIED"),
                     "APPROVED": ("CLOSED",), "DENIED": ("SUBMITTED", "CLOSED"), "CLOSED": ()}
CLAIM_OPEN = ("DRAFT", "SUBMITTED", "APPROVED", "DENIED")

MOVE_KINDS = [("RECEIPT", "Receipt", "Entrada"), ("ISSUE", "Issue to site", "Salida a sitio"),
              ("RETURN", "Return from site", "Devolución de sitio"), ("TRANSFER", "Transfer", "Traspaso"),
              ("ADJUST_IN", "Count adjustment +", "Ajuste de conteo +"), ("ADJUST_OUT", "Count adjustment -", "Ajuste de conteo -"),
              ("SCRAP", "Scrapped", "Baja")]
MOVE_KEYS = [k for k, _, _ in MOVE_KINDS]
# which side of a move must name a stock location ('' = outside the inventory)
_NEEDS = {"RECEIPT": (False, True), "ISSUE": (True, False), "RETURN": (False, True), "TRANSFER": (True, True),
          "ADJUST_IN": (False, True), "ADJUST_OUT": (True, False), "SCRAP": (True, False)}
OWNERS = [("PROLOGIS", "Prologis (EPC or reimbursed)", "Prologis (EPC o reembolsado)"), ("ARGIA", "ARGIA", "ARGIA")]
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,39}$")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    """Create the v320 tables and the two default locations (idempotent)."""
    c.executescript(SCHEMA)
    for code, name, transit in DEFAULT_LOCATIONS:
        c.execute("INSERT OR IGNORE INTO stock_locations (code, name, transit) VALUES (?,?,?)", (code, name, transit))
    c.commit()


def _audit(c, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    from argia.prologis import store as S        # late import: store imports this module in connect()
    S.audit(c, user, ip, action, target, detail)


def _date_ok(s: str) -> bool:
    if not s:
        return True
    if not _DATE.match(s):
        return False
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _num(v, cast=float) -> Optional[float]:
    s = str(v if v is not None else "").strip().replace(",", "")
    if not s:
        return None
    if cast is int:
        f = float(s)
        if not f.is_integer():
            raise ValueError("not a whole number")
        return int(f)
    return cast(s)


# ------------------------------------------------------------------ pure rules
def warranty_state(until: str, today: dt.date) -> str:
    """'none' (no date), 'active', 'expiring' (90 days or less) or 'expired'."""
    if not until:
        return "none"
    d = dt.date.fromisoformat(until)
    if d < today:
        return "expired"
    return "expiring" if (d - today).days <= WARN_WARRANTY_DAYS else "active"


def calib_state(category: str, due: str, today: dt.date) -> str:
    """'n/a' (not a calibrated device), 'unknown' (no date yet), 'ok',
    'due_soon' (30 days or less) or 'overdue'."""
    if category not in CALIBRATED:
        return "n/a"
    if not due:
        return "unknown"
    d = dt.date.fromisoformat(due)
    if d < today:
        return "overdue"
    return "due_soon" if (d - today).days <= WARN_CALIB_DAYS else "ok"


EQ_FIELDS = ("site_code", "category", "tag", "make", "model", "serial", "qty", "dc_kw", "ac_kw", "installed",
             "status", "warranty_by", "warranty_until", "calib_due", "notes")


def clean_equipment(d: Dict[str, object], site_codes: Iterable[str]) -> Tuple[Dict[str, object], List[str]]:
    """Normalise one equipment record (form or CSV row) and list every
    problem with it. Pure."""
    errs: List[str] = []
    r: Dict[str, object] = {}
    for k in ("site_code", "category", "tag", "make", "model", "serial", "installed", "status",
              "warranty_by", "warranty_until", "calib_due", "notes"):
        r[k] = str(d.get(k) or "").strip()
    r["site_code"] = str(r["site_code"]).upper()
    r["category"] = str(r["category"]).lower()
    r["status"] = str(r["status"] or "in_service").lower()
    r["tag"] = str(r["tag"])[:60]
    r["make"], r["model"], r["serial"] = str(r["make"])[:60], str(r["model"])[:80], str(r["serial"])[:80]
    r["warranty_by"], r["notes"] = str(r["warranty_by"])[:80], str(r["notes"])[:1000]
    if r["site_code"] not in set(site_codes):
        errs.append(f"unknown site {r['site_code']!r}")
    if r["category"] not in CAT_KEYS:
        errs.append(f"unknown category {r['category']!r}")
    if r["status"] not in EQ_STATUS_KEYS:
        errs.append(f"unknown status {r['status']!r}")
    for k in ("installed", "warranty_until", "calib_due"):
        if not _date_ok(str(r[k])):
            errs.append(f"{k} must be YYYY-MM-DD")
    try:
        q = _num(d.get("qty"), int)
        r["qty"] = 1 if q is None else q
        if not (1 <= int(r["qty"]) <= 100000):
            errs.append("qty must be 1 to 100000")
    except ValueError:
        errs.append("qty must be a whole number")
        r["qty"] = 1
    for k in ("dc_kw", "ac_kw"):
        try:
            r[k] = _num(d.get(k))
            if r[k] is not None and not (0 <= float(r[k]) <= 20000):
                errs.append(f"{k} out of range")
        except ValueError:
            errs.append(f"{k} must be a number")
            r[k] = None
    if r["serial"] and int(r["qty"]) != 1:
        errs.append("a row with a serial number is one unit (qty 1)")
    if not r["serial"] and not r["tag"] and not r["model"]:
        errs.append("give at least a tag, a model or a serial number")
    return r, errs


@dataclass
class ImportResult:
    rows: List[Dict[str, object]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


IMPORT_COLUMNS = ["site", "category", "tag", "make", "model", "serial", "qty", "dc_kw", "ac_kw", "installed",
                  "warranty_by", "warranty_until", "calib_due", "notes"]


def parse_import(text: str, site_codes: Iterable[str], existing: Iterable[Tuple[str, str]] = ()) -> ImportResult:
    """Parse a CSV of equipment (header row = IMPORT_COLUMNS, any order,
    unknown columns refused). All or nothing: the caller saves only when
    ``errors`` is empty. ``existing`` = (make, serial) pairs already in the
    register; a serial is never imported twice. Pure."""
    res = ImportResult()
    codes = list(site_codes)
    text = text.lstrip("﻿")
    rd = csv.DictReader(io.StringIO(text))
    head = [h.strip().lower() for h in (rd.fieldnames or [])]
    if not head:
        res.errors.append("empty file")
        return res
    unknown = [h for h in head if h not in IMPORT_COLUMNS]
    if unknown:
        res.errors.append("unknown columns: " + ", ".join(unknown) + " (allowed: " + ", ".join(IMPORT_COLUMNS) + ")")
        return res
    for need in ("site", "category"):
        if need not in head:
            res.errors.append(f"missing column {need!r}")
    if res.errors:
        return res
    seen = {(m.lower(), s.lower()) for m, s in existing}
    for i, raw in enumerate(rd, start=2):
        row = {(k or "").strip().lower(): v for k, v in raw.items()}
        if not any(str(v or "").strip() for v in row.values()):
            continue
        row["site_code"] = row.pop("site", "")
        r, errs = clean_equipment(row, codes)
        key = (str(r["make"]).lower(), str(r["serial"]).lower())
        if r["serial"]:
            if key in seen:
                errs.append(f"serial {r['serial']} is already registered (or repeated in the file)")
            seen.add(key)
        res.errors.extend(f"line {i}: {e}" for e in errs)
        res.rows.append(r)
    if not res.rows and not res.errors:
        res.errors.append("no data rows")
    return res


def dc_check(rows: Sequence[sqlite3.Row], site_kwp: float) -> Optional[Tuple[float, float]]:
    """Registered module DC (kW, in service) vs the site's kWp: (dc_kw,
    share). None when no module row carries dc_kw yet. The availability
    formula needs both to agree."""
    vals = [r["dc_kw"] for r in rows if r["category"] == "module" and r["status"] != "removed" and r["dc_kw"] is not None]
    if not vals or not site_kwp:
        return None
    tot = float(sum(vals))
    return tot, tot / site_kwp


# ------------------------------------------------------------------ equipment
def equipment(c, site: str = "", category: str = "", include_removed: bool = False) -> List[sqlite3.Row]:
    q, a = "SELECT * FROM equipment WHERE 1=1", []
    if site:
        q += " AND site_code=?"
        a.append(site.upper())
    if category:
        q += " AND category=?"
        a.append(category)
    if not include_removed:
        q += " AND status<>'removed'"
    return c.execute(q + " ORDER BY site_code, category, tag, serial, id", a).fetchall()


def equipment_row(c, eid: int) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()


def _serial_taken(c, make: str, serial: str, not_id: Optional[int] = None) -> bool:
    if not serial:
        return False
    r = c.execute("SELECT id FROM equipment WHERE lower(make)=lower(?) AND lower(serial)=lower(?)", (make, serial)).fetchone()
    return bool(r) and r["id"] != not_id


def save_equipment(c, d: Dict[str, object], site_codes: Iterable[str], by: str, ip: str = "",
                   eid: Optional[int] = None) -> int:
    """Insert (eid None) or update one row. Raises ValueError with every problem."""
    r, errs = clean_equipment(d, site_codes)
    if _serial_taken(c, str(r["make"]), str(r["serial"]), eid):
        errs.append(f"serial {r['serial']} is already registered")
    if errs:
        raise ValueError("; ".join(errs))
    cols = [k for k in EQ_FIELDS]
    vals = [r[k] for k in cols]
    if eid is None:
        cur = c.execute(f"INSERT INTO equipment ({', '.join(cols)}, created_by, created_utc) VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                        (*vals, by, now_utc()))
        c.commit()
        new_id = int(cur.lastrowid)
        _audit(c, by, ip, "equipment_add", f"{r['site_code']}/{r['category']}/{r['tag'] or r['serial'] or new_id}",
               f"{r['make']} {r['model']} serial={r['serial']} qty={r['qty']} dc_kw={r['dc_kw']}")
        return new_id
    old = equipment_row(c, eid)
    if old is None:
        raise ValueError("no such equipment")
    c.execute(f"UPDATE equipment SET {', '.join(k + '=?' for k in cols)}, updated_by=?, updated_utc=? WHERE id=?",
              (*vals, by, now_utc(), eid))
    c.commit()
    diff = [f"{k}: {old[k]!r} -> {r[k]!r}" for k in cols if (old[k] if old[k] is not None else None) != r[k]]
    _audit(c, by, ip, "equipment_edit", f"{r['site_code']}/{eid}", "; ".join(diff)[:1000] or "no change")
    return eid


def import_equipment(c, res: ImportResult, by: str, ip: str = "") -> int:
    if res.errors:
        raise ValueError("; ".join(res.errors))
    cols = list(EQ_FIELDS)
    with c:
        for r in res.rows:
            c.execute(f"INSERT INTO equipment ({', '.join(cols)}, created_by, created_utc) VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                      (*[r[k] for k in cols], by, now_utc()))
    _audit(c, by, ip, "equipment_import", f"{len(res.rows)} rows",
           ", ".join(sorted({str(r["site_code"]) for r in res.rows}))[:500])
    return len(res.rows)


def existing_serials(c) -> List[Tuple[str, str]]:
    return [(r["make"], r["serial"]) for r in c.execute("SELECT make, serial FROM equipment WHERE serial<>''")]


# ------------------------------------------------------------------ warranty claims
def _next_claim(c) -> str:
    r = c.execute("SELECT max(CAST(substr(number, 4) AS INTEGER)) FROM warranty_claims").fetchone()
    return f"WC-{(r[0] or 0) + 1:04d}"


def claim_event(c, claim_id: int, who: str, kind: str, body: str = "", meta: Optional[dict] = None,
                ts: Optional[str] = None) -> None:
    c.execute("INSERT INTO claim_events (claim_id, ts_utc, username, kind, body, meta) VALUES (?,?,?,?,?,?)",
              (claim_id, ts or now_utc(), who, kind, body[:5000], json.dumps(meta or {})))
    c.commit()


def open_claim(c, site_code: str, supplier: str, fault: str, by: str, equipment_id: Optional[int] = None,
               ticket_id: Optional[int] = None, supplier_ref: str = "", ip: str = "") -> sqlite3.Row:
    supplier = (supplier or "").strip()
    if not supplier:
        raise ValueError("supplier is required")
    if equipment_id is not None:
        eq = equipment_row(c, equipment_id)
        if eq is None or eq["site_code"] != site_code.upper():
            raise ValueError("the equipment is not at that site")
    num = _next_claim(c)
    c.execute("INSERT INTO warranty_claims (number, site_code, equipment_id, ticket_id, supplier, supplier_ref, fault, opened_utc, created_by)"
              " VALUES (?,?,?,?,?,?,?,?,?)", (num, site_code.upper(), equipment_id, ticket_id, supplier[:80],
                                             supplier_ref.strip()[:80], fault.strip()[:5000], now_utc(), by))
    c.commit()
    cl = claim(c, num)
    claim_event(c, cl["id"], by, "created", fault[:500], {"supplier": supplier, "equipment": equipment_id, "ticket": ticket_id})
    _audit(c, by, ip, "claim_open", num, f"{site_code} {supplier} eq={equipment_id}")
    return cl


def claim(c, number: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM warranty_claims WHERE number=?", (number,)).fetchone()


def claims(c, open_only: bool = False) -> List[sqlite3.Row]:
    if open_only:
        q = ",".join("?" * len(CLAIM_OPEN))
        return c.execute(f"SELECT * FROM warranty_claims WHERE status IN ({q}) ORDER BY id DESC", CLAIM_OPEN).fetchall()
    return c.execute("SELECT * FROM warranty_claims ORDER BY id DESC").fetchall()


def claim_events(c, claim_id: int) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM claim_events WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()


def set_claim_status(c, cl: sqlite3.Row, new: str, who: str, note: str = "", supplier_ref: str = "",
                     compensation: Optional[float] = None, ip: str = "", ts: Optional[str] = None) -> None:
    """Move a claim along DRAFT > SUBMITTED > APPROVED | DENIED > CLOSED.
    A denial resets the Owner acknowledgement: Prologis sees the claim
    flagged until a Prologis manager acknowledges it (MSA: "notify Owner
    immediately of any warranty claims that are denied")."""
    if new not in CLAIM_TRANSITIONS.get(cl["status"], ()):
        raise ValueError(f"{cl['status']} -> {new} not allowed")
    if new == "DENIED" and not note.strip():
        raise ValueError("a denial needs the supplier's reason")
    if compensation is not None and not (0 <= compensation <= 50_000_000):
        raise ValueError("compensation out of range")
    ts = ts or now_utc()
    sets, vals = ["status=?"], [new]
    if new == "SUBMITTED":
        sets.append("submitted_utc=?")
        vals.append(ts)
        sets.append("decided_utc=''")
    if new in ("APPROVED", "DENIED"):
        sets.append("decided_utc=?")
        vals.append(ts)
    if new == "DENIED":
        sets += ["owner_ack_utc=''", "owner_ack_by=''"]
    if new == "CLOSED":
        sets.append("closed_utc=?")
        vals.append(ts)
    if supplier_ref.strip():
        sets.append("supplier_ref=?")
        vals.append(supplier_ref.strip()[:80])
    if compensation is not None:
        sets.append("compensation_mxn=?")
        vals.append(compensation)
    c.execute(f"UPDATE warranty_claims SET {', '.join(sets)} WHERE id=?", (*vals, cl["id"]))
    c.commit()
    claim_event(c, cl["id"], who, "status", note, {"from": cl["status"], "to": new, "compensation": compensation}, ts=ts)
    _audit(c, who, ip, "claim_status", cl["number"], f"{cl['status']}->{new}" + (f" compensation={compensation}" if compensation is not None else ""))


def ack_denial(c, cl: sqlite3.Row, who: str, note: str = "", ip: str = "") -> None:
    if cl["owner_ack_utc"]:
        raise ValueError("already acknowledged")
    if not _was_denied(c, cl["id"]):
        raise ValueError("nothing to acknowledge")
    c.execute("UPDATE warranty_claims SET owner_ack_utc=?, owner_ack_by=? WHERE id=?", (now_utc(), who, cl["id"]))
    c.commit()
    claim_event(c, cl["id"], who, "owner_ack", note)
    _audit(c, who, ip, "claim_owner_ack", cl["number"])


def _was_denied(c, claim_id: int) -> bool:
    return any(json.loads(e["meta"] or "{}").get("to") == "DENIED" for e in claim_events(c, claim_id) if e["kind"] == "status")


def needs_owner_ack(c, cl: sqlite3.Row) -> bool:
    """A claim that was denied (now or before it was closed or re-submitted)
    and that no Prologis manager has acknowledged yet."""
    return not cl["owner_ack_utc"] and _was_denied(c, cl["id"])


def record_pass_through(c, cl: sqlite3.Row, day: str, who: str, note: str = "", ip: str = "") -> None:
    if cl["compensation_mxn"] in (None, 0):
        raise ValueError("no compensation recorded on this claim")
    if not day or not _date_ok(day):
        raise ValueError("date must be YYYY-MM-DD")
    c.execute("UPDATE warranty_claims SET passed_through=? WHERE id=?", (day, cl["id"]))
    c.commit()
    claim_event(c, cl["id"], who, "pass_through", note, {"day": day, "mxn": cl["compensation_mxn"]})
    _audit(c, who, ip, "claim_pass_through", cl["number"], f"{day} mxn={cl['compensation_mxn']}")


def turnaround_days(cl: sqlite3.Row, now: dt.datetime) -> Optional[float]:
    """Days the supplier has had the claim: submitted -> decided (or now).
    This is the 'manufacturer warranty processing' time the availability
    exclusion (b) refers to."""
    if not cl["submitted_utc"]:
        return None
    a = dt.datetime.fromisoformat(cl["submitted_utc"])
    b = dt.datetime.fromisoformat(cl["decided_utc"]) if cl["decided_utc"] else now
    return max(0.0, (b - a).total_seconds() / 86400)


# ------------------------------------------------------------------ parts and locations
PART_FIELDS = ("code", "name_en", "name_es", "category", "make", "model", "unit", "serialized", "owner", "active")


def clean_part(d: Dict[str, object]) -> Tuple[Dict[str, object], List[str]]:
    errs = []
    r = {k: str(d.get(k) or "").strip() for k in ("code", "name_en", "name_es", "category", "make", "model", "unit", "owner")}
    r["code"] = str(r["code"]).upper()
    r["category"] = str(r["category"]).lower()
    r["owner"] = str(r["owner"] or "PROLOGIS").upper()
    r["unit"] = str(r["unit"] or "pc")[:12]
    r["serialized"] = 1 if str(d.get("serialized") or "").lower() in ("1", "true", "yes", "on") else 0
    r["active"] = 0 if str(d.get("active", "1")).lower() in ("0", "false", "no") else 1
    if not _CODE.match(str(r["code"])):
        errs.append("code: 2-40 characters, A-Z 0-9 . _ -")
    if not r["name_en"]:
        errs.append("English name is required")
    if r["category"] not in CAT_KEYS:
        errs.append(f"unknown category {r['category']!r}")
    if r["owner"] not in [k for k, _, _ in OWNERS]:
        errs.append("owner must be PROLOGIS or ARGIA")
    return r, errs


def save_part(c, d: Dict[str, object], by: str, ip: str = "") -> str:
    r, errs = clean_part(d)
    if errs:
        raise ValueError("; ".join(errs))
    old = part(c, str(r["code"]))
    if old is not None and old["serialized"] != r["serialized"] and on_hand_total(c, str(r["code"])) > 0:
        raise ValueError("serial tracking cannot change while the part is in stock")
    c.execute(f"INSERT OR REPLACE INTO parts ({', '.join(PART_FIELDS)}, updated_by, updated_utc) VALUES ({', '.join('?' * len(PART_FIELDS))}, ?, ?)",
              (*[r[k] for k in PART_FIELDS], by, now_utc()))
    c.commit()
    if old is None:
        _audit(c, by, ip, "part_add", str(r["code"]), f"{r['name_en']} owner={r['owner']} serialized={r['serialized']}")
    else:
        diff = [f"{k}: {old[k]!r} -> {r[k]!r}" for k in PART_FIELDS if old[k] != r[k]]
        _audit(c, by, ip, "part_edit", str(r["code"]), "; ".join(diff)[:1000] or "no change")
    return str(r["code"])


def seed_parts(c, items: Sequence[dict], by: str = "seed") -> int:
    """Add parts (and their minimum stock) that do not exist yet; never
    overwrites. Item: part fields plus optional {"min": {"WH-CDMX": 11}}."""
    n = 0
    for d in items:
        code = str(d.get("code", "")).upper()
        if part(c, code) is not None:
            continue
        save_part(c, d, by)
        for loc, q in (d.get("min") or {}).items():
            set_min(c, code, loc, float(q), by)
        n += 1
    return n


def part(c, code: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM parts WHERE code=?", ((code or "").upper(),)).fetchone()


def parts(c, include_inactive: bool = False) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM parts" + ("" if include_inactive else " WHERE active=1") + " ORDER BY category, code").fetchall()


def locations(c, include_inactive: bool = False) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM stock_locations" + ("" if include_inactive else " WHERE active=1")
                     + " ORDER BY transit, code").fetchall()


def add_location(c, code: str, name: str, by: str, transit: bool = False, ip: str = "") -> None:
    code = (code or "").strip().upper()
    if not _CODE.match(code):
        raise ValueError("code: 2-40 characters, A-Z 0-9 . _ -")
    if not (name or "").strip():
        raise ValueError("name is required")
    if c.execute("SELECT 1 FROM stock_locations WHERE code=?", (code,)).fetchone():
        raise ValueError("location exists")
    c.execute("INSERT INTO stock_locations (code, name, transit) VALUES (?,?,?)", (code, name.strip()[:80], 1 if transit else 0))
    c.commit()
    _audit(c, by, ip, "location_add", code, name)


def set_min(c, part_code: str, loc: str, qty: float, by: str, ip: str = "") -> None:
    if qty < 0 or qty > 1_000_000:
        raise ValueError("minimum out of range")
    if part(c, part_code) is None:
        raise ValueError("no such part")
    if not c.execute("SELECT 1 FROM stock_locations WHERE code=?", (loc,)).fetchone():
        raise ValueError("no such location")
    old = c.execute("SELECT min_qty FROM stock_min WHERE part_code=? AND loc=?", (part_code.upper(), loc)).fetchone()
    c.execute("INSERT OR REPLACE INTO stock_min (part_code, loc, min_qty) VALUES (?,?,?)", (part_code.upper(), loc, qty))
    c.commit()
    _audit(c, by, ip, "stock_min", f"{part_code}@{loc}", f"{old[0] if old else None} -> {qty}")


def minimums(c) -> Dict[Tuple[str, str], float]:
    return {(r["part_code"], r["loc"]): r["min_qty"] for r in c.execute("SELECT * FROM stock_min")}


# ------------------------------------------------------------------ stock
def balances(moves: Iterable, upto_day: Optional[str] = None, before_day: Optional[str] = None) -> Dict[Tuple[str, str], float]:
    """{(part, location): quantity} from movements (pure). ``upto_day``
    includes that day; ``before_day`` excludes it."""
    out: Dict[Tuple[str, str], float] = {}
    for m in moves:
        if upto_day and m["day"] > upto_day:
            continue
        if before_day and m["day"] >= before_day:
            continue
        if m["to_loc"]:
            out[(m["part_code"], m["to_loc"])] = out.get((m["part_code"], m["to_loc"]), 0.0) + m["qty"]
        if m["from_loc"]:
            out[(m["part_code"], m["from_loc"])] = out.get((m["part_code"], m["from_loc"]), 0.0) - m["qty"]
    return out


def serial_locations(moves: Iterable) -> Dict[Tuple[str, str], str]:
    """{(part, serial): location} for serialized units; '' = outside the
    inventory (issued, scrapped). Moves in id order. Pure."""
    out: Dict[Tuple[str, str], str] = {}
    for m in moves:
        if m["serial"]:
            out[(m["part_code"], m["serial"].upper())] = m["to_loc"] or ""
    return out


def all_moves(c) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM stock_moves ORDER BY id").fetchall()


def on_hand(c, part_code: str, loc: str) -> float:
    r = c.execute("SELECT coalesce(sum(CASE WHEN to_loc=? THEN qty ELSE 0 END),0) - coalesce(sum(CASE WHEN from_loc=? THEN qty ELSE 0 END),0)"
                  " FROM stock_moves WHERE part_code=? AND (to_loc=? OR from_loc=?)", (loc, loc, part_code, loc, loc)).fetchone()
    return float(r[0] or 0)


def on_hand_total(c, part_code: str) -> float:
    return sum(v for (p, _l), v in balances(all_moves(c)).items() if p == part_code)


def move(c, kind: str, part_code: str, qty: float, by: str, from_loc: str = "", to_loc: str = "", serial: str = "",
         site_code: str = "", ticket_id: Optional[int] = None, note: str = "", condition: str = "ok",
         day: Optional[str] = None, site_codes: Iterable[str] = (), replaces: Optional[int] = None,
         ip: str = "", today: Optional[dt.date] = None) -> int:
    """Record one stock movement after checking it (ValueError otherwise):
    known part and locations, the right side(s) named for the kind, enough
    stock (never below zero), serialized parts one unit at a time with a
    serial that is where the move says it is, issues and returns name a
    site, adjustments and scrapping name a reason.

    ISSUE with ``replaces`` (an equipment row at that site) also updates the
    register: the old unit becomes 'removed' and the issued serial becomes
    a new in-service row with the same tag - the repair keeps its serials."""
    kind = (kind or "").upper()
    if kind not in MOVE_KEYS:
        raise ValueError("unknown movement")
    p = part(c, part_code)
    if p is None or not p["active"]:
        raise ValueError("unknown part")
    today = today or dt.datetime.now(dt.timezone.utc).date()
    day = day or today.isoformat()
    if not _date_ok(day) or day > today.isoformat():
        raise ValueError("date must be YYYY-MM-DD and not in the future")
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise ValueError("quantity must be a number")
    if not (0 < qty <= 100000):
        raise ValueError("quantity must be more than 0")
    need_from, need_to = _NEEDS[kind]
    from_loc = (from_loc or "").upper() if need_from else ""
    to_loc = (to_loc or "").upper() if need_to else ""
    locs = {r["code"] for r in locations(c)}
    if need_from and from_loc not in locs:
        raise ValueError("choose the location the stock leaves")
    if need_to and to_loc not in locs:
        raise ValueError("choose the location the stock goes to")
    if kind == "TRANSFER" and from_loc == to_loc:
        raise ValueError("a transfer needs two different locations")
    site_code = (site_code or "").upper()
    if kind in ("ISSUE", "RETURN") and site_code not in set(site_codes):
        raise ValueError("choose the site")
    if kind in ("ADJUST_IN", "ADJUST_OUT", "SCRAP") and not note.strip():
        raise ValueError("an adjustment or a scrapping needs a reason")
    if condition not in ("ok", "damaged"):
        raise ValueError("condition must be ok or damaged")
    serial = (serial or "").strip().upper()
    if p["serialized"]:
        if not serial:
            raise ValueError("this part is tracked by serial number: give the serial")
        if qty != 1:
            raise ValueError("serialized parts move one unit at a time")
        where = serial_locations(all_moves(c)).get((p["code"], serial))
        if kind in ("RECEIPT", "RETURN", "ADJUST_IN"):
            if where:
                raise ValueError(f"serial {serial} is already in stock at {where}")
        elif where != from_loc:
            raise ValueError(f"serial {serial} is not at {from_loc}" + (f" (it is at {where})" if where else ""))
    elif serial:
        raise ValueError("this part is not tracked by serial number")
    if need_from and on_hand(c, p["code"], from_loc) + 1e-9 < qty:
        raise ValueError(f"only {on_hand(c, p['code'], from_loc):g} {p['unit']} at {from_loc}")
    old_eq = None
    if replaces is not None:
        if kind != "ISSUE":
            raise ValueError("only an issue to site can replace equipment")
        old_eq = equipment_row(c, replaces)
        if old_eq is None or old_eq["site_code"] != site_code or old_eq["status"] == "removed":
            raise ValueError("the equipment to replace is not in service at that site")
    cur = c.execute("INSERT INTO stock_moves (ts_utc, day, part_code, kind, qty, from_loc, to_loc, serial, site_code, ticket_id,"
                    " equipment_id, condition, note, username) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now_utc(), day, p["code"], kind, qty, from_loc, to_loc, serial, site_code, ticket_id, replaces,
                     condition, note.strip()[:1000], by))
    c.commit()
    mid = int(cur.lastrowid)
    _audit(c, by, ip, "stock_" + kind.lower(), p["code"],
           f"qty={qty:g} {from_loc or '-'} > {to_loc or site_code or '-'} serial={serial} ticket={ticket_id} {condition}")
    if old_eq is not None:
        new_id = save_equipment(c, {"site_code": site_code, "category": old_eq["category"], "tag": old_eq["tag"],
                                    "make": p["make"] or old_eq["make"], "model": p["model"] or old_eq["model"],
                                    "serial": serial, "qty": 1 if serial else old_eq["qty"], "dc_kw": old_eq["dc_kw"],
                                    "ac_kw": old_eq["ac_kw"], "installed": day, "status": "in_service",
                                    "notes": f"replaced equipment #{old_eq['id']} (stock move #{mid})"},
                                site_codes, by, ip)
        c.execute("UPDATE equipment SET status='removed', replaced_by=?, updated_by=?, updated_utc=? WHERE id=?",
                  (new_id, by, now_utc(), old_eq["id"]))
        c.commit()
        _audit(c, by, ip, "equipment_replace", f"{site_code}/{old_eq['id']}", f"by #{new_id} serial={serial}")
    return mid


# ------------------------------------------------------------------ quarterly report
def quarter_bounds(q: str) -> Tuple[dt.date, dt.date]:
    """'2026-Q4' -> (2026-10-01, 2026-12-31)."""
    m = re.match(r"^(\d{4})-Q([1-4])$", (q or "").strip().upper())
    if not m:
        raise ValueError("quarter must look like 2026-Q4")
    y, n = int(m.group(1)), int(m.group(2))
    start = dt.date(y, 3 * n - 2, 1)
    end = (dt.date(y + 1, 1, 1) if n == 4 else dt.date(y, 3 * n + 1, 1)) - dt.timedelta(days=1)
    return start, end


def quarter_of(d: dt.date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


@dataclass
class QRow:
    part: str
    loc: str
    opening: float
    ins: Dict[str, float]
    outs: Dict[str, float]
    closing: float
    min_qty: Optional[float]

    @property
    def below_min(self) -> bool:
        return self.min_qty is not None and self.closing + 1e-9 < self.min_qty


@dataclass
class Restock:
    part: str
    min_total: float
    closing_total: float
    transit: float

    @property
    def order_qty(self) -> float:
        return max(0.0, self.min_total - self.closing_total - self.transit)


def quarter_report(moves: Sequence, part_codes: Sequence[str], locs: Sequence[Tuple[str, bool]],
                   mins: Dict[Tuple[str, str], float], q: str) -> Tuple[List[QRow], List[Restock], List]:
    """The quarterly inventory report (pure). Returns (rows per part and
    location, restocking recommendations per part, the quarter's moves).

    opening = balance before the first day; in / out by kind; closing =
    balance at the last day, so opening + in - out = closing always holds.
    Restocking: for each part with a minimum, the order quantity is the
    total minimum over warehouses minus the warehouse closing stock minus
    what is in transit (already on its way)."""
    start, end = quarter_bounds(q)
    s, e = start.isoformat(), end.isoformat()
    open_b = balances(moves, before_day=s)
    close_b = balances(moves, upto_day=e)
    inq = [m for m in moves if s <= m["day"] <= e]
    transit_locs = {code for code, tr in locs if tr}
    rows: List[QRow] = []
    for pc in part_codes:
        for code, _tr in locs:
            ins: Dict[str, float] = {}
            outs: Dict[str, float] = {}
            for m in inq:
                if m["part_code"] != pc:
                    continue
                if m["to_loc"] == code:
                    ins[m["kind"]] = ins.get(m["kind"], 0.0) + m["qty"]
                if m["from_loc"] == code:
                    outs[m["kind"]] = outs.get(m["kind"], 0.0) + m["qty"]
            ob, cb = open_b.get((pc, code), 0.0), close_b.get((pc, code), 0.0)
            mn = mins.get((pc, code))
            if ob or cb or ins or outs or mn is not None:
                rows.append(QRow(pc, code, ob, ins, outs, cb, mn))
    rest: List[Restock] = []
    for pc in part_codes:
        mt = sum(v for (p, l), v in mins.items() if p == pc and l not in transit_locs)
        if not mt:
            continue
        ct = sum(v for (p, l), v in close_b.items() if p == pc and l not in transit_locs)
        tr = sum(v for (p, l), v in close_b.items() if p == pc and l in transit_locs)
        rest.append(Restock(pc, mt, ct, tr))
    return rows, rest, inq
