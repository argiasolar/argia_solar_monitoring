"""E-mail for alert-engine (ledger) alerts — the server side of what the
Apps Script notifier did from the Alerts sheet tab (v196).

The notifier mailed every newly OPEN row of the ``Alerts`` tab once to
the 'om' recipients and remembered the ids in Alert_Notifications. It
stopped stamping on 2026-09-02, and since v194 the ledger lives in
PostgreSQL, so the sheet path is gone for good. This module does the
same job from the ledger the engine just reconciled:

* candidates: records that are OPEN and whose ``channels_sent`` does not
  contain 'email' (the ledger's own memory — no second table);
* recipients: the 'maintenance' channel of mail_subscription (managed in
  /setup/, portal users only), each scoped to their plants (v176) —
  ledger alerts always name a plant, so scoping is exact; a subscriber
  with no visible alert gets no mail;
* one message per identical view, plain text like the notifier's;
* after a successful send the records are returned with 'email' added
  to channels_sent, so the caller's write_ledger persists the memory.

Without /root/.argia_mail (no SMTP) nothing is sent and nothing is
marked — the alerts stay unmailed and are retried next run, logged.

v223 (Tomasz, 2026-09-07, after a 10-alert morning mail): ONE readable
mail, not ten cards. CRITICAL first, then WARNING; inside a severity the
alerts are grouped plant → issue type → inverters on one line, the
per-inverter facts in small print under it; the long explanation appears
ONCE per issue type at the bottom ("What these mean"), not under every
alert; a "still open" line per plant replaces the old daily_digest
pseudo-alert. Intraday ticks mail CRITICAL only (``severities``) — a
WARNING waits for the morning mail. Severity rule the mail follows:
CRITICAL = energy is being lost or a plant/inverter is off; everything
about data, heat without loss or a flag without loss is WARNING.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from argia.core.alerts_state import AlertRecord, AlertState, mark_channels_sent

LOG = logging.getLogger("argia.alerts.ledger_mail")

CHANNEL = "maintenance"
SUBJECT_PREFIX = "[ARGIA]"
MAX_PER_RUN = 20          # the notifier's safety valve


# ---------------------------------------------------------------- pure

MAILED_SEVERITIES = ("WARNING", "CRITICAL")
"""v220: INFO alerts (a flag without production evidence) live in the
ledger and on the portal; nobody is mailed about them."""


def unmailed(records: Sequence[AlertRecord],
             severities: Sequence[str] = MAILED_SEVERITIES) -> List[AlertRecord]:
    """OPEN records of the given severities never mailed, oldest first
    (the digest pseudo-alert never; v223)."""
    sevs = {x.upper() for x in severities}
    out = [r for r in records if r.state == AlertState.OPEN
           and (r.severity or "").upper() in sevs and r.metric != DIGEST_METRIC
           and "email" not in {c.strip() for c in r.channels_sent.split(",")}]
    out.sort(key=lambda r: (r.opened_utc, r.alert_id))
    return out


DIGEST_METRIC = "daily_digest"
_TAG = re.compile(r"\s*\[(?:CRITICAL|WARNING|INFO)\]\s*$")
_BRACKET_PK = re.compile(r"^\[[A-Z0-9]{3,6}\]\s*")
_RANK = {"CRITICAL": 0, "WARNING": 1}


def clean_message(r: AlertRecord, n) -> str:
    """The alert text for people: no 'PK SN: ' prefix, no trailing
    '[SEVERITY]' tag (the pill says it), codes replaced by names."""
    return _TAG.sub("", n.text(_BRACKET_PK.sub("", r.message or ""), r.plant_key, r.inverter_sn)).strip()


def _age_days(r: AlertRecord, now_utc: Optional[dt.datetime]) -> int:
    if now_utc is None:
        return 0
    try:
        opened = dt.datetime.fromisoformat(r.opened_utc)
    except (TypeError, ValueError):
        return 0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)
    return max(0, (now_utc.date() - opened.date()).days)


def _inv_sort(n):
    """Sort key: 'Inverter 10' after 'Inverter 2' (natural), label first, SN as tie-break."""
    def key(a: AlertRecord):
        lab = n.inverter(a.plant_key, a.inverter_sn) if a.inverter_sn else ""
        parts = re.split(r"(\d+)", lab)
        return ([(int(x) if x.isdigit() else x.lower()) for x in parts], a.inverter_sn, a.alert_id)
    return key


def grouped(alerts: Sequence[AlertRecord], n=None) -> List[Tuple[str, str, List[Tuple[str, str, List[AlertRecord]]]]]:
    """[(severity, plant_key, [(metric, plant_key, alerts)])] — CRITICAL
    plants first, plants alphabetical, metrics alphabetical, inverters in
    label order (Inverter 1, 2, … 10). One entry per (severity, plant).
    Pure."""
    n = n or _names(None)
    tree: Dict[Tuple[int, str], Dict[str, List[AlertRecord]]] = {}
    for a in alerts:
        sev = (a.severity or "").upper()
        tree.setdefault((_RANK.get(sev, 2), a.plant_key or ""), {}).setdefault(a.metric, []).append(a)
    out = []
    for (rank, pk) in sorted(tree):
        sev = "CRITICAL" if rank == 0 else "WARNING" if rank == 1 else "INFO"
        mets = [(m, pk, sorted(items, key=_inv_sort(n)))
                for m, items in sorted(tree[(rank, pk)].items())]
        out.append((sev, pk, mets))
    return out


def still_open_lines(records: Sequence[AlertRecord], exclude_ids: set, n,
                     now_utc: Optional[dt.datetime]) -> Tuple[int, int, List[str]]:
    """(n_critical, n_warning, ['Plastic Omnium: inverter running hot ×4 (12 d)',
    ...]) for OPEN WARNING/CRITICAL alerts that are not in this mail —
    the reminder that silence means all clear (v223, replaces the
    daily_digest pseudo-alert). Pure."""
    rows = [r for r in records if r.state == AlertState.OPEN and r.metric != DIGEST_METRIC
            and (r.severity or "").upper() in ("WARNING", "CRITICAL") and r.alert_id not in exclude_ids]
    n_crit = sum(1 for r in rows if (r.severity or "").upper() == "CRITICAL")
    groups: Dict[Tuple[int, str, str], List[AlertRecord]] = {}
    for r in rows:
        groups.setdefault((_RANK.get((r.severity or "").upper(), 2), r.plant_key, r.metric), []).append(r)
    lines = []
    for (rank, pk, metric), items in sorted(groups.items()):
        from argia.alerts import naming
        age = max(_age_days(r, now_utc) for r in items)
        who = ", ".join(n.inverter(pk, r.inverter_sn) for r in items if r.inverter_sn)
        lines.append(f"{'CRITICAL' if rank == 0 else 'WARNING'} · {n.plant(pk)}: {naming.phrase(metric)}"
                     + (f" ({who})" if who else "") + f" — {age} d")
    return n_crit, len(rows) - n_crit, lines


def glossary(alerts: Sequence[AlertRecord]) -> List[Tuple[str, str]]:
    """[(phrase, explanation)] once per issue type in the mail. Pure."""
    from argia.alerts import explanations, naming
    seen: Dict[str, str] = {}
    for a in alerts:
        if a.metric not in seen:
            seen[a.metric] = explanations.explain(a.metric) or ""
    return [(naming.phrase(m), txt) for m, txt in seen.items() if txt]


def in_hand(alerts: Sequence[AlertRecord], still_open: Sequence[AlertRecord],
            tickets: Dict[str, "object"], n, now_utc: Optional[dt.datetime]) -> Tuple[List[AlertRecord], List[str]]:
    """v226: alerts whose key has an OPEN maintenance ticket are 'in
    hand'. Returns (those alerts — new or still open — , one line per
    ticket: 'TK-NL1-0007 · In progress · juan · 12 d — last update: …
    (inverter running hot — Inverter 1, Inverter 4)'). Pure."""
    from argia.alerts import naming
    from argia.maintenance import tickets as TK
    if not tickets:
        return [], []
    seen: Dict[str, List[AlertRecord]] = {}
    handled: List[AlertRecord] = []
    for a in list(alerts) + [r for r in still_open if r.state == AlertState.OPEN and r.metric != DIGEST_METRIC]:
        b = tickets.get(a.alert_key)
        if b is None or not getattr(b, "open", False):
            continue
        if any(x.alert_id == a.alert_id for x in seen.setdefault(b.number, [])):
            continue
        seen[b.number].append(a)
        handled.append(a)
    lines = []
    for number, items in sorted(seen.items()):
        b = tickets[items[0].alert_key]
        t = TK.Ticket(0, number, items[0].plant_key, "", "", "", "", b.priority, b.status, "", b.assigned_to,
                      b.created_at, b.created_at)
        head = TK.progress_line(t, TK.Event(0, 0, "", "", "comment", b.last_update) if b.last_update else None,
                                now_utc or dt.datetime.now(dt.timezone.utc))
        what = "; ".join(sorted({f"{naming.phrase(a.metric)}" + (f" — {n.inverter(a.plant_key, a.inverter_sn)}" if a.inverter_sn else "")
                                 for a in items}))
        lines.append(f"{n.plant(items[0].plant_key)}: {head} ({what})")
    return handled, lines


