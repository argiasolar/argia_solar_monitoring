#!/usr/bin/env python3
"""Maintenance tickets — the /maintenance/ pages of portal.argia.com.mx (v226).

Runs on 127.0.0.1:8514; nginx proxies /maintenance/ here behind the
session login and forwards the account as X-Remote-User. Internal
accounts (level 'argia' and global admins) may open, work and close
tickets; customer accounts see a no-access page (customer visibility is
a later phase). The pure rules live in argia.maintenance.tickets; this
file is routing, HTML and PostgreSQL / file I/O.

    /maintenance/                dashboard: counts + open tickets
    /maintenance/new/            open a ticket (prefilled from ?alert=…)
    /maintenance/t/<number>/     the ticket: header, actions, timeline
    /maintenance/t/<number>/file/<id>   an attachment
    /maintenance/resolved/       resolved / closed tickets

Every change is a row on the ticket's timeline and an e-mail to the
other participants (creator, assignee, followers) — from
service@argia.com.mx, through the same emailer as the alerts.
"""
from __future__ import annotations

import datetime as dt
import html
import mimetypes
import os
import re
import sys
import uuid
from typing import Dict, List, Optional

from flask import Flask, abort, redirect, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get('ARGIA_V2_DIR', '/root/argia_v2/v2'))

import portal_chrome as PC                              # noqa: E402
from argia.alerts import emailer, naming                # noqa: E402
from argia.maintenance import notify as NOTIFY           # noqa: E402
from argia.maintenance import tickets as TK             # noqa: E402
from argia.store import pgq                             # noqa: E402

app = Flask(__name__)
FILES_DIR = os.environ.get('ARGIA_TICKET_FILES', '/opt/argia/tickets')
MAX_UPLOAD = 15 * 1024 * 1024
ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.txt', '.csv', '.xlsx', '.docx', '.zip', '.mp4', '.heic'}
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD + 1024 * 1024
PORTAL = 'https://portal.argia.com.mx'
MX = dt.timezone(dt.timedelta(hours=-6))
_SEV_TAG = re.compile(r"\s*\[(?:CRITICAL|WARNING|INFO)\]$")


# ------------------------------------------------------------- database
def rows_csv(sql: str) -> str:
    fn = app.config.get('ROWS_CSV')
    return fn(sql) if fn else pgq.psql_csv(sql)


def execute(sql: str) -> str:
    """Run DML; returns psql's stdout (RETURNING values, one per line)."""
    fn = app.config.get('EXEC')
    if fn:
        return fn(sql) or ''
    return pgq._run(sql, ['-t', '-A'])


def ensure():
    if not app.config.get('ENSURED'):
        execute(TK.ENSURE_SQL)
        app.config['ENSURED'] = True


# ------------------------------------------------------------- identity
def actor() -> str:
    return (request.headers.get('X-Remote-User') or '').strip()


def user_row(username: str) -> Optional[dict]:
    fn = app.config.get('USER_ROW')
    if fn:
        return fn(username)
    try:
        import auth_app
        return auth_app.user_row(username)
    except Exception:                                    # noqa: BLE001
        return None


def is_internal(username: str) -> bool:
    u = user_row(username)
    if u is None:
        return False
    if u.get('disabled'):
        return False
    return u.get('level') == 'argia' or bool(u.get('is_admin'))


def staff() -> List[Dict[str, str]]:
    """Internal accounts for the assignee / follower pickers:
    [{username, name, email}]."""
    fn = app.config.get('STAFF')
    if fn:
        return fn()
    try:
        import setup_app as sa
        c = sa.db()
        rows = c.execute("SELECT username, first_name, last_name, email FROM users"
                         " WHERE disabled = 0 AND (level = 'argia' OR is_admin = 1) ORDER BY username").fetchall()
        c.close()
        return [{'username': r[0], 'name': sa.display_name(r[1], r[2], r[0]), 'email': (r[3] or '').strip()} for r in rows]
    except Exception:                                    # noqa: BLE001
        return []


def email_of(username: str) -> str:
    if TK.is_email(username):
        return username.strip().lower()
    for s in staff():
        if s['username'] == username:
            return s['email']
    return ''


def name_of(username: str) -> str:
    """A portal account by its display name; an e-mail participant by
    the address itself (v227)."""
    if username == 'monitoring':
        return 'Monitoring'
    for s in staff():
        if s['username'] == username:
            return s['name'] or username
    return username or '—'


# ------------------------------------------------------------ names
_NAMES: Optional[naming.Names] = None


def names() -> naming.Names:
    global _NAMES
    if _NAMES is None or app.config.get('TESTING'):
        try:
            _NAMES = naming.load_names()
        except Exception:                                # noqa: BLE001
            _NAMES = naming.Names()
    return _NAMES


def plants() -> List[Dict[str, str]]:
    try:
        rows = TK.rows_from_csv(rows_csv("SELECT plant_key, customer, coalesce(portfolio,'') AS portfolio"
                                         " FROM plant WHERE active ORDER BY plant_key;"))
    except Exception:                                    # noqa: BLE001
        rows = []
    return [{'key': r['plant_key'], 'name': naming.short_customer(r['customer']) or r['plant_key'],
             'portfolio': r['portfolio']} for r in rows]


def inverters(plant_key: str) -> List[Dict[str, str]]:
    try:
        rows = TK.rows_from_csv(rows_csv("SELECT plant_key, inverter_sn, coalesce(inverter_label, '') AS label"
                                         " FROM inverter WHERE active ORDER BY plant_key, inverter_label, inverter_sn;"))
    except Exception:                                    # noqa: BLE001
        rows = []
    return [{'sn': r['inverter_sn'], 'label': r['label'] or r['inverter_sn'], 'plant': r['plant_key']}
            for r in rows if not plant_key or r['plant_key'] == plant_key.upper()]


