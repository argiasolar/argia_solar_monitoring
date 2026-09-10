#!/usr/bin/env python3
"""/finance/ and /projects/ — the finance snapshot and the project
portfolio (v244, Phase 1 first slice). nginx proxies both prefixes here
behind the session login and forwards the account as X-Remote-User;
the app then allow-lists WHO may see these pages (Tomasz only for now
— "let's keep it a secret") and answers a plain 403 to everyone else,
including the landing-page card that stays hidden unless /finance/me
says allowed.

  ARGIA_FIN_PORT      default 8515
  ARGIA_FIN_EMAILS    comma-separated e-mails (default tomasz.zemelka@argia.com.mx)
  ARGIA_FIN_USERS     comma-separated usernames (dev boxes)
  ARGIA_FIN_ALLOW     allow file (default /opt/argia/auth/fin_allow.txt)
  ARGIA_FIN_ENTITY    the entity shown (default: DEMO-MX until the real ones are listed)

Pages: /finance/ (Today), /finance/ar/, /finance/ap/, /finance/bank/,
/finance/exceptions/, /finance/me; /projects/ (portfolio),
/projects/<ARG-ID>/ (one project). Reads only — every write in this
module arrives in a later slice with CSRF + fin_event, like Maintenance.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import os
import sys
from decimal import Decimal
from typing import Dict, List, Optional

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get('ARGIA_V2_DIR', '/root/argia_v2/v2'))

import portal_chrome as PC                                      # noqa: E402
import fin_books as FB                                          # noqa: E402  v245: the pages on the real books
from argia.fin import cost as COST, health as HEALTH, ledger as LG, rules as RULES   # noqa: E402
from argia.fin.money import D                                    # noqa: E402
from argia.store import pgq                                      # noqa: E402

t, ti, tile, pill = PC.t, PC.ti, PC.tile, PC.pill
app = Flask(__name__)
ENTITY = os.environ.get('ARGIA_FIN_ENTITY', 'DEMO-MX')
# v245: 'books' = the real books read from Drive (entity ARGIA-MX …); 'demo' = the v244 demo world.
# The demo pages stay reachable under /finance/demo/ and /projects/demo/ in books mode.
MODE = os.environ.get('ARGIA_FIN_MODE', 'demo' if ENTITY.startswith('DEMO') else 'books')
ALLOWED_USERS = {u.strip().lower() for u in os.environ.get('ARGIA_FIN_USERS', '').split(',') if u.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get('ARGIA_FIN_EMAILS', 'tomasz.zemelka@argia.com.mx').split(',') if e.strip()}
ALLOW_FILE = os.environ.get('ARGIA_FIN_ALLOW', '/opt/argia/auth/fin_allow.txt')
MX = dt.timezone(dt.timedelta(hours=-6))


# ------------------------------------------------------------- identity
def email_of(username):
    try:
        import setup_app as sa
        return (sa.profile_of(username)[1] or '').strip().lower()
    except Exception:                                # noqa: BLE001
        return ''


def allow_file_entries(path=None):
    try:
        with open(path or ALLOW_FILE, encoding='utf-8') as fh:
            return {ln.split('#', 1)[0].strip().lower() for ln in fh if ln.split('#', 1)[0].strip()}
    except OSError:
        return set()


def allowed(username, email_lookup=None):
    email_lookup = email_lookup or email_of
    u = (username or '').strip().lower()
    if not u:
        return False
    extra = allow_file_entries()
    if u in ALLOWED_USERS or u in extra:
        return True
    email = (email_lookup(u) or '').strip().lower()
    return bool(email) and (email in ALLOWED_EMAILS or email in extra)


def actor() -> str:
    return (request.headers.get('X-Remote-User') or '').strip()


def today() -> dt.date:
    f = app.config.get('TODAY')
    return f() if f else dt.datetime.now(MX).date()


# ------------------------------------------------------------- database
def rows(name: str, sql: str) -> List[Dict[str, str]]:
    """SELECT -> list of dict rows. ``name`` tags the query so a fake can
    answer it by name in tests (the SQL itself is still what runs)."""
    fn = app.config.get('ROWS')
    if fn:
        return fn(name, sql)
    text = pgq.psql_csv(f"SET statement_timeout='15s'; -- q:{name}\n" + sql)
    return list(csv.DictReader(io.StringIO(text)))


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _ent() -> str:
    """The entity the DEMO pages query: in books mode the demo world keeps
    its own rows (DEMO-MX) under /finance/demo/ and /projects/demo/."""
    if app.config.get('MODE', MODE) == 'books':
        return app.config.get('DEMO_ENTITY', 'DEMO-MX')
    return app.config.get('ENTITY', ENTITY)


def q_accounts():
    return rows('accounts', f"SELECT a.account_id, a.bank, a.currency, a.purpose,"
                            f" (SELECT closing FROM bank_statement s WHERE s.account_id = a.account_id ORDER BY period_end DESC LIMIT 1) AS closing,"
                            f" (SELECT period_end FROM bank_statement s WHERE s.account_id = a.account_id ORDER BY period_end DESC LIMIT 1) AS as_of,"
                            f" (SELECT count(*) FROM bank_transaction x WHERE x.account_id = a.account_id AND NOT x.own_transfer AND NOT EXISTS"
                            f"   (SELECT 1 FROM bank_match m WHERE m.line_key = x.line_key AND m.reversed_at IS NULL)) AS unreconciled"
                            f" FROM bank_account a WHERE a.entity_id = {_q(_ent())} AND a.active ORDER BY a.account_id;")


def q_ar():
    return rows('ar', f"SELECT i.savio_invoice_id AS ref, i.cfdi_uuid, coalesce(c.name, i.customer_id::text, '') AS who, i.project_id, i.plant_key,"
                      f" i.issue_date, i.due_date, i.currency, i.total, i.status,"
                      f" coalesce((SELECT sum(amount) FROM allocation al WHERE al.invoice_ref = i.savio_invoice_id AND al.invoice_side = 'ar'), 0) AS applied"
                      f" FROM customer_invoice i LEFT JOIN customer_master c ON c.customer_id = i.customer_id"
                      f" WHERE i.entity_id = {_q(_ent())} ORDER BY i.due_date, i.savio_invoice_id;")


def q_ap():
    return rows('ap', f"SELECT i.cfdi_uuid AS ref, coalesce(s.name, i.emisor_rfc) AS who, i.project_id, i.cost_code, p.po_number,"
                      f" i.issue_date, i.due_date, i.currency, i.total, i.subtotal, i.tipo, i.status, i.related_uuid,"
                      f" coalesce((SELECT sum(amount) FROM allocation al WHERE al.invoice_ref = i.cfdi_uuid AND al.invoice_side = 'ap'), 0) AS applied"
                      f" FROM supplier_invoice i LEFT JOIN supplier s ON s.supplier_id = i.supplier_id LEFT JOIN purchase_order p ON p.po_id = i.po_id"
                      f" WHERE i.entity_id = {_q(_ent())} ORDER BY i.due_date, i.cfdi_uuid;")


def q_exceptions():
    return rows('exceptions', f"SELECT exception_id, kind, ref, project_id, owner, status, detail, opened_at::date AS opened"
                              f" FROM fin_exception WHERE status IN ('open','in_progress') AND (entity_id = {_q(_ent())} OR entity_id IS NULL)"
                              f" ORDER BY opened_at DESC, exception_id DESC;")


def q_projects():
    return rows('projects', f"SELECT p.project_id, p.name, p.site, p.project_type, p.status, p.pm_user, p.contract_value, p.contract_ccy, p.kwp_dc,"
                            f" coalesce(c.name, '') AS customer, p.plant_key, p.pmo_sheet_id"
                            f" FROM project p LEFT JOIN customer_master c ON c.customer_id = p.customer_id"
                            f" WHERE p.entity_id = {_q(_ent())} ORDER BY p.project_id;")


def q_milestones():
    return rows('milestones', "SELECT project_id, ref, name, kind, baseline_date, planned_date, actual_date, billable, amount, billed_invoice, depends_on"
                              " FROM project_milestone ORDER BY project_id, planned_date, ref;")


def q_budget_lines():
    return rows('budget_lines', "SELECT v.project_id, v.version, v.status, l.cost_code, l.amount FROM budget_version v JOIN budget_line l"
                                " ON l.project_id = v.project_id AND l.version = v.version ORDER BY v.project_id, v.version, l.cost_code;")


def q_change_orders():
    return rows('change_orders', "SELECT project_id, ref, status, revenue_impact, cost_impact FROM change_order ORDER BY project_id, ref;")


def q_pos():
    return rows('pos', f"SELECT p.po_id, p.po_number, p.project_id, p.status, p.currency, p.total, p.subtotal, p.expected_delivery, coalesce(s.name, '') AS supplier,"
                       f" coalesce((SELECT cost_code FROM po_line l WHERE l.po_id = p.po_id ORDER BY line_no LIMIT 1), '') AS cost_code,"
                       f" coalesce((SELECT sum(total) FROM supplier_invoice i WHERE i.po_id = p.po_id AND i.tipo = 'I' AND i.status <> 'rejected'), 0) AS invoiced,"
                       f" coalesce((SELECT sum(subtotal) FROM supplier_invoice i WHERE i.po_id = p.po_id AND i.tipo = 'I' AND i.status <> 'rejected'), 0) AS invoiced_net,"
                       f" coalesce((SELECT sum(value) FROM receipt r WHERE r.po_id = p.po_id), 0) AS received"
                       f" FROM purchase_order p LEFT JOIN supplier s ON s.supplier_id = p.supplier_id WHERE p.entity_id = {_q(_ent())} ORDER BY p.po_number;")


def q_tasks(pid: str):
    return rows('tasks', f"SELECT task_id, name, phase, start_date, end_date, progress_pct, resource FROM project_task WHERE project_id = {_q(pid)} ORDER BY start_date, task_id;")


# ------------------------------------------------------------- shaping
ACTUAL_STATUSES = ('approved', 'partially_paid', 'paid')


def money(v, ccy='MXN', dec=0) -> str:
    try:
        x = D(v)
    except (ValueError, TypeError):
        return '—'
    return f'{x:,.{dec}f} <span class="unit">{html.escape(ccy)}</span>'


def _d(s) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def invoices_of(raw: List[dict], ref_key: str = 'ref') -> List[LG.Invoice]:
    out = []
    for r in raw:
        if r.get('tipo', 'I') != 'I':
            continue
        out.append(LG.Invoice(r[ref_key], D(r['total']), _d(r['issue_date']) or today(), 30, r['currency'], r['status'], _d(r.get('due_date'))))
    return out


def allocations_of(raw: List[dict], ref_key: str = 'ref') -> List[LG.Allocation]:
    return [LG.Allocation(r[ref_key], D(r.get('applied') or 0)) for r in raw if D(r.get('applied') or 0) > 0]


def aging_by_ccy(raw: List[dict]) -> Dict[str, Dict[str, Decimal]]:
    inv = invoices_of(raw)
    al = allocations_of(raw)
    out = {}
    for ccy in sorted({i.currency for i in inv}):
        out[ccy] = LG.aging(inv, al, today(), currency=ccy)
    return out


def project_cost(pid: str, ap_rows: List[dict], po_rows: List[dict], budget_rows: List[dict], co_rows: List[dict], contract: Decimal) -> dict:
    """actual / committed / EAC / margin per project from the ledgers."""
    lines: List[COST.CostLine] = []
    for r in ap_rows:
        if r.get('project_id') != pid or r.get('tipo') not in ('I', 'E'):
            continue
        st = 'approved' if r['status'] in ACTUAL_STATUSES else r['status']
        kind = 'invoice' if r['tipo'] == 'I' else 'credit_note'
        net = D(r.get('subtotal') if r.get('subtotal') not in (None, '') else r['total'])   # cost is net of IVA (recoverable); budgets are net
        lines.append(COST.CostLine(r.get('cost_code') or '?', net, kind, st, r['ref']))
    for p in po_rows:
        if p.get('project_id') != pid:
            continue
        po_net = D(p.get('subtotal') if p.get('subtotal') not in (None, '') else p['total'])
        lines.append(COST.CostLine(p.get('cost_code') or '?', po_net, 'po', p['status'], p['po_number']))
        inv_net = D(p.get('invoiced_net') if p.get('invoiced_net') not in (None, '') else p.get('invoiced') or 0)
        if inv_net > 0:
            lines.append(COST.CostLine(p.get('cost_code') or '?', inv_net, 'po_invoiced', 'approved', p['po_number']))
    versions: Dict[int, COST.BudgetVersion] = {}
    for b in budget_rows:
        if b['project_id'] != pid:
            continue
        v = versions.get(int(b['version']))
        lines_v = dict(v.lines) if v else {}
        lines_v[b['cost_code']] = D(b['amount'])
        versions[int(b['version'])] = COST.BudgetVersion(int(b['version']), b['status'], lines_v)
    cos = [COST.ChangeOrder(c['ref'], c['status'], D(c['revenue_impact']), D(c['cost_impact'])) for c in co_rows if c['project_id'] == pid]
    m = COST.margin(contract, cos, list(versions.values()), lines)
    pending = sum((D(r.get('subtotal') or r['total']) for r in ap_rows if r.get('project_id') == pid and r.get('tipo') == 'I' and r['status'] in ('received', 'matched', 'exception')), Decimal('0'))
    return {'margin': m, 'actual': COST.total(COST.actual(lines)), 'committed': COST.total(COST.committed(lines)),
            'pending': pending, 'exposure': COST.pending_exposure(cos), 'budget': (COST.active_budget(list(versions.values())).total if versions else Decimal('0')),
            'versions': versions}


def project_health(pid: str, ms_rows: List[dict], cost: dict, ap_rows: List[dict], contract: Decimal) -> HEALTH.Health:
    ms = [RULES.Milestone(m['ref'], m['kind'], _d(m['planned_date']), _d(m['baseline_date']), _d(m['actual_date']), m['billable'] in ('t', 'true', True, '1'),
                          m.get('billed_invoice') or '', D(m.get('amount') or 0)) for m in ms_rows if m['project_id'] == pid]
    late = 0
    for m in ms:
        if m.actual is None and m.planned and m.baseline:
            late = max(late, (m.planned - m.baseline).days)
    overdue = sum((LG.outstanding(i, []) for i in invoices_of([r for r in ap_rows if r.get('project_id') == pid]) if LG.age_days(i, today()) > 0), Decimal('0'))
    unbilled = sum((m.amount for m in RULES.unbilled(ms)), Decimal('0'))
    return HEALTH.score(late, cost['margin'].eac, cost['budget'], overdue + unbilled, contract)


# --------------------------------------------------------------- pages
def _table(head: List[str], body: List[str], cls='') -> str:
    """v246: the demo tables get the same search / filter / sort tools as the books pages."""
    return PC.data_table(head, body, cls)


def _bucket_tiles(label_en, label_es, ag: Dict[str, Dict[str, Decimal]], tone_on_overdue=True) -> str:
    out = ''
    live = {c: a for c, a in ag.items() if a['total'] > 0}
    for ccy, a in (live or ag).items():
        over = a['0-30'] + a['31-60'] + a['61-90'] + a['90+']
        tone = ('bad' if a['90+'] > 0 else 'warn' if over > 0 else 'good') if tone_on_overdue and a['total'] > 0 else ''
        why = (f"{a['90+']:,.0f} {ccy} more than 90 days overdue" if a['90+'] > 0 else f"{over:,.0f} {ccy} past due") if tone in ('warn', 'bad') else ''
        why_es = (f"{a['90+']:,.0f} {ccy} con más de 90 días de atraso" if a['90+'] > 0 else f"{over:,.0f} {ccy} vencidos") if why else ''
        out += tile(f'{label_en} · {ccy}', f'{label_es} · {ccy}', money(a['total'], ccy),
                    f"current {a['current']:,.0f} · 0-30 {a['0-30']:,.0f} · 31-60 {a['31-60']:,.0f} · 61-90 {a['61-90']:,.0f} · 90+ {a['90+']:,.0f}",
                    f"al día {a['current']:,.0f} · 0-30 {a['0-30']:,.0f} · 31-60 {a['31-60']:,.0f} · 61-90 {a['61-90']:,.0f} · 90+ {a['90+']:,.0f}",
                    tone=tone, why_en=why, why_es=why_es)
    return out or tile(label_en, label_es, '—', 'nothing open', 'nada abierto')


def page_today() -> str:
    acc = q_accounts()
    ar, ap = q_ar(), q_ap()
    exc = q_exceptions()
    projects, ms, bl, co, pos = q_projects(), q_milestones(), q_budget_lines(), q_change_orders(), q_pos()
    cash_tiles = ''
    for a in acc:
        n = int(a.get('unreconciled') or 0)
        tone = 'warn' if n else ('good' if a.get('closing') else '')
        cash_tiles += tile(f"{a['account_id']}", f"{a['account_id']}", money(a.get('closing'), a['currency']) if a.get('closing') else '—',
                           f"statement to {a.get('as_of') or '—'} · {n} unreconciled line(s)", f"estado al {a.get('as_of') or '—'} · {n} línea(s) sin conciliar",
                           tone=tone, why_en=(f"{n} bank line(s) not yet matched to an invoice or payment" if n else ''),
                           why_es=(f"{n} línea(s) bancarias sin conciliar" if n else ''))
    ar_t = _bucket_tiles('Receivables', 'Por cobrar', aging_by_ccy(ar))
    ap_t = _bucket_tiles('Payables', 'Por pagar', aging_by_ccy([r for r in ap if r['status'] not in ('received', 'exception')]))
    pend = sum((D(r['total']) for r in ap if r['tipo'] == 'I' and r['status'] in ('received', 'matched', 'exception')), Decimal('0'))
    kinds: Dict[str, int] = {}
    for e in exc:
        kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
    exc_t = tile('Exceptions', 'Excepciones', str(len(exc)), ' · '.join(f'{k} {v}' for k, v in sorted(kinds.items())) or 'queue empty',
                 ' · '.join(f'{k} {v}' for k, v in sorted(kinds.items())) or 'sin pendientes', tone=('bad' if len(exc) >= 5 else 'warn' if exc else 'good'),
                 why_en=('every item needs an owner and a resolution' if exc else ''), why_es=('cada punto necesita dueño y resolución' if exc else ''))
    pend_t = tile('Supplier invoices awaiting approval', 'Facturas de proveedor por aprobar', money(pend), 'received or matched, not yet approved — not in payables yet',
                  'recibidas o conciliadas, aún sin aprobar — todavía no son por pagar', tone=('warn' if pend > 0 else ''),
                  why_en=(f'{pend:,.0f} MXN of supplier invoices are waiting for a person' if pend > 0 else ''), why_es=(f'{pend:,.0f} MXN de facturas esperan a una persona' if pend > 0 else ''))
    rows_html = []
    for p in projects:
        if p['status'] in ('draft', 'cancelled'):
            continue
        c = project_cost(p['project_id'], ap, pos, bl, co, D(p['contract_value'] or 0))
        h = project_health(p['project_id'], ms, c, ap, D(p['contract_value'] or 0))
        m = c['margin']
        rows_html.append(f'<tr><td><a href="/projects/{html.escape(p["project_id"])}/"><b>{html.escape(p["name"])}</b></a> <span class="mono muted">{p["project_id"]}</span></td>'
                         f'<td>{pill("ok" if p["status"] == "active" else "off", p["status"])}</td>'
                         f'<td class="r">{money(m.contract, p["contract_ccy"])}</td><td class="r">{money(c["budget"], p["contract_ccy"])}</td>'
                         f'<td class="r">{money(c["actual"], p["contract_ccy"])}</td><td class="r">{money(c["committed"], p["contract_ccy"])}</td>'
                         f'<td class="r">{money(m.eac, p["contract_ccy"])}</td><td class="r"><b>{money(m.margin, p["contract_ccy"])}</b> <span class="muted">{m.margin_pct if m.margin_pct is not None else "—"}%</span></td>'
                         f'<td class="r">{money(m.erosion, p["contract_ccy"])}</td>'
                         f'<td><span class="pill {"ok" if h.band == "green" else "warn" if h.band == "amber" else "crit"}">{h.score}</span></td></tr>')
    body = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{t("Finance · today", "Finanzas · hoy")} · {today().isoformat()} · <span class="mono">{html.escape(_ent())}</span> · {t("demo data", "datos demo")}</div><h1 class="pt">{t("Where the money is", "Dónde está el dinero")}</h1></div>
</div>
<div class="tiles" style="margin-top:20px">{cash_tiles}{ar_t}{ap_t}{pend_t}{exc_t}</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Projects — cost and margin", "Proyectos — costo y margen")}</h2><span class="muted" style="font-size:12.5px">{t("net of IVA · actual = approved supplier invoices − credit notes · committed = approved open POs − invoiced · EAC = actual + committed + estimate to complete", "sin IVA · real = facturas aprobadas − notas de crédito · comprometido = OC aprobadas abiertas − facturado · EAC = real + comprometido + estimado por completar")}</span></div>
 {_table([t("Project", "Proyecto"), "Status", t("Contract", "Contrato"), t("Budget", "Presupuesto"), t("Actual", "Real"), t("Committed", "Comprometido"), "EAC", t("Margin", "Margen"), t("Erosion", "Erosión"), t("Health", "Salud")], rows_html)}
</div>
<div class="card" style="margin-top:16px;padding:16px 20px">
 <h2 class="ct">{t("How the numbers are calculated", "Cómo se calculan los números")}</h2>
 <p class="note">{t("Cash = the closing balance of the latest bank statement per account (statements load whole or not at all). Receivables = open customer invoices mirrored from Savio, aged from the due date per currency. Payables = approved supplier invoices (CFDI XML is the source; a received or matched invoice is not payable until a person approves it). Exceptions = what a person must decide: missing PO, over PO, foreign CFDI, rejected statement, unreconciled line. Project figures follow AGS-904 §3; health follows AGS-903 §4. Everything on this page is DEMO data until the real entities are configured.",
 "Efectivo = saldo final del último estado de cuenta por cuenta (los estados cargan completos o no cargan). Por cobrar = facturas de cliente abiertas reflejadas de Savio, envejecidas desde su vencimiento por moneda. Por pagar = facturas de proveedor aprobadas (el XML CFDI es la fuente; una factura recibida o conciliada no es pagadera hasta que una persona la apruebe). Excepciones = lo que una persona debe decidir. Las cifras de proyecto siguen AGS-904 §3; la salud AGS-903 §4. Todo en esta página es información DEMO hasta configurar las entidades reales.")}</p>
</div>'''
    return PC.page('Finance', body, 'finance', '', wide=True)


def _ledger_page(kind: str) -> str:
    raw = q_ar() if kind == 'ar' else q_ap()
    inv = {i.ref: i for i in invoices_of(raw)}
    al = allocations_of(raw)
    trs = []
    for r in raw:
        i = inv.get(r['ref'])
        left = LG.outstanding(i, al) if i else Decimal('0')
        days = LG.age_days(i, today()) if i else 0
        b = LG.bucket_of(days) if i and left > 0 else '—'
        who = html.escape(r.get('who') or '')
        link = f'<a href="/projects/{html.escape(r["project_id"])}/">{html.escape(r["project_id"])}</a>' if r.get('project_id') else (html.escape(r.get('plant_key') or '') or '—')
        extra = (f'<td>{html.escape(r.get("po_number") or "—")}</td><td>{html.escape(r.get("cost_code") or "—")}</td><td>{html.escape(r.get("tipo") or "")}</td>' if kind == 'ap' else '')
        st = r['status']
        cls = 'ok' if st in ('paid',) else 'warn' if st in ('partially_paid', 'matched', 'received') else 'crit' if st in ('exception', 'cancelled', 'rejected') else 'off'
        trs.append(f'<tr><td class="mono" style="font-size:12px">{html.escape(r["ref"][:18])}</td><td>{who}</td><td>{link}</td>{extra}'
                   f'<td>{html.escape(str(r.get("issue_date") or ""))}</td><td>{html.escape(str(r.get("due_date") or ""))}</td>'
                   f'<td class="r">{money(r["total"], r["currency"])}</td><td class="r">{money(r.get("applied") or 0, r["currency"])}</td>'
                   f'<td class="r"><b>{money(left, r["currency"])}</b></td><td>{b}</td><td>{pill(cls, st)}</td></tr>')
    head = ['Ref', t('Customer', 'Cliente') if kind == 'ar' else t('Supplier', 'Proveedor'), t('Project', 'Proyecto')]
    if kind == 'ap':
        head += ['PO', t('Cost code', 'Código'), t('Type', 'Tipo')]
    head += [t('Issued', 'Emitida'), t('Due', 'Vence'), 'Total', t('Applied', 'Aplicado'), t('Outstanding', 'Saldo'), t('Bucket', 'Antigüedad'), 'Status']
    title = ('Receivables', 'Por cobrar') if kind == 'ar' else ('Payables', 'Por pagar')
    ag = aging_by_ccy(raw if kind == 'ar' else [r for r in raw if r['status'] not in ('received', 'exception')])
    body = f'''<div class="kicker">{t("Finance", "Finanzas")} · {today().isoformat()} · <span class="mono">{html.escape(_ent())}</span></div><h1 class="pt">{t(*title)}</h1>
<div class="tiles" style="margin-top:16px">{_bucket_tiles(title[0], title[1], ag)}</div>
<div class="card" style="margin-top:16px;overflow:hidden">{_table(head, trs)}</div>
<p class="note">{t("Source: Savio (customer invoices, CFDI UUID) — mirrored, never re-typed." if kind == "ar" else "Source: the supplier's CFDI XML (UUID once, totals re-checked). Received/matched rows await approval and are not payables yet; a credit note (E) reduces its original; a complemento (P) is the supplier's receipt of our payment.",
 "Fuente: Savio (facturas de cliente, UUID CFDI) — espejo, nunca recapturado." if kind == "ar" else "Fuente: el XML CFDI del proveedor (UUID una vez, totales verificados). Las filas recibidas/conciliadas esperan aprobación y aún no son por pagar; una nota de crédito (E) reduce su original; un complemento (P) es el recibo del proveedor de nuestro pago.")}</p>'''
    return PC.page(title[0], body, 'finance', kind, wide=True)


def page_bank() -> str:
    acc = q_accounts()
    lines = rows('bank_lines', f"SELECT x.line_key, x.account_id, x.tx_date, x.amount, x.description, x.counterpart, x.own_transfer,"
                               f" coalesce((SELECT string_agg(m.target_kind || ':' || m.target_ref, ', ') FROM bank_match m WHERE m.line_key = x.line_key AND m.reversed_at IS NULL), '') AS matched"
                               f" FROM bank_transaction x JOIN bank_account a ON a.account_id = x.account_id WHERE a.entity_id = {_q(_ent())} ORDER BY x.account_id, x.tx_date DESC, x.line_key;")
    tiles = ''.join(tile(a['account_id'], a['account_id'], money(a.get('closing'), a['currency']) if a.get('closing') else '—',
                         f"{a['bank']} · {a.get('purpose') or ''} · to {a.get('as_of') or '—'}", f"{a['bank']} · {a.get('purpose') or ''} · al {a.get('as_of') or '—'}") for a in acc)
    trs = []
    for l in lines:
        own = l.get('own_transfer') in ('t', 'true', True)
        state = 'transfer' if own else ('reconciled' if l.get('matched') else 'open')
        trs.append(f'<tr><td>{html.escape(l["account_id"])}</td><td>{html.escape(str(l["tx_date"]))}</td><td class="r">{money(l["amount"], "", 2)}</td>'
                   f'<td>{html.escape(l.get("description") or "")}</td><td class="mono" style="font-size:11px">{html.escape(l.get("counterpart") or "")}</td>'
                   f'<td>{html.escape(l.get("matched") or ("own transfer" if own else ""))}</td><td>{pill("ok" if state == "reconciled" else "off" if state == "transfer" else "warn", state)}</td></tr>')
    body = f'''<div class="kicker">{t("Finance", "Finanzas")} · <span class="mono">{html.escape(_ent())}</span></div><h1 class="pt">{t("Bank", "Banco")}</h1>
<div class="grid g3" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden">{_table([t("Account", "Cuenta"), t("Date", "Fecha"), t("Amount", "Importe"), t("Description", "Descripción"), t("Counterpart", "Contraparte"), t("Matched to", "Conciliado con"), "State"], trs)}</div>
<p class="note">{t("A statement loads whole (opening + activity = closing) or not at all; a line reconciles once unless split; own transfers are neither income nor expense.", "Un estado de cuenta carga completo (inicial + movimientos = final) o no carga; una línea se concilia una vez salvo división; los traspasos propios no son ingreso ni gasto.")}</p>'''
    return PC.page('Bank', body, 'finance', 'bank', wide=True)


def page_exceptions() -> str:
    exc = q_exceptions()
    trs = [f'<tr><td>{pill("crit" if e["kind"] in ("CFDI_TOTALS", "FOREIGN_CFDI", "STATEMENT_REJECTED") else "warn", e["kind"])}</td>'
           f'<td class="mono" style="font-size:12px">{html.escape(e["ref"][:40])}</td><td>{html.escape(e.get("project_id") or "—")}</td>'
           f'<td>{html.escape(e.get("owner") or "—")}</td><td>{html.escape(str(e.get("opened") or ""))}</td><td>{html.escape(e.get("detail") or "")}</td></tr>' for e in exc]
    body = f'''<div class="kicker">{t("Finance", "Finanzas")} · <span class="mono">{html.escape(_ent())}</span></div><h1 class="pt">{t("Exceptions", "Excepciones")} · {len(exc)}</h1>
<div class="card" style="margin-top:16px;overflow:hidden">{_table([t("Kind", "Tipo"), "Ref", t("Project", "Proyecto"), t("Owner", "Dueño"), t("Opened", "Abierta"), t("Detail", "Detalle")], trs) if trs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("Nothing to decide.", "Nada que decidir.")}</p>'}</div>
<p class="note">{t("Every exception has an owner, a status and a resolution history (AGS-904 R6). Resolution actions arrive in the next slice, with CSRF and an audit event.", "Cada excepción tiene dueño, estado e historial de resolución (AGS-904 R6). Las acciones de resolución llegan en el siguiente corte, con CSRF y evento de auditoría.")}</p>'''
    return PC.page('Exceptions', body, 'finance', 'exceptions', wide=True)


def page_portfolio() -> str:
    projects, ms, bl, co, pos, ap = q_projects(), q_milestones(), q_budget_lines(), q_change_orders(), q_pos(), q_ap()
    cards = ''
    n_active = 0
    tot_contract = Decimal('0')
    tot_margin = Decimal('0')
    for p in projects:
        contract = D(p['contract_value'] or 0)
        c = project_cost(p['project_id'], ap, pos, bl, co, contract)
        h = project_health(p['project_id'], ms, c, ap, contract)
        my_ms = [m for m in ms if m['project_id'] == p['project_id']]
        nxt = next((m for m in sorted(my_ms, key=lambda x: str(x['planned_date'])) if not m['actual_date']), None)
        late = RULES.late_milestones([RULES.Milestone(m['ref'], m['kind'], _d(m['planned_date']), _d(m['baseline_date']), _d(m['actual_date'])) for m in my_ms], today())
        if p['status'] == 'active':
            n_active += 1
            if p['contract_ccy'] == 'MXN':
                tot_contract += c['margin'].contract
                tot_margin += c['margin'].margin
        band = {'green': 'ok', 'amber': 'warn', 'red': 'crit'}[h.band]
        cards += f'''
 <a href="/projects/{html.escape(p["project_id"])}/" class="card pcard" style="padding:18px 20px;display:flex;flex-direction:column;gap:8px;color:var(--ink)">
  <div style="display:flex;align-items:center;justify-content:space-between"><span class="mono muted">{p["project_id"]} · {html.escape(p["project_type"])}</span><span class="pill {band}" title="{html.escape("; ".join(h.reasons))}">{t("health", "salud")} {h.score}</span></div>
  <div><b style="font-size:17px">{html.escape(p["name"])}</b><div class="muted" style="font-size:12.5px">{html.escape(p.get("customer") or "")} · {html.escape(p.get("site") or "")} · PM {html.escape(p.get("pm_user") or "—")}</div></div>
  <div style="display:flex;gap:14px;font-size:12.5px;flex-wrap:wrap"><span>{pill("ok" if p["status"] == "active" else "off", p["status"])}</span><span><b>{t("contract", "contrato")}</b> {money(c["margin"].contract, p["contract_ccy"])}</span><span><b>{t("margin", "margen")}</b> {money(c["margin"].margin, p["contract_ccy"])} ({c["margin"].margin_pct if c["margin"].margin_pct is not None else "—"}%)</span></div>
  <div class="muted" style="font-size:12.5px">{t("next", "siguiente")}: {html.escape(nxt["name"]) + " · " + html.escape(str(nxt["planned_date"])) if nxt else t("no open milestone", "sin hitos abiertos")}{(" · <b style='color:#c2554e'>" + str(late[0][1]) + " d late</b>") if late else ""}</div>
 </a>'''
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px"><div class="kicker">{t("Projects", "Proyectos")} · {today().isoformat()} · <span class="mono">{html.escape(_ent())}</span> · {t("demo data", "datos demo")}</div><h1 class="pt">{t("Portfolio", "Portafolio")}</h1></div>
</div>
<div class="grid g4" style="margin-top:20px">
 {tile("Active projects", "Proyectos activos", str(n_active), f"{len(projects)} in total", f"{len(projects)} en total")}
 {tile("Contract value · active MXN", "Valor contratado · activos MXN", money(tot_contract), "baseline + approved change orders", "base + órdenes de cambio aprobadas")}
 {tile("Forecast margin · active MXN", "Margen previsto · activos MXN", money(tot_margin), "contract − EAC", "contrato − EAC", tone=("good" if tot_margin > 0 else "bad"))}
 {tile("Schedule from", "Cronograma de", "PMO", "Sheets snapshot; money from the portal", "snapshot de Sheets; dinero del portal")}
</div>
<div class="grid g3" style="margin-top:16px;gap:16px">{cards}</div>'''
    return PC.page('Projects', body, 'projects', '', wide=True)


def page_project(pid: str) -> Optional[str]:
    projects = [p for p in q_projects() if p['project_id'] == pid]
    if not projects:
        return None
    p = projects[0]
    ms, bl, co, pos, ap = q_milestones(), q_budget_lines(), q_change_orders(), q_pos(), q_ap()
    tasks = q_tasks(pid)
    exc = [e for e in q_exceptions() if e.get('project_id') == pid]
    contract = D(p['contract_value'] or 0)
    c = project_cost(pid, ap, pos, bl, co, contract)
    h = project_health(pid, ms, c, ap, contract)
    m = c['margin']
    ccy = p['contract_ccy']
    band = {'green': 'ok', 'amber': 'warn', 'red': 'crit'}[h.band]
    ms_rows = []
    for x in [r for r in ms if r['project_id'] == pid]:
        planned, base, actual = _d(x['planned_date']), _d(x['baseline_date']), _d(x['actual_date'])
        slip = (planned - base).days if planned and base else 0
        st = 'done' if actual else ('late' if planned and planned < today() else 'open')
        ms_rows.append(f'<tr><td><b>{html.escape(x["name"])}</b> <span class="mono muted">{x["ref"]}</span></td><td>{x["kind"]}</td><td>{base or ""}</td><td>{planned or ""}{(" <span style=color:#c2554e>+" + str(slip) + "d</span>") if slip > 0 else ""}</td>'
                       f'<td>{actual or "—"}</td><td class="r">{money(x.get("amount") or 0, ccy) if x["billable"] in ("t", "true", True) else "—"}</td>'
                       f'<td>{html.escape(x.get("billed_invoice") or ("" if x["billable"] not in ("t", "true", True) else ("UNBILLED" if actual else "")))}</td>'
                       f'<td>{pill("ok" if st == "done" else "crit" if st == "late" else "off", st)}</td></tr>')
    active = COST.active_budget(list(c['versions'].values())) if c['versions'] else None
    act_by = COST.actual([COST.CostLine(r.get('cost_code') or '?', D(r.get('subtotal') or r['total']), 'invoice' if r['tipo'] == 'I' else 'credit_note', 'approved' if r['status'] in ACTUAL_STATUSES else r['status'], r['ref'])
                          for r in ap if r.get('project_id') == pid and r.get('tipo') in ('I', 'E')])
    codes = sorted(set(active.lines) | set(act_by)) if active else sorted(act_by)
    bud_rows = [f'<tr><td class="mono">{html.escape(code)}</td><td class="r">{money(active.lines.get(code, 0) if active else 0, ccy)}</td><td class="r">{money(act_by.get(code, 0), ccy)}</td>'
                f'<td class="r">{money((active.lines.get(code, 0) if active else Decimal("0")) - act_by.get(code, Decimal("0")), ccy)}</td></tr>' for code in codes]
    po_rows = [f'<tr><td class="mono">{html.escape(x["po_number"])}</td><td>{html.escape(x["supplier"])}</td><td>{html.escape(x["cost_code"] or "")}</td><td class="r">{money(x["total"], x["currency"])}</td>'
               f'<td class="r">{money(x["received"], x["currency"])}</td><td class="r">{money(x["invoiced"], x["currency"])}</td><td>{pill("ok" if x["status"] in ("approved", "partially_received", "closed") else "warn" if x["status"] == "submitted" else "off", x["status"])}</td><td>{html.escape(str(x.get("expected_delivery") or ""))}</td></tr>'
               for x in pos if x.get('project_id') == pid]
    inv_rows = [f'<tr><td class="mono" style="font-size:12px">{html.escape(r["ref"][:18])}</td><td>{html.escape(r["who"])}</td><td>{html.escape(r.get("po_number") or "—")}</td><td>{r["tipo"]}</td>'
                f'<td>{html.escape(str(r["issue_date"]))}</td><td class="r">{money(r["total"], r["currency"])}</td><td>{pill("ok" if r["status"] in ("paid",) else "warn" if r["status"] in ("received", "matched", "partially_paid") else "crit" if r["status"] == "exception" else "off", r["status"])}</td></tr>'
                for r in ap if r.get('project_id') == pid]
    co_rows = [f'<tr><td class="mono">{html.escape(x["ref"])}</td><td>{pill("ok" if x["status"] == "approved" else "warn" if x["status"] == "pending" else "off", x["status"])}</td><td class="r">{money(x["revenue_impact"], ccy)}</td><td class="r">{money(x["cost_impact"], ccy)}</td></tr>'
               for x in co if x['project_id'] == pid]
    task_rows = [f'<tr><td>{html.escape(x["name"])}</td><td>{html.escape(x.get("phase") or "")}</td><td>{html.escape(str(x.get("start_date") or ""))}</td><td>{html.escape(str(x.get("end_date") or ""))}</td><td class="r">{D(x.get("progress_pct") or 0):.0f}%</td><td>{html.escape(x.get("resource") or "")}</td></tr>' for x in tasks]
    exc_rows = [f'<tr><td>{pill("warn", e["kind"])}</td><td class="mono" style="font-size:12px">{html.escape(e["ref"][:30])}</td><td>{html.escape(e.get("detail") or "")}</td></tr>' for e in exc]
    body = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px"><div class="kicker"><a href="/projects/">{t("Projects", "Proyectos")}</a> · <span class="mono">{pid}</span> · {html.escape(p["project_type"])} · PM {html.escape(p.get("pm_user") or "—")}</div>
  <h1 class="pt">{html.escape(p["name"])}</h1><div class="muted">{html.escape(p.get("customer") or "")} · {html.escape(p.get("site") or "")}{(" · " + str(D(p["kwp_dc"])) + " kWp") if p.get("kwp_dc") and D(p["kwp_dc"]) > 0 else ""}</div></div>
 <div style="display:flex;gap:8px;align-items:center"><span class="pill {"ok" if p["status"] == "active" else "off"}">{p["status"]}</span><span class="pill {band}" title="{html.escape("; ".join(h.reasons))}">{t("health", "salud")} {h.score}</span></div>
</div>
<div class="grid g5" style="margin-top:20px">
 {tile("Contract", "Contrato", money(m.contract, ccy), f"baseline {D(p['contract_value'] or 0):,.0f} + approved COs", f"base {D(p['contract_value'] or 0):,.0f} + OC aprobadas")}
 {tile("Budget (active)", "Presupuesto (vigente)", money(c["budget"], ccy), f"baseline {m.baseline_budget:,.0f}", f"base {m.baseline_budget:,.0f}")}
 {tile("Actual + committed", "Real + comprometido", money(c["actual"] + c["committed"], ccy), f"actual {c['actual']:,.0f} · committed {c['committed']:,.0f} · awaiting approval {c['pending']:,.0f}", f"real {c['actual']:,.0f} · comprometido {c['committed']:,.0f} · por aprobar {c['pending']:,.0f}")}
 {tile("EAC", "EAC", money(m.eac, ccy), "actual + committed + estimate to complete", "real + comprometido + estimado por completar")}
 {tile("Forecast margin", "Margen previsto", money(m.margin, ccy), f"{m.margin_pct if m.margin_pct is not None else '—'}% · baseline {m.baseline_margin:,.0f} · erosion {m.erosion:,.0f}", f"{m.margin_pct if m.margin_pct is not None else '—'}% · base {m.baseline_margin:,.0f} · erosión {m.erosion:,.0f}", tone=("bad" if m.erosion > 0 and m.baseline_margin > 0 and m.erosion / m.baseline_margin > Decimal("0.2") else "warn" if m.erosion > 0 else "good"), why_en=(f"forecast margin is {m.erosion:,.0f} below the baseline margin" if m.erosion > 0 else ""), why_es=(f"el margen previsto está {m.erosion:,.0f} por debajo del margen base" if m.erosion > 0 else ""))}
</div>
<div class="card" style="margin-top:16px;padding:12px 20px 4px"><b>{t("Health", "Salud")} {h.score} · {h.band}</b> — {html.escape("; ".join(h.reasons))} <span class="muted">(AGS-903 §4, {h.thresholds_version})</span>
 {(" · " + t("unapproved exposure", "exposición no aprobada") + f": {t('cost', 'costo')} {c['exposure']['cost']:,.0f} / {t('revenue', 'ingreso')} {c['exposure']['revenue']:,.0f} {ccy}") if c["exposure"]["cost"] or c["exposure"]["revenue"] else ""}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Milestones", "Hitos")}</h2><span class="muted" style="font-size:12.5px">{t("baseline never moves; slip = planned − baseline", "la base nunca se mueve; desfase = planeado − base")}</span></div>
 {_table([t("Milestone", "Hito"), t("Kind", "Tipo"), t("Baseline", "Base"), t("Planned", "Planeado"), t("Actual", "Real"), t("Billing", "Facturación"), t("Invoice", "Factura"), "State"], ms_rows)}</div>
<div class="grid g2" style="margin-top:16px;gap:16px">
 <div class="card" style="overflow:hidden"><div class="chead"><h2 class="ct">{t("Budget vs actual by cost code", "Presupuesto vs real por código")}</h2></div>{_table([t("Code", "Código"), t("Budget", "Presupuesto"), t("Actual", "Real"), t("Remaining", "Restante")], bud_rows)}</div>
 <div class="card" style="overflow:hidden"><div class="chead"><h2 class="ct">{t("Change orders", "Órdenes de cambio")}</h2></div>{_table(["Ref", "Status", t("Revenue", "Ingreso"), t("Cost", "Costo")], co_rows) if co_rows else f'<p class="muted" style="padding:16px 20px;margin:0">{t("None.", "Ninguna.")}</p>'}</div>
</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Purchase orders", "Órdenes de compra")}</h2></div>{_table(["PO", t("Supplier", "Proveedor"), t("Code", "Código"), "Total", t("Received", "Recibido"), t("Invoiced", "Facturado"), "Status", t("Expected", "Esperado")], po_rows) if po_rows else f'<p class="muted" style="padding:16px 20px;margin:0">{t("None.", "Ninguna.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Supplier invoices", "Facturas de proveedor")}</h2></div>{_table(["UUID", t("Supplier", "Proveedor"), "PO", t("Type", "Tipo"), t("Issued", "Emitida"), "Total", "Status"], inv_rows) if inv_rows else f'<p class="muted" style="padding:16px 20px;margin:0">{t("None.", "Ninguna.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Tasks (PMO snapshot)", "Tareas (snapshot PMO)")}</h2><span class="muted" style="font-size:12.5px">{html.escape(p.get("pmo_sheet_id") or "")}</span></div>{_table([t("Task", "Tarea"), t("Phase", "Fase"), t("Start", "Inicio"), t("End", "Fin"), t("Progress", "Avance"), t("Resource", "Recurso")], task_rows) if task_rows else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No snapshot yet.", "Sin snapshot todavía.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Exceptions", "Excepciones")}</h2></div>{_table([t("Kind", "Tipo"), "Ref", t("Detail", "Detalle")], exc_rows) if exc_rows else f'<p class="muted" style="padding:16px 20px;margin:0">{t("None.", "Ninguna.")}</p>'}</div>'''
    return PC.page(p['name'], body, 'projects', '', wide=True)


# --------------------------------------------------------------- routes
def _gate():
    if not allowed(actor()):
        return PC.page('No access', f'<div style="max-width:520px;margin:60px auto;text-align:center"><h1 class="pt">{t("No access", "Sin acceso")}</h1>'
                                    f'<p class="muted">{t("This part of the portal is not open to your account.", "Esta sección no está disponible para su cuenta.")}</p>'
                                    f'<a class="btn" href="/">{t("Back to the portal", "Volver al portal")}</a></div>'), 403
    return None


@app.get('/finance/me')
def me():
    return jsonify({'user': actor(), 'allowed': allowed(actor())})


def mode() -> str:
    return app.config.get('MODE', MODE)


def _books():
    FB.bind(rows, money, today, app.config.get('ENTITY', ENTITY))
    return FB


@app.get('/finance/')
def finance_today():
    return _gate() or (_books().page_today() if mode() == 'books' else page_today())


@app.get('/finance/ar/')
def finance_ar():
    return _gate() or (_books().page_ledger('ar') if mode() == 'books' else _ledger_page('ar'))


@app.get('/finance/ap/')
def finance_ap():
    return _gate() or (_books().page_ledger('ap') if mode() == 'books' else _ledger_page('ap'))


@app.get('/finance/bank/')
def finance_bank():
    return _gate() or (_books().page_bank() if mode() == 'books' else page_bank())


@app.get('/finance/pl/')
def finance_pl():
    return _gate() or _books().page_pl()


@app.get('/finance/exceptions/')
def finance_exceptions():
    return _gate() or page_exceptions()


@app.get('/projects/')
def projects_index():
    return _gate() or (_books().page_portfolio() if mode() == 'books' else page_portfolio())


@app.get('/projects/<pid>/')
def project_one(pid):
    g = _gate()
    if g:
        return g
    pid = pid.strip().upper()[:16]
    if pid == 'DEMO':
        return page_portfolio()
    html_ = _books().page_project(pid) if mode() == 'books' else page_project(pid)
    if html_ is None:
        return PC.page('Not found', f'<p class="muted" style="margin:40px 0">{t("No such project.", "No existe ese proyecto.")}</p>', 'projects'), 404
    return html_


# the v244 demo world stays reachable (its own entity, DEMO-MX rows only)
@app.get('/finance/demo/')
def finance_demo():
    return _gate() or page_today()


@app.get('/finance/demo/<kind>/')
def finance_demo_kind(kind):
    g = _gate()
    if g:
        return g
    return {'ar': lambda: _ledger_page('ar'), 'ap': lambda: _ledger_page('ap'), 'bank': page_bank, 'exceptions': page_exceptions}.get(kind, page_today)()


@app.get('/projects/demo/<pid>/')
def project_demo(pid):
    g = _gate()
    if g:
        return g
    html_ = page_project(pid.strip().upper()[:16])
    return html_ if html_ is not None else (PC.page('Not found', '', 'projects'), 404)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('ARGIA_FIN_PORT', '8515')), debug=False)