def render_mail(alerts: Sequence[AlertRecord], labels=None,
                still_open: Sequence[AlertRecord] = (),
                when_mx: str = "", now_utc: Optional[dt.datetime] = None,
                tickets: Optional[Dict[str, "object"]] = None) -> Tuple[str, str, str]:
    """(subject, text, html) — the one mail format (v223). ``alerts`` are
    the new ones; ``still_open`` the ledger (or the recipient's view of
    it) for the reminder section; ``tickets`` (v226, {alert_key:
    TicketBrief}) turns alerts that have an open ticket into the
    ticket's progress line instead of a repeated warning. Pure."""
    import html as _h
    from argia.alerts import naming
    n = _names(labels)
    e = lambda x: _h.escape(str(x))  # noqa: E731
    handled, hand_lines = in_hand(alerts, still_open, tickets or {}, n, now_utc)
    handled_ids = {a.alert_id for a in handled}
    alerts = [a for a in alerts if a.alert_id not in handled_ids]
    n_crit = sum(1 for a in alerts if (a.severity or "").upper() == "CRITICAL")
    n_warn = sum(1 for a in alerts if (a.severity or "").upper() == "WARNING")
    ids = {a.alert_id for a in alerts} | handled_ids
    so_crit, so_warn, so_lines = still_open_lines(still_open, ids, n, now_utc)
    plants = sorted({n.plant(a.plant_key) for a in alerts})
    day = ""
    try:
        d0 = dt.date.fromisoformat(when_mx.split(" ")[0])
        day = f"{d0.day} {d0.strftime('%b')}"
    except ValueError:
        pass
    head = f"{SUBJECT_PREFIX} {day} — " if day else f"{SUBJECT_PREFIX} "
    if alerts:
        parts = ([f"{n_crit} critical"] if n_crit else []) + ([f"{n_warn} warning{'s' if n_warn != 1 else ''}"] if n_warn else [])
        subject = head + ", ".join(parts) + f" ({', '.join(plants)})"
    elif hand_lines:
        subject = head + f"nothing new — {len(hand_lines)} ticket{'s' if len(hand_lines) != 1 else ''} in hand"
    else:
        subject = head + f"nothing new — {so_crit} critical still open"
    # ---- text
    t = [f"ARGIA monitoring — {when_mx} MX" if when_mx else "ARGIA monitoring", ""]
    cur_sev = None
    for sev, pk, mets in grouped(alerts, n):
        if sev != cur_sev:
            t.append(f"{sev} — new" if sev in ("CRITICAL", "WARNING") else sev)
            cur_sev = sev
        t.append(f"  {n.plant_full(pk)}")
        for metric, _pk, items in mets:
            who = ", ".join(n.inverter(pk, a.inverter_sn) for a in items if a.inverter_sn)
            t.append(f"    {naming.phrase(metric)}" + (f" — {who}" if who else ""))
            for a in items:
                lab = n.inverter(pk, a.inverter_sn) + ": " if a.inverter_sn else ""
                t.append(f"      {lab}{clean_message(a, n)}  ({a.alert_id})")
        t.append("")
    if hand_lines:
        t.append("In hand — open maintenance tickets")
        t.extend(f"  {ln}" for ln in hand_lines)
        t.append("")
    if so_lines:
        t.append(f"Still open from previous days: {so_crit} critical / {so_warn} warning")
        t.extend(f"  {ln}" for ln in so_lines)
        t.append("")
    gl = glossary(alerts)
    if gl:
        t.append("What these mean")
        t.extend(f"  {ph}: {txt}" for ph, txt in gl)
        t.append("")
    t.append("Portal: https://portal.argia.com.mx/monitoring/")
    text = "\n".join(t)
    # ---- html
    h = ['<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a19;max-width:720px">',
         f'<div style="color:#5f6368;font-size:12px;margin-bottom:8px">ARGIA monitoring — {e(when_mx)} MX</div>' if when_mx else ""]
    cur_sev = None
    for sev, pk, mets in grouped(alerts, n):
        fg, bg = _SEV_COLOR.get(sev, ("#5f6368", "#eceef0"))
        if sev != cur_sev:
            h.append(f'<div style="margin:16px 0 6px;padding:6px 12px;border-left:6px solid {fg};background:{bg};'
                     f'font-weight:700;font-size:15px;color:{fg}">{e(sev)}<span style="font-weight:400;color:#5f6368;margin-left:8px">new</span></div>')
            cur_sev = sev
        h.append(f'<div style="margin:8px 0 2px 12px;font-weight:700">{e(n.plant_full(pk))}</div>')
        for metric, _pk, items in mets:
            who = ", ".join(n.inverter(pk, a.inverter_sn) for a in items if a.inverter_sn)
            h.append(f'<div style="margin:2px 0 0 24px">'
                     f'<span style="display:inline-block;padding:0 7px;border-radius:10px;font-size:11px;font-weight:700;color:{fg};background:{bg}">{e(sev)}</span> '
                     f'<b>{e(naming.phrase(metric))}</b>' + (f' — {e(who)}' if who else "") + '</div>')
            for a in items:
                lab = n.inverter(pk, a.inverter_sn) + ": " if a.inverter_sn else ""
                h.append(f'<div style="margin:0 0 2px 36px;color:#5f6368;font-size:12px">{e(lab)}{e(clean_message(a, n))}'
                         f' <span style="color:#b0b4b8">{e(a.alert_id)}</span></div>')
    if hand_lines:
        h.append('<div style="margin:18px 0 4px;font-weight:700">In hand <span style="font-weight:400;color:#5f6368">open maintenance tickets</span></div>')
        for ln in hand_lines:
            num = ln.split(": ", 1)[1].split(" · ")[0] if ": " in ln else ""
            link = f'https://portal.argia.com.mx/maintenance/t/{num}/' if num.startswith("TK-") else ""
            h.append(f'<div style="margin:0 0 3px 12px;font-size:12px;color:#05847d">' + (f'<a href="{link}" style="color:#05847d">' if link else '')
                     + e(ln) + ('</a>' if link else '') + '</div>')
    if so_lines:
        h.append(f'<div style="margin:18px 0 4px;font-weight:700">Still open from previous days '
                 f'<span style="font-weight:400;color:#5f6368">{so_crit} critical / {so_warn} warning</span></div>')
        for ln in so_lines:
            c = _SEV_COLOR["CRITICAL"][0] if ln.startswith("CRITICAL") else "#5f6368"
            h.append(f'<div style="margin:0 0 2px 12px;font-size:12px;color:{c}">{e(ln)}</div>')
    if gl:
        h.append('<div style="margin:18px 0 4px;font-weight:700;color:#5f6368">What these mean</div>')
        for ph, txt in gl:
            h.append(f'<div style="margin:0 0 6px 12px;font-size:12px;color:#5f6368"><b>{e(ph)}</b> — {e(txt)}</div>')
    h.append('<p style="color:#9aa0a6;font-size:11px;margin-top:16px">ARGIA Monitoring · '
             'portal: https://portal.argia.com.mx/monitoring/ · CRITICAL = energy being lost or a unit off; '
             'WARNING = data, heat or a flag without a measured loss</p></div>')
    return subject, text, "".join(h)