# ------------------------------------------------------------ data access
def load_ticket(number: str) -> Optional[TK.Ticket]:
    ensure()
    rows = TK.rows_from_csv(rows_csv(TK.SELECT_TICKETS + f" WHERE number = {TK._txt(number.upper())};"))
    return TK.ticket_from_row(rows[0]) if rows else None


def load_tickets(where: str) -> List[TK.Ticket]:
    ensure()
    return [TK.ticket_from_row(r) for r in TK.rows_from_csv(rows_csv(
        TK.SELECT_TICKETS + f" WHERE {where} ORDER BY CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 WHEN 'P3' THEN 2 ELSE 3 END, updated_at DESC;"))]


def load_events(ticket_id: int) -> List[TK.Event]:
    return [TK.event_from_row(r) for r in TK.rows_from_csv(rows_csv(
        "SELECT id, ticket_id, ts::text AS ts, actor, kind, body, meta::text AS meta FROM ticket_event"
        f" WHERE ticket_id = {int(ticket_id)} ORDER BY ts, id;"))]


def load_attachments(ticket_id: int) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for r in TK.rows_from_csv(rows_csv(
            "SELECT id, coalesce(event_id, 0) AS event_id, filename, stored_as, bytes, mime FROM ticket_attachment"
            f" WHERE ticket_id = {int(ticket_id)} ORDER BY id;")):
        out.setdefault(int(r['event_id'] or 0), []).append(r)
    return out


def open_ledger_alerts(plant_key: str, sn: str) -> List[dict]:
    """The ledger's OPEN alerts for this plant (and inverter, when given)
    — what a ticket can be linked to."""
    try:
        rows = TK.rows_from_csv(rows_csv(
            "SELECT alert_key, inverter_sn, metric, severity, left(opened_utc, 10) AS since, message FROM alert_ledger"
            f" WHERE state = 'OPEN' AND metric <> 'daily_digest' AND plant_key = {TK._txt(plant_key.upper())}"
            + (f" AND inverter_sn = {TK._txt(sn)}" if sn else "") + " ORDER BY opened_utc;"))
    except Exception:                                    # noqa: BLE001
        rows = []
    return rows


def _returning_int(out: str) -> Optional[int]:
    for ln in (out or '').splitlines():
        ln = ln.strip()
        if ln.isdigit():
            return int(ln)
    return None


def add_event(t: TK.Ticket, who: str, kind: str, body: str = '', meta: Optional[dict] = None) -> Optional[int]:
    return _returning_int(execute(TK.event_sql(t.id, who, kind, body, meta)))


# ------------------------------------------------------------ notify
def notify(t: TK.Ticket, who: str, what: str, detail: str = '') -> int:
    """E-mail the other participants about one change (v227: through
    argia.maintenance.notify — usernames and bare e-mails alike)."""
    n = names()
    return NOTIFY.send(t, who, what, detail, email_of, name_of, n.plant(t.plant_key),
                       n.inverter(t.plant_key, t.inverter_sn) if t.inverter_sn else '')


# --------------------------------------------------------------- HTML
def e(x) -> str:
    return html.escape(str(x if x is not None else ''))


PRIO_CLS = {'P1': 'crit', 'P2': 'warn', 'P3': 'ok', 'P4': 'off'}
STATUS_CLS = {'NEW': 'crit', 'IN_PROGRESS': 'warn', 'WAITING': 'off', 'VERIFICATION': 'ok', 'RESOLVED': 'ok', 'CLOSED': 'off'}
CSS = '''
.tkrow td{vertical-align:top}.tkrow a.tkn{font:600 12px ui-monospace,Menlo,Consolas,monospace}
.tl{list-style:none;margin:0;padding:0}.tl li{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.tl li:first-child{border-top:0}.tl .when{flex:0 0 118px;font-size:12px;color:var(--muted)}
.tl .who{font-weight:700}.tl .body{white-space:pre-wrap;margin-top:2px}.tl .kind{font-size:11px;color:var(--muted);margin-left:6px}
.tl .files a{display:inline-block;margin:4px 8px 0 0;font-size:12.5px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 18px;font-size:13px}
.fields .k{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700}
.act{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.act form{display:inline}
.frm label{display:block;font-size:12px;color:var(--muted);margin:10px 0 3px}
.frm input[type=text],.frm select,.frm textarea{width:100%;border:1px solid var(--line2);border-radius:8px;padding:8px 10px;font:inherit;font-size:13.5px}
.frm textarea{min-height:90px}
.cnt{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:0 0 16px}
.cnt .card{padding:14px 16px}.cnt .n{font-size:26px;font-weight:800;color:var(--deep)}.cnt .l{font-size:12px;color:var(--muted)}
.sla-ok{color:var(--teal2)}.sla-due{color:#b26a00}.sla-breached{color:#c2554e}
.tw{position:relative;display:inline-flex;align-items:center}
.tw .tipbox{left:0;right:auto;top:22px;width:300px;font-weight:400;text-transform:none;letter-spacing:0;white-space:normal;text-align:left;line-height:1.45}
th .tw .ti{margin-left:4px;width:13px;height:13px;font-size:9px}
th:nth-last-child(-n+3) .tw .tipbox{left:auto;right:0}   /* the last columns open leftwards, not off the page */
.frm label .tw .ti{margin-left:5px}
'''
COL_HELP = {
    'ticket': 'TK-<plant>-<number>: the plant is in the number. Click to open.',
    'prio': 'P1 critical · P2 high · P3 medium · P4 low — sets the SLA clock; hover a priority for its meaning.',
    'status': 'New → In progress → Waiting → Verification → Resolved → Closed. Resolved comes from the data when alerts are linked.',
    'plant': 'Plant (and inverter) the ticket is about.',
    'title': 'What the ticket is about, with its category underneath.',
    'assigned': 'Who is working on it. Unassigned tickets belong to nobody yet.',
    'age': 'Time since the ticket was opened.',
    'sla': 'Resolve target of the priority (P1 4 h, P2 24 h, P3 72 h, P4 planned): time left, or how far over.',
}
TABS = [('', 'Open', 'Abiertos'), ('new', 'New ticket', 'Nuevo ticket'), ('resolved', 'Resolved', 'Resueltos'),
        ('stats', 'Statistics', 'Estadísticas')]
