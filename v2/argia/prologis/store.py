"""SQLite store of the Prologis platform (v292): users, sessions, audit
log, tickets, ticket timeline, documents, project state (v320: the
equipment register, warranty claims and spare parts live in assets.py,
created here by connect()).

One database file per customer (ARGIA_PL_DB, default
/opt/argia/prologis/prologis.db), separate from the ARGIA portal's
users.db and from PostgreSQL - Prologis data never shares a table with
another customer's. Passwords: scrypt (stdlib). Sessions: a random id
in the cookie, only its SHA-256 stored. Everything that changes data
writes an audit row.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_DB = "/opt/argia/prologis/prologis.db"
ROLES = ("viewer", "manager", "operator", "admin")
ROLE_LABEL = {"viewer": ("Viewer", "Consulta"), "manager": ("Prologis manager", "Gerente Prologis"),
              "operator": ("ARGIA operator", "Operador ARGIA"), "admin": ("Administrator", "Administrador")}
PERMS = {
    "view": {"viewer", "manager", "operator", "admin"},
    "comment": {"viewer", "manager", "operator", "admin"},
    "ticket_new": {"manager", "operator", "admin"},
    "ticket_work": {"operator", "admin"},              # status, response, assignment
    "ticket_approve": {"manager", "admin"},            # Owner dispatch / quote approval
    "doc_upload": {"manager", "operator", "admin"},
    "project_edit": {"operator", "admin"},
    "export": {"manager", "admin"},
    "audit": {"manager", "admin"},
    "users": {"admin"},
    "order": {"manager", "operator", "admin"},           # v293: place service orders
    "order_work": {"operator", "admin"},                 # confirm, schedule, price quotes, complete
    "catalog_edit": {"admin"},                           # services and prices
    "assets_edit": {"operator", "admin"},                # v320: equipment register, warranty claims, spare parts
    "assets_ack": {"manager", "admin"},                  # v320: Prologis acknowledges a denied warranty claim
    "alarms_work": {"operator", "admin"},                # v321: triage alarms, record the daily review, see the outbox
    "alarms_admin": {"admin"},                           # v321: alarm mail mode and the ARGIA desk addresses
    "exclusion_decide": {"manager", "admin"},            # v323: Prologis accepts or rejects an availability exclusion
    "contract_edit": {"admin"},                          # v323: effective date, kWh rate and annual fee per site
}
TICKET_STATUSES = [("NEW", "New", "Nuevo"), ("RESPONDED", "Responded", "Atendido"),
                   ("IN_PROGRESS", "In progress", "En curso"),
                   ("WAITING_OWNER", "Waiting on Prologis / access", "En espera de Prologis / acceso"),
                   ("RESOLVED", "Resolved", "Resuelto"), ("CLOSED", "Closed", "Cerrado")]
OPEN = ("NEW", "RESPONDED", "IN_PROGRESS", "WAITING_OWNER")
FAIL_MAX, FAIL_WINDOW_S = 5, 600
IDLE_S, ABSOLUTE_S = 8 * 3600, 7 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
  org TEXT NOT NULL DEFAULT 'Prologis', role TEXT NOT NULL DEFAULT 'viewer',
  pw TEXT NOT NULL, totp TEXT NOT NULL DEFAULT '', totp_step INTEGER NOT NULL DEFAULT -1,
  must_change INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
  lang TEXT NOT NULL DEFAULT 'en', created_utc TEXT NOT NULL, last_login_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions (
  sid_hash TEXT PRIMARY KEY, username TEXT NOT NULL, mfa_ok INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, seen REAL NOT NULL, ip TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS login_fail (key TEXT PRIMARY KEY, n INTEGER NOT NULL, first REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE, site_code TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', sla_class TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NEW', kw_lost REAL, estimate_mxn REAL,
  approval TEXT NOT NULL DEFAULT 'not_required', detected_utc TEXT NOT NULL,
  approved_utc TEXT NOT NULL DEFAULT '', responded_utc TEXT NOT NULL DEFAULT '',
  resolved_utc TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT '',
  created_utc TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ticket_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, ts_utc TEXT NOT NULL,
  username TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_code TEXT NOT NULL DEFAULT '', folder TEXT NOT NULL,
  name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL DEFAULT '',
  ticket_id INTEGER, uploaded_by TEXT NOT NULL, uploaded_utc TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS catalog (
  code TEXT PRIMARY KEY, category TEXT NOT NULL, name_en TEXT NOT NULL, name_es TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL, price_mxn REAL, desc_en TEXT NOT NULL DEFAULT '', desc_es TEXT NOT NULL DEFAULT '',
  includes_en TEXT NOT NULL DEFAULT '', includes_es TEXT NOT NULL DEFAULT '', lead_days INTEGER NOT NULL DEFAULT 10,
  orderable INTEGER NOT NULL DEFAULT 1, published INTEGER NOT NULL DEFAULT 0, basis TEXT NOT NULL DEFAULT 'catalog',
  sort INTEGER NOT NULL DEFAULT 100, active INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL DEFAULT '', updated_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS cart (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, item_code TEXT NOT NULL,
  site_code TEXT NOT NULL DEFAULT '', qty REAL NOT NULL, note TEXT NOT NULL DEFAULT '', added_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, item_code TEXT NOT NULL, name TEXT NOT NULL,
  unit TEXT NOT NULL, site_code TEXT NOT NULL DEFAULT '', qty REAL NOT NULL, unit_price REAL, lead_days INTEGER NOT NULL DEFAULT 10,
  note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS project_state (
  project_id TEXT PRIMARY KEY, stage TEXT, next TEXT, next_date TEXT, notes TEXT,
  updated_by TEXT NOT NULL, updated_utc TEXT NOT NULL);
"""
DOC_FOLDERS = [("as_built", "As-built drawings", "Planos as-built"),
               ("datasheets", "Datasheets & manuals", "Fichas técnicas y manuales"),
               ("warranties", "Warranties & serials", "Garantías y números de serie"),
               ("commissioning", "Commissioning & PTO", "Puesta en marcha y PTO"),
               ("om_reports", "O&M reports", "Reportes de O&M"),
               ("thermal", "Thermography", "Termografía"),
               ("hse", "HSE & permits", "SSMA y permisos"),
               ("contract", "Contract & SLA", "Contrato y SLA")]
