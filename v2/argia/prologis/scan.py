"""Malware scanning of the files uploaded to the Prologis platform (v324).

Every upload (document library and ticket attachments) is stored with
scan_status 'pending' and cannot be downloaded until the scanner has
passed it (fail closed). A timer (every 5 minutes) runs ClamAV's
``clamscan`` once over all pending files: one signature-database load
per batch instead of a resident daemon - the server has about 3 GB free
and the database alone needs about 1 GB, so clamd would sit on it all day
for a handful of uploads a week. An infected file is moved to a root-only
quarantine folder, every document with that content is marked 'infected',
the event is audited and mailed to the ARGIA desk through the outbox.

Before any of that, an upload that is an executable (Windows PE, ELF,
Mach-O, a script with a #! line) is refused outright, whatever its name.

Without clamscan installed nothing is ever marked clean: files stay
pending (not downloadable) and the run says so.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sqlite3
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

SCAN_CMD = ["clamscan", "--no-summary", "--infected", "--stdout"]
BATCH = 200
_EXEC_MAGIC = [(b"MZ", "Windows executable"), (b"\x7fELF", "Linux executable"), (b"#!", "script"),
               (b"\xfe\xed\xfa\xce", "macOS executable"), (b"\xfe\xed\xfa\xcf", "macOS executable"),
               (b"\xce\xfa\xed\xfe", "macOS executable"), (b"\xcf\xfa\xed\xfe", "macOS executable"),
               (b"\xca\xfe\xba\xbe", "macOS / Java executable")]
_FOUND = re.compile(r"^(.*): (.+) FOUND$")
_ERROR = re.compile(r"^(.*): (.+) ERROR$")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(documents)")}
    if have and "scan_status" not in have:
        # files uploaded before v324 start as pending too: they are scanned on the first run
        c.execute("ALTER TABLE documents ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'pending'")
        c.execute("ALTER TABLE documents ADD COLUMN scan_utc TEXT NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE documents ADD COLUMN scan_detail TEXT NOT NULL DEFAULT ''")
    c.commit()


def executable_kind(data: bytes) -> Optional[str]:
    """What kind of executable the bytes are, or None (pure)."""
    for magic, kind in _EXEC_MAGIC:
        if data.startswith(magic):
            return kind
    return None


def parse_output(text: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """clamscan --infected --stdout lines -> ({path: signature}, {path: error})."""
    found: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for ln in (text or "").splitlines():
        ln = ln.strip()
        m = _FOUND.match(ln)
        if m:
            found[m.group(1)] = m.group(2)
            continue
        m = _ERROR.match(ln)
        if m:
            errors[m.group(1)] = m.group(2)
    return found, errors


def scan_pending(c, files_dir: str, quarantine_dir: str, cmd: Optional[Sequence[str]] = None,
                 run=subprocess.run, which=shutil.which) -> Dict[str, object]:
    """One pass over the pending uploads. Returns counts (and 'skipped'
    with the reason when nothing could be scanned)."""
    from argia.prologis import store as S
    cmd = list(cmd or SCAN_CMD)
    rows = c.execute("SELECT * FROM documents WHERE scan_status='pending' AND deleted=0 ORDER BY id LIMIT ?", (BATCH,)).fetchall()
    out: Dict[str, object] = {"pending": len(rows), "clean": 0, "infected": 0, "errors": 0, "missing": 0}
    if not rows:
        return out
    if not which(cmd[0]):
        out["skipped"] = f"{cmd[0]} is not installed - uploads stay blocked until it is"
        return out
    paths: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        p = S.doc_path(files_dir, r)
        if not os.path.exists(p):
            c.execute("UPDATE documents SET scan_status='error', scan_utc=?, scan_detail='file missing' WHERE id=?", (now_utc(), r["id"]))
            out["missing"] += 1
            continue
        paths.setdefault(p, []).append(r)
    c.commit()
    if not paths:
        return out
    try:
        res = run(cmd + sorted(paths), capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["skipped"] = f"scanner failed to run: {type(e).__name__}"
        return out
    if res.returncode not in (0, 1, 2):
        out["skipped"] = f"scanner exit {res.returncode}"
        return out
    found, errors = parse_output(res.stdout)
    os.makedirs(quarantine_dir, mode=0o700, exist_ok=True)
    ts = now_utc()
    for p, docs in paths.items():
        if p in found:
            q = os.path.join(quarantine_dir, os.path.basename(p))
            try:
                shutil.move(p, q)
                os.chmod(q, 0o600)
            except OSError:
                pass
            for r in c.execute("SELECT * FROM documents WHERE sha256=?", (docs[0]["sha256"],)).fetchall():
                c.execute("UPDATE documents SET scan_status='infected', scan_utc=?, scan_detail=? WHERE id=?", (ts, found[p][:200], r["id"]))
                S.audit(c, "scanner", "", "upload_infected", f"{r['site_code']}/{r['folder']}/{r['name']}", f"{found[p]} sha256={r['sha256'][:12]}")
                out["infected"] += 1
        elif p in errors or res.returncode == 2:
            if p in errors:
                for r in docs:
                    c.execute("UPDATE documents SET scan_status='error', scan_utc=?, scan_detail=? WHERE id=?", (ts, errors[p][:200], r["id"]))
                out["errors"] += len(docs)
            # exit 2 without a line for this file: leave it pending, the next run retries
        else:
            for r in docs:
                c.execute("UPDATE documents SET scan_status='clean', scan_utc=? WHERE id=?", (ts, r["id"]))
            out["clean"] += len(docs)
    c.commit()
    return out


def infected_mail(rows: Sequence[sqlite3.Row]) -> Tuple[str, str]:
    lines = "\n".join(f"- {r['name']} ({r['site_code'] or 'portfolio'} / {r['folder']}), uploaded by {r['uploaded_by']}: {r['scan_detail']}"
                      for r in rows)
    return (f"[ARGIA for Prologis] Malware found in {len(rows)} upload(s) - quarantined",
            "The malware scan quarantined these uploads; they cannot be downloaded:\n" + lines +
            "\n\nCheck with the uploader and the device it came from.\n")