PRIO_HELP = {
    'P1': 'P1 Critical — plant outage, safety, transformer/MV, >50% of production unavailable. Resolve target 4 h.',
    'P2': 'P2 High — energy is being lost: inverter offline or off, measured thermal loss, production <70% of expected. Resolve target 24 h.',
    'P3': 'P3 Medium — a single string, a data gap, heat without a measured loss, a diagnostic flag. Resolve target 72 h.',
    'P4': 'P4 Low — cleaning, cosmetic, documentation, planned preventive work. No SLA clock.',
}
STATUS_HELP = {
    'NEW': 'New — opened, nobody has started yet.',
    'IN_PROGRESS': 'In progress — someone is working on it.',
    'WAITING': 'Waiting — blocked on parts, the customer, the vendor or the weather.',
    'VERIFICATION': 'Verification — the work is done; monitoring watches the linked alerts. When none recurs for 2 days the data marks the ticket Resolved; if one recurs it goes back to In progress.',
    'RESOLVED': 'Resolved — confirmed by the data (or by a person when no alert is linked). Record the root cause, then close.',
    'CLOSED': 'Closed — paperwork done. Can be re-opened.',
}
BTN_HELP = {
    'assign': 'Who works on it. The assignee is notified of every change.',
    'priority': 'How urgent it is; sets the SLA clock. Hover a priority for its meaning.',
    'follow': 'Followers are notified of every change but have no duties.',
    'link': 'Tie this monitoring alert to the ticket: the morning mail then reports the ticket instead of the warning, new occurrences land on the timeline, and the data verifies the fix.',
    'update': 'Post what was found, done or planned. Attach photos or documents. Participants are notified.',
}


def page(title: str, body: str, on: str = '') -> str:
    return PC.page(title, f'<div class="maintbody">{body}</div>', 'maintenance', on,
                   extra_head='<style>' + CSS + '</style>', tabs=TABS)


def no_access() -> tuple:
    return page('Maintenance', '<div class="card" style="padding:18px"><b>Maintenance tickets are for the ARGIA team.</b>'
                '<div class="muted">Your account sees the reports and monitoring pages.</div></div>'), 403


def when(ts: str) -> str:
    """'2026-09-07 15:03:21.1+00' -> '07 Sep 09:03' MX."""
    d = TK.parse_ts(ts)
    if d is None:
        return ts[:16]
    return d.astimezone(MX).strftime('%d %b %H:%M')


def pill(cls: str, txt: str) -> str:
    return f'<span class="pill {cls}">{e(txt)}</span>'


def tip(text: str) -> str:
    """The report pages' tooltip: an 'i' badge with a hover/focus box —
    the same .ti/.tipbox the KPI tiles use (Tomasz: one tooltip design)."""
    return (f'<span class="tw"><span class="ti" tabindex="0" role="note" aria-label="definition">i</span>'
            f'<span class="tipbox">{e(text)}</span></span>')


def th(label: str, key: str) -> str:
    return f'<th>{e(label)}{tip(COL_HELP[key])}</th>'


def ticket_rows(tks: List[TK.Ticket], now: dt.datetime) -> str:
    n = names()
    out = []
    for t in tks:
        state, sla_txt = TK.sla_state(t, now)
        out.append(
            f'<tr class="tkrow"><td><a class="tkn" href="/maintenance/t/{e(t.number)}/">{e(t.number)}</a></td>'
            f'<td>{pill(PRIO_CLS.get(t.priority, "off"), t.priority)}</td>'
            f'<td>{pill(STATUS_CLS.get(t.status, "off"), TK.STATUS_LABEL.get(t.status, t.status))}</td>'
            f'<td><b>{e(n.plant(t.plant_key))}</b>' + (f'<div class="muted" style="font-size:12px">{e(n.inverter(t.plant_key, t.inverter_sn))}</div>' if t.inverter_sn else '')
            + f'</td><td><a href="/maintenance/t/{e(t.number)}/">{e(t.title)}</a><div class="muted" style="font-size:12px">{e(TK.CATEGORY_LABEL.get(t.category, t.category))}</div></td>'
            f'<td>{e(name_of(t.assigned_to)) if t.assigned_to else "<span class=muted>—</span>"}</td>'
            f'<td>{e(TK.fmt_age(TK.age(t, now)))}</td><td class="sla-{state}" style="font-size:12px">{e(sla_txt)}</td></tr>')
    if not out:
        return '<p class="muted" style="padding:6px 0">No tickets.</p>'
    return ('<table><tr>' + th('Ticket', 'ticket') + th('Prio', 'prio') + th('Status', 'status') + th('Plant', 'plant')
            + th('Title', 'title') + th('Assigned', 'assigned') + th('Age', 'age') + th('SLA', 'sla') + '</tr>'
            + ''.join(out) + '</table>')


def dashboard(tks: List[TK.Ticket], now: dt.datetime, me: str) -> str:
    n_open = len(tks)
    n_crit = sum(1 for t in tks if t.priority in ('P1', 'P2'))
    n_prog = sum(1 for t in tks if t.status == 'IN_PROGRESS')
    n_ver = sum(1 for t in tks if t.status == 'VERIFICATION')
    n_over = sum(1 for t in tks if TK.sla_state(t, now)[0] == 'breached')
    mine = [t for t in tks if me in (t.assigned_to, t.created_by) or me in t.followers]
    by_plant: Dict[str, int] = {}
    for t in tks:
        by_plant[t.plant_key] = by_plant.get(t.plant_key, 0) + 1
    n = names()
    plants_txt = ' · '.join(f'{e(n.plant(k))} {v}' for k, v in sorted(by_plant.items(), key=lambda kv: -kv[1])) or '—'
    cnt = ''.join(f'<div class="card"><div class="n">{v}</div><div class="l">{l}</div></div>' for v, l in
                  ((n_open, 'open'), (n_crit, 'P1 / P2'), (n_prog, 'in progress'), (n_ver, 'verification'), (n_over, 'over SLA')))
    return (f'<div class="kicker">Maintenance</div><h1 class="pt">Tickets</h1>'
            f'<div class="muted" style="margin:4px 0 16px">Open by plant: {plants_txt}</div>'
            f'<div class="cnt">{cnt}</div>'
            + (f'<div class="card"><h2 class="ct" style="padding:14px 20px 0">My tickets ({len(mine)})</h2>{ticket_rows(mine, now)}</div>' if mine else '')
            + f'<div class="card" style="margin-top:14px"><h2 class="ct" style="padding:14px 20px 0">All open ({n_open})</h2>{ticket_rows(tks, now)}</div>'
            + legend())