FOLDER_KEYS = [k for k, _, _ in DOC_FOLDERS]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or os.environ.get("ARGIA_PL_DB", DEFAULT_DB)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    _migrate(c)
    from argia.prologis import assets as _assets       # v320: equipment, warranty claims, spares
    _assets.ensure(c)
    from argia.prologis import alarms as _alarms       # v321: alarms, outbox, daily review, settings
    _alarms.ensure(c)
    from argia.prologis import monthly as _monthly     # v322: monthly report, design yield, HSE register
    _monthly.ensure(c)
    from argia.prologis import availability as _avail  # v323: MSA availability, exclusions, contract terms
    _avail.ensure(c)
    from argia.prologis import scan as _scan           # v324: malware scan status of uploads
    _scan.ensure(c)
    return c


# v293: columns added after v292 (SQLite has no ADD COLUMN IF NOT EXISTS)
_TICKET_COLS = {"kind": "TEXT NOT NULL DEFAULT 'incident'", "po_number": "TEXT NOT NULL DEFAULT ''",
                "preferred_date": "TEXT NOT NULL DEFAULT ''", "scheduled_date": "TEXT NOT NULL DEFAULT ''",
                "target_date": "TEXT NOT NULL DEFAULT ''"}


def _migrate(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(tickets)")}
    for col, ddl in _TICKET_COLS.items():
        if col not in have:
            c.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
    c.commit()


def can(role: str, perm: str) -> bool:
    return role in PERMS.get(perm, set())


# ------------------------------------------------------------- passwords
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_pw(stored: str, pw: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt((pw or "").encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(h)))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


PW_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_password(n: int = 16) -> str:
    return "".join(secrets.choice(PW_ALPHABET) for _ in range(n))


def password_problem(pw: str, username: str = "") -> Optional[str]:
    if len(pw or "") < 12:
        return "At least 12 characters. / Mínimo 12 caracteres."
    if username and username.lower() in pw.lower():
        return "Must not contain the user name. / No debe contener el usuario."
    if len(set(pw)) < 6:
        return "Too simple. / Demasiado simple."
    return None


# ------------------------------------------------------------- audit
def audit(c: sqlite3.Connection, user: str, ip: str, action: str, target: str = "", detail: str = "") -> None:
    c.execute("INSERT INTO audit (ts_utc, username, ip, action, target, detail) VALUES (?,?,?,?,?,?)",
              (now_utc(), user or "", ip or "", action, str(target)[:200], str(detail)[:1000]))
    c.commit()


# ------------------------------------------------------------- users
def user(c: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),)).fetchone()


