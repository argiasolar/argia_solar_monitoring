#!/usr/bin/env python3
"""Argia_Mont - Monday reminder of every open maintenance ticket (v303).

Tomasz, 2026-10-03: "if there is open ticket stop sending me the same
status over and over again - remind me about all tickets once a week or
when the status changes". Since v303 an alert that belongs to an open
ticket is not repeated by the alert mails or the 19:00 mail; a status
change is mailed when it happens (argia.maintenance.notify, to the
participants and the maintenance subscribers); and this job sends, every
Monday at 07:10 MX, ONE mail with all open tickets:

    number · plant · inverter · title
    status · priority · assignee · open for · SLA
    last update (when, what) - flagged when nothing happened for 7 days
    the ticket's alerts that are still open (severity, issue)

Recipients: the 'maintenance' channel (portal users only), each scoped to
their plants, excluded (CAPEX / hold) plants never. No open ticket -> no
mail. No SMTP -> logged, nothing sent.

USAGE
    PYTHONPATH=. python scripts/ticket_weekly.py            # send
    PYTHONPATH=. python scripts/ticket_weekly.py --dry-run  # print, send nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as _h
import logging
import sys
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from argia.core.job_log import instrument
from argia.maintenance import tickets as TK

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
LOG = logging.getLogger("argia.ticket_weekly")

CHANNEL = "maintenance"
QUIET_DAYS = 7
"""A ticket without any update for this long is flagged in the mail."""
PORTAL = "https://portal.argia.com.mx/maintenance"
_PRIO = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
_SEV = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


@dataclass(frozen=True)
class Item:
    """One open ticket as the mail shows it."""
    ticket: TK.Ticket
    last_ts: Optional[dt.datetime]           # newest timeline event that is not an alert occurrence
    last_body: str
    alerts: Tuple[Tuple[str, str, str], ...] = field(default_factory=tuple)   # (severity, metric, inverter_sn), OPEN only


# ------------------------------------------------------------------ pure
def items(tickets: Sequence[TK.Ticket], last: Dict[int, Tuple[Optional[dt.datetime], str]],
          alerts: Dict[int, List[Tuple[str, str, str]]]) -> List[Item]:
    """Open tickets only, P1 first, then the oldest first. Pure."""
    out = [Item(t, *(last.get(t.id) or (None, "")),
                tuple(sorted(set(alerts.get(t.id, [])), key=lambda a: (_SEV.get(a[0], 3), a[1], a[2]))))
           for t in tickets if t.open]
    out.sort(key=lambda i: (_PRIO.get(i.ticket.priority, 9), TK.parse_ts(i.ticket.created_at)
                            or dt.datetime.max.replace(tzinfo=dt.timezone.utc), i.ticket.number))
    return out


def quiet_for(i: Item, now: dt.datetime) -> dt.timedelta:
    """Time since anything happened on the ticket (its creation counts)."""
    ref = i.last_ts or TK.parse_ts(i.ticket.updated_at) or TK.parse_ts(i.ticket.created_at)
    return max(dt.timedelta(0), now - ref) if ref else dt.timedelta(0)


def lines_for(i: Item, n, now: dt.datetime) -> List[str]:
    """The ticket in plain text: a head line and up to three detail lines. Pure."""
    t = i.ticket
    who = n.plant(t.plant_key) + (f" · {n.inverter(t.plant_key, t.inverter_sn)}" if t.inverter_sn else "")
    out = [f"{t.number} · {who} · {t.title}"]
    _sla, sla_txt = TK.sla_state(t, now)
    out.append(f"{TK.STATUS_LABEL.get(t.status, t.status)} · {t.priority} · "
               f"{('assigned to ' + t.assigned_to) if t.assigned_to else 'nobody assigned'} · "
               f"open {TK.fmt_age(TK.age(t, now))} · SLA: {sla_txt}")
    q = quiet_for(i, now)
    body = (i.last_body or "").strip().replace("\n", " ")
    upd = f"last update {TK.fmt_age(q)} ago" + (f": {body[:160]}{'…' if len(body) > 160 else ''}" if body else "")
    if q >= dt.timedelta(days=QUIET_DAYS):
        upd = f"NO UPDATE FOR {q.days} DAYS - " + upd
    out.append(upd)
    if i.alerts:
        from argia.alerts import naming
        out.append("still open: " + "; ".join(
            f"{sev} {naming.phrase(m)}" + (f" ({n.inverter(t.plant_key, sn)})" if sn and sn != t.inverter_sn else "")
            for sev, m, sn in i.alerts))
    else:
        out.append("no alert of this ticket is open any more")
    return out


def render(its: Sequence[Item], n, now: dt.datetime) -> Tuple[str, str, str]:
    """(subject, text, html). Pure."""
    e = lambda x: _h.escape(str(x))  # noqa: E731
    mx = now - dt.timedelta(hours=6)
    quiet = sum(1 for i in its if quiet_for(i, now) >= dt.timedelta(days=QUIET_DAYS))
    subject = (f"[ARGIA] {mx.day} {mx.strftime('%b')} - {len(its)} open maintenance ticket{'s' if len(its) != 1 else ''}"
               + (f", {quiet} without update for {QUIET_DAYS}+ days" if quiet else ""))
    intro = ("Weekly reminder of every open maintenance ticket. Their alerts are not repeated in the daily "
             "mails; every status change is mailed when it happens.")
    t = [f"ARGIA maintenance - open tickets, {mx:%Y-%m-%d}", "", intro, ""]
    h = ['<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a19;max-width:720px">',
         f'<div style="color:#5f6368;font-size:12px;margin-bottom:6px">ARGIA maintenance - open tickets, {e(f"{mx:%Y-%m-%d}")}</div>',
         f'<p style="margin:0 0 12px;color:#41474f">{e(intro)}</p>']
    for i in its:
        ls = lines_for(i, n, now)
        t.append(ls[0])
        t.extend(f"    {x}" for x in ls[1:])
        t.append(f"    {PORTAL}/t/{i.ticket.number}/")
        t.append("")
        stale = ls[2].startswith("NO UPDATE")
        h.append(f'<div style="margin:0 0 10px;padding:8px 12px;border-left:4px solid {"#a05c00" if stale else "#05b1a9"};'
                 f'background:{"#fdf0dc" if stale else "#f4fbfa"}">'
                 f'<div><a href="{PORTAL}/t/{e(i.ticket.number)}/" style="color:#05847d;font-weight:700">{e(i.ticket.number)}</a>'
                 f' · {e(ls[0].split(" · ", 1)[1])}</div>'
                 + "".join(f'<div style="font-size:12px;color:{"#a05c00" if (k == 1 and stale) else "#5f6368"};margin-top:2px">{e(x)}</div>'
                           for k, x in enumerate(ls[1:], start=0))
                 + '</div>')
    t.append(f"All tickets: {PORTAL}/")
    h.append(f'<p style="color:#9aa0a6;font-size:11px;margin-top:14px">ARGIA Monitoring · all tickets: {PORTAL}/</p></div>')
    return subject, "\n".join(t), "".join(h)


def views(its: Sequence[Item], recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]],
          excluded: FrozenSet[str]) -> List[Tuple[List[str], List[Item]]]:
    """(emails, items) per identical scoped view; nobody sees an excluded
    plant; a recipient with nothing visible gets no mail. Pure."""
    from argia.alerts import subscriptions as S
    mailable = [i for i in its if S.is_mailable(i.ticket.plant_key, excluded)]
    by_sig: Dict[tuple, List[str]] = {}
    payload: Dict[tuple, List[Item]] = {}
    for email, scope in recipients:
        mine = [i for i in mailable if scope is None or i.ticket.plant_key.upper() in scope]
        if not mine:
            continue
        sig = tuple(i.ticket.number for i in mine)
        by_sig.setdefault(sig, []).append(email)
        payload[sig] = mine
    return [(sorted(by_sig[s]), payload[s]) for s in sorted(by_sig)]


# ------------------------------------------------------------------ I/O
LAST_SQL = ("SELECT DISTINCT ON (ticket_id) ticket_id, ts::text AS ts, body FROM ticket_event"
            " WHERE kind <> 'alert' ORDER BY ticket_id, ts DESC;")
ALERTS_SQL = ("SELECT a.ticket_id, l.severity, l.metric, coalesce(l.inverter_sn, '') AS inverter_sn"
              " FROM ticket_alert a JOIN alert_ledger l ON l.alert_key = a.alert_key AND l.state = 'OPEN';")


def load(csv_of=None) -> List[Item]:
    """The open tickets with their last update and open alerts."""
    from argia.store.pgq import psql_csv
    q = csv_of or (lambda sql: psql_csv("SET statement_timeout = '30s';" + sql))
    tks = [TK.ticket_from_row(r) for r in TK.rows_from_csv(q(
        TK.SELECT_TICKETS + " WHERE status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION');"))]
    last = {int(r["ticket_id"]): (TK.parse_ts(r.get("ts") or ""), r.get("body") or "")
            for r in TK.rows_from_csv(q(LAST_SQL))}
    alerts: Dict[int, List[Tuple[str, str, str]]] = {}
    for r in TK.rows_from_csv(q(ALERTS_SQL)):
        alerts.setdefault(int(r["ticket_id"]), []).append((r["severity"] or "", r["metric"] or "", r["inverter_sn"] or ""))
    return items(tks, last, alerts)


@instrument("ticket_weekly")
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Monday reminder of every open maintenance ticket")
    p.add_argument("--dry-run", action="store_true", help="print the mails, send nothing")
    args = p.parse_args(argv)
    from argia.alerts import emailer, naming, subscriptions as S
    now = dt.datetime.now(dt.timezone.utc)
    its = load()
    if not its:
        LOG.info("no open maintenance ticket - no reminder")
        return 0
    rc = S.only_portal(S.recipients_for(CHANNEL), S.portal_emails())
    vs = views(its, rc, S.load_excluded_plants())
    if not vs:
        LOG.warning("%d open ticket(s) but no '%s' subscriber sees them", len(its), CHANNEL)
        return 0
    n = naming.load_names()
    cfg = None if args.dry_run else emailer.load_smtp()
    if cfg is None and not args.dry_run:
        LOG.warning("no SMTP config - weekly ticket reminder not sent")
        return 0
    for emails, mine in vs:
        subject, text, html = render(mine, n, now)
        if args.dry_run:
            LOG.info("[DRY RUN] would mail %s: %s", ", ".join(emails), subject)
            print(text)
            continue
        msg = emailer.build_html_email(subject, text, html, cfg["SMTP_USER"], emails)
        if emailer.send(msg, cfg):
            LOG.info("ticket reminder: %s -> %s", subject, ", ".join(emails))
        else:
            LOG.error("ticket reminder: send FAILED for %s", ", ".join(emails))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