def legend() -> str:
    pr = ''.join(f'<div><b>{e(k)}</b> {e(v.split(" — ", 1)[1])}</div>' for k, v in PRIO_HELP.items())
    st = ''.join(f'<div><b>{e(TK.STATUS_LABEL[k])}</b> {e(v.split(" — ", 1)[1])}</div>' for k, v in STATUS_HELP.items())
    return (f'<div class="card" style="padding:14px 20px;margin-top:14px;font-size:12.5px;color:var(--ink2)">'
            f'<h2 class="ct">How it works</h2><div class="fields" style="margin-top:8px"><div><div class="k">Priorities</div>{pr}</div>'
            f'<div><div class="k">Statuses</div>{st}</div></div>'
            '<div class="muted" style="margin-top:8px">A ticket linked to a monitoring alert is verified by the data: put it in Verification when the work is done; '
            'the nightly run marks it Resolved after 2 quiet days, or sends it back to In progress if the alert recurs. The <b>i</b> badges explain every column, badge and button.</div></div>')


def select(name: str, options, value: str = '', blank: str = '', titles: Optional[Dict[str, str]] = None,
           attrs: str = '') -> str:
    opts = (f'<option value="">{e(blank)}</option>' if blank is not None else '')
    for v, lab in options:
        tt = f' title="{e(titles[v])}"' if titles and v in titles else ''
        dp = f' data-plant="{e(v.split("|")[0])}"' if '|' in v else ''
        opts += f'<option value="{e(v)}"{" selected" if v == value else ""}{tt}{dp}>{e(lab)}</option>'
    return f'<select name="{name}"{(" " + attrs) if attrs else ""}>{opts}</select>'


def new_form(pre: dict) -> str:
    n = names()
    ps = plants()
    inv = inverters('')
    inv_opts = [('', '— plant level —')] + [(f"{i['plant']}|{i['sn']}", f"{n.plant(i['plant'])} · {i['label']} ({i['sn']})") for i in inv]
    st = staff()
    people = [(s['username'], s['name']) for s in st]
    return (f'<div class="kicker">Maintenance</div><h1 class="pt">New ticket</h1>'
            f'<form class="card frm" method="post" action="/maintenance/new/" style="padding:18px 20px;margin-top:14px;max-width:760px">'
            f'<input type="hidden" name="alert_key" value="{e(pre.get("alert_key", ""))}">'
            '<label>Plant</label>' + select("plant", [(p["key"], p["name"] + " (" + p["key"] + ")") for p in ps], pre.get("plant", ""), "— choose —", attrs='id="plant"')
            + '<label>Inverter (optional — the list follows the plant)</label>' + select("inverter", inv_opts, pre.get("inverter", ""), None, attrs='id="inverter"')
            + f'<label>Title</label><input type="text" name="title" maxlength="140" required value="{e(pre.get("title", ""))}">'
            f'<label>Category</label>{select("category", [(c[0], c[1]) for c in TK.CATEGORIES], pre.get("category", "other"), None)}'
            f'<label>Priority{tip(BTN_HELP["priority"])}</label>{select("priority", [(p[0], p[0] + " " + p[1]) for p in TK.PRIORITIES], pre.get("priority", "P3"), None, titles=PRIO_HELP)}'
            f'<div class="muted" style="font-size:12px;margin-top:3px">{" · ".join(e(v) for v in PRIO_HELP.values())}</div>'
            f'<label>Assign to{tip(BTN_HELP["assign"])}</label>{select("assigned_to", people, pre.get("assigned_to", ""), "— unassigned —")}'
            f'<label>Followers{tip(BTN_HELP["follow"])}</label><div class="fields">'
            + ''.join(f'<label style="margin:0"><input type="checkbox" name="follower" value="{e(u)}"> {e(nm)}</label>' for u, nm in people)
            + '</div><label>External followers — e-mail addresses, comma separated (a technician, a customer contact; they get every update and can reply by mail)</label>'
            f'<input type="text" name="emails" placeholder="name@company.com, other@company.com" value="{e(pre.get("emails", ""))}">'
            f'<label>Description</label><textarea name="description">{e(pre.get("description", ""))}</textarea>'
            '<div class="act"><button class="btn" type="submit">Open ticket</button>'
            '<a class="btn2" href="/maintenance/">Cancel</a></div></form>'
            + PLANT_FILTER_JS)


PLANT_FILTER_JS = '''<script>
(function(){var p=document.getElementById('plant'),i=document.getElementById('inverter');if(!p||!i)return;
function f(){var k=p.value;for(var o of i.options){var pk=o.getAttribute('data-plant');o.hidden=!!(pk&&k&&pk!==k);
 if(o.selected&&o.hidden)i.value='';}}
p.addEventListener('change',f);f();})();
</script>'''


