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
"""
from __future__ import annotations

import logging
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from argia.core.alerts_state import AlertRecord, AlertState, mark_channels_sent

LOG = logging.getLogger("argia.alerts.ledger_mail")

CHANNEL = "maintenance"
SUBJECT_PREFIX = "[ARGIA]"
MAX_PER_RUN = 20          # the notifier's safety valve


# ---------------------------------------------------------------- pure

def unmailed(records: Sequence[AlertRecord]) -> List[AlertRecord]:
    """OPEN records never mailed, oldest first."""
    out = [r for r in records if r.state == AlertState.OPEN
           and "email" not in {c.strip() for c in r.channels_sent.split(",")}]
    out.sort(key=lambda r: (r.opened_utc, r.alert_id))
    return out


def subject_for(r: AlertRecord) -> str:
    return f"{SUBJECT_PREFIX} {r.severity or 'ALERT'} — {r.plant_key} {r.metric}".strip()


def body_for(r: AlertRecord) -> str:
    inv = f"  inverter {r.inverter_sn}" if r.inverter_sn else ""
    val = "" if r.value is None else r.value
    thr = "" if r.threshold is None else r.threshold
    return (f"Alert:      {r.alert_id}\n"
            f"Plant:      {r.plant_key}{inv}\n"
            f"Metric:     {r.metric}\n"
            f"Severity:   {r.severity}\n"
            f"Opened UTC: {r.opened_utc}\n"
            f"Value:      {val} (threshold {thr})\n\n"
            f"{r.message}\n\n{r.explanation}".rstrip() + "\n")


def _plant_title(plant_key: str, labels: Optional[Dict[str, str]]) -> str:
    name = (labels or {}).get(plant_key)
    return f"{plant_key} · {name}" if name else plant_key


def by_plant(alerts: Sequence[AlertRecord]) -> List[Tuple[str, List[AlertRecord]]]:
    """[(plant_key, alerts)] — CRITICAL plants first, then by key; inside a
    plant CRITICAL first, then by inverter. PORTFOLIO (digest) last. Pure."""
    groups: Dict[str, List[AlertRecord]] = {}
    for a in alerts:
        groups.setdefault(a.plant_key or "", []).append(a)
    rank = {"CRITICAL": 0, "WARNING": 1}

    def key(item):
        pk, items = item
        worst = min(rank.get(a.severity, 2) for a in items)
        return (pk == "PORTFOLIO", worst, pk)
    out = []
    for pk, items in sorted(groups.items(), key=key):
        items.sort(key=lambda a: (rank.get(a.severity, 2), a.inverter_sn, a.alert_id))
        out.append((pk, items))
    return out


def digest_body(alerts: Sequence[AlertRecord],
                labels: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """Several alerts for one recipient view -> one mail (subject, text
    body). v204: grouped by plant with a header per plant (Tomasz: "divided
    by plants visually easier to recognize")."""
    if len(alerts) == 1:
        return subject_for(alerts[0]), body_for(alerts[0])
    worst = "CRITICAL" if any(a.severity == "CRITICAL" for a in alerts) else \
            "WARNING" if any(a.severity == "WARNING" for a in alerts) else "INFO"
    plants = sorted({a.plant_key for a in alerts})
    subject = f"{SUBJECT_PREFIX} {worst} — {len(alerts)} new alerts ({', '.join(plants)})"
    blocks = []
    for pk, items in by_plant(alerts):
        n_crit = sum(1 for a in items if a.severity == "CRITICAL")
        head = f"{_plant_title(pk, labels)}  —  {len(items)} alert(s)" + \
               (f", {n_crit} critical" if n_crit else "")
        bar = "=" * max(len(head), 40)
        blocks.append(f"{bar}\n{head}\n{bar}\n\n"
                      + "\n\n- - - - -\n\n".join(body_for(a) for a in items))
    return subject, "\n\n\n".join(blocks) + "\n"


_SEV_COLOR = {"CRITICAL": ("#c5221f", "#fde7e9"), "WARNING": ("#a05c00", "#fdf0dc")}


def digest_html(alerts: Sequence[AlertRecord],
                labels: Optional[Dict[str, str]] = None) -> str:
    """HTML alternative of digest_body: one section per plant with a
    coloured header bar, one card per alert. Pure, no external assets."""
    import html as _h

    def e(x):
        return _h.escape(str(x))
    out = ['<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;'
           'color:#1a1a19;max-width:720px">']
    for pk, items in by_plant(alerts):
        n_crit = sum(1 for a in items if a.severity == "CRITICAL")
        fg, bg = _SEV_COLOR["CRITICAL" if n_crit else "WARNING"]
        out.append(f'<div style="margin:18px 0 6px;padding:8px 12px;border-left:6px solid {fg};'
                   f'background:{bg};font-weight:700;font-size:15px">{e(_plant_title(pk, labels))}'
                   f'<span style="font-weight:400;color:#5f6368;margin-left:10px">{len(items)} alert(s)'
                   f'{", " + str(n_crit) + " critical" if n_crit else ""}</span></div>')
        for a in items:
            sfg, sbg = _SEV_COLOR.get(a.severity, ("#5f6368", "#eceef0"))
            inv = f" · inverter {e(a.inverter_sn)}" if a.inverter_sn else ""
            val = "" if a.value is None else f" · value {e(a.value)}" + \
                  ("" if a.threshold is None else f" (threshold {e(a.threshold)})")
            out.append(
                f'<div style="margin:0 0 8px 12px;padding:8px 12px;border:1px solid #e3e3e0;'
                f'border-radius:6px;background:#fff">'
                f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;'
                f'font-size:11px;font-weight:700;color:{sfg};background:{sbg}">{e(a.severity)}</span> '
                f'<b>{e(a.metric)}</b>{inv}{val}'
                f'<div style="margin-top:4px">{e(a.message)}</div>'
                + (f'<div style="margin-top:4px;color:#5f6368;font-size:13px">{e(a.explanation)}</div>'
                   if a.explanation else "")
                + f'<div style="margin-top:4px;color:#9aa0a6;font-size:11px">{e(a.alert_id)} · opened {e(a.opened_utc)} UTC</div>'
                '</div>')
    out.append('<p style="color:#9aa0a6;font-size:11px;margin-top:16px">ARGIA Monitoring · '
               'portal: https://report.argia.com.mx/monitoring/</p></div>')
    return "".join(out)


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

def plant_labels() -> Dict[str, str]:
    """{plant_key: short customer name} for the plant headers; {} on error."""
    try:
        from argia.store.pgq import psql_rows
        out = {}
        for r in psql_rows("SELECT plant_key, coalesce(customer,'') FROM plant;"):
            if len(r) >= 2 and r[0]:
                out[r[0]] = short_customer(r[1])
        return out
    except Exception:  # noqa: BLE001
        return {}


def short_customer(name: str) -> str:
    """'PLASTIC OMNIUM PPA land (Monterrey, NL)' -> 'Plastic Omnium'. Pure."""
    import re
    head = re.split(r",|\(|\s+(?:PPA|CAPEX|LaaS|roof|land)\b", name or "", 1)[0].strip(" ,·")
    return head.title() if head.isupper() else head


def recipients():
    from argia.alerts import subscriptions
    return subscriptions.only_portal(subscriptions.recipients_for(CHANNEL),
                                     subscriptions.portal_emails())


def mail_new_alerts(records: Sequence[AlertRecord],
                    dry_run: bool = False) -> List[AlertRecord]:
    """Mail every unmailed OPEN alert to its subscribers; return the
    records with 'email' marked on the ones that went out. Never raises
    — alerting must not crash the engine run."""
    from argia.alerts import emailer, subscriptions
    excluded = subscriptions.load_excluded_plants()
    cands = [r for r in unmailed(records)
             if subscriptions.is_mailable(r.plant_key, excluded)][:MAX_PER_RUN]
    if not cands:
        return list(records)
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
    if not views:
        LOG.info("ledger mail: %d new alert(s), no subscriber sees them — "
                 "marking as handled", len(cands))
        # nobody to mail: don't retry forever, the ledger is the record
        return mark_mailed(records, {a.alert_id for a in cands})
    cfg = None if dry_run else emailer.load_smtp()
    if cfg is None and not dry_run:
        LOG.warning("ledger mail: no SMTP config — %d alert(s) stay unmailed",
                    len(cands))
        return list(records)
    mailed: set = set()
    labels = plant_labels()
    for emails, alerts in views:
        subject, body = digest_body(alerts, labels)
        if dry_run:
            LOG.info("[DRY RUN] would mail %s: %s (%d alert(s))",
                     ", ".join(emails), subject, len(alerts))
            continue
        msg = emailer.build_html_email(subject, body, digest_html(alerts, labels),
                                       cfg["SMTP_USER"], emails)
        if emailer.send(msg, cfg):
            LOG.info("ledger mail: %s -> %s (%d alert(s))", subject,
                     ", ".join(emails), len(alerts))
            mailed |= {a.alert_id for a in alerts}
        else:
            LOG.error("ledger mail: send FAILED for %s — will retry next run",
                      ", ".join(emails))
    return mark_mailed(records, mailed) if mailed else list(records)