def _names(labels):
    """v217: ``labels`` may be the old {plant_key: name} dict or a
    naming.Names; either way a Names comes out (codes-only for None)."""
    from argia.alerts import naming
    if isinstance(labels, naming.Names):
        return labels
    return naming.Names(labels or {})


_SEV_COLOR = {"CRITICAL": ("#c5221f", "#fde7e9"), "WARNING": ("#a05c00", "#fdf0dc")}


def group_views(alerts: Sequence[AlertRecord],
                recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]]
                ) -> List[Tuple[List[str], List[AlertRecord]]]:
    """(emails, alerts) per identical scoped view. A scope of None sees
    everything; a limited scope sees only its plants. Pure."""
    views: Dict[tuple, List[str]] = {}
    payload: Dict[tuple, List[AlertRecord]] = {}
    for email, scope in recipients:
        mine = [a for a in alerts
                if scope is None or a.plant_key.upper() in scope]
        if not mine:
            continue
        sig = tuple(a.alert_id for a in mine)
        views.setdefault(sig, []).append(email)
        payload[sig] = mine
    return [(sorted(views[sig]), payload[sig]) for sig in sorted(views)]


def mark_mailed(records: Sequence[AlertRecord], mailed_ids: set,
                ) -> List[AlertRecord]:
    return [mark_channels_sent(r, ["email"]) if r.alert_id in mailed_ids else r
            for r in records]


