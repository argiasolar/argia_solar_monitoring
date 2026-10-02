"""SQLite store of the Prologis platform (v292): users, sessions, audit
log, tickets, ticket timeline, documents, project state.

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
    return c


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
def next_number(c) -> str:
    r = c.execute("SELECT max(id) AS m FROM tickets").fetchone()
    return f"PL-{(r['m'] or 0) + 1:04d}"


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