def timeline(t: TK.Ticket, evs: List[TK.Event], files: Dict[int, List[dict]]) -> str:
    n = names()
    items = []
    for ev in evs:
        who = name_of(ev.actor) if ev.actor and ev.actor != 'monitoring' else ('Monitoring' if ev.actor == 'monitoring' else 'System')
        kind = {'created': 'opened the ticket', 'comment': '', 'status': 'changed status', 'assign': 'assigned',
                'follow': 'follows', 'unfollow': 'stopped following', 'attachment': 'attached a file',
                'alert': 'alert', 'priority': 'changed priority', 'system': '', 'resolution': 'resolution'}.get(ev.kind, ev.kind)
        body = ev.body
        if ev.kind == 'status':
            body = f'{TK.STATUS_LABEL.get(ev.meta.get("from", ""), ev.meta.get("from", ""))} → {TK.STATUS_LABEL.get(ev.meta.get("to", ""), ev.meta.get("to", ""))}' + (f'\n{ev.body}' if ev.body else '')
        elif ev.kind == 'alert':
            body = n.text(ev.body, t.plant_key, t.inverter_sn)
        fl = ''.join(f'<a href="/maintenance/t/{e(t.number)}/file/{e(f["id"])}">📎 {e(f["filename"])} <span class="muted">({int(f["bytes"] or 0) // 1024} kB)</span></a>'
                     for f in files.get(ev.id, []))
        items.append(f'<li><div class="when">{e(when(ev.ts))}</div><div style="flex:1"><span class="who">{e(who)}</span>'
                     f'<span class="kind">{e(kind)}</span>' + (f'<div class="body">{e(body)}</div>' if body else '')
                     + (f'<div class="files">{fl}</div>' if fl else '') + '</div></li>')
    return '<ul class="tl">' + ''.join(items) + '</ul>'


def ticket_page(t: TK.Ticket, evs: List[TK.Event], files: Dict[int, List[dict]], me: str, now: dt.datetime,
                msg: str = '') -> str:
    n = names()
    state, sla_txt = TK.sla_state(t, now)
    st = staff()
    people = [(s['username'], s['name']) for s in st]
    nxt = ''
    for s_ in TK.TRANSITIONS.get(t.status, ()):
        if s_ == 'RESOLVED' and t.alert_keys:
            nxt += ('<span class="btn2" style="opacity:.55;cursor:default">→ Resolved: by the data</span>' + tip(STATUS_HELP["VERIFICATION"]))
            continue
        nxt += (f'<form method="post" action="/maintenance/t/{e(t.number)}/status"><input type="hidden" name="to" value="{s_}">'
                f'<button class="btn2" type="submit">→ {e(TK.STATUS_LABEL[s_])}</button></form>' + tip(STATUS_HELP.get(s_, "")))
    following = me in t.followers
    alerts = open_ledger_alerts(t.plant_key, t.inverter_sn)
    linked = set(t.alert_keys)
    al = ''
    if alerts or linked:
        rows = []
        for a in alerts:
            on = a['alert_key'] in linked
            amsg = n.text(_SEV_TAG.sub('', a['message']), t.plant_key, a['inverter_sn'])
            rows.append(f'<tr><td>{pill("crit" if a["severity"] == "CRITICAL" else "warn", a["severity"])}</td>'
                        f'<td>{e(naming.phrase(a["metric"]))}</td><td>{e(a["since"])}</td>'
                        f'<td class="wrap-text">{e(amsg)}</td>'
                        f'<td>' + ('<span class="pill ok">linked</span>' if on else
                                   f'<form method="post" action="/maintenance/t/{e(t.number)}/link"><input type="hidden" name="alert_key" value="{e(a["alert_key"])}">'
                                   f'<button class="btn2" type="submit" style="padding:4px 10px">link</button></form>' + tip(BTN_HELP["link"])) + '</td></tr>')
        stale = [k for k in linked if k not in {a['alert_key'] for a in alerts}]
        al = ('<div class="card" style="padding:14px 20px;margin-top:14px"><h2 class="ct">Monitoring alerts on this plant'
              + (' / inverter' if t.inverter_sn else '') + '</h2><div class="muted" style="font-size:12px;margin-bottom:6px">'
              'A linked alert reports this ticket\'s progress in the morning mail instead of repeating the warning; new occurrences land on the timeline.</div>'
              + ('<table><tr><th>Sev</th><th>Issue</th><th>Since</th><th>Message</th><th></th></tr>' + ''.join(rows) + '</table>' if rows else '')
              + (f'<div class="muted" style="font-size:12px;margin-top:6px">Linked alerts no longer open in the ledger: {e(", ".join(stale))}</div>' if stale else '')
              + '</div>')
    res = ''
    if t.status in ('VERIFICATION', 'RESOLVED', 'CLOSED') or t.root_cause:
        res = (f'<form class="card frm" method="post" action="/maintenance/t/{e(t.number)}/resolution" style="padding:14px 20px;margin-top:14px">'
               f'<h2 class="ct">Resolution</h2><label>Root cause</label>{select("root_cause", TK.ROOT_CAUSES, t.root_cause, "— pick —")}'
               f'<label>What was done / prevent recurrence</label><textarea name="resolution">{e(t.resolution)}</textarea>'
               f'<label>Energy lost (kWh, if known)</label><input type="text" name="lost_kwh" value="{e("" if t.lost_kwh is None else t.lost_kwh)}" style="max-width:160px">'
               '<div class="act"><button class="btn2" type="submit">Save resolution</button></div></form>')
    head = (f'<div class="kicker">Maintenance · <span class="mono">{e(t.number)}</span></div>'
            f'<h1 class="pt" style="font-size:24px">{e(t.title)}</h1>'
            f'<div class="act">{pill(STATUS_CLS.get(t.status, "off"), TK.STATUS_LABEL.get(t.status, t.status))}{tip(STATUS_HELP.get(t.status, ""))}'
            f'{pill(PRIO_CLS.get(t.priority, "off"), t.priority + " " + TK.PRIORITY_LABEL.get(t.priority, ""))}{tip(PRIO_HELP.get(t.priority, ""))}'
            f'<span class="sla-{state}" style="font-size:12.5px">SLA: {e(sla_txt)}</span>{tip(COL_HELP["sla"] + " Green = on track, amber = last quarter of the target, red = over.")}</div>'
            + (f'<p class="pill ok" style="display:inline-flex">{e(msg)}</p>' if msg else '')
            + '<div class="card" style="padding:14px 20px"><div class="fields">'
            f'<div><div class="k">Plant</div><b>{e(n.plant_full(t.plant_key))}</b></div>'
            f'<div><div class="k">Inverter</div>{e(n.inverter_full(t.plant_key, t.inverter_sn)) if t.inverter_sn else "<span class=muted>plant level</span>"}</div>'
            f'<div><div class="k">Category</div>{e(TK.CATEGORY_LABEL.get(t.category, t.category))}</div>'
            f'<div><div class="k">Opened</div>{e(when(t.created_at))} by {e(name_of(t.created_by))} · {e(TK.fmt_age(TK.age(t, now)))}</div>'
            f'<div><div class="k">Assigned</div>{e(name_of(t.assigned_to)) if t.assigned_to else "<span class=muted>—</span>"}</div>'
            f'<div><div class="k">Followers</div>{e(", ".join(name_of(u) for u in t.followers)) or "<span class=muted>—</span>"}</div>'
            '</div>' + (f'<div style="margin-top:12px;white-space:pre-wrap">{e(t.description)}</div>' if t.description else '') + '</div>'
            f'<div class="act">{nxt}'
            f'<form method="post" action="/maintenance/t/{e(t.number)}/assign">{select("assigned_to", people, t.assigned_to, "— unassigned —")} <button class="btn2" type="submit">Assign</button></form>{tip(BTN_HELP["assign"])}'
            f'<form method="post" action="/maintenance/t/{e(t.number)}/priority">{select("priority", [(p[0], p[0] + " " + p[1]) for p in TK.PRIORITIES], t.priority, None, titles=PRIO_HELP)} <button class="btn2" type="submit">Priority</button></form>{tip(BTN_HELP["priority"])}'
            f'<form method="post" action="/maintenance/t/{e(t.number)}/follow"><input type="hidden" name="on" value="{0 if following else 1}"><button class="btn2" type="submit">{"Unfollow" if following else "Follow"}</button></form>{tip(BTN_HELP["follow"])}'
            f'<form method="post" action="/maintenance/t/{e(t.number)}/follow"><input type="text" name="email" placeholder="add follower by e-mail" style="border:1px solid var(--line2);border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;width:220px"> '
            f'<button class="btn2" type="submit">Add</button></form>{tip("An outside address (technician, customer contact): notified of every change, can reply by mail to comment.")}'
            '</div>')
    comment = (f'<form class="card frm" method="post" action="/maintenance/t/{e(t.number)}/comment" enctype="multipart/form-data" style="padding:14px 20px;margin-top:14px">'
               f'<h2 class="ct" title="{e(BTN_HELP["update"])}">Add an update</h2><textarea name="body" placeholder="What was found, what was done, what is next…"></textarea>'
               f'<label>Attachments (photos, PDF, up to {MAX_UPLOAD // (1024 * 1024)} MB each)</label><input type="file" name="files" multiple>'
               '<div class="act"><button class="btn" type="submit">Post update</button></div></form>')
    tl = f'<div class="card" style="padding:14px 20px;margin-top:14px"><h2 class="ct">Timeline</h2>{timeline(t, evs, files)}</div>'
    return head + al + comment + tl + res