def create_user(c, username: str, name: str, email: str, org: str, role: str, by: str, ip: str = "") -> str:
    """Returns the one-time password (shown once, never stored in clear)."""
    u = (username or "").strip().lower()
    if not u or not all(ch.isalnum() or ch in "._-@" for ch in u):
        raise ValueError("invalid user name")
    if role not in ROLES:
        raise ValueError("invalid role")
    if user(c, u):
        raise ValueError("user exists")
    pw = make_password()
    c.execute("INSERT INTO users (username,name,email,org,role,pw,created_utc) VALUES (?,?,?,?,?,?,?)",
              (u, name.strip()[:80], email.strip().lower()[:120], org.strip()[:40] or "Prologis", role, hash_pw(pw), now_utc()))
    c.commit()
    audit(c, by, ip, "user_create", u, f"role={role} org={org}")
    return pw


def set_role(c, username: str, role: str, by: str, ip: str = "") -> None:
    if role not in ROLES:
        raise ValueError("invalid role")
    c.execute("UPDATE users SET role=? WHERE username=?", (role, username))
    c.commit()
    audit(c, by, ip, "user_role", username, role)


def set_disabled(c, username: str, disabled: bool, by: str, ip: str = "") -> None:
    c.execute("UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, username))
    if disabled:
        c.execute("DELETE FROM sessions WHERE username=?", (username,))
    c.commit()
    audit(c, by, ip, "user_disable" if disabled else "user_enable", username)


def reset_password(c, username: str, by: str, ip: str = "") -> str:
    pw = make_password()
    c.execute("UPDATE users SET pw=?, must_change=1 WHERE username=?", (hash_pw(pw), username))
    c.execute("DELETE FROM sessions WHERE username=?", (username,))
    c.commit()
    audit(c, by, ip, "user_password_reset", username)
    return pw


def reset_mfa(c, username: str, by: str, ip: str = "") -> None:
    c.execute("UPDATE users SET totp='', totp_step=-1 WHERE username=?", (username,))
    c.execute("DELETE FROM sessions WHERE username=?", (username,))
    c.commit()
    audit(c, by, ip, "user_mfa_reset", username)


def change_password(c, username: str, new_pw: str, ip: str = "") -> None:
    c.execute("UPDATE users SET pw=?, must_change=0 WHERE username=?", (hash_pw(new_pw), username))
    c.commit()
    audit(c, username, ip, "password_change", username)


# ------------------------------------------------------------- login throttle
def locked_for(c, key: str, now: float) -> int:
    r = c.execute("SELECT n, first FROM login_fail WHERE key=?", (key,)).fetchone()
    if r and r["n"] >= FAIL_MAX and now - r["first"] < FAIL_WINDOW_S:
        return int(FAIL_WINDOW_S - (now - r["first"])) + 1
    return 0


def note_fail(c, key: str, now: float) -> None:
    r = c.execute("SELECT n, first FROM login_fail WHERE key=?", (key,)).fetchone()
    if not r or now - r["first"] >= FAIL_WINDOW_S:
        c.execute("INSERT OR REPLACE INTO login_fail (key,n,first) VALUES (?,?,?)", (key, 1, now))
    else:
        c.execute("UPDATE login_fail SET n=n+1 WHERE key=?", (key,))
    c.commit()


def clear_fail(c, key: str) -> None:
    c.execute("DELETE FROM login_fail WHERE key=?", (key,))
    c.commit()


# ------------------------------------------------------------- sessions
def _h(sid: str) -> str:
    return hashlib.sha256((sid or "").encode()).hexdigest()


def new_session(c, username: str, ip: str, now: float, mfa_ok: bool = False) -> str:
    sid = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (sid_hash,username,mfa_ok,created,seen,ip) VALUES (?,?,?,?,?,?)",
              (_h(sid), username, 1 if mfa_ok else 0, now, now, ip))
    c.commit()
    return sid


