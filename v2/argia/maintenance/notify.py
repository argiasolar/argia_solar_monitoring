"""Ticket notifications (v227) - one place for the app, the daily job
and the mail-in job to tell the participants what changed.

Participants are portal usernames or bare e-mail addresses (external
technicians, customer contacts). The subject starts with ``[TK-NL1-0007]``
so a reply threads and ``scripts/ticket_mail_in.py`` can file it as a
comment on the ticket. Never raises - a mail problem must not break the
ticket action.
"""
from __future__ import annotations

import html
import logging
from typing import Callable, List, Optional, Sequence

from argia.maintenance import tickets as TK

LOG = logging.getLogger("argia.maintenance.notify")
PORTAL = "https://portal.argia.com.mx"


USERS_DB = "/opt/argia/auth/users.db"


def account_lookups(db_path: str = USERS_DB):
    """(email_of, name_of) reading the portal's users.db directly (no
    Flask) - for the jobs. Unknown usernames and bare e-mails resolve to
    themselves."""
    import sqlite3
    rows = {}
    try:
        c = sqlite3.connect(db_path)
        for u, first, last, email in c.execute("SELECT username, first_name, last_name, email FROM users"):
            rows[u] = ((" ".join(x for x in ((first or "").strip(), (last or "").strip()) if x) or u), (email or "").strip())
        c.close()
    except Exception as e:  # noqa: BLE001
        LOG.warning("users.db unreadable (%s)", e)

    def email_of(u: str) -> str:
        return u.strip().lower() if TK.is_email(u) else rows.get(u, ("", ""))[1]

    def name_of(u: str) -> str:
        if u == "monitoring":
            return "Monitoring"
        return u if TK.is_email(u) else rows.get(u, (u, ""))[0]
    return email_of, name_of


def address_of(identity: str, email_lookup: Callable[[str], str]) -> str:
    """An e-mail participant is its own address; a username resolves
    through the account table ('' when the account has none)."""
    if TK.is_email(identity):
        return identity.strip().lower()
    return (email_lookup(identity) or "").strip().lower()


def render(t: TK.Ticket, who_name: str, what: str, detail: str, plant: str, inverter: str,
           assignee_name: str, reply_hint: bool = True):
    """(subject, text, html) for one change. Pure."""
    subject = f"[{t.number}] {plant}: {t.title} - {TK.STATUS_LABEL.get(t.status, t.status)}"
    url = f"{PORTAL}/maintenance/t/{t.number}/"
    hint = "Reply to this mail to add a comment to the ticket." if reply_hint else ""
    text = (f"{what}\n{detail}\n\n{t.number} · {plant}" + (f" · {inverter}" if inverter else "")
            + f"\n{t.title}\nStatus: {TK.STATUS_LABEL.get(t.status, t.status)} · Priority: {t.priority}"
            f" · Assigned: {assignee_name or ' - '}\nBy: {who_name}\n\n{url}\n" + (f"\n{hint}\n" if hint else ""))
    e = html.escape
    htm = (f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a19;max-width:640px">'
           f'<div style="font-size:12px;color:#5f6368">{e(t.number)} · {e(plant)}' + (f' · {e(inverter)}' if inverter else '')
           + f'</div><div style="font-size:16px;font-weight:700;margin:2px 0 10px">{e(t.title)}</div>'
           f'<div style="padding:10px 12px;border-left:4px solid #05b1a9;background:#f4fbfa;margin-bottom:10px"><b>{e(what)}</b>'
           + (f'<div style="margin-top:4px;white-space:pre-wrap">{e(detail)}</div>' if detail else '')
           + f'<div style="margin-top:6px;color:#5f6368;font-size:12px">by {e(who_name)}</div></div>'
           f'<div style="font-size:13px;color:#41474f">Status <b>{e(TK.STATUS_LABEL.get(t.status, t.status))}</b> · '
           f'Priority <b>{e(t.priority)}</b> · Assigned <b>{e(assignee_name or " - ")}</b></div>'
           f'<p><a href="{url}">{url}</a></p>'
           + (f'<p style="font-size:12px;color:#9aa0a6">{e(hint)}</p>' if hint else '') + '</div>')
    return subject, text, htm


def status_watchers(plant_key: str, recipients=None, portal=None, excluded=None) -> List[str]:
    """v303 (Tomasz: "remind me about all tickets once a week or when the
    status changes"): the 'maintenance' mail subscribers who see this
    plant - they hear about every ticket's creation and status change,
    whether or not they are on the ticket. Portal users only, scoped to
    their plants, never for an excluded (CAPEX / hold) plant. [] on any
    failure: the participants are still mailed. The arguments are seams
    for tests; None reads PostgreSQL / users.db."""
    try:
        from argia.alerts import subscriptions as S
        excluded = S.load_excluded_plants() if excluded is None else excluded
        if not S.is_mailable(plant_key, excluded):
            return []
        rc = S.recipients_for("maintenance") if recipients is None else recipients
        rc = S.only_portal(rc, S.portal_emails() if portal is None else portal)
        pk = (plant_key or "").upper()
        return sorted({e.strip().lower() for e, scope in rc if e and (scope is None or pk in scope)})
    except Exception as e:  # noqa: BLE001
        LOG.warning("ticket status watchers unavailable (%s)", e)
        return []


def send(t: TK.Ticket, who: str, what: str, detail: str,
         email_lookup: Callable[[str], str], name_lookup: Callable[[str], str],
         plant: str, inverter: str, cfg: Optional[dict] = None, mailer=None,
         watchers: Sequence[str] = ()) -> int:
    """Mail the other participants - and (v303) ``watchers``, the
    maintenance subscribers, for a creation or a status change; never the
    person who made the change. Returns the number of addresses mailed
    (0 when nobody, no SMTP, or on any failure)."""
    try:
        from argia.alerts import emailer
        mailer = mailer or emailer
        me = address_of(who, email_lookup) if who else ""
        to = sorted(({address_of(u, email_lookup) for u in TK.recipients(t, who)}
                     | {w.strip().lower() for w in watchers}) - {"", me})
        if not to:
            return 0
        cfg = cfg or mailer.load_smtp()
        if not cfg:
            return 0
        subject, text, htm = render(t, name_lookup(who), what, detail, plant, inverter,
                                    name_lookup(t.assigned_to) if t.assigned_to else "")
        msg = mailer.build_html_email(subject, text, htm, cfg["SMTP_USER"], to)
        msg["Reply-To"] = cfg["SMTP_USER"]
        return len(to) if mailer.send(msg, cfg) else 0
    except Exception as e:  # noqa: BLE001
        LOG.warning("ticket notification failed: %s", e)
        return 0