# --------------------------------------------------------------- routes
def _guard():
    me = actor()
    if not is_internal(me):
        return None
    return me


@app.get('/')
def home():
    me = _guard()
    if me is None:
        return no_access()
    now = dt.datetime.now(dt.timezone.utc)
    tks = load_tickets("status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION')")
    return page('Maintenance', dashboard(tks, now, me), '')


@app.get('/resolved/')
def resolved():
    me = _guard()
    if me is None:
        return no_access()
    now = dt.datetime.now(dt.timezone.utc)
    tks = load_tickets("status IN ('RESOLVED','CLOSED') AND updated_at > now() - interval '90 days'")
    return page('Maintenance', f'<div class="kicker">Maintenance</div><h1 class="pt">Resolved (90 days)</h1>'
                f'<div class="card" style="margin-top:14px">{ticket_rows(tks, now)}</div>', 'resolved')


@app.get('/stats/')
def stats_page():
    me = _guard()
    if me is None:
        return no_access()
    now = dt.datetime.now(dt.timezone.utc)
    tks = load_tickets("created_at > now() - interval '180 days'")
    evs = [TK.event_from_row(r) for r in TK.rows_from_csv(rows_csv(
        "SELECT id, ticket_id, ts::text AS ts, actor, kind, body, meta::text AS meta FROM ticket_event"
        " WHERE kind = 'status' AND ts > now() - interval '180 days' ORDER BY ts, id;"))]
    n = names()
    series = TK.status_timeline(tks, evs, 60, now)
    st = TK.stats(tks, now)
    leg = ''.join(f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:12px;font-size:12px"><span style="width:10px;height:10px;background:{TK.STATUS_COLOR[k]};border-radius:2px"></span>{e(TK.STATUS_LABEL[k])}</span>'
                  for k in TK.STATUS_COLOR)
    weeks = ''.join(f'<tr><td>{w.strftime("%d %b")}</td><td class="num">{o}</td><td class="num">{r}</td></tr>' for w, o, r in st["weeks"])
    tbl = lambda rows, lab: ('<table><tr><th>' + lab + '</th><th class="num">open</th></tr>'  # noqa: E731
                             + ''.join(f'<tr><td>{e(k)}</td><td class="num">{v}</td></tr>' for k, v in rows) + '</table>') if rows else '<p class="muted">—</p>'
    body = (f'<div class="kicker">Maintenance</div><h1 class="pt">Statistics</h1>'
            f'<div class="cnt" style="margin-top:14px">'
            f'<div class="card"><div class="n">{st["n_open"]}</div><div class="l">open now</div></div>'
            f'<div class="card"><div class="n">{st["over_sla"]}</div><div class="l">over SLA</div></div>'
            f'<div class="card"><div class="n">{st["mttr_h"] if st["mttr_h"] is not None else "—"}</div><div class="l">MTTR, hours (resolved: {st["n_resolved"]})</div></div>'
            f'<div class="card"><div class="n">{st["n_total"]}</div><div class="l">tickets, 180 days</div></div></div>'
            f'<div class="card" style="padding:14px 20px"><h2 class="ct" style="display:flex;align-items:center">Open tickets by status — last 60 days{tip("Open tickets at the end of each day, stacked by status, replayed from the status changes on every timeline.")}</h2>'
            f'<div style="margin:6px 0">{leg}</div>{TK.status_chart_svg(series)}</div>'
            f'<div class="grid g3" style="margin-top:14px">'
            f'<div class="card" style="padding:14px 20px"><h2 class="ct">Opened / resolved per week</h2><table><tr><th>week of</th><th class="num">opened</th><th class="num">resolved</th></tr>{weeks}</table></div>'
            f'<div class="card" style="padding:14px 20px"><h2 class="ct">Open by plant</h2>{tbl([(n.plant(k), v) for k, v in st["by_plant"]], "plant")}</div>'
            f'<div class="card" style="padding:14px 20px"><h2 class="ct">Open by category</h2>{tbl([(TK.CATEGORY_LABEL.get(k, k), v) for k, v in st["by_category"]], "category")}</div>'
            f'</div><div class="card" style="padding:14px 20px;margin-top:14px"><h2 class="ct">Open by priority</h2>{tbl([(k + " " + TK.PRIORITY_LABEL.get(k, ""), v) for k, v in st["by_priority"]], "priority")}</div>')
    return page('Maintenance statistics', body, 'stats')