# ---------------------------------------------------------------- I/O

def plant_labels():
    """v217: the naming layer (plants by customer name, inverters by
    label) for the headers and the text; codes-only on error."""
    from argia.alerts import naming
    return naming.load_names()


def short_customer(name: str) -> str:
    """'PLASTIC OMNIUM PPA land (Monterrey, NL)' -> 'Plastic Omnium'. Pure."""
    from argia.alerts import naming
    return naming.short_customer(name)


def recipients():
    from argia.alerts import subscriptions
    return subscriptions.only_portal(subscriptions.recipients_for(CHANNEL),
                                     subscriptions.portal_emails())


def mail_new_alerts(records: Sequence[AlertRecord],
                    dry_run: bool = False,
                    severities: Sequence[str] = MAILED_SEVERITIES,
                    morning: bool = False,
                    when_mx: str = "",
                    now_utc: Optional[dt.datetime] = None,
                    tickets: Optional[Dict[str, "object"]] = None) -> List[AlertRecord]:
    """Mail every unmailed OPEN alert of ``severities`` to its subscribers;
    return the records with 'email' marked on the ones that went out.
    ``morning`` (the 06:30 daily run) adds the still-open reminder and
    sends even without news when a CRITICAL is still open. Never raises
    — alerting must not crash the engine run."""
    from argia.alerts import emailer, subscriptions
    excluded = subscriptions.load_excluded_plants()
    cands = [r for r in unmailed(records, severities)
             if subscriptions.is_mailable(r.plant_key, excluded)][:MAX_PER_RUN]
    open_crit = [r for r in records if r.state == AlertState.OPEN and r.metric != DIGEST_METRIC
                 and (r.severity or "").upper() == "CRITICAL"
                 and subscriptions.is_mailable(r.plant_key, excluded)]
    if not cands and not (morning and open_crit):
        return list(records)
    tickets = tickets or {}
    try:
        rcpts = recipients()
    except Exception as e:  # noqa: BLE001
        LOG.warning("ledger mail: recipients unavailable (%s) — %d alert(s) "
                    "stay unmailed", e, len(cands))
        return list(records)
    if not rcpts:
        LOG.warning("ledger mail: no '%s' subscribers — %d alert(s) stay "
                    "unmailed (subscribe in /setup/)", CHANNEL, len(cands))
        return list(records)
    views = group_views(cands, rcpts)
    if not views and cands:
        LOG.info("ledger mail: %d new alert(s), no subscriber sees them — "
                 "marking as handled", len(cands))
        # nobody to mail: don't retry forever, the ledger is the record
        return mark_mailed(records, {a.alert_id for a in cands})
    if not views:                       # morning reminder without news
        views = [([email for email, _ in rcpts], [])]
    mailable_open = [r for r in records if subscriptions.is_mailable(r.plant_key, excluded)] if morning else []
    cfg = None if dry_run else emailer.load_smtp()
    if cfg is None and not dry_run:
        LOG.warning("ledger mail: no SMTP config — %d alert(s) stay unmailed",
                    len(cands))
        return list(records)
    mailed: set = set()
    labels = plant_labels()
    scope_of = {email: scope for email, scope in rcpts}
    for emails, alerts in views:
        scope = scope_of.get(emails[0]) if emails else None
        so = [r for r in mailable_open if scope is None or r.plant_key.upper() in scope]
        if not alerts and not any((r.severity or "").upper() == "CRITICAL" and r.state == AlertState.OPEN for r in so):
            continue                    # this view has nothing to say
        subject, body, html = render_mail(alerts, labels, still_open=so, when_mx=when_mx, now_utc=now_utc, tickets=tickets)
        if dry_run:
            LOG.info("[DRY RUN] would mail %s: %s (%d alert(s))",
                     ", ".join(emails), subject, len(alerts))
            continue
        msg = emailer.build_html_email(subject, body, html, cfg["SMTP_USER"], emails)
        if emailer.send(msg, cfg):
            LOG.info("ledger mail: %s -> %s (%d alert(s))", subject,
                     ", ".join(emails), len(alerts))
            mailed |= {a.alert_id for a in alerts}
        else:
            LOG.error("ledger mail: send FAILED for %s — will retry next run",
                      ", ".join(emails))
    return mark_mailed(records, mailed) if mailed else list(records)