def session(c, sid: str, now: float) -> Optional[sqlite3.Row]:
    """The live session row joined with its user, or None (expired,
    idle too long, disabled user). Touches ``seen``."""
    if not sid:
        return None
    r = c.execute("SELECT s.*, u.role, u.name, u.org, u.disabled, u.must_change, u.totp, u.lang"
                  " FROM sessions s JOIN users u ON u.username=s.username WHERE s.sid_hash=?", (_h(sid),)).fetchone()
    if not r:
        return None
    if r["disabled"] or now - r["seen"] > IDLE_S or now - r["created"] > ABSOLUTE_S:
        c.execute("DELETE FROM sessions WHERE sid_hash=?", (_h(sid),))
        c.commit()
        return None
    c.execute("UPDATE sessions SET seen=? WHERE sid_hash=?", (now, _h(sid)))
    c.commit()
    return r


def mark_mfa(c, sid: str) -> None:
    c.execute("UPDATE sessions SET mfa_ok=1 WHERE sid_hash=?", (_h(sid),))
    c.commit()


def end_session(c, sid: str) -> None:
    c.execute("DELETE FROM sessions WHERE sid_hash=?", (_h(sid),))
    c.commit()


# ------------------------------------------------------------- tickets
def _next(c, prefix: str) -> str:
    """Next number in one series (PL- incidents, SO- service orders); the
    series do not share a counter, so neither has gaps from the other."""
    r = c.execute("SELECT max(CAST(substr(number, ?) AS INTEGER)) FROM tickets WHERE number LIKE ?",
                  (len(prefix) + 1, prefix + "%")).fetchone()
    return f"{prefix}{(r[0] or 0) + 1:04d}"


def next_number(c) -> str:
    return _next(c, "PL-")


def create_ticket(c, site_code: str, title: str, description: str, sla_class: str, by: str,
                  detected_utc: Optional[str] = None, kw_lost: Optional[float] = None,
                  estimate_mxn: Optional[float] = None, needs_approval: bool = False,
                  sample: bool = False, ip: str = "") -> sqlite3.Row:
    num = next_number(c)
    ts = detected_utc or now_utc()
    c.execute("INSERT INTO tickets (number,site_code,title,description,sla_class,kw_lost,estimate_mxn,approval,"
              "detected_utc,created_by,created_utc,sample) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (num, site_code.upper(), title.strip()[:160], description.strip()[:5000], sla_class, kw_lost,
               estimate_mxn, "pending" if needs_approval else "not_required", ts, by, now_utc(), 1 if sample else 0))
    t = c.execute("SELECT * FROM tickets WHERE number=?", (num,)).fetchone()
    add_event(c, t["id"], by, "created", description[:500], {"sla_class": sla_class})
    audit(c, by, ip, "ticket_create", num, f"{site_code} {sla_class}")
    return t


def add_event(c, ticket_id: int, who: str, kind: str, body: str = "", meta: Optional[dict] = None,
              ts: Optional[str] = None) -> None:
    c.execute("INSERT INTO ticket_events (ticket_id, ts_utc, username, kind, body, meta) VALUES (?,?,?,?,?,?)",
              (ticket_id, ts or now_utc(), who, kind, body[:5000], json.dumps(meta or {})))
    c.commit()


