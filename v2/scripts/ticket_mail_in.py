"""Reply-by-mail for maintenance tickets (v227).

Every ticket notification goes out as ``[TK-NL1-0007] …`` from
service@argia.com.mx. When a participant replies, this job reads the
mailbox over IMAP, files the reply as a comment on the ticket (actor =
the sender's address; a portal account is recognised by its e-mail),
saves attachments, notifies the other participants and marks the mail
read. Only participants of the ticket may comment this way — anything
else is left unread and logged.

Configuration (in /root/.argia_mail, next to the SMTP keys — never in
the repo):

    IMAP_HOST=imap.gmail.com
    IMAP_USER=service@argia.com.mx
    IMAP_PASS=<app password>
    IMAP_FOLDER=INBOX            (optional)

Without those keys the job logs "IMAP not configured" and exits 0 —
the timer stays harmless until the mailbox is set up.

    ticket_mail_in.py            # process unread replies
    ticket_mail_in.py --dry-run  # parse and print, change nothing
"""
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import logging
import os
import re
import sys
import uuid
from typing import Dict, List, Optional, Tuple

from argia.alerts import emailer
from argia.maintenance import notify as NOTIFY
from argia.maintenance import tickets as TK
from argia.store.pgq import psql_csv, psql_exec

LOG = logging.getLogger("argia.ticket_mail_in")
FILES_DIR = os.environ.get("ARGIA_TICKET_FILES", "/opt/argia/tickets")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".txt", ".csv", ".xlsx", ".docx", ".zip", ".mp4", ".heic"}
MAX_BYTES = 15 * 1024 * 1024


# ----------------------------------------------------------------- pure
def imap_config(cfg: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """The IMAP keys of the mail config, or None when any is missing."""
    if not cfg or not all(cfg.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS")):
        return None
    return {"host": cfg["IMAP_HOST"], "user": cfg["IMAP_USER"], "password": cfg["IMAP_PASS"],
            "folder": cfg.get("IMAP_FOLDER") or "INBOX"}


def sender_address(msg) -> str:
    raw = msg.get("From", "") or ""
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).strip().lower()


def parse_message(raw: bytes) -> Tuple[Optional[str], str, str, List[Tuple[str, bytes, str]]]:
    """(ticket number, sender, comment text, [(filename, bytes, mime)])
    from a raw RFC 822 message. Pure."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    number = TK.reply_ticket_number(msg.get("Subject", "") or "")
    sender = sender_address(msg)
    body = ""
    files: List[Tuple[str, bytes, str]] = []
    for part in msg.walk():
        cd = part.get_content_disposition()
        if cd == "attachment" or (part.get_filename() and not part.get_content_type().startswith("text/")):
            fn = os.path.basename(part.get_filename() or "file")
            ext = os.path.splitext(fn)[1].lower()
            data = part.get_payload(decode=True) or b""
            if ext in ALLOWED_EXT and 0 < len(data) <= MAX_BYTES:
                files.append((fn, data, part.get_content_type()))
        elif part.get_content_type() == "text/plain" and not body:
            try:
                body = part.get_content()
            except Exception:  # noqa: BLE001
                body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if not body:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    h = part.get_content()
                except Exception:  # noqa: BLE001
                    h = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                body = re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>", "\n", h))
                break
    return number, sender, TK.strip_reply(body), files


def may_comment(t: TK.Ticket, sender: str, email_of) -> Optional[str]:
    """The participant identity when the sender is on the ticket, else None."""
    for u in TK.participants(t):
        if NOTIFY.address_of(u, email_of) == sender:
            return u
    return None


# ------------------------------------------------------------------ I/O
def load_ticket(number: str) -> Optional[TK.Ticket]:
    rows = TK.rows_from_csv(psql_csv(TK.SELECT_TICKETS + f" WHERE number = {TK._txt(number)};"))
    return TK.ticket_from_row(rows[0]) if rows else None


def file_comment(t: TK.Ticket, who: str, text: str, files, dry_run: bool) -> int:
    if dry_run:
        LOG.info("[DRY RUN] %s <- %s: %s (%d file(s))", t.number, who, text[:80], len(files))
        return 0
    from argia.store.pgq import _run
    ev = _run(TK.event_sql(t.id, who, "comment" if text else "attachment", text or "(attachment by mail)",
                           {"via": "mail"}), ["-t", "-A"])
    eid = next((int(x) for x in ev.splitlines() if x.strip().isdigit()), None)
    saved = []
    for fn, data, mime in files:
        stored = f"{uuid.uuid4().hex}{os.path.splitext(fn)[1].lower()}"
        d = os.path.join(FILES_DIR, t.number)
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, stored), "wb") as fh:
            fh.write(data)
        psql_exec(TK.attachment_sql(t.id, eid, re.sub(r"[^A-Za-z0-9._-]+", "_", fn)[:80], stored, len(data), mime, who))
        saved.append(fn)
    email_of, name_of = NOTIFY.account_lookups()
    from argia.alerts import naming
    n = naming.load_names()
    NOTIFY.send(t, who, "Update by mail", text + (("\nFiles: " + ", ".join(saved)) if saved else ""), email_of, name_of,
                n.plant(t.plant_key), n.inverter(t.plant_key, t.inverter_sn) if t.inverter_sn else "")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="file ticket replies from the service mailbox")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = imap_config(emailer.load_smtp())
    if cfg is None:
        LOG.info("IMAP not configured (IMAP_HOST/IMAP_USER/IMAP_PASS in the mail config) — nothing to do")
        return 0
    email_of, _name_of = NOTIFY.account_lookups()
    try:
        box = imaplib.IMAP4_SSL(cfg["host"])
        box.login(cfg["user"], cfg["password"])
        box.select(cfg["folder"])
        _typ, data = box.search(None, "UNSEEN")
    except Exception as e:  # noqa: BLE001
        LOG.error("IMAP failed: %s", e)
        return 1
    ids = data[0].split() if data and data[0] else []
    LOG.info("%d unread message(s)", len(ids))
    n_filed = 0
    for mid in ids:
        try:
            _typ, msgdata = box.fetch(mid, "(BODY.PEEK[])")
            raw = msgdata[0][1]
            number, sender, text, files = parse_message(raw)
            if not number:
                LOG.info("mail from %s without a ticket number — left unread", sender)
                continue
            t = load_ticket(number)
            if t is None:
                LOG.info("mail from %s for unknown ticket %s — left unread", sender, number)
                continue
            who = may_comment(t, sender, email_of)
            if who is None:
                LOG.warning("mail from %s is not a participant of %s — ignored", sender, number)
                if not a.dry_run:
                    box.store(mid, "+FLAGS", "\\Seen")
                continue
            if not text and not files:
                LOG.info("empty reply from %s on %s", sender, number)
            else:
                n_filed += file_comment(t, who, text, files, a.dry_run)
            if not a.dry_run:
                box.store(mid, "+FLAGS", "\\Seen")
        except Exception as e:  # noqa: BLE001
            LOG.error("message %s: %s", mid, e)
    try:
        box.logout()
    except Exception:  # noqa: BLE001
        pass
    LOG.info("DONE: %d comment(s) filed dry_run=%s", n_filed, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
