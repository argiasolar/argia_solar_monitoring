"""v245 — the /finance/ and /projects/ pages on the REAL books (entity
ARGIA-MX): what ``scripts/fin_drive_ingest.py`` loaded from the Drive
folders — the CONTPAQi prints, the accountants' workbook, the projects
overview, the AR/AP tracker and the PMO project sheets.

Bound by fin_app (``bind(rows, money, today, entity)``) so the same fake
row source the tests use for the demo pages answers these queries by
name. Every string goes through ``t(en, es)``.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal
from typing import Callable, Dict, List, Optional

import portal_chrome as PC
from argia.fin.money import D

t, ti, tile, pill = PC.t, PC.ti, PC.tile, PC.pill

_rows: Callable = None
_money: Callable = None
_today: Callable = None
ENTITY = 'ARGIA-MX'


def bind(rows, money, today, entity):
    global _rows, _money, _today, ENTITY
    _rows, _money, _today, ENTITY = rows, money, today, entity


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _n(v) -> Decimal:
    try:
        return D(v if v not in (None, '') else 0)
    except Exception:            # noqa: BLE001
        return D(0)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ''))


def _table(head: List[str], body: List[str], cls='', cols=None) -> str:
    """v246: every table gets search, filters, sorting and column tooltips (portal_chrome.data_table)."""
    return PC.data_table(head, body, cls, cols)


# --------------------------------------------------- column explanations (v246)
# One entry per column, (EN, ES); shown as the ⓘ tooltip on the header and in the
# "Columns explained" block under the table. Written for Tania and the accountants.
def COLS(*keys):
    return [_COL.get(k) for k in keys]


_COL = {
    'project': ("Business case name and its 4-digit code. The code is the CONTPAQi segment: every journal line the accountants book for this project carries it, so it is the join between the books, the projects overview and the ARGnnnn PMO folder.",
                "Nombre del caso de negocio y su código de 4 dígitos. El código es el segmento CONTPAQi: cada línea de póliza que contabilidad registra para este proyecto lo lleva, así que es la unión entre libros, el overview de proyectos y la carpeta PMO ARGnnnn."),
    'phase': ("Phase from the projects overview: 0 closing (deal pending), 1 specification, 2 preparation (orders placed), 3 execution (installing), 4 finalization (>95 %), 5 review (handover), 6 done, 7 warranty claim, 8 on hold.",
              "Fase del overview de proyectos: 0 cierre (trato pendiente), 1 especificación, 2 preparación (pedidos colocados), 3 ejecución (instalando), 4 finalización (>95 %), 5 revisión (entrega), 6 terminado, 7 garantía, 8 en pausa."),
    'status': ("Traffic light the PM keeps in the overview: ok / warning / critical.", "Semáforo que el PM mantiene en el overview: ok / warning / critical."),
    'pm': ("Project manager named in the projects overview (or in the PMO sheet when the overview has none).", "Project manager nombrado en el overview de proyectos (o en la hoja PMO cuando el overview no lo tiene)."),
    'contract': ("Contract start → contract end as written in the projects overview. A project still in phases 0-3 after its contract end is flagged 'past contract end'.",
                 "Inicio → fin de contrato como está en el overview de proyectos. Un proyecto aún en fases 0-3 después del fin de contrato se marca 'pasó el fin de contrato'."),
    'value': ("Contract value in MXN from the projects overview (net of IVA). USD deals are shown at the overview's own conversion.",
              "Valor del contrato en MXN del overview de proyectos (sin IVA). Los tratos en USD se muestran a la conversión propia del overview."),
    'planned_cost': ("Planned cost in MXN from the projects overview and, in brackets, the planned margin it implies ((value − cost) / value).",
                     "Costo planeado en MXN del overview de proyectos y, entre paréntesis, el margen planeado que implica ((valor − costo) / valor)."),
    'rev_ytd': ("Revenue BOOKED in 2026 on this project: the sum of revenue-account lines (401-xx) carrying the project's segment, from the accountants' GM per project. Not invoicing — booking. Blank means nothing booked yet.",
                "Ingresos CONTABILIZADOS en 2026 en este proyecto: la suma de líneas de cuentas de ingreso (401-xx) con el segmento del proyecto, del GM por proyecto de contabilidad. No es facturación, es registro contable. Vacío = nada registrado aún."),
    'cos_ytd': ("Cost of sales BOOKED in 2026 on this project (501-xx lines with the segment), shown positive. A negative value is a cost reversal.",
                "Costo de ventas CONTABILIZADO en 2026 en este proyecto (líneas 501-xx con el segmento), mostrado en positivo. Un valor negativo es una reversión de costo."),
    'gm_ytd': ("Booked revenue minus booked cost for 2026, with the margin rate. Compare with the planned margin: a big gap means costs booked ahead of revenue (or the reverse), not necessarily a problem until the project closes.",
               "Ingresos menos costo contabilizados en 2026, con la tasa de margen. Compárese con el margen planeado: una brecha grande significa costos registrados antes que ingresos (o al revés), no necesariamente un problema hasta que el proyecto cierre."),
    'invoiced': ("Amount invoiced to the customer (MXN) as kept in the projects overview; '/ paid' = collected so far.", "Monto facturado al cliente (MXN) según el overview de proyectos; '/ cobrado' = cobrado hasta hoy."),
    'progress': ("Installation progress: the higher of the PM's estimate in the overview and the PMO sheet's logged task progress.", "Avance de instalación: el mayor entre la estimación del PM en el overview y el avance de tareas registrado en la hoja PMO."),
    'pmo': ("'sheet' = the project's ARGIA PROJECT workbook is found under PROJECT MANAGEMENT and read (tasks, milestones, costs); '—' = no sheet yet.", "'sheet' = la hoja ARGIA PROJECT del proyecto existe en PROJECT MANAGEMENT y se lee (tareas, hitos, costos); '—' = aún sin hoja."),
    'src_kind': ("Which reader loaded the file: polizas / auxiliares (CONTPAQi prints), acctbook (the accountants' workbook), overview (projects overview), tracker (AR/AP open items), pmo_sheet (a project workbook).",
                 "Qué lector cargó el archivo: polizas / auxiliares (impresiones CONTPAQi), acctbook (libro de contabilidad), overview (overview de proyectos), tracker (partidas abiertas), pmo_sheet (hoja de proyecto)."),
    'src_file': ("File name exactly as it is in Google Drive.", "Nombre del archivo tal cual está en Google Drive."),
    'src_period': ("The month the file reports through (books close ~3 weeks after month end).", "El mes hasta el que reporta el archivo (los libros cierran ~3 semanas después del fin de mes)."),
    'src_modified': ("Last modification in Drive.", "Última modificación en Drive."),
    'src_imported': ("When the portal read it. Files are identified by content hash: an unchanged file is never imported twice.", "Cuándo lo leyó el portal. Los archivos se identifican por hash de contenido: un archivo sin cambios nunca se importa dos veces."),
    'src_rows': ("Rows taken from the file (journal lines, movements, accounts, projects, items…).", "Filas tomadas del archivo (líneas de póliza, movimientos, cuentas, proyectos, partidas…)."),
    'oi_status': ("From the tracker: Delay = past its final due date; On time = not yet due; Paid = payment confirmed (date in 'Paid').", "Del seguimiento: Delay = pasó su vencimiento final; On time = aún no vence; Paid = pago confirmado (fecha en 'Pagada')."),
    'oi_invoice': ("Invoice number (ours for receivables, the supplier's for payables); 'Permanent' = a recurring charge such as leasing.", "Número de factura (nuestro en cobrar, del proveedor en pagar); 'Permanent' = cargo recurrente como arrendamiento."),
    'oi_customer': ("Customer as named in the tracker.", "Cliente como aparece en el seguimiento."),
    'oi_supplier': ("Supplier as named in the tracker.", "Proveedor como aparece en el seguimiento."),
    'oi_project': ("Business case the invoice belongs to (links to the project page); 701 = operation costs.", "Caso de negocio al que pertenece la factura (enlaza a la página del proyecto); 701 = costos de operación."),
    'oi_po': ("Customer PO or reference the invoice was issued against.", "OC del cliente o referencia contra la que se emitió la factura."),
    'oi_issued': ("Invoice date.", "Fecha de la factura."),
    'oi_due': ("Final due date: the renegotiated date when there is one, otherwise the original payment term.", "Vencimiento final: la fecha renegociada cuando existe, si no el plazo original."),
    'oi_days': ("Days to the final due date as of today; negative = days overdue. Blank once paid.", "Días al vencimiento final a hoy; negativo = días de atraso. Vacío una vez pagada."),
    'oi_total': ("Invoice total including IVA, in the invoice currency.", "Total de la factura con IVA, en la moneda de la factura."),
    'oi_net': ("Amount before IVA.", "Importe antes de IVA."),
    'oi_paid': ("Date the payment was confirmed in the tracker.", "Fecha en que se confirmó el pago en el seguimiento."),
    'oi_folio': ("First 8 characters of the CFDI folio fiscal (UUID) — enough to find it in SAT / Savio.", "Primeros 8 caracteres del folio fiscal CFDI (UUID) — suficiente para ubicarla en SAT / Savio."),
    'oi_comment': ("The tracker's own comment (milestone, partial, dispute…).", "Comentario propio del seguimiento (hito, parcial, disputa…)."),
    'bk_account': ("CONTPAQi sub-account: 105-01-xxx one per customer, 201-01-xxx one per supplier.", "Subcuenta CONTPAQi: 105-01-xxx una por cliente, 201-01-xxx una por proveedor."),
    'bk_party': ("Counterparty name as the accountants keep it; 'USD' / 'Compl' pairs are one dollar counterparty (face value + peso complement).", "Nombre de la contraparte como lo lleva contabilidad; los pares 'USD' / 'Compl' son una contraparte en dólares (valor nominal + complemento en pesos)."),
    'bk_balance': ("Balance at the last closed month from the auxiliares print (receivable: what they owe us; payable: what we owe them).", "Saldo al último mes cerrado según el auxiliar (cobrar: lo que nos deben; pagar: lo que debemos)."),
    'bl_date': ("Date of the póliza.", "Fecha de la póliza."),
    'bl_poliza': ("Póliza type and number (Ingresos = money in, Egresos = money out, Diario = other entries).", "Tipo y número de póliza (Ingresos = entradas, Egresos = salidas, Diario = otros asientos)."),
    'bl_concept': ("Concept the accountants wrote on the póliza.", "Concepto que contabilidad escribió en la póliza."),
    'bl_ref': ("Reference on the line: SPEI, invoice number, supplier…", "Referencia de la línea: SPEI, número de factura, proveedor…"),
    'bl_in': ("Debit to the bank account = money in.", "Cargo a la cuenta bancaria = entrada."),
    'bl_out': ("Credit to the bank account = money out.", "Abono a la cuenta bancaria = salida."),
    'bl_project': ("Business-case segment on the line, when the accountants assigned one.", "Segmento de caso de negocio en la línea, cuando contabilidad lo asignó."),
    'pl_line': ("Management report line as the accountants define it (sheet PL / BS of Argia_Accounting_Data). Bold rows are subtotals.", "Línea del reporte de gestión como la define contabilidad (hoja PL / BS de Argia_Accounting_Data). Las filas en negritas son subtotales."),
    'pl_month': ("Value for the month in thousands of MXN; costs negative.", "Valor del mes en miles de MXN; costos en negativo."),
    'pl_ytd': ("Sum January → last closed month (rates recomputed, not summed).", "Suma enero → último mes cerrado (las tasas se recalculan, no se suman)."),
    'pl_budget': ("2026 budget for the same months, sheet PL_Budget.", "Presupuesto 2026 para los mismos meses, hoja PL_Budget."),
    'pl_var': ("YTD actual minus budget: green favourable, red unfavourable (for costs a smaller negative number is favourable).", "Real YTD menos presupuesto: verde favorable, rojo desfavorable (en costos, un negativo menor es favorable)."),
    'ms_id': ("Task id in the PMO sheet (Mnnn = milestone).", "Id de tarea en la hoja PMO (Mnnn = hito)."),
    'ms_name': ("Milestone as the PM named it.", "Hito como lo nombró el PM."),
    'ms_date': ("Planned end date of the milestone.", "Fecha planeada de fin del hito."),
    'ms_status': ("Status the PM keeps in the sheet.", "Estatus que el PM mantiene en la hoja."),
    'ph_wbs': ("Work-breakdown number (phase = whole number).", "Número de la EDT (fase = número entero)."),
    'ph_name': ("Phase name.", "Nombre de la fase."),
    'ph_dates': ("Planned start → end.", "Inicio → fin planeados."),
    'ph_res': ("Resource assigned (installer, ARGIA team).", "Recurso asignado (instalador, equipo ARGIA)."),
    'c_id': ("Cost line id in the PMO sheet.", "Id de la línea de costo en la hoja PMO."),
    'c_date': ("Date of the cost (order or invoice).", "Fecha del costo (pedido o factura)."),
    'c_cat': ("Cost category (panels, inverters, structure, installation…).", "Categoría de costo (paneles, inversores, estructura, instalación…)."),
    'c_vendor': ("Supplier.", "Proveedor."),
    'c_desc': ("Description the PM wrote.", "Descripción que escribió el PM."),
    'c_net': ("Amount before IVA.", "Importe antes de IVA."),
    'c_total': ("Amount including IVA.", "Importe con IVA."),
    'c_status': ("Planned (not ordered) → Committed (ordered) → Incurred (received/invoiced) → Paid.", "Planned (sin ordenar) → Committed (ordenado) → Incurred (recibido/facturado) → Paid (pagado)."),
    'c_appr': ("Approval status and who approved.", "Estatus de aprobación y quién aprobó."),
    'iv_id': ("Invoice line id in the PMO sheet.", "Id de la factura en la hoja PMO."),
    'iv_ms': ("Billing milestone (engineering, materials, installation…).", "Hito de facturación (ingeniería, materiales, instalación…)."),
    'iv_no': ("Our invoice number.", "Nuestro número de factura."),
    'iv_date': ("Invoice date.", "Fecha de la factura."),
    'iv_due': ("Due date.", "Fecha de vencimiento."),
    'iv_total': ("Amount including IVA.", "Importe con IVA."),
    'iv_status': ("Invoice status / payment status as the PM keeps them.", "Estatus de factura / de pago como los lleva el PM."),
    'gl_bspl': ("BS = balance-sheet account (asset/liability), PL = profit-and-loss account.", "BS = cuenta de balance (activo/pasivo), PL = cuenta de resultados."),
    'gl_account': ("CONTPAQi account the line was booked to.", "Cuenta CONTPAQi donde se registró la línea."),
    'gl_report': ("Management report line the account maps to (Revenues, Cost of Sales, Travel…).", "Línea del reporte de gestión a la que mapea la cuenta (Ingresos, Costo de ventas, Viáticos…)."),
    'gl_debit': ("Sum of debits (cargos) on this account with the project's segment, 2026 year to date.", "Suma de cargos en esta cuenta con el segmento del proyecto, 2026 acumulado."),
    'gl_credit': ("Sum of credits (abonos).", "Suma de abonos."),
    'gl_net': ("Debits minus credits: positive = cost/asset, negative = revenue/liability.", "Cargos menos abonos: positivo = costo/activo, negativo = ingreso/pasivo."),
    'gl_n': ("Number of journal lines.", "Número de líneas de póliza."),
    'gl_dates': ("First → last póliza date.", "Primera → última fecha de póliza."),
    'it_side': ("AR = we invoice (receivable), AP = we owe (payable).", "AR = nosotros facturamos (cobrar), AP = debemos (pagar)."),
    'it_company': ("Customer or supplier.", "Cliente o proveedor."),
}


# ------------------------------------------------------------------ queries
def q_sources():
    return _rows('sources', f"SELECT kind, name, period, to_char(modified, 'YYYY-MM-DD') AS modified, to_char(imported_at, 'YYYY-MM-DD HH24:MI') AS imported,"
                            f" rows, notes FROM fin_source_file WHERE entity_id = {_q(ENTITY)} ORDER BY kind, modified DESC, imported_at DESC;")


def q_period():
    r = _rows('period', f"SELECT max(period) AS period FROM gl_balance WHERE entity_id = {_q(ENTITY)};")
    return (r[0].get('period') or '') if r else ''


def q_cash(period: str):
    return _rows('cash', f"SELECT account, name, closing, movements FROM gl_balance WHERE entity_id = {_q(ENTITY)} AND period = {_q(period)}"
                         f" AND account LIKE '102-01-%' AND (closing <> 0 OR movements > 0) ORDER BY account;")


def q_book_ar(period: str):
    return _rows('book_ar', f"SELECT account, name, closing FROM gl_balance WHERE entity_id = {_q(ENTITY)} AND period = {_q(period)}"
                            f" AND account LIKE '105-01-%' AND closing <> 0 ORDER BY closing DESC;")


def q_book_ap(period: str):
    return _rows('book_ap', f"SELECT account, name, closing FROM gl_balance WHERE entity_id = {_q(ENTITY)} AND period = {_q(period)}"
                            f" AND account LIKE '201-01-%' AND closing <> 0 ORDER BY closing DESC;")


def q_open_items(side: str):
    return _rows(f'open_{side}', f"SELECT status, invoice, company, project_code, project_name, po, issued, due, final_due, days_to_due, currency,"
                                 f" total, net, mxn_equiv_net, folio_fiscal, paid_on, kind, comment FROM open_item WHERE entity_id = {_q(ENTITY)}"
                                 f" AND side = {_q(side)} ORDER BY (paid_on IS NOT NULL), coalesce(final_due, due), company;")


def q_report(period: str, sheet: str):
    return _rows(f'report_{sheet}', f"SELECT code, label, ord, m01, m02, m03, m04, m05, m06, m07, m08, m09, m10, m11, m12, ytd FROM fin_report_line"
                                    f" WHERE entity_id = {_q(ENTITY)} AND period = {_q(period)} AND sheet = {_q(sheet)} ORDER BY ord;")


def q_portfolio():
    return _rows('portfolio', f"SELECT p.code, p.project_id, p.name, p.phase, p.status, p.business_manager, p.project_manager, p.value_usd, p.value_mxn,"
                              f" p.planned_cost_mxn, p.margin_planned_pct, p.contract_start, p.contract_end, p.progress, p.invoiced_mxn, p.paid_mxn, p.comment,"
                              f" m.revenue_ytd, m.cos_ytd, m.revenue_total, m.cos_total, m.gm, m.planned_value, m.planned_cost, m.planned_margin,"
                              f" s.project_id AS pmo_id, s.phase AS pmo_phase, s.status AS pmo_status, s.progress AS pmo_progress, s.manager AS pmo_manager"
                              f" FROM portfolio_project p"
                              f" LEFT JOIN project_margin m ON m.entity_id = p.entity_id AND m.code = p.code AND m.period = (SELECT max(period) FROM project_margin x WHERE x.entity_id = p.entity_id)"
                              f" LEFT JOIN pmo_project s ON s.entity_id = p.entity_id AND s.code = p.code"
                              f" WHERE p.entity_id = {_q(ENTITY)} ORDER BY p.phase, p.code DESC;")


def q_project(code: int):
    r = [x for x in q_portfolio() if str(x.get('code')) == str(code)]
    return r[0] if r else None


def q_project_gl(code: int):
    return _rows('project_gl', f"SELECT l.account, coalesce(a.report_account, l.account_name) AS bucket, a.bs_pl, a.a_p,"
                               f" sum(l.debit) AS debit, sum(l.credit) AS credit, count(*) AS n, min(j.jdate) AS first, max(j.jdate) AS last"
                               f" FROM gl_line l JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                               f" LEFT JOIN gl_account a ON a.entity_id = l.entity_id AND a.account = l.account"
                               f" WHERE l.entity_id = {_q(ENTITY)} AND l.segment = {int(code)} GROUP BY 1, 2, 3, 4 ORDER BY a.bs_pl, l.account;")


def q_project_items(code: int):
    return _rows('project_items', f"SELECT side, status, invoice, company, issued, due, final_due, days_to_due, currency, total, net, paid_on, kind FROM open_item"
                                  f" WHERE entity_id = {_q(ENTITY)} AND project_code = {int(code)} ORDER BY side, (paid_on IS NOT NULL), coalesce(final_due, due);")


def q_pmo_tasks(pid: str):
    return _rows('pmo_tasks', f"SELECT wbs, task_id, name, is_phase, is_milestone, resource, start_date, end_date, duration_days, status, progress"
                              f" FROM pmo_task WHERE project_id = {_q(pid)} ORDER BY string_to_array(wbs, '.')::int[];")


def q_pmo_costs(pid: str):
    return _rows('pmo_costs', f"SELECT cost_id, cost_date, category, vendor, description, net, vat, total, cost_status, approval, approved_by, paid, payment_status"
                              f" FROM pmo_cost WHERE project_id = {_q(pid)} ORDER BY cost_date, cost_id;")


def q_pmo_invoices(pid: str):
    return _rows('pmo_invoices', f"SELECT invoice_id, customer, milestone, number, inv_date, due, net, vat, total, status, payment_status, paid_on, received"
                                 f" FROM pmo_invoice WHERE project_id = {_q(pid)} ORDER BY inv_date, invoice_id;")


def q_bank_lines(account: str, since: dt.date):
    return _rows('bank_lines', f"SELECT j.jdate, j.kind, j.number, j.concept, l.reference, l.debit, l.credit, l.segment FROM gl_line l"
                               f" JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                               f" WHERE l.entity_id = {_q(ENTITY)} AND l.account = {_q(account)} AND j.jdate >= {_q(since.isoformat())}"
                               f" ORDER BY j.jdate DESC, j.kind, j.number, l.line_no LIMIT 400;")


# ------------------------------------------------------------- derivations
def cash_view(cash_rows: List[dict]) -> List[dict]:
    """Bank sub-accounts → one line per real bank account. CONTPAQi keeps a
    dollar account as two sub-accounts: '… DLLS' holds the USD figure at
    face value and '… Compl' the peso complement of its valuation; their
    sum is the MXN value, the DLLS line alone is the USD balance. A
    'Compl' account pairs with the base whose name it repeats, else with
    the previous dollar account in account order ('Banbajio DLLS 402' +
    'Banbajio DLLS Compl' are 102-01-008 / -009 in the real chart)."""
    out: List[dict] = []
    by_name: Dict[str, dict] = {}
    last_usd: Optional[dict] = None
    for r in sorted(cash_rows, key=lambda x: x['account']):
        name = r['name']
        is_compl = bool(re.search(r'\bCompl(emento)?\b', name, re.I))
        base = re.sub(r'\s+Compl(emento)?$', '', name, flags=re.I).strip()
        usd = bool(re.search(r'\b(DLLS|USD)\b', base, re.I))
        c = _n(r['closing'])
        v = None
        if is_compl:
            v = by_name.get(base)
            if v is None and last_usd is not None and last_usd['name'].split()[0].lower() == base.split()[0].lower():
                v = last_usd
        if v is None:
            v = by_name.get(base)
        if v is None:
            v = {'name': base, 'usd': usd, 'usd_amount': D(0), 'mxn': D(0), 'accounts': [], 'movements': 0}
            by_name[base] = v
            out.append(v)
        v['accounts'].append(r['account'])
        v['movements'] += int(r.get('movements') or 0)
        v['mxn'] += c
        if v['usd'] and not is_compl:
            v['usd_amount'] += c
        if usd and not is_compl:
            last_usd = v
    live = [v for v in out if v['mxn'] != 0 or v['usd_amount'] != 0]
    return sorted(live, key=lambda v: (v['usd'], v['name']))


def norm_code(code: str) -> str:
    """Subtotal lines are keyed by label and the budget sheet spells them a
    little differently ('LAAS EBITDA' vs 'LAAS and PPA  EBITDA'): compare
    without 'and PPA', case and spacing."""
    if not code.startswith('label:'):
        return code
    return 'label:' + re.sub(r'\s+', ' ', code[6:].lower().replace('and ppa', '').replace('incl.', '')).strip()


def report_index(lines: List[dict]) -> Dict[str, dict]:
    return {norm_code(l['code']): l for l in lines}


def rline(idx: Dict[str, dict], code: str) -> Optional[dict]:
    return idx.get(norm_code(code))


def ytd(line: Optional[dict], month: int) -> Decimal:
    if not line:
        return D(0)
    return sum((_n(line.get(f'm{i:02d}')) for i in range(1, month + 1)), D(0))


def open_summary(items: List[dict], today: dt.date) -> Dict[str, dict]:
    """Per currency: open total, overdue total, count, oldest days overdue."""
    out: Dict[str, dict] = {}
    for it in items:
        if it.get('paid_on'):
            continue
        c = it.get('currency') or 'MXN'
        s = out.setdefault(c, {'open': D(0), 'overdue': D(0), 'n': 0, 'n_over': 0, 'max_days': 0})
        amt = _n(it.get('total'))
        s['open'] += amt
        s['n'] += 1
        due = it.get('final_due') or it.get('due')
        try:
            dd = dt.date.fromisoformat(str(due)[:10]) if due else None
        except ValueError:
            dd = None
        days = (today - dd).days if dd else 0
        if days > 0:
            s['overdue'] += amt
            s['n_over'] += 1
            s['max_days'] = max(s['max_days'], days)
    return out


def phase_pill(phase: str) -> str:
    n = (phase or '')[:1]
    cls = {'0': 'off', '1': 'off', '2': 'warn', '3': 'ok', '4': 'ok', '5': 'ok', '6': 'ok', '7': 'crit', '8': 'crit'}.get(n, 'off')
    label = re.sub(r'^\d_', '', phase or '').replace('_', ' ')
    return pill(cls, label or '—')


# ------------------------------------------------------------------ pages
def _kicker(period: str, extra: str = '') -> str:
    per = f"{t('books through', 'libros al')} {period}" if period else t('no books loaded yet', 'aún sin libros')
    return f'<div class="kicker">{t("Finance", "Finanzas")} · {_today().isoformat()} · <span class="mono">{_e(ENTITY)}</span> · {per}{extra}</div>'


def page_today() -> str:
    period = q_period()
    month = int(period[5:7]) if period else 0
    cash = cash_view(q_cash(period)) if period else []
    ar_items, ap_items = q_open_items('ar'), q_open_items('ap')
    pl = report_index(q_report(period, 'pl')) if period else {}
    bud = report_index(q_report(period, 'budget')) if period else {}
    tiles = ''
    total_mxn = sum((v['mxn'] for v in cash), D(0))
    for v in cash:
        if v['usd']:
            tiles += tile(v['name'], v['name'], _money(v['usd_amount'], 'USD'), f"≈ {v['mxn']:,.0f} MXN in the books · {v['movements']} movements YTD",
                          f"≈ {v['mxn']:,.0f} MXN en libros · {v['movements']} movimientos YTD", tone='good' if v['usd_amount'] > 0 else '')
        else:
            tiles += tile(v['name'], v['name'], _money(v['mxn'], 'MXN'), f"{v['movements']} movements YTD", f"{v['movements']} movimientos YTD",
                          tone='good' if v['mxn'] > 0 else ('bad' if v['mxn'] < 0 else ''), why_en=('overdrawn in the books' if v['mxn'] < 0 else ''),
                          why_es=('sobregirada en libros' if v['mxn'] < 0 else ''))
    tiles = tile('Cash in the books', 'Efectivo en libros', _money(total_mxn), f"all bank accounts at {period} month end, MXN valuation",
                 f"todas las cuentas bancarias al cierre de {period}, valuación en MXN", tone='good' if total_mxn > 0 else 'bad') + tiles
    tod = _today()
    for side, items, en, es in (('ar', ar_items, 'Receivables (tracker)', 'Por cobrar (seguimiento)'), ('ap', ap_items, 'Payables (tracker)', 'Por pagar (seguimiento)')):
        summ = open_summary(items, tod)
        if not summ:
            tiles += tile(en, es, '—', 'no open items', 'sin partidas abiertas')
        for ccy, s in sorted(summ.items()):
            tone = 'bad' if s['max_days'] > 60 else 'warn' if s['n_over'] else 'good'
            tiles += tile(f'{en} · {ccy}', f'{es} · {ccy}', _money(s['open'], ccy), f"{s['n']} open · {s['n_over']} overdue · {s['overdue']:,.0f} {ccy} past due",
                          f"{s['n']} abiertas · {s['n_over']} vencidas · {s['overdue']:,.0f} {ccy} vencidos", tone=tone,
                          why_en=(f"oldest item {s['max_days']} days past due" if s['n_over'] else ''), why_es=(f"la partida más antigua lleva {s['max_days']} días vencida" if s['n_over'] else ''))
    if period:
        rev, rev_b = ytd(rline(pl, 'PL_040'), month), ytd(rline(bud, 'PL_040'), month)
        gm, gm_b = ytd(rline(pl, 'label:Gross Margin'), month), ytd(rline(bud, 'label:Gross Margin'), month)
        ebitda = ytd(rline(pl, 'label:EBITDA incl. LAAS'), month)
        net, net_b = ytd(rline(pl, 'label:NET Income'), month), ytd(rline(bud, 'label:NET Income'), month)
        ppa = ytd(rline(pl, 'PL_010'), month)
        var = (rev - rev_b) / rev_b * 100 if rev_b else D(0)
        tiles += tile('Revenue YTD', 'Ingresos YTD', _money(rev * 1000), f"budget {rev_b:,.0f}k · {var:+.0f}% · plus LAAS/PPA {ppa:,.0f}k",
                      f"presupuesto {rev_b:,.0f}k · {var:+.0f}% · más LAAS/PPA {ppa:,.0f}k", tone=('bad' if var < -15 else 'warn' if var < 0 else 'good'),
                      why_en=(f"core revenue {abs(var):.0f}% below the 2026 plan" if var < 0 else ''), why_es=(f"ingresos {abs(var):.0f}% por debajo del plan 2026" if var < 0 else ''))
        gm_pct = gm / rev * 100 if rev else D(0)
        gm_pct_b = gm_b / rev_b * 100 if rev_b else D(0)
        tiles += tile('Gross margin YTD', 'Margen bruto YTD', f"{gm_pct:.1f} <span class=\"unit\">%</span>", f"{gm:,.0f}k MXN · plan {gm_pct_b:.1f}% ({gm_b:,.0f}k)",
                      f"{gm:,.0f}k MXN · plan {gm_pct_b:.1f}% ({gm_b:,.0f}k)", tone=('good' if gm_pct >= gm_pct_b else 'warn'),
                      why_en=('margin below the planned rate' if gm_pct < gm_pct_b else ''), why_es=('margen por debajo de la tasa planeada' if gm_pct < gm_pct_b else ''))
        tiles += tile('EBITDA YTD (incl. LAAS)', 'EBITDA YTD (incl. LAAS)', _money(ebitda * 1000), 'after management budget', 'después del presupuesto de dirección',
                      tone=('good' if ebitda > 0 else 'bad'), why_en=('negative EBITDA year to date' if ebitda <= 0 else ''), why_es=('EBITDA negativo en el año' if ebitda <= 0 else ''))
        tiles += tile('Net income YTD', 'Resultado neto YTD', _money(net * 1000), f"budget {net_b:,.0f}k", f"presupuesto {net_b:,.0f}k",
                      tone=('good' if net >= net_b else 'warn'), why_en=('below the planned result' if net < net_b else ''), why_es=('por debajo del resultado planeado' if net < net_b else ''))
    # projects: active ones, booked vs planned
    rows_html = []
    for p in q_portfolio():
        if (p.get('phase') or '')[:1] not in '012345':
            continue
        rev_y, cos_y = _n(p.get('revenue_ytd')), _n(p.get('cos_ytd'))
        gm_y = rev_y + cos_y
        planned_m = _n(p.get('planned_cost_mxn'))
        val = _n(p.get('value_mxn'))
        pmargin = (val - planned_m) / val * 100 if val and planned_m else None
        prog = max(_n(p.get('pmo_progress')), _n(p.get('progress')))   # the PM's estimate (overview) until the sheet logs progress
        rows_html.append(f'<tr><td style="min-width:240px"><a href="/projects/{_e(p["project_id"])}/"><b>{_e(p["name"][:60])}</b></a> <span class="mono muted">{_e(p["code"])}</span></td>'
                         f'<td>{phase_pill(p["phase"])}</td><td>{_e(p.get("project_manager") or p.get("pmo_manager") or "—")}</td>'
                         f'<td class="r">{_money(val)}</td><td class="r">{_money(planned_m)} <span class="muted">{f"{pmargin:.0f}%" if pmargin is not None else ""}</span></td>'
                         f'<td class="r">{_money(rev_y)}</td><td class="r">{_money(-cos_y)}</td><td class="r"><b>{_money(gm_y)}</b> <span class="muted">{f"{gm_y / rev_y * 100:.0f}%" if rev_y else ""}</span></td>'
                         f'<td class="r">{_money(_n(p.get("invoiced_mxn")))}</td><td class="r">{f"{prog * 100:.0f}%" if prog else "—"}</td>'
                         f'<td>{pill("ok", "sheet") if p.get("pmo_id") else pill("off", "—")}</td></tr>')
    src = q_sources()
    src_html = ''.join(f'<tr><td>{_e(s["kind"])}</td><td>{_e(s["name"])}</td><td>{_e(s.get("period") or "")}</td><td>{_e(s.get("modified") or "")}</td><td>{_e(s.get("imported") or "")}</td><td class="r">{_e(s.get("rows"))}</td></tr>' for s in src)
    body = f'''
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
 <div style="display:flex;flex-direction:column;gap:4px">{_kicker(period)}<h1 class="pt">{t("Where the money is", "Dónde está el dinero")}</h1></div>
 {PC.print_button()}
</div>
<div class="tiles" style="margin-top:20px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Active projects — planned vs booked", "Proyectos activos — plan vs libros")}</h2><span class="muted" style="font-size:12.5px">{t("value and planned cost from the projects overview · booked revenue / cost / margin YTD from the accountants' GM per project (CONTPAQi segments)", "valor y costo planeado del overview de proyectos · ingresos / costo / margen contabilizados YTD del GM por proyecto de contabilidad (segmentos CONTPAQi)")}</span></div>
 {_table([t("Project", "Proyecto"), t("Phase", "Fase"), "PM", t("Value", "Valor"), t("Planned cost", "Costo planeado"), t("Booked revenue YTD", "Ingresos YTD"), t("Booked cost YTD", "Costo YTD"), t("Booked margin YTD", "Margen YTD"), t("Invoiced", "Facturado"), t("Progress", "Avance"), "PMO"], rows_html, cols=COLS('project', 'phase', 'pm', 'value', 'planned_cost', 'rev_ytd', 'cos_ytd', 'gm_ytd', 'invoiced', 'progress', 'pmo'))}
</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Data sources", "Fuentes de datos")}</h2><span class="muted" style="font-size:12.5px">{t("read from Google Drive by content hash — an unchanged file is never re-imported", "leídas de Google Drive por hash de contenido — un archivo sin cambios nunca se reimporta")}</span></div>
 {_table([t("Kind", "Tipo"), t("File", "Archivo"), t("Period", "Periodo"), t("Modified", "Modificado"), t("Imported", "Importado"), t("Rows", "Filas")], [src_html] if src_html else [], cols=COLS('src_kind', 'src_file', 'src_period', 'src_modified', 'src_imported', 'src_rows'))}
</div>
<div class="card" style="margin-top:16px;padding:16px 20px">
 <h2 class="ct">{t("How the numbers are calculated", "Cómo se calculan los números")}</h2>
 <p class="note">{t("Cash = closing balance of every bank sub-account in the CONTPAQi auxiliares at the last closed month (a dollar account is its USD sub-account; its peso value adds the complement). Receivables / payables = the accountants' open-item tracker (every open customer and supplier invoice with its due date), aged from the final due date; the books' balances per counterparty at month end are on the AR and AP pages. P&L = the accountants' management P&L in thousands of MXN, year to date, against the 2026 budget on the same lines. Project figures: value and planned cost from the projects overview; booked revenue, cost and margin from GM per project, i.e. the journal lines carrying the project's segment. Nothing here is typed by hand: every number traces to a file in the Data sources table.",
 "Efectivo = saldo final de cada subcuenta bancaria en los auxiliares CONTPAQi al último mes cerrado (una cuenta en dólares es su subcuenta USD; su valor en pesos suma el complemento). Por cobrar / por pagar = el seguimiento de partidas abiertas de contabilidad (cada factura de cliente y proveedor abierta con su vencimiento), envejecidas desde el vencimiento final; los saldos de libros por contraparte al cierre están en las páginas de cobrar y pagar. Resultados = el estado de resultados de gestión de contabilidad en miles de MXN, acumulado del año, contra el presupuesto 2026 en las mismas líneas. Cifras de proyecto: valor y costo planeado del overview de proyectos; ingresos, costo y margen contabilizados del GM por proyecto, es decir las líneas de póliza con el segmento del proyecto. Nada aquí se captura a mano: cada número lleva a un archivo de la tabla de fuentes.")}</p>
</div>'''
    return PC.page('Finance', body, 'finance', '', wide=True)


def page_ledger(side: str) -> str:
    period = q_period()
    items = q_open_items(side)
    tod = _today()
    summ = open_summary(items, tod)
    tiles = ''
    en, es = ('Receivables', 'Por cobrar') if side == 'ar' else ('Payables', 'Por pagar')
    for ccy, s in sorted(summ.items()):
        tone = 'bad' if s['max_days'] > 60 else 'warn' if s['n_over'] else 'good'
        tiles += tile(f'{en} · {ccy}', f'{es} · {ccy}', _money(s['open'], ccy), f"{s['n']} open · {s['n_over']} overdue · {s['overdue']:,.0f} {ccy} past due",
                      f"{s['n']} abiertas · {s['n_over']} vencidas · {s['overdue']:,.0f} {ccy} vencidos", tone=tone,
                      why_en=(f"oldest item {s['max_days']} days past due" if s['n_over'] else ''), why_es=(f"la partida más antigua lleva {s['max_days']} días vencida" if s['n_over'] else ''))
    book = q_book_ar(period) if side == 'ar' else q_book_ap(period)
    book_total = sum((_n(b['closing']) for b in book), D(0))
    tiles += tile(f'{en} in the books', f'{es} en libros', _money(book_total), f"{len(book)} counterparties with a balance at {period}", f"{len(book)} contrapartes con saldo al {period}")
    trs = []
    for it in items:
        paid = bool(it.get('paid_on'))
        days = int(it.get('days_to_due') or 0)
        st = it.get('status') or ''
        cls = 'ok' if paid or st.lower() == 'paid' else 'crit' if st.lower() == 'delay' else 'warn' if st.lower() == 'on time' else 'off'
        proj = f'<a href="/projects/ARG{int(it["project_code"]):04d}/">{_e(it.get("project_name") or it["project_code"])}</a>' if it.get('project_code') else _e(it.get('project_name') or '—')
        trs.append(f'<tr><td>{pill(cls, st)}</td><td class="mono" style="font-size:12px">{_e(it.get("invoice"))}</td><td>{_e(it.get("company"))}</td><td>{proj}</td>'
                   f'<td>{_e(it.get("po"))}</td><td>{_e(it.get("issued") or "")}</td><td>{_e(it.get("final_due") or it.get("due") or "")}</td>'
                   f'<td class="r">{"" if paid else f"{days:+d}"}</td><td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td>'
                   f'<td class="r">{_money(it.get("net"), it.get("currency") or "MXN")}</td><td>{_e(it.get("paid_on") or "")}</td><td class="mono" style="font-size:11px">{_e((it.get("folio_fiscal") or "")[:8])}</td><td>{_e(it.get("comment") or "")}</td></tr>')
    brs = [f'<tr><td class="mono" style="font-size:12px">{_e(b["account"])}</td><td>{_e(b["name"])}</td><td class="r"><b>{_money(b["closing"])}</b></td></tr>' for b in book]
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t(en, es)}</h1></div>{PC.print_button()}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Open items (tracker)", "Partidas abiertas (seguimiento)")}</h2><span class="muted" style="font-size:12.5px">{t("days = to the final due date; negative = overdue", "días = al vencimiento final; negativo = vencida")}</span></div>
 {_table(["Status", t("Invoice", "Factura"), t("Customer", "Cliente") if side == "ar" else t("Supplier", "Proveedor"), t("Project", "Proyecto"), "PO", t("Issued", "Emitida"), t("Due", "Vence"), t("Days", "Días"), "Total", t("Net", "Neto"), t("Paid", "Pagada"), "Folio", t("Comment", "Comentario")], trs, cols=COLS('oi_status', 'oi_invoice', 'oi_customer' if side == 'ar' else 'oi_supplier', 'oi_project', 'oi_po', 'oi_issued', 'oi_due', 'oi_days', 'oi_total', 'oi_net', 'oi_paid', 'oi_folio', 'oi_comment'))}
</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Balances in the books", "Saldos en libros")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("CONTPAQi auxiliares, one sub-account per counterparty; USD accounts at face value", "auxiliares CONTPAQi, una subcuenta por contraparte; cuentas USD a valor nominal")}</span></div>
 {_table([t("Account", "Cuenta"), t("Counterparty", "Contraparte"), t("Balance", "Saldo")], brs, cols=COLS('bk_account', 'bk_party', 'bk_balance'))}
</div>'''
    return PC.page(en, body, 'finance', side, wide=True)


def page_bank() -> str:
    period = q_period()
    cash = cash_view(q_cash(period)) if period else []
    # the books stop at the period end — show the 45 days before it, not before today
    if period:
        y, m = int(period[:4]), int(period[5:7])
        pend = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))
    else:
        pend = _today()
    since = pend - dt.timedelta(days=45)
    tiles = ''.join(tile(v['name'], v['name'], _money(v['usd_amount'], 'USD') if v['usd'] else _money(v['mxn']),
                         f"{'≈ ' + format(v['mxn'], ',.0f') + ' MXN · ' if v['usd'] else ''}{', '.join(v['accounts'])}",
                         f"{'≈ ' + format(v['mxn'], ',.0f') + ' MXN · ' if v['usd'] else ''}{', '.join(v['accounts'])}") for v in cash)
    sections = ''
    for v in cash:
        for acct in v['accounts']:
            lines = q_bank_lines(acct, since)
            if not lines:
                continue
            trs = [f'<tr><td>{_e(l["jdate"])}</td><td>{_e(l["kind"])} {_e(l["number"])}</td><td>{_e(l["concept"])}</td><td class="mono" style="font-size:11px">{_e(l.get("reference") or "")}</td>'
                   f'<td class="r">{_money(l["debit"], "", 2) if _n(l["debit"]) else ""}</td><td class="r">{_money(l["credit"], "", 2) if _n(l["credit"]) else ""}</td><td>{_e(l.get("segment") or "")}</td></tr>' for l in lines]
            sections += f'''<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{_e(v["name"])} · <span class="mono">{_e(acct)}</span></h2><span class="muted" style="font-size:12.5px">{t("last 45 days of the books", "últimos 45 días de libros")} · {since.isoformat()} → {pend.isoformat()}</span></div>
{_table([t("Date", "Fecha"), t("Póliza", "Póliza"), t("Concept", "Concepto"), t("Reference", "Referencia"), t("In", "Entrada"), t("Out", "Salida"), t("Project", "Proyecto")], trs, cols=COLS('bl_date', 'bl_poliza', 'bl_concept', 'bl_ref', 'bl_in', 'bl_out', 'bl_project'))}</div>'''
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Bank", "Banco")}</h1></div>{PC.print_button()}</div>
<div class="tiles" style="margin-top:16px">{tiles or tile("Bank", "Banco", "—", "no books loaded", "sin libros")}</div>
{sections}
<p class="note">{t("These are the bank movements as booked by the accountants (CONTPAQi pólizas), not the bank's own statement. A statement-to-books reconciliation arrives when the bank exports are in the drop folder.", "Estos son los movimientos bancarios como los contabilizó contabilidad (pólizas CONTPAQi), no el estado de cuenta del banco. La conciliación estado de cuenta vs libros llega cuando los exports del banco estén en la carpeta.")}</p>'''
    return PC.page('Bank', body, 'finance', 'bank', wide=True)


def page_pl() -> str:
    period = q_period()
    month = int(period[5:7]) if period else 0
    pl = q_report(period, 'pl') if period else []
    pli = report_index(pl)
    bud = report_index(q_report(period, 'budget')) if period else {}
    bs = q_report(period, 'bs') if period else []
    mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    mon_es = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

    def fmt(v, pct=False):
        if pct:                      # a rate: keep the digits (D() rounds to cents)
            x = Decimal(str(v or 0))
            return f'{x * 100:.1f}%' if x else '<span class="muted">·</span>'
        x = _n(v)
        return f'{x:,.0f}' if x else '<span class="muted">·</span>'
    trs = []
    for l in pl:
        sub = l['code'].startswith('label:')
        pct = l['label'].strip().startswith('%')
        b = rline(bud, l['code'])
        style = ' style="font-weight:700;background:#f6f8fa"' if sub else ''
        cells = ''.join(f'<td class="r">{fmt(l.get(f"m{i:02d}"), pct)}</td>' for i in range(1, month + 1))
        if pct:      # a rate: YTD = recomputed from the lines above, not a sum of rates
            rev, gm = rline(pli, 'PL_040'), rline(pli, 'label:Gross Margin')
            y = (ytd(gm, month) / ytd(rev, month)) if rev and ytd(rev, month) else D(0)
            revb, gmb = rline(bud, 'PL_040'), rline(bud, 'label:Gross Margin')
            yb = (ytd(gmb, month) / ytd(revb, month)) if revb and ytd(revb, month) else D(0)
            trs.append(f'<tr{style}><td>{_e(l["label"])}</td>{cells}<td class="r"><b>{fmt(y, True)}</b></td><td class="r">{fmt(yb, True)}</td><td class="r"><span class="pill {"ok" if y >= yb else "crit"}">{(y - yb) * 100:+.1f} pt</span></td></tr>')
            continue
        y, yb = ytd(l, month), ytd(b, month)
        var = y - yb
        vcls = 'ok' if var >= 0 else 'crit'
        trs.append(f'<tr{style}><td>{_e(l["label"])}</td>{cells}<td class="r"><b>{fmt(y)}</b></td><td class="r">{fmt(yb)}</td><td class="r"><span class="pill {vcls}">{var:+,.0f}</span></td></tr>')
    heads = [t('Line (k MXN)', 'Línea (k MXN)')] + [t(mon[i], mon_es[i]) for i in range(month)] + ['YTD', t('Budget', 'Presupuesto'), t('Var', 'Var')]
    brs = []
    for l in bs:
        sub = l['code'].startswith('label:')
        style = ' style="font-weight:700;background:#f6f8fa"' if sub else ''
        cells = ''.join(f'<td class="r">{fmt(l.get(f"m{i:02d}"))}</td>' for i in range(1, month + 1))
        brs.append(f'<tr{style}><td>{_e(l["label"])}</td>{cells}</td></tr>')
    bheads = [t('Line (k MXN)', 'Línea (k MXN)')] + [t(mon[i], mon_es[i]) for i in range(month)]
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Profit & loss and balance sheet", "Resultados y balance")}</h1></div>{PC.print_button('Print / PDF for the accountants', 'Imprimir / PDF para contabilidad')}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Management P&L", "Estado de resultados de gestión")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("thousands of MXN, the accountants' sheet PL; budget = sheet PL_Budget (2026 plan)", "miles de MXN, hoja PL de contabilidad; presupuesto = hoja PL_Budget (plan 2026)")}</span></div>{_table(heads, trs, cols=[_COL['pl_line']] + [_COL['pl_month']] * month + [_COL['pl_ytd'], _COL['pl_budget'], _COL['pl_var']])}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Balance sheet", "Balance")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("thousands of MXN, month-end balances", "miles de MXN, saldos al cierre de mes")}</span></div>{_table(bheads, brs, cols=[_COL['pl_line']] + [_COL['pl_month']] * month)}</div>
<p class="note">{t("Sign convention as in the workbook: costs negative. The accountants close a month about three weeks after it ends; the portal shows the last closed month.", "Convención de signos como en el libro: costos en negativo. Contabilidad cierra un mes unas tres semanas después de terminado; el portal muestra el último mes cerrado.")}</p>'''
    return PC.page('P&L', body, 'finance', 'pl', wide=True)


def page_portfolio() -> str:
    period = q_period()
    allp = q_portfolio()
    active = [p for p in allp if (p.get('phase') or '')[:1] in '012345']
    hold = [p for p in allp if (p.get('phase') or '')[:1] in '78']
    val = sum((_n(p.get('value_mxn')) for p in active), D(0))
    booked = sum((_n(p.get('revenue_ytd')) for p in active), D(0))
    inv = sum((_n(p.get('invoiced_mxn')) for p in active), D(0))
    with_sheet = sum(1 for p in active if p.get('pmo_id'))
    tiles = (tile('Active projects', 'Proyectos activos', str(len(active)), f"{with_sheet} with a PMO sheet · {len(hold)} on hold / warranty", f"{with_sheet} con hoja PMO · {len(hold)} en pausa / garantía")
             + tile('Contract value (active)', 'Valor contratado (activos)', _money(val), 'from the projects overview', 'del overview de proyectos')
             + tile('Invoiced (active)', 'Facturado (activos)', _money(inv), f"{inv / val * 100:.0f}% of value" if val else '', f"{inv / val * 100:.0f}% del valor" if val else '')
             + tile('Booked revenue YTD (active)', 'Ingresos YTD (activos)', _money(booked), 'journal lines carrying the project segment', 'líneas de póliza con el segmento del proyecto'))
    by_phase: Dict[str, List[dict]] = {}
    for p in active + hold:
        by_phase.setdefault(p['phase'], []).append(p)
    cards = ''
    for phase in sorted(by_phase):
        rows_html = []
        for p in by_phase[phase]:
            rev_y, cos_y = _n(p.get('revenue_ytd')), _n(p.get('cos_ytd'))
            prog = max(_n(p.get('pmo_progress')), _n(p.get('progress')))   # the PM's estimate (overview) until the sheet logs progress
            end = p.get('contract_end') or ''
            late = ''
            try:
                if end and dt.date.fromisoformat(str(end)[:10]) < _today() and (p.get('phase') or '')[:1] in '0123':
                    late = pill('crit', t('past contract end', 'pasó el fin de contrato'))
            except ValueError:
                pass
            rows_html.append(f'<tr><td style="min-width:240px"><a href="/projects/{_e(p["project_id"])}/"><b>{_e(p["name"][:64])}</b></a> <span class="mono muted">{_e(p["code"])}</span> {late}</td>'
                             f'<td>{pill("ok" if (p.get("status") or "") == "ok" else "warn" if p.get("status") == "warning" else "crit" if p.get("status") == "critical" else "off", p.get("status") or "—")}</td>'
                             f'<td>{_e(p.get("project_manager") or p.get("pmo_manager") or "—")}</td><td>{_e(p.get("contract_start") or "")} → {_e(end)}</td>'
                             f'<td class="r">{_money(_n(p.get("value_mxn")))}</td><td class="r">{_money(_n(p.get("planned_cost_mxn")))}</td>'
                             f'<td class="r">{_money(rev_y)}</td><td class="r">{_money(-cos_y)}</td><td class="r">{_money(_n(p.get("invoiced_mxn")))} <span class="muted">/ {_money(_n(p.get("paid_mxn")))}</span></td>'
                             f'<td class="r">{f"{prog * 100:.0f}%" if prog else "—"}</td><td>{pill("ok", "sheet") if p.get("pmo_id") else pill("off", "—")}</td></tr>')
        cards += f'''<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{phase_pill(phase)} <span class="muted" style="font-weight:400;font-size:13px">{len(by_phase[phase])}</span></h2></div>
{_table([t("Project", "Proyecto"), "Status", "PM", t("Contract", "Contrato"), t("Value", "Valor"), t("Planned cost", "Costo planeado"), t("Booked rev. YTD", "Ingresos YTD"), t("Booked cost YTD", "Costo YTD"), t("Invoiced / paid", "Facturado / cobrado"), t("Progress", "Avance"), "PMO"], rows_html, cols=COLS('project', 'status', 'pm', 'contract', 'value', 'planned_cost', 'rev_ytd', 'cos_ytd', 'invoiced', 'progress', 'pmo'))}</div>'''
    body = f'''<div class="kicker">{t("Projects", "Proyectos")} · {_today().isoformat()} · <span class="mono">{_e(ENTITY)}</span> · {t("overview + books through", "overview + libros al")} {period}</div><div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{t("Portfolio", "Portafolio")}</h1>{PC.print_button('Print / PDF for shareholders', 'Imprimir / PDF para accionistas')}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>{cards}
<p class="note">{t("Phase, dates, value, planned cost, progress, invoiced and paid come from Argia_Projects_Overview_MX.xlsx (sheet Data); booked revenue and cost from the accountants' GM per project; 'sheet' = the project's ARGIA PROJECT workbook is being read (tasks, milestones, costs). The dummy PLD projects ARG0001–ARG0012 and the template are skipped.", "Fase, fechas, valor, costo planeado, avance, facturado y cobrado vienen de Argia_Projects_Overview_MX.xlsx (hoja Data); ingresos y costo contabilizados del GM por proyecto de contabilidad; 'sheet' = se lee la hoja ARGIA PROJECT del proyecto (tareas, hitos, costos). Los proyectos PLD ARG0001–ARG0012 de prueba y la plantilla se omiten.")}</p>'''
    return PC.page('Projects', body, 'projects', '', wide=True)


def page_project(pid: str) -> Optional[str]:
    m = re.match(r'^ARG(\d{4})$', pid)
    if not m:
        return None
    code = int(m.group(1))
    p = q_project(code)
    if not p:
        return None
    period = q_period()
    gl = q_project_gl(code)
    items = q_project_items(code)
    tasks = q_pmo_tasks(pid) if p.get('pmo_id') else []
    costs = q_pmo_costs(pid) if p.get('pmo_id') else []
    invs = q_pmo_invoices(pid) if p.get('pmo_id') else []
    val, planned = _n(p.get('value_mxn')), _n(p.get('planned_cost_mxn'))
    rev_y, cos_y, rev_t, cos_t = _n(p.get('revenue_ytd')), _n(p.get('cos_ytd')), _n(p.get('revenue_total')), _n(p.get('cos_total'))
    gm_t = rev_t + cos_t
    prog = max(_n(p.get('pmo_progress')), _n(p.get('progress')))   # the PM's estimate (overview) until the sheet logs progress
    pm_margin = (val - planned) if val and planned else None
    tiles = (tile('Contract value', 'Valor contratado', _money(val), f"planned cost {planned:,.0f} · planned margin {pm_margin:,.0f} ({pm_margin / val * 100:.0f}%)" if pm_margin is not None else 'planned cost not set',
                  f"costo planeado {planned:,.0f} · margen planeado {pm_margin:,.0f} ({pm_margin / val * 100:.0f}%)" if pm_margin is not None else 'sin costo planeado')
             + tile('Booked to date', 'Contabilizado a la fecha', _money(gm_t), f"revenue {rev_t:,.0f} − cost {-cos_t:,.0f} · of which 2026: {rev_y:,.0f} / {-cos_y:,.0f}",
                    f"ingresos {rev_t:,.0f} − costo {-cos_t:,.0f} · de los cuales 2026: {rev_y:,.0f} / {-cos_y:,.0f}",
                    tone=('bad' if pm_margin is not None and gm_t < 0 and rev_t > 0 else 'good' if gm_t > 0 else ''),
                    why_en=('booked cost exceeds booked revenue' if gm_t < 0 and rev_t > 0 else ''), why_es=('el costo contabilizado supera los ingresos' if gm_t < 0 and rev_t > 0 else ''))
             + tile('Invoiced / paid', 'Facturado / cobrado', _money(_n(p.get('invoiced_mxn'))), f"paid {_n(p.get('paid_mxn')):,.0f} · {(_n(p.get('invoiced_mxn')) / val * 100):.0f}% of value invoiced" if val else '',
                    f"cobrado {_n(p.get('paid_mxn')):,.0f} · {(_n(p.get('invoiced_mxn')) / val * 100):.0f}% del valor facturado" if val else '')
             + tile('Progress', 'Avance', f"{prog * 100:.0f} <span class=\"unit\">%</span>" if prog else '—', f"{_e(p.get('contract_start') or '')} → {_e(p.get('contract_end') or '')}", f"{_e(p.get('contract_start') or '')} → {_e(p.get('contract_end') or '')}"))
    # books detail by account
    grs = []
    for g in gl:
        net = _n(g['debit']) - _n(g['credit'])
        grs.append(f'<tr><td>{_e(g.get("bs_pl") or "")}</td><td class="mono" style="font-size:12px">{_e(g["account"])}</td><td>{_e(g["bucket"])}</td><td class="r">{_money(_n(g["debit"]), "", 2)}</td><td class="r">{_money(_n(g["credit"]), "", 2)}</td><td class="r"><b>{_money(net, "", 2)}</b></td><td class="r">{_e(g["n"])}</td><td>{_e(g.get("first"))} → {_e(g.get("last"))}</td></tr>')
    irs = [f'<tr><td>{pill("ok" if it.get("paid_on") else "crit" if (it.get("status") or "").lower() == "delay" else "warn", (it.get("status") or ""))}</td><td>{_e(it["side"]).upper()}</td><td>{_e(it.get("invoice"))}</td><td>{_e(it.get("company"))}</td>'
           f'<td>{_e(it.get("issued") or "")}</td><td>{_e(it.get("final_due") or it.get("due") or "")}</td><td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td><td>{_e(it.get("paid_on") or "")}</td></tr>' for it in items]
    pmo_html = ''
    if p.get('pmo_id'):
        ms = [x for x in tasks if x.get('is_milestone') in ('t', 'true', True)]
        mrs = [f'<tr><td class="mono" style="font-size:12px">{_e(x["task_id"])}</td><td>{_e(x["name"])}</td><td>{_e(x.get("end_date") or "")}</td><td>{pill("ok" if (x.get("status") or "").lower() == "completed" else "warn" if (x.get("status") or "").lower() == "in progress" else "off", x.get("status") or "—")}</td></tr>' for x in ms]
        by_status: Dict[str, Decimal] = {}
        for c in costs:
            by_status[c.get('cost_status') or '—'] = by_status.get(c.get('cost_status') or '—', D(0)) + _n(c['net'])
        crs = [f'<tr><td class="mono" style="font-size:12px">{_e(c["cost_id"])}</td><td>{_e(c.get("cost_date") or "")}</td><td>{_e(c["category"])}</td><td>{_e(c["vendor"])}</td><td>{_e(c["description"])}</td><td class="r">{_money(c["net"])}</td><td class="r">{_money(c["total"])}</td><td>{pill("ok" if (c.get("cost_status") or "") == "Paid" else "warn" if c.get("cost_status") in ("Committed", "Incurred") else "off", c.get("cost_status") or "—")}</td><td>{_e(c.get("approval") or "")} {_e(c.get("approved_by") or "")}</td></tr>' for c in costs]
        phases = [x for x in tasks if x.get('is_phase') in ('t', 'true', True)]
        prs = [f'<tr><td class="mono" style="font-size:12px">{_e(x["wbs"])}</td><td>{_e(x["name"])}</td><td>{_e(x.get("start_date") or "")} → {_e(x.get("end_date") or "")}</td><td>{_e(x.get("resource") or "")}</td></tr>' for x in phases]
        vrs = [f'<tr><td>{_e(i["invoice_id"])}</td><td>{_e(i["milestone"])}</td><td>{_e(i["number"])}</td><td>{_e(i.get("inv_date") or "")}</td><td>{_e(i.get("due") or "")}</td><td class="r">{_money(i["total"])}</td><td>{_e(i["status"])} / {_e(i["payment_status"])}</td></tr>' for i in invs]
        pmo_html = f'''
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("PMO sheet", "Hoja PMO")} · {_e(p.get("pmo_phase") or "")} · {_e(p.get("pmo_status") or "")}</h2><span class="muted" style="font-size:12.5px">{t("ARGIA PROJECT workbook: phases, milestones, costs, invoices", "libro ARGIA PROJECT: fases, hitos, costos, facturas")}</span></div>
 <div class="tiles" style="padding:12px 16px 0">{''.join(tile(f"Costs · {k}", f"Costos · {k}", _money(v), "net of IVA, from the Costs tab", "sin IVA, de la pestaña Costs") for k, v in sorted(by_status.items()))}</div>
 <div class="chead"><h3 class="ct">{t("Milestones", "Hitos")}</h3></div>{_table(["ID", t("Milestone", "Hito"), t("Date", "Fecha"), "Status"], mrs, cols=COLS('ms_id', 'ms_name', 'ms_date', 'ms_status'))}
 <div class="chead"><h3 class="ct">{t("Phases", "Fases")}</h3></div>{_table(["WBS", t("Phase", "Fase"), t("Dates", "Fechas"), t("Resource", "Recurso")], prs, cols=COLS('ph_wbs', 'ph_name', 'ph_dates', 'ph_res'))}
 <div class="chead"><h3 class="ct">{t("Costs", "Costos")}</h3></div>{_table(["ID", t("Date", "Fecha"), t("Category", "Categoría"), t("Vendor", "Proveedor"), t("Description", "Descripción"), t("Net", "Neto"), "Total", "Status", t("Approval", "Aprobación")], crs, cols=COLS('c_id', 'c_date', 'c_cat', 'c_vendor', 'c_desc', 'c_net', 'c_total', 'c_status', 'c_appr'))}
 {('<div class="chead"><h3 class="ct">' + t("Invoices", "Facturas") + '</h3></div>' + _table(["ID", t("Milestone", "Hito"), t("Number", "Número"), t("Date", "Fecha"), t("Due", "Vence"), "Total", "Status"], vrs, cols=COLS('iv_id', 'iv_ms', 'iv_no', 'iv_date', 'iv_due', 'iv_total', 'iv_status'))) if vrs else ''}
</div>'''
    body = f'''<div class="kicker"><a href="/projects/">{t("Projects", "Proyectos")}</a> · <span class="mono">{_e(pid)}</span> · {t("books through", "libros al")} {period}</div>
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{_e(p["name"])}</h1>{PC.print_button()}</div>
<div class="muted" style="font-size:14px">{phase_pill(p["phase"])} · {t("BM", "BM")} {_e(p.get("business_manager") or "—")} · PM {_e(p.get("project_manager") or p.get("pmo_manager") or "—")} · {_e(p.get("comment") or "")}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
{pmo_html}
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("In the books", "En libros")} · {t("segment", "segmento")} {code}</h2><span class="muted" style="font-size:12.5px">{t("every posted journal line carrying this project's segment, grouped by account (2026 year to date)", "cada línea de póliza contabilizada con el segmento de este proyecto, agrupada por cuenta (2026 acumulado)")}</span></div>
{_table(["BS/PL", t("Account", "Cuenta"), t("Report line", "Línea de reporte"), t("Debit", "Cargo"), t("Credit", "Abono"), t("Net", "Neto"), "n", t("Dates", "Fechas")], grs, cols=COLS('gl_bspl', 'gl_account', 'gl_report', 'gl_debit', 'gl_credit', 'gl_net', 'gl_n', 'gl_dates')) if grs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No journal lines carry this segment in 2026.", "Ninguna línea de póliza lleva este segmento en 2026.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Invoices in the tracker", "Facturas en seguimiento")}</h2></div>
{_table(["Status", t("Side", "Lado"), t("Invoice", "Factura"), t("Company", "Empresa"), t("Issued", "Emitida"), t("Due", "Vence"), "Total", t("Paid", "Pagada")], irs, cols=COLS('oi_status', 'it_side', 'oi_invoice', 'it_company', 'oi_issued', 'oi_due', 'oi_total', 'oi_paid')) if irs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No open or recently paid invoice for this project.", "Sin factura abierta o pagada recientemente para este proyecto.")}</p>'}</div>'''
    return PC.page(p['name'], body, 'projects', '', wide=True)


# --------------------------------------------------------------- Savio (v247)
def q_savio_check():
    return _rows('savio_check', f"SELECT to_char(checked_at, 'YYYY-MM-DD HH24:MI') AS checked_at, source, kind, severity, savio_ref, our_ref, amount, currency, detail"
                                f" FROM savio_check WHERE entity_id = {_q(ENTITY)} ORDER BY (kind <> 'SUMMARY'), CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, kind, check_id;")


_SAVIO_KIND = {
    'SAVIO_ONLY': ('Invoice in Savio, not in the tracker', 'Factura en Savio, no en el seguimiento'),
    'TRACKER_ONLY': ('Open AR row without a Savio invoice', 'Cuenta por cobrar sin factura en Savio'),
    'AMOUNT': ('Amount differs', 'Importe distinto'), 'CURRENCY': ('Currency differs', 'Moneda distinta'),
    'STATE': ('Paid / open state differs', 'Estado pagada / abierta distinto'),
    'NO_DEPOSIT': ('Savio payment without a deposit in the books', 'Pago en Savio sin depósito en libros'),
    'DEPOSIT_UNMATCHED': ('Deposit in the books without a Savio payment', 'Depósito en libros sin pago en Savio'),
    'UNKNOWN_ACCOUNT': ('Payment to an unregistered bank account', 'Pago a una cuenta bancaria no registrada'),
}


def page_savio() -> str:
    rows = q_savio_check()
    summary = next((r for r in rows if r['kind'] == 'SUMMARY'), None)
    findings = [r for r in rows if r['kind'] != 'SUMMARY']
    source = (summary or {}).get('source') or ''
    banner = ''
    if source == 'mock':
        banner = (f'<div class="card" style="margin-top:16px;padding:12px 18px;border-left:4px solid #e0b100;background:#fffbe8">'
                  f'<b>{t("Mock data.", "Datos de prueba.")}</b> {t("Savio has not issued the API key yet; this page reconciles the demo invoices served by the loopback mock. The checks, the page and the schedule are final — only the input changes when the key lands in /root/.argia_savio.", "Savio aún no entrega la llave del API; esta página concilia las facturas demo del mock local. Las verificaciones, la página y la programación son definitivas — solo cambia la entrada cuando la llave llegue a /root/.argia_savio.")}</div>')
    by_kind: Dict[str, int] = {}
    crit = warn = 0
    for f in findings:
        by_kind[f['kind']] = by_kind.get(f['kind'], 0) + 1
        crit += f['severity'] == 'crit'
        warn += f['severity'] == 'warn'
    tiles = tile('Last check', 'Última verificación', (summary or {}).get('checked_at') or '—', (summary or {}).get('detail') or t('never run', 'nunca ejecutada'), (summary or {}).get('detail') or 'nunca ejecutada')
    tiles += tile('Findings', 'Hallazgos', str(len(findings)), f"{crit} critical · {warn} to review · {len(findings) - crit - warn} informational", f"{crit} críticos · {warn} por revisar · {len(findings) - crit - warn} informativos",
                  tone=('bad' if crit else 'warn' if warn else 'good' if summary else ''), why_en=('a critical finding is money or an invoice that does not match between Savio and the books' if crit else ''),
                  why_es=('un hallazgo crítico es dinero o una factura que no coincide entre Savio y los libros' if crit else ''))
    for k, (en, es) in _SAVIO_KIND.items():
        n = by_kind.get(k, 0)
        if n:
            tiles += tile(en, es, str(n), '', '', tone=('bad' if k in ('AMOUNT', 'CURRENCY', 'UNKNOWN_ACCOUNT') else 'warn' if k in ('SAVIO_ONLY', 'STATE', 'NO_DEPOSIT') else ''))
    trs = [f'<tr><td>{pill("crit" if f["severity"] == "crit" else "warn" if f["severity"] == "warn" else "off", f["severity"])}</td><td>{t(*_SAVIO_KIND.get(f["kind"], (f["kind"], f["kind"])))}</td>'
           f'<td class="mono" style="font-size:12px">{_e(f["savio_ref"])}</td><td class="mono" style="font-size:12px">{_e(f["our_ref"])}</td><td class="r">{_money(f["amount"], f["currency"] or "") if _n(f["amount"]) else ""}</td><td>{_e(f["detail"])}</td></tr>' for f in findings]
    cols = [("Critical = an amount, currency or bank account that does not agree; to review = a state or a missing counterpart; informational = expected differences (income outside Savio).", "Crítico = importe, moneda o cuenta bancaria que no coinciden; por revisar = un estado o contraparte faltante; informativo = diferencias esperadas (ingresos fuera de Savio)."),
            ("What was compared and how it differs.", "Qué se comparó y en qué difiere."), ("Savio invoice or payment id.", "Id de factura o pago en Savio."),
            ("Our side: the tracker invoice number, or the bank account and date of the deposit.", "Nuestro lado: número de factura del seguimiento, o cuenta bancaria y fecha del depósito."),
            ("Amount involved (for AMOUNT: the difference Savio − tracker).", "Importe involucrado (en AMOUNT: la diferencia Savio − seguimiento)."), ("Plain-language explanation.", "Explicación en lenguaje llano.")]
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(q_period(), f" · Savio {_e(source) or '—'}")}<h1 class="pt">{t("Savio check", "Verificación Savio")}</h1></div>{PC.print_button()}</div>
{banner}
<div class="tiles" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Findings", "Hallazgos")}</h2><span class="muted" style="font-size:12.5px">{t("every Savio invoice against the AR tracker (by CFDI UUID, then folio + total); every Savio payment against the deposits in the books (same amount, ±3 days); every reported receiving account against the registered ones", "cada factura de Savio contra el seguimiento de cobrar (por UUID CFDI, luego folio + total); cada pago de Savio contra los depósitos en libros (mismo importe, ±3 días); cada cuenta receptora reportada contra las registradas")}</span></div>
{_table(["Severity", t("Check", "Verificación"), "Savio", t("Ours", "Nuestro"), t("Amount", "Importe"), t("Detail", "Detalle")], trs, cols=cols) if trs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No findings — or the check has not run yet.", "Sin hallazgos — o la verificación aún no corre.")}</p>'}</div>
<div class="card" style="margin-top:16px;padding:16px 20px"><h2 class="ct">{t("How this check works", "Cómo funciona esta verificación")}</h2>
<p class="note">{t("Savio is the invoicing and collections tool: it knows every customer invoice it stamped and every payment it applied. The books (CONTPAQi) and the AR tracker are kept by the accountants. The plugin reads Savio through its API (read-only) and asks three questions: is every stamped invoice being followed for collection, with the same amount and the same paid/open state; did every payment Savio applied really arrive as a deposit on one of ARGIA's bank accounts; and was the money received on an account ARGIA actually owns. Nothing is written back to Savio. It runs daily after the books ingest; the last result is what you see here.",
"Savio es la herramienta de facturación y cobranza: conoce cada factura de cliente que timbró y cada pago que aplicó. Los libros (CONTPAQi) y el seguimiento de cobrar los lleva contabilidad. El plugin lee Savio por su API (solo lectura) y hace tres preguntas: ¿cada factura timbrada está en seguimiento de cobranza, con el mismo importe y el mismo estado pagada/abierta?; ¿cada pago que Savio aplicó llegó realmente como depósito a una cuenta bancaria de ARGIA?; ¿el dinero se recibió en una cuenta que ARGIA realmente posee? No se escribe nada de vuelta a Savio. Corre a diario después de la carga de libros; el último resultado es lo que se ve aquí.")}</p></div>'''
    return PC.page('Savio', body, 'finance', 'savio', wide=True)