@app.get('/new/')
def new_get():
    me = _guard()
    if me is None:
        return no_access()
    pre = {'plant': (request.args.get('plant') or '').upper(), 'title': request.args.get('title') or '',
           'alert_key': request.args.get('alert') or '', 'priority': request.args.get('priority') or 'P3',
           'category': request.args.get('category') or 'other', 'description': request.args.get('description') or ''}
    sn = request.args.get('sn') or ''
    if sn and pre['plant']:
        pre['inverter'] = f"{pre['plant']}|{sn}"
    if pre['alert_key'] and not pre['title']:
        # prefill from the ledger row
        try:
            rows = TK.rows_from_csv(rows_csv("SELECT plant_key, inverter_sn, metric, severity, message FROM alert_ledger"
                                             f" WHERE alert_key = {TK._txt(pre['alert_key'])} AND state = 'OPEN' ORDER BY opened_utc DESC LIMIT 1;"))
        except Exception:                                # noqa: BLE001
            rows = []
        if rows:
            r = rows[0]
            n = names()
            pre['plant'] = r['plant_key']
            if r['inverter_sn']:
                pre['inverter'] = f"{r['plant_key']}|{r['inverter_sn']}"
            pre['title'] = TK.title_for_alert(r['metric'], n.plant(r['plant_key']), n.inverter(r['plant_key'], r['inverter_sn']) if r['inverter_sn'] else '')
            pre['priority'] = TK.PRIORITY_FOR_SEVERITY.get(r['severity'], 'P3')
            pre['category'] = TK.CATEGORY_FOR_METRIC.get(r['metric'], 'other')
            pre['description'] = n.text(r['message'], r['plant_key'], r['inverter_sn'])
    return page('New ticket', new_form(pre), 'new')


@app.post('/new/')
def new_post():
    me = _guard()
    if me is None:
        return no_access()
    ensure()
    f = request.form
    plant = (f.get('plant') or '').strip().upper()
    if not plant or not (f.get('title') or '').strip():
        abort(400)
    inv = (f.get('inverter') or '').split('|')
    sn = inv[1].strip() if len(inv) == 2 and inv[0].strip().upper() == plant else ''
    priority = f.get('priority') if f.get('priority') in TK.PRIORITY_LABEL else 'P3'
    category = f.get('category') if f.get('category') in TK.CATEGORY_LABEL else 'other'
    assigned = (f.get('assigned_to') or '').strip()
    number = None
    for _attempt in range(3):
        seq = _returning_int(execute(TK.next_number_sql(plant))) or 1
        number = TK.make_number(plant, seq)
        try:
            tid = _returning_int(execute(TK.insert_ticket_sql(number, plant, sn, f.get('title').strip()[:140],
                                                              (f.get('description') or '').strip()[:4000], category,
                                                              priority, me, assigned)))
            break
        except RuntimeError as err:                      # unique clash: try the next number
            if 'duplicate' not in str(err).lower() or _attempt == 2:
                raise
    else:
        abort(500)
    for u in f.getlist('follower'):
        if u.strip():
            execute(TK.follow_sql(tid, u.strip()))
    for em in (f.get('emails') or '').replace(';', ',').split(','):
        em = TK.valid_email(em)
        if em:
            execute(TK.follow_sql(tid, em))
    # v227: every open alert on this asset is the ticket's from the start —
    # the morning mail reports the ticket, not the warnings
    keys = {a['alert_key'] for a in open_ledger_alerts(plant, sn)} if sn else \
        {a['alert_key'] for a in open_ledger_alerts(plant, '') if not a['inverter_sn']}
    if f.get('alert_key'):
        keys.add(f.get('alert_key').strip())
    for k in sorted(keys):
        execute(TK.link_alert_sql(tid, k))
    t = load_ticket(number)
    add_event(t, me, 'created', t.description)
    if assigned:
        add_event(t, me, 'assign', f'assigned to {name_of(assigned)}', {'to': assigned})
    for k in sorted(keys):
        add_event(t, me, 'alert', f'linked to alert {k}', {'alert_key': k})
    notify(t, me, 'New ticket opened', t.description)
    return redirect(f'/maintenance/t/{number}/')


def _ticket_or_404(number: str) -> TK.Ticket:
    t = load_ticket(number)
    if t is None:
        abort(404)
    return t