TRANSITIONS = {"NEW": ("RESPONDED", "IN_PROGRESS", "WAITING_OWNER", "CLOSED"),
               "RESPONDED": ("IN_PROGRESS", "WAITING_OWNER", "RESOLVED"),
               "IN_PROGRESS": ("WAITING_OWNER", "RESOLVED"),
               "WAITING_OWNER": ("IN_PROGRESS", "RESOLVED"),
               "RESOLVED": ("CLOSED", "IN_PROGRESS"),
               "CLOSED": ("IN_PROGRESS",)}


def set_status(c, t: sqlite3.Row, new: str, who: str, note: str = "", ip: str = "",
               ts: Optional[str] = None) -> None:
    if new not in TRANSITIONS.get(t["status"], ()):
        raise ValueError(f"{t['status']} -> {new} not allowed")
    ts = ts or now_utc()
    sets = ["status=?"]
    vals: List[Any] = [new]
    if new in ("RESPONDED", "IN_PROGRESS", "WAITING_OWNER", "RESOLVED") and not t["responded_utc"]:
        sets.append("responded_utc=?")
        vals.append(ts)
    if new == "RESOLVED":
        sets.append("resolved_utc=?")
        vals.append(ts)
    c.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", (*vals, t["id"]))
    c.commit()
    add_event(c, t["id"], who, "status", note, {"from": t["status"], "to": new}, ts=ts)
    audit(c, who, ip, "ticket_status", t["number"], f"{t['status']}->{new}")


def set_approval(c, t: sqlite3.Row, approve: bool, who: str, note: str = "", ip: str = "",
                 ts: Optional[str] = None) -> None:
    if t["approval"] != "pending":
        raise ValueError("no approval pending")
    ts = ts or now_utc()
    c.execute("UPDATE tickets SET approval=?, approved_utc=? WHERE id=?",
              ("approved" if approve else "rejected", ts if approve else "", t["id"]))
    c.commit()
    add_event(c, t["id"], who, "approval", note, {"approved": approve}, ts=ts)
    audit(c, who, ip, "ticket_approval", t["number"], "approved" if approve else "rejected")


def ticket_events(c, ticket_id: int) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()


def waiting_spans(events: Sequence[sqlite3.Row]) -> List[Tuple[str, Optional[str]]]:
    """[(start_utc, end_utc|None)] of WAITING_OWNER stretches (clock paused)."""
    spans: List[Tuple[str, Optional[str]]] = []
    start: Optional[str] = None
    for e in events:
        if e["kind"] != "status":
            continue
        m = json.loads(e["meta"] or "{}")
        if m.get("to") == "WAITING_OWNER" and start is None:
            start = e["ts_utc"]
        elif m.get("from") == "WAITING_OWNER" and start is not None:
            spans.append((start, e["ts_utc"]))
            start = None
    if start is not None:
        spans.append((start, None))
    return spans