@app.get('/t/<number>/')
def ticket_get(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    now = dt.datetime.now(dt.timezone.utc)
    return page(t.number, ticket_page(t, load_events(t.id), load_attachments(t.id), me, now,
                                      msg=(request.args.get('m') or '')[:120]))


@app.post('/t/<number>/status')
def ticket_status(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    to = (request.form.get('to') or '').strip().upper()
    if not TK.can_transition(t.status, to):
        abort(400)
    if to == 'RESOLVED' and t.alert_keys:
        abort(400)                          # v227: the data resolves a ticket that has alerts (verify job)
    execute(TK.status_sql(t.id, to))
    old = t.status
    t = load_ticket(number)
    add_event(t, me, 'status', (request.form.get('note') or '').strip(), {'from': old, 'to': to})
    notify(t, me, f'Status: {TK.STATUS_LABEL.get(old, old)} → {TK.STATUS_LABEL.get(to, to)}', request.form.get('note') or '')
    return redirect(f'/maintenance/t/{t.number}/?m=status+updated')


@app.post('/t/<number>/assign')
def ticket_assign(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    who = (request.form.get('assigned_to') or '').strip()
    execute(TK.assign_sql(t.id, who))
    t = load_ticket(number)
    add_event(t, me, 'assign', f'assigned to {name_of(who)}' if who else 'unassigned', {'to': who})
    notify(t, me, f'Assigned to {name_of(who)}' if who else 'Unassigned')
    return redirect(f'/maintenance/t/{t.number}/?m=assigned')


@app.post('/t/<number>/priority')
def ticket_priority(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    p = (request.form.get('priority') or '').strip().upper()
    if p not in TK.PRIORITY_LABEL:
        abort(400)
    execute(TK.priority_sql(t.id, p))
    old = t.priority
    t = load_ticket(number)
    add_event(t, me, 'priority', f'{old} → {p}', {'from': old, 'to': p})
    notify(t, me, f'Priority {old} → {p}')
    return redirect(f'/maintenance/t/{t.number}/?m=priority+updated')


@app.post('/t/<number>/follow')
def ticket_follow(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    em = TK.valid_email(request.form.get('email') or '')
    if request.form.get('email') and not em:
        abort(400)
    if em:                                  # v227: an outside address as a follower
        execute(TK.follow_sql(t.id, em, True))
        t = load_ticket(number)
        add_event(t, me, 'follow', f'added {em} as a follower', {'who': em})
        notify(t, me, f'{em} added as a follower')
        return redirect(f'/maintenance/t/{t.number}/?m=follower+added')
    on = (request.form.get('on') or '1') == '1'
    execute(TK.follow_sql(t.id, me, on))
    t = load_ticket(number)
    add_event(t, me, 'follow' if on else 'unfollow')
    return redirect(f'/maintenance/t/{t.number}/')


@app.post('/t/<number>/link')
def ticket_link(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    key = (request.form.get('alert_key') or '').strip()
    if not key:
        abort(400)
    execute(TK.link_alert_sql(t.id, key))
    t = load_ticket(number)
    add_event(t, me, 'alert', f'linked to alert {key}', {'alert_key': key})
    return redirect(f'/maintenance/t/{t.number}/?m=alert+linked')


@app.post('/t/<number>/resolution')
def ticket_resolution(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    rc = (request.form.get('root_cause') or '').strip()
    if rc and rc not in dict(TK.ROOT_CAUSES):
        abort(400)
    lk = (request.form.get('lost_kwh') or '').strip().replace(',', '')
    try:
        lost = float(lk) if lk else None
    except ValueError:
        abort(400)
    res = (request.form.get('resolution') or '').strip()[:4000]
    execute(TK.resolution_sql(t.id, rc, res, lost))
    t = load_ticket(number)
    add_event(t, me, 'resolution', res, {'root_cause': rc, 'lost_kwh': lost})
    notify(t, me, 'Resolution recorded', res)
    return redirect(f'/maintenance/t/{t.number}/?m=resolution+saved')


def _safe_name(name: str) -> str:
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(name or 'file'))[:80]
    return base or 'file'


@app.post('/t/<number>/comment')
def ticket_comment(number):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    body = (request.form.get('body') or '').strip()[:4000]
    uploads = [u for u in request.files.getlist('files') if u and u.filename]
    if not body and not uploads:
        return redirect(f'/maintenance/t/{t.number}/')
    ev = add_event(t, me, 'comment' if body else 'attachment', body)
    saved = []
    for u in uploads:
        fn = _safe_name(u.filename)
        ext = os.path.splitext(fn)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        data = u.read()
        if not data or len(data) > MAX_UPLOAD:
            continue
        stored = f'{uuid.uuid4().hex}{ext}'
        d = os.path.join(FILES_DIR, t.number)
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, stored), 'wb') as fh:
            fh.write(data)
        mime = u.mimetype or mimetypes.guess_type(fn)[0] or 'application/octet-stream'
        execute(TK.attachment_sql(t.id, ev, fn, stored, len(data), mime, me))
        saved.append(fn)
    t = load_ticket(number)
    notify(t, me, 'Update' if body else 'Attachment', body + (('\nFiles: ' + ', '.join(saved)) if saved else ''))
    return redirect(f'/maintenance/t/{t.number}/?m=update+posted')


@app.get('/t/<number>/file/<int:fid>')
def ticket_file(number, fid):
    me = _guard()
    if me is None:
        return no_access()
    t = _ticket_or_404(number)
    rows = TK.rows_from_csv(rows_csv("SELECT filename, stored_as, mime FROM ticket_attachment"
                                     f" WHERE id = {int(fid)} AND ticket_id = {t.id};"))
    if not rows:
        abort(404)
    r = rows[0]
    path = os.path.join(FILES_DIR, t.number, os.path.basename(r['stored_as']))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=r['mime'] or 'application/octet-stream', as_attachment=False, download_name=r['filename'])


@app.get('/healthz')
def healthz():
    return 'ok\n'


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('ARGIA_MAINT_PORT', '8514')))