# ------------------------------------------------------------- documents
def add_document(c, files_dir: str, site_code: str, folder: str, name: str, data: bytes,
                 mime: str, by: str, ticket_id: Optional[int] = None, ip: str = "") -> int:
    if folder not in FOLDER_KEYS and folder != "ticket":
        raise ValueError("unknown folder")
    sha = hashlib.sha256(data).hexdigest()
    d = os.path.join(files_dir, sha[:2])
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, sha)
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(data)
        os.chmod(p, 0o600)
    cur = c.execute("INSERT INTO documents (site_code,folder,name,size,sha256,mime,ticket_id,uploaded_by,uploaded_utc)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (site_code.upper(), folder, name[:200], len(data), sha, mime[:80], ticket_id, by, now_utc()))
    c.commit()
    audit(c, by, ip, "doc_upload", f"{site_code}/{folder}/{name}", f"{len(data)} bytes sha256={sha[:12]}")
    return int(cur.lastrowid)


def doc_path(files_dir: str, row: sqlite3.Row) -> str:
    return os.path.join(files_dir, row["sha256"][:2], row["sha256"])


# ------------------------------------------------------------- projects
def project_state(c) -> Dict[str, sqlite3.Row]:
    return {r["project_id"]: r for r in c.execute("SELECT * FROM project_state")}


def save_project(c, pid: str, stage: str, nxt: str, next_date: str, notes: str, by: str, ip: str = "") -> None:
    c.execute("INSERT OR REPLACE INTO project_state VALUES (?,?,?,?,?,?,?)",
              (pid, stage, nxt[:200], next_date[:20], notes[:2000], by, now_utc()))
    c.commit()
    audit(c, by, ip, "project_update", pid, f"stage={stage} next={nxt} {next_date}")


# ------------------------------------------------------------- catalogue (v293)
from argia.prologis import catalog as CAT    # noqa: E402  (pure, no cycle)

CAT_FIELDS = ("code", "category", "name_en", "name_es", "unit", "price_mxn", "desc_en", "desc_es",
              "includes_en", "includes_es", "lead_days", "orderable", "published", "basis", "sort", "active")


def _item(r) -> CAT.Item:
    return CAT.Item(code=r["code"], category=r["category"], name_en=r["name_en"], name_es=r["name_es"] or r["name_en"],
                    unit=r["unit"], price_mxn=r["price_mxn"], desc_en=r["desc_en"], desc_es=r["desc_es"],
                    includes_en=r["includes_en"], includes_es=r["includes_es"], lead_days=r["lead_days"],
                    orderable=bool(r["orderable"]), published=bool(r["published"]), basis=r["basis"],
                    sort=r["sort"], active=bool(r["active"]))


def catalog(c, include_drafts: bool = False, include_removed: bool = False) -> List[CAT.Item]:
    q = "SELECT * FROM catalog WHERE 1=1"
    if not include_drafts:
        q += " AND published=1"
    if not include_removed:
        q += " AND active=1"
    return [_item(r) for r in c.execute(q + " ORDER BY sort, code")]


def catalog_item(c, code: str) -> Optional[CAT.Item]:
    r = c.execute("SELECT * FROM catalog WHERE code=?", ((code or "").upper(),)).fetchone()
    return _item(r) if r else None


def save_item(c, it: CAT.Item, by: str, ip: str = "") -> None:
    errs = CAT.validate(it)
    if errs:
        raise ValueError("; ".join(errs))
    old = catalog_item(c, it.code)
    vals = [getattr(it, f) for f in CAT_FIELDS]
    vals = [int(v) if isinstance(v, bool) else v for v in vals]
    c.execute(f"INSERT OR REPLACE INTO catalog ({', '.join(CAT_FIELDS)}, updated_by, updated_utc)"
              f" VALUES ({', '.join('?' * len(CAT_FIELDS))}, ?, ?)", (*vals, by, now_utc()))
    c.commit()
    if old is None:
        audit(c, by, ip, "catalog_add", it.code, f"{it.name_en} price={it.price_mxn} unit={it.unit} published={it.published}")
    else:
        diff = [f"{f}: {getattr(old, f)!r} -> {getattr(it, f)!r}" for f in CAT_FIELDS if getattr(old, f) != getattr(it, f)]
        audit(c, by, ip, "catalog_edit", it.code, "; ".join(diff)[:1000] or "no change")


def remove_item(c, code: str, by: str, ip: str = "") -> None:
    """Soft removal: the item leaves the shop; past orders keep their lines."""
    c.execute("UPDATE catalog SET active=0, published=0, updated_by=?, updated_utc=? WHERE code=?", (by, now_utc(), code))
    c.commit()
    audit(c, by, ip, "catalog_remove", code)


def seed_catalog(c, items: Sequence[dict], by: str = "seed") -> int:
    """Insert items that do not exist yet (never overwrites a price someone set)."""
    n = 0
    for d in items:
        it = CAT.from_dict(d)
        if catalog_item(c, it.code) is None:
            save_item(c, it, by)
            n += 1
    return n


# ------------------------------------------------------------- cart and orders (v293)
def cart(c, username: str) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM cart WHERE username=? ORDER BY id", (username,)).fetchall()


def cart_add(c, username: str, lines: Sequence[CAT.Line], note: str = "") -> int:
    """Add lines; the same service for the same site is merged, not doubled:
    a per-site or per-kWp line is already there (skipped), a typed quantity
    is added to the existing line. Returns how many new rows went in."""
    n = 0
    for ln in lines:
        old = c.execute("SELECT id, qty FROM cart WHERE username=? AND item_code=? AND site_code=?",
                        (username, ln.item_code, ln.site_code)).fetchone()
        if old:
            if ln.unit not in CAT.AUTO_QTY:
                c.execute("UPDATE cart SET qty=? WHERE id=?", (min(old["qty"] + ln.qty, 100000), old["id"]))
            continue
        n += 1
        c.execute("INSERT INTO cart (username,item_code,site_code,qty,note,added_utc) VALUES (?,?,?,?,?,?)",
                  (username, ln.item_code, ln.site_code, ln.qty, note[:300], now_utc()))
    c.commit()
    return n


def cart_remove(c, username: str, row_id: int) -> None:
    c.execute("DELETE FROM cart WHERE id=? AND username=?", (row_id, username))
    c.commit()


def _cart_pairs(c, username: str) -> List[Tuple[sqlite3.Row, CAT.Line]]:
    out = []
    for r in cart(c, username):
        it = catalog_item(c, r["item_code"])
        if it and it.active and it.orderable and it.published:
            out.append((r, CAT.Line(it.code, it.name_en, it.unit, r["site_code"], r["qty"], it.price_mxn, it.lead_days)))
    return out


def cart_lines(c, username: str) -> List[CAT.Line]:
    """The cart priced at today's catalogue (removed or unpublished items drop out)."""
    return [ln for _, ln in _cart_pairs(c, username)]


def place_order(c, username: str, title: str, po: str = "", preferred: str = "", notes: str = "",
                ip: str = "", today: Optional[dt.date] = None, role: str = "manager") -> sqlite3.Row:
    """Turn the user's cart into one service order (a ticket of kind
    'order', number SO-NNNN) with its priced lines; empties the cart.

    Approval: a Prologis manager placing an order with every line priced
    has approved it by ordering (not_required). An order with a line on
    quote waits for ARGIA's price (quote_needed) and then for Prologis to
    accept it (pending). An order ARGIA's operator places for Prologis
    always waits for Prologis to accept it (pending)."""
    pairs = _cart_pairs(c, username)
    lines = [ln for _, ln in pairs]
    if not lines:
        raise ValueError("the cart is empty")
    today = today or dt.datetime.now(dt.timezone.utc).date()
    pref = dt.date.fromisoformat(preferred) if preferred else None
    target = CAT.target_date(today, lines, pref)
    num = _next(c, "SO-")
    tot = CAT.totals(lines)
    sites = sorted({ln.site_code for ln in lines if ln.site_code})
    approval = "quote_needed" if tot["on_quote"] else ("not_required" if role in PERMS["ticket_approve"] else "pending")
    c.execute("INSERT INTO tickets (number,site_code,title,description,sla_class,status,estimate_mxn,approval,detected_utc,"
              "created_by,created_utc,kind,po_number,preferred_date,target_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (num, sites[0] if len(sites) == 1 else ("PORTFOLIO" if not sites else "MULTI"), title.strip()[:160] or "Service order",
               notes.strip()[:5000], "OTHER", "ORDERED", tot["subtotal"], approval, now_utc(), username, now_utc(),
               "order", po.strip()[:60], preferred, target.isoformat()))
    tid = c.execute("SELECT id FROM tickets WHERE number=?", (num,)).fetchone()[0]
    for r, ln in pairs:
        c.execute("INSERT INTO order_lines (ticket_id,item_code,name,unit,site_code,qty,unit_price,lead_days,note) VALUES (?,?,?,?,?,?,?,?,?)",
                  (tid, ln.item_code, ln.name, ln.unit, ln.site_code, ln.qty, ln.unit_price, ln.lead_days, r["note"]))
    c.execute("DELETE FROM cart WHERE username=?", (username,))
    c.commit()
    add_event(c, tid, username, "created", notes[:500], {"order": True, "lines": len(lines), "subtotal": tot["subtotal"],
                                                         "po": po, "sites": sites})
    audit(c, username, ip, "order_place", num, f"{len(lines)} lines subtotal={tot['subtotal']} on_quote={tot['on_quote']} po={po}")
    return c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()


def open_orders(c) -> List[sqlite3.Row]:
    q = ",".join("?" * len(CAT.ORDER_OPEN))
    return c.execute(f"SELECT * FROM tickets WHERE kind='order' AND status IN ({q}) ORDER BY id DESC", CAT.ORDER_OPEN).fetchall()


def order_lines(c, ticket_id: int) -> List[sqlite3.Row]:
    return c.execute("SELECT * FROM order_lines WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()


def as_lines(rows: Sequence[sqlite3.Row]) -> List[CAT.Line]:
    return [CAT.Line(r["item_code"], r["name"], r["unit"], r["site_code"], r["qty"], r["unit_price"], r["lead_days"]) for r in rows]


def price_quote_line(c, t: sqlite3.Row, line_id: int, price: float, who: str, ip: str = "") -> None:
    """ARGIA prices an on-quote line; when no line is left unpriced the
    order waits for Prologis to accept the quote."""
    if price < 0 or price > 50_000_000:
        raise ValueError("price out of range")
    if t["status"] != "ORDERED":
        raise ValueError("prices are fixed once the order is confirmed")
    r = c.execute("SELECT * FROM order_lines WHERE id=? AND ticket_id=?", (line_id, t["id"])).fetchone()
    if not r:
        raise ValueError("no such line")
    it = catalog_item(c, r["item_code"])
    if it is not None and not it.on_quote:
        raise ValueError("a catalogue-priced line keeps its list price")
    c.execute("UPDATE order_lines SET unit_price=? WHERE id=?", (price, line_id))
    lines = as_lines(order_lines(c, t["id"]))
    tot = CAT.totals(lines)
    new_appr = "pending" if not tot["on_quote"] else "quote_needed"
    c.execute("UPDATE tickets SET estimate_mxn=?, approval=? WHERE id=?", (tot["subtotal"], new_appr, t["id"]))
    c.commit()
    add_event(c, t["id"], who, "quote", f"{r['name']}: MXN {price:,.2f}", {"line": line_id, "price": price})
    audit(c, who, ip, "order_quote", t["number"], f"line {line_id} price={price}")


def set_order_status(c, t: sqlite3.Row, new: str, who: str, role: str, note: str = "",
                     scheduled: str = "", ip: str = "", ts: Optional[str] = None) -> None:
    if new not in CAT.ORDER_TRANSITIONS.get(t["status"], ()):
        raise ValueError(f"{t['status']} -> {new} not allowed")
    if role not in PERMS["order_work"] and new not in CAT.CUSTOMER_MAY:
        raise PermissionError("only ARGIA moves an order forward")
    if new == "CONFIRMED" and t["approval"] not in ("not_required", "approved"):
        raise ValueError("the quote must be accepted before the order is confirmed")
    if new == "SCHEDULED" and not scheduled:
        raise ValueError("a scheduled date is required")
    ts = ts or now_utc()
    sets, vals = ["status=?"], [new]
    if scheduled:
        dt.date.fromisoformat(scheduled)
        sets.append("scheduled_date=?")
        vals.append(scheduled)
    if new in ("CONFIRMED",) and not t["responded_utc"]:
        sets.append("responded_utc=?")
        vals.append(ts)
    if new == "COMPLETED":
        sets.append("resolved_utc=?")
        vals.append(ts)
    c.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", (*vals, t["id"]))
    c.commit()
    add_event(c, t["id"], who, "status", note, {"from": t["status"], "to": new, "scheduled": scheduled}, ts=ts)
    audit(c, who, ip, "order_status", t["number"], f"{t['status']}->{new} {scheduled}")
