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
from argia.fin import costcenter as CC
from argia.fin import source as SRC
from argia.fin.money import D

t, ti, tile, pill = PC.t, PC.ti, PC.tile, PC.pill

_rows: Callable = None
_money_raw: Callable = None
_today: Callable = None
ENTITY = 'ARGIA-MX'
_pref = ''                     # v248: '' (as issued) | 'MXN' | 'USD' — the reader's display currency
_rate: Optional[Decimal] = None   # USD → MXN used for the display conversion
_rate_src = ()
_cc_cache: Optional[Dict[int, dict]] = None


def bind(rows, money, today, entity):
    global _rows, _money_raw, _today, ENTITY, _cc_cache
    _rows, _money_raw, _today, ENTITY, _cc_cache = rows, money, today, entity, None


def set_display(pref: str = '', rate: Optional[Decimal] = None, src: str = ''):
    """v248: amounts shown in one currency (Tomasz 2026-09-10: 'currently it is a mix')."""
    global _pref, _rate, _rate_src
    _pref, _rate, _rate_src = (pref if pref in ('MXN', 'USD') else ''), rate, src


def _money(v, ccy='MXN', dec=0) -> str:
    """As issued, or converted to the reader's currency at the display rate
    (the original stays in the tooltip)."""
    if not _pref or not ccy or ccy == _pref or _rate is None or ccy not in ('MXN', 'USD'):
        return _money_raw(v, ccy, dec)
    try:
        x = D(v)
    except (ValueError, TypeError):
        return _money_raw(v, ccy, dec)
    conv = x * _rate if ccy == 'USD' else x / _rate
    return f'<span class="conv" title="{x:,.2f} {ccy} as issued / como se emitió">{_money_raw(conv, _pref, dec)}</span>'


def _nm(s) -> str:
    """One case for every name (v248)."""
    return _e(CC.canon(s))


def _dt(s) -> str:
    return f'<td class="nw">{_e(str(s or "")[:10])}</td>'


def cc_map() -> Dict[int, dict]:
    global _cc_cache
    if _cc_cache is None:
        _cc_cache = {int(r['code']): r for r in _rows('cost_centers', f"SELECT code, name, kind, grp, manual FROM cost_center WHERE entity_id = {_q(ENTITY)} ORDER BY code;")}
    return _cc_cache


def cc_kind(code) -> str:
    c = cc_map().get(int(code)) if code not in (None, '') else None
    return c['kind'] if c else (CC.PROJECT if code not in (None, '') and int(code) >= 1000 else CC.OTHER)


def _proj(code, name=None, link=True) -> str:
    """The one way a project / cost centre is written: code first, one case,
    linked to its page (project page or cost-centre page)."""
    if code in (None, ''):
        return _nm(name) or '—'
    c = cc_map().get(int(code))
    kind = c['kind'] if c else cc_kind(code)
    lbl = _e(CC.label(code, (c['name'] if c else name) or name, kind))
    return f'<a href="{CC.href(code, kind)}">{lbl}</a>' if link else lbl


def party_key(name) -> str:
    """Tracker company ↔ books sub-account: same key after dropping accents,
    legal suffixes, the accountants' currency/complement suffixes (USD, DLLS,
    COMP, COMPL, MXP, PROVEED) and punctuation ('C10 Group s.r.o' = 'C10 GROUP')."""
    import unicodedata
    s = unicodedata.normalize('NFKD', CC.canon(name)).encode('ascii', 'ignore').decode().replace('.', '')
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    s = re.sub(r'\b(SAPI|SA|DE|CV|SRL|SRO|RL|LLC|INC|LTD|LTDA|GMBH|SC|SAS|USD|DLLS|DLS|MXN|MXP|COMPL|COMP|PROVEED|PROVEEDOR|PROVEEDORES|PROV)\b', ' ', s)
    return re.sub(r' +', ' ', s).strip()


def _acct_ccy(name: str) -> str:
    n = (name or '').upper()
    return 'USD' if re.search(r'\b(USD|DLLS|DLS)\b', n) else 'MXN'


def supplier_groups(sup: List[dict]) -> List[dict]:
    """One supplier = its sub-accounts (MXN, USD at face value, the peso
    complement) grouped by name key; amounts kept per currency."""
    groups: Dict[str, dict] = {}
    for s_ in sup:
        k = party_key(s_['name']) or s_['account']
        g = groups.setdefault(k, {'key': k, 'name': '', 'accounts': [], 'closing': {}, 'credits': {}, 'debits': {}, 'movements': 0, 'last': ''})
        ccy = _acct_ccy(s_['name'])
        if not g['name'] or (ccy == 'MXN' and not re.search(r'COMPL?$', CC.canon(s_['name']))):
            g['name'] = re.sub(r'\s*(-\s*)?\b(PROVEED|PROVEEDOR|COMPL|COMP|USD|DLLS|MXP)\b\s*$', '', CC.canon(s_['name'])).strip(' -,') or g['name']
        g['accounts'].append(s_['account'])
        for f in ('closing', 'credits', 'debits'):
            g[f][ccy] = g[f].get(ccy, D(0)) + _n(s_[f])
        g['movements'] += int(s_.get('movements') or 0)
        g['last'] = max(g['last'], str(s_.get('last') or ''))
    return sorted(groups.values(), key=lambda g: -(g['closing'].get('MXN', D(0)) + g['closing'].get('USD', D(0)) * (_rate or D(17))))


def _per_ccy(d: Dict[str, Decimal], bold=False) -> str:
    parts = [_money(v, k) for k, v in sorted(d.items()) if v or k == 'MXN']
    x = ' · '.join(parts) or _money(0)
    return f'<b>{x}</b>' if bold else x


_SRC_JOIN = ("f.name AS src_name, f.kind AS src_kind, f.drive_id AS src_drive_id, coalesce(f.mime, '') AS src_mime")


def src_link(row: dict, column: str = '', value: str = '', label: str = '') -> str:
    """v250: the ⎘ that opens the file this row was read from — on the cell
    itself when the file is a Google Sheet, on the file otherwise, with the
    sheet and row in the tooltip (Tomasz 2026-09-10)."""
    p = SRC.pointer(row, column, value)
    if not p or not p['url']:
        return ''
    tip = f"{p['en']} / {p['es']}"
    mark = '⎘' if p['exact'] else '↗'
    return (f'<a class="src" target="_blank" rel="noopener" href="{_e(p["url"])}" title="{_e(tip)}"'
            f' aria-label="{_e(p["en"])}">{label or mark}</a>')


_COL_SRC = ("Opens the file this row was read from — the exact cell when the file is a Google Sheet, otherwise the file, with the sheet and row in the tooltip. This is where the value is changed; the portal never writes to any source file.",
            "Abre el archivo del que se leyó esta fila — la celda exacta si el archivo es una hoja de Google, si no el archivo, con la hoja y la fila en el tooltip. Ahí se cambia el valor; el portal nunca escribe en los archivos de origen.")


def doc_link(it: dict) -> str:
    """v248: a link to the document — the tracker has no URL column, so this
    opens a Google Drive search for the folio fiscal (or the invoice number)."""
    key = (it.get('folio_fiscal') or '').strip() or (it.get('invoice') or '').strip()
    if not key or key.lower() in ('d/a', 'n/a', '-', '—'):
        return ''
    from urllib.parse import quote
    return f'<a class="doc" target="_blank" rel="noopener" href="https://drive.google.com/drive/search?q={quote(key)}" title="search this document in Google Drive / buscar este documento en Google Drive">Drive ↗</a>'


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _n(v) -> Decimal:
    try:
        return D(v if v not in (None, '') else 0)
    except Exception:            # noqa: BLE001
        return D(0)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ''))


def _table(head: List[str], body: List[str], cls='', cols=None, sums=None, tools=True) -> str:
    """v246: every table gets search, filters, sorting and column tooltips (portal_chrome.data_table);
    v248: ``sums`` = the money columns that get a total row; v250: ``tools=False``
    for a short reference table that needs no search box."""
    return PC.data_table(head, body, cls, cols, tools=tools, sums=sums)


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
    return _rows(f'open_{side}', f"SELECT o.status, o.invoice, o.company, o.project_code, o.project_name, o.po, o.issued, o.due, o.final_due,"
                                 f" o.days_to_due, o.currency, o.total, o.net, o.mxn_equiv_net, o.folio_fiscal, o.paid_on, o.kind, o.comment,"
                                 f" o.src_sheet, o.src_row, o.src_gid, {_SRC_JOIN} FROM open_item o"
                                 f" LEFT JOIN fin_source_file f ON f.sha256 = o.source_sha WHERE o.entity_id = {_q(ENTITY)}"
                                 f" AND o.side = {_q(side)} ORDER BY (o.paid_on IS NOT NULL), coalesce(o.final_due, o.due), o.company;")


def q_report(period: str, sheet: str):
    return _rows(f'report_{sheet}', f"SELECT code, label, ord, m01, m02, m03, m04, m05, m06, m07, m08, m09, m10, m11, m12, ytd FROM fin_report_line"
                                    f" WHERE entity_id = {_q(ENTITY)} AND period = {_q(period)} AND sheet = {_q(sheet)} ORDER BY ord;")


def q_portfolio():
    return _rows('portfolio', f"SELECT p.code, p.project_id, p.name, p.phase, p.status, p.business_manager, p.project_manager, p.value_usd, p.value_mxn,"
                              f" p.planned_cost_mxn, p.margin_planned_pct, p.contract_start, p.contract_end, p.progress, p.invoiced_mxn, p.paid_mxn, p.comment,"
                              f" m.revenue_ytd, m.cos_ytd, m.revenue_total, m.cos_total, m.gm, m.planned_value, m.planned_cost, m.planned_margin,"
                              f" s.project_id AS pmo_id, s.phase AS pmo_phase, s.status AS pmo_status, s.progress AS pmo_progress, s.manager AS pmo_manager"
                              f", p.src_sheet, p.src_row, p.src_gid, {_SRC_JOIN}, s.sheet_id AS pmo_sheet_id"
                              f" FROM portfolio_project p"
                              f" LEFT JOIN fin_source_file f ON f.sha256 = p.source_sha"
                              f" LEFT JOIN project_margin m ON m.entity_id = p.entity_id AND m.code = p.code AND m.period = (SELECT max(period) FROM project_margin x WHERE x.entity_id = p.entity_id)"
                              f" LEFT JOIN pmo_project s ON s.entity_id = p.entity_id AND s.code = p.code"
                              f" WHERE p.entity_id = {_q(ENTITY)} ORDER BY p.phase, p.code DESC;")


def q_project(code: int):
    r = [x for x in q_portfolio() if str(x.get('code')) == str(code)]
    return r[0] if r else None


def q_project_gl(code: int, period: str = ''):
    year = (period or q_period() or '2026')[:4]
    return _rows('project_gl', f"SELECT l.account, coalesce(a.report_account, l.account_name) AS bucket, a.bs_pl, a.a_p,"
                               f" sum(l.debit) AS debit, sum(l.credit) AS credit, count(*) AS n, min(j.jdate) AS first, max(j.jdate) AS last"
                               f" FROM gl_line l JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                               f" LEFT JOIN gl_account a ON a.entity_id = l.entity_id AND a.account = l.account"
                               f" WHERE l.entity_id = {_q(ENTITY)} AND l.segment = {int(code)} AND j.jdate >= {_q(year + '-01-01')} GROUP BY 1, 2, 3, 4 ORDER BY a.bs_pl, l.account;")


def q_project_items(code: int):
    return _rows('project_items', f"SELECT o.side, o.status, o.invoice, o.company, o.issued, o.due, o.final_due, o.days_to_due, o.currency, o.total,"
                                  f" o.net, o.paid_on, o.kind, o.folio_fiscal, o.src_sheet, o.src_row, o.src_gid, {_SRC_JOIN} FROM open_item o"
                                  f" LEFT JOIN fin_source_file f ON f.sha256 = o.source_sha"
                                  f" WHERE o.entity_id = {_q(ENTITY)} AND o.project_code = {int(code)} ORDER BY o.side, (o.paid_on IS NOT NULL), coalesce(o.final_due, o.due);")


def q_pmo_tasks(pid: str):
    return _rows('pmo_tasks', f"SELECT t.wbs, t.task_id, t.name, t.is_phase, t.is_milestone, t.resource, t.start_date, t.end_date, t.duration_days,"
                              f" t.status, t.progress, t.src_sheet, t.src_row, t.src_gid, {_SRC_JOIN} FROM pmo_task t"
                              f" JOIN pmo_project p ON p.project_id = t.project_id LEFT JOIN fin_source_file f ON f.sha256 = p.source_sha"
                              f" WHERE t.project_id = {_q(pid)} ORDER BY string_to_array(t.wbs, '.')::int[];")


def q_pmo_costs(pid: str):
    return _rows('pmo_costs', f"SELECT c.cost_id, c.cost_date, c.category, c.vendor, c.description, c.net, c.vat, c.total, c.cost_status, c.approval,"
                              f" c.approved_by, c.paid, c.payment_status, c.src_sheet, c.src_row, c.src_gid, {_SRC_JOIN} FROM pmo_cost c"
                              f" JOIN pmo_project p ON p.project_id = c.project_id LEFT JOIN fin_source_file f ON f.sha256 = p.source_sha"
                              f" WHERE c.project_id = {_q(pid)} ORDER BY c.cost_date, c.cost_id;")


def q_pmo_invoices(pid: str):
    return _rows('pmo_invoices', f"SELECT i.invoice_id, i.customer, i.milestone, i.number, i.inv_date, i.due, i.net, i.vat, i.total, i.status,"
                                 f" i.payment_status, i.paid_on, i.received, i.src_sheet, i.src_row, i.src_gid, {_SRC_JOIN} FROM pmo_invoice i"
                                 f" JOIN pmo_project p ON p.project_id = i.project_id LEFT JOIN fin_source_file f ON f.sha256 = p.source_sha"
                                 f" WHERE i.project_id = {_q(pid)} ORDER BY i.inv_date, i.invoice_id;")


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
        rows_html.append(f'<tr><td style="min-width:240px"><a href="/projects/{_e(p["project_id"])}/"><b>{_e(CC.label(p["code"], p["name"]))}</b></a></td>'
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
 {_tools()}
</div>
<div class="tiles" style="margin-top:20px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Active projects — planned vs booked", "Proyectos activos — plan vs libros")}</h2><span class="muted" style="font-size:12.5px">{t("value and planned cost from the projects overview · booked revenue / cost / margin YTD from the accountants' GM per project (CONTPAQi segments)", "valor y costo planeado del overview de proyectos · ingresos / costo / margen contabilizados YTD del GM por proyecto de contabilidad (segmentos CONTPAQi)")}</span></div>
 {_table([t("Project", "Proyecto"), t("Phase", "Fase"), "PM", t("Value", "Valor"), t("Planned cost", "Costo planeado"), t("Booked revenue YTD", "Ingresos YTD"), t("Booked cost YTD", "Costo YTD"), t("Booked margin YTD", "Margen YTD"), t("Invoiced", "Facturado"), t("Progress", "Avance"), "PMO"], rows_html, cols=COLS('project', 'phase', 'pm', 'value', 'planned_cost', 'rev_ytd', 'cos_ytd', 'gm_ytd', 'invoiced', 'progress', 'pmo'), sums=[3, 4, 5, 6, 7, 8])}
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
    idx = supplier_index(q_suppliers(period)) if (side == 'ap' and period) else {}
    trs = []
    for it in items:
        paid = bool(it.get('paid_on'))
        days = int(it.get('days_to_due') or 0)
        st = it.get('status') or ''
        cls = 'ok' if paid or st.lower() == 'paid' else 'crit' if st.lower() == 'delay' else 'warn' if st.lower() == 'on time' else 'off'
        acct = match_account(idx, it.get('company') or '') if idx else ''
        party = f'<a href="/finance/suppliers/{acct}/">{_nm(it.get("company"))}</a>' if acct else _nm(it.get('company'))
        trs.append(f'<tr><td>{pill(cls, st)}</td><td class="mono" style="font-size:12px">{_e(it.get("invoice"))}</td><td>{party}</td><td>{_proj(it.get("project_code"), it.get("project_name"))}</td>'
                   f'<td class="nw">{_e(re.sub(r" 00:00:00$", "", str(it.get("po") or "")))}</td>{_dt(it.get("issued"))}{_dt(it.get("final_due") or it.get("due"))}'
                   f'<td class="r">{"" if paid else f"{days:+d}"}</td><td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td>'
                   f'<td class="r">{_money(it.get("net"), it.get("currency") or "MXN")}</td>{_dt(it.get("paid_on"))}<td class="mono nw" style="font-size:11px">{_e((it.get("folio_fiscal") or "")[:8])}{doc_link(it)}</td><td>{_e(it.get("comment") or "")}</td>'
                   f'<td class="nw">{src_link(it, "Payment Due Day" if not paid else "Confirmation Payment date", str(it.get("final_due") or it.get("due") or ""))}</td></tr>')
    brs = []
    for b in book:
        nm = f'<a href="/finance/suppliers/{_e(b["account"])}/">{_nm(b["name"])}</a>' if side == 'ap' else _nm(b['name'])
        brs.append(f'<tr><td class="mono" style="font-size:12px">{_e(b["account"])}</td><td>{nm}</td><td class="r"><b>{_money(b["closing"])}</b></td></tr>')
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t(en, es)}</h1></div>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Open items (tracker)", "Partidas abiertas (seguimiento)")}</h2><span class="muted" style="font-size:12.5px">{t("days = to the final due date; negative = overdue", "días = al vencimiento final; negativo = vencida")}</span></div>
 {_table(["Status", t("Invoice", "Factura"), t("Customer", "Cliente") if side == "ar" else t("Supplier", "Proveedor"), t("Project", "Proyecto"), "PO", t("Issued", "Emitida"), t("Due", "Vence"), t("Days", "Días"), "Total", t("Net", "Neto"), t("Paid", "Pagada"), "Folio", t("Comment", "Comentario"), t("Source", "Origen")], trs, cols=COLS('oi_status', 'oi_invoice', 'oi_customer' if side == 'ar' else 'oi_supplier', 'oi_project', 'oi_po', 'oi_issued', 'oi_due', 'oi_days', 'oi_total', 'oi_net', 'oi_paid', 'oi_folio', 'oi_comment') + [_COL_SRC], sums=[8, 9])}
</div>
<div class="card" style="margin-top:16px;overflow:hidden">
 <div class="chead"><h2 class="ct">{t("Balances in the books", "Saldos en libros")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("CONTPAQi auxiliares, one sub-account per counterparty; USD accounts at face value", "auxiliares CONTPAQi, una subcuenta por contraparte; cuentas USD a valor nominal")}</span></div>
 {_table([t("Account", "Cuenta"), t("Counterparty", "Contraparte"), t("Balance", "Saldo")], brs, cols=COLS('bk_account', 'bk_party', 'bk_balance'), sums=[2])}
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
            trs = [f'<tr>{_dt(l["jdate"])}<td class="nw">{_e(l["kind"])} {_e(l["number"])}</td><td>{_e(l["concept"])}</td><td class="mono" style="font-size:11px">{_e(l.get("reference") or "")}</td>'
                   f'<td class="r">{_money(l["debit"], "", 2) if _n(l["debit"]) else ""}</td><td class="r">{_money(l["credit"], "", 2) if _n(l["credit"]) else ""}</td><td>{_proj(l.get("segment"))}</td></tr>' for l in lines]
            sections += f'''<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{_e(v["name"])} · <span class="mono">{_e(acct)}</span></h2><span class="muted" style="font-size:12.5px">{t("last 45 days of the books", "últimos 45 días de libros")} · {since.isoformat()} → {pend.isoformat()}</span></div>
{_table([t("Date", "Fecha"), t("Póliza", "Póliza"), t("Concept", "Concepto"), t("Reference", "Referencia"), t("In", "Entrada"), t("Out", "Salida"), t("Project", "Proyecto")], trs, cols=COLS('bl_date', 'bl_poliza', 'bl_concept', 'bl_ref', 'bl_in', 'bl_out', 'bl_project'), sums=[4, 5])}</div>'''
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Bank", "Banco")}</h1></div>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tiles or tile("Bank", "Banco", "—", "no books loaded", "sin libros")}</div>
{sections}
<p class="note">{t("These are the bank movements as booked by the accountants (CONTPAQi pólizas), not the bank's own statement. A statement-to-books reconciliation arrives when the bank exports are in the drop folder.", "Estos son los movimientos bancarios como los contabilizó contabilidad (pólizas CONTPAQi), no el estado de cuenta del banco. La conciliación estado de cuenta vs libros llega cuando los exports del banco estén en la carpeta.")}</p>'''
    return PC.page('Bank', body, 'finance', 'bank', wide=True)


def q_report_periods():
    return [r['period'] for r in _rows('report_periods', f"SELECT DISTINCT period FROM fin_report_line WHERE entity_id = {_q(ENTITY)} AND sheet = 'pl' ORDER BY period DESC;")]


def year_links(periods: List[str], current: str) -> str:
    """v249: the closed years beside the current one ('2026-07 · 2025 · 2024 · 2023')."""
    if len(periods) < 2:
        return ''
    parts = []
    for p in periods:
        lbl = p if p == periods[0] else p[:4]
        parts.append(f'<a href="?period={p}"{" class=on" if p == current else ""}>{lbl}</a>')
    return f'<span class="ccysw noprint" title="closed years / años cerrados">{"".join(parts)}</span>'


def page_pl(period: str = '') -> str:
    periods = q_report_periods()
    period = period if period in periods else (periods[0] if periods else q_period())
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
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Profit & loss and balance sheet", "Resultados y balance")} <span class="muted" style="font-size:16px;font-weight:400">{period}</span></h1></div><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">{year_links(periods, period)}{ccy_switch()}{PC.print_button('Print / PDF for the accountants', 'Imprimir / PDF para contabilidad')}</div></div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Management P&L", "Estado de resultados de gestión")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("thousands of MXN, the accountants' sheet PL; budget = sheet PL_Budget (" + period[:4] + " plan)", "miles de MXN, hoja PL de contabilidad; presupuesto = hoja PL_Budget (plan " + period[:4] + ")")}</span></div>{_table(heads, trs, cols=[_COL['pl_line']] + [_COL['pl_month']] * month + [_COL['pl_ytd'], _COL['pl_budget'], _COL['pl_var']])}</div>
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
            rows_html.append(f'<tr><td style="min-width:240px"><a href="/projects/{_e(p["project_id"])}/"><b>{_e(CC.label(p["code"], p["name"]))}</b></a> {late}</td>'
                             f'<td>{pill("ok" if (p.get("status") or "") == "ok" else "warn" if p.get("status") == "warning" else "crit" if p.get("status") == "critical" else "off", p.get("status") or "—")}</td>'
                             f'<td>{_e(p.get("project_manager") or p.get("pmo_manager") or "—")}</td><td>{_e(p.get("contract_start") or "")} → {_e(end)}</td>'
                             f'<td class="r">{_money(_n(p.get("value_mxn")))}</td><td class="r">{_money(_n(p.get("planned_cost_mxn")))}</td>'
                             f'<td class="r">{_money(rev_y)}</td><td class="r">{_money(-cos_y)}</td><td class="r">{_money(_n(p.get("invoiced_mxn")))} <span class="muted">/ {_money(_n(p.get("paid_mxn")))}</span></td>'
                             f'<td class="r">{f"{prog * 100:.0f}%" if prog else "—"}</td><td>{pill("ok", "sheet") if p.get("pmo_id") else pill("off", "—")}</td></tr>')
        cards += f'''<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{phase_pill(phase)} <span class="muted" style="font-weight:400;font-size:13px">{len(by_phase[phase])}</span></h2></div>
{_table([t("Project", "Proyecto"), "Status", "PM", t("Contract", "Contrato"), t("Value", "Valor"), t("Planned cost", "Costo planeado"), t("Booked rev. YTD", "Ingresos YTD"), t("Booked cost YTD", "Costo YTD"), t("Invoiced / paid", "Facturado / cobrado"), t("Progress", "Avance"), "PMO"], rows_html, cols=COLS('project', 'status', 'pm', 'contract', 'value', 'planned_cost', 'rev_ytd', 'cos_ytd', 'invoiced', 'progress', 'pmo'), sums=[4, 5, 6, 7, 8])}</div>'''
    body = f'''<div class="kicker">{t("Projects", "Proyectos")} · {_today().isoformat()} · <span class="mono">{_e(ENTITY)}</span> · {t("overview + books through", "overview + libros al")} {period}</div><div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{t("Portfolio", "Portafolio")}</h1><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">{ccy_switch()}{PC.print_button('Print / PDF for shareholders', 'Imprimir / PDF para accionistas')}</div></div>
<div class="tiles" style="margin-top:16px">{tiles}</div>{cards}
<p class="note">{t("Phase, dates, value, planned cost, progress, invoiced and paid come from Argia_Projects_Overview_MX.xlsx (sheet Data); booked revenue and cost from the accountants' GM per project; 'sheet' = the project's ARGIA PROJECT workbook is being read (tasks, milestones, costs). The dummy PLD projects ARG0001–ARG0012 and the template are skipped.", "Fase, fechas, valor, costo planeado, avance, facturado y cobrado vienen de Argia_Projects_Overview_MX.xlsx (hoja Data); ingresos y costo contabilizados del GM por proyecto de contabilidad; 'sheet' = se lee la hoja ARGIA PROJECT del proyecto (tareas, hitos, costos). Los proyectos PLD ARG0001–ARG0012 de prueba y la plantilla se omiten.")}</p>'''
    return PC.page('Projects', body, 'projects', '', wide=True)


def q_source(kind: str) -> Optional[dict]:
    """The newest file of one kind, shaped like the src_* fields of a row."""
    r = _rows(f'src_{kind}', f"SELECT name AS src_name, kind AS src_kind, drive_id AS src_drive_id, coalesce(mime, '') AS src_mime"
                             f" FROM fin_source_file WHERE entity_id = {_q(ENTITY)} AND kind = {_q(kind)}"
                             f" ORDER BY period DESC NULLS LAST, coalesce(modified, imported_at) DESC LIMIT 1;")
    return r[0] if r else None


def _tab_of(row: Optional[dict]) -> Optional[dict]:
    """A row's pointer without its row number: the tab, not the cell — what
    you want when the answer is "add a line here", not "look at this line"."""
    return dict(row, src_row=0) if row else None


def where_box(entries: List[dict]) -> str:
    """v250 — "where do I change this?": one row per file that feeds the page,
    what it decides, who keeps it and a link straight to it. The books are
    listed too, marked read-only: a wrong booked figure is corrected in
    CONTPAQi by the accountants and arrives with the next close."""
    trs = []
    for e in entries:
        p = e.get('pointer')
        if not p or not p.get('url'):
            continue
        own_en, own_es = SRC.owner(p['kind'])
        link = f'<a class="src" target="_blank" rel="noopener" href="{_e(p["url"])}">{_e(p["name"])} {"⎘" if p["exact"] else "↗"}</a>'
        badge = pill('ok', 'Edit here', 'Aquí se edita') if p['editable'] else pill('off', 'Read-only', 'Solo lectura')
        trs.append(f'<tr><td>{t(*e["what"])}</td><td>{link}</td><td class="muted" style="font-size:12.5px">{_e(p["en"])}</td>'
                   f'<td>{t(own_en, own_es)}</td><td>{badge}</td></tr>')
    if not trs:
        return ''
    cols = [("What this file decides on this page.", "Qué decide este archivo en esta página."),
            ("The file itself — the link opens it (the exact cell when it is a Google Sheet).", "El archivo — el enlace lo abre (la celda exacta si es una hoja de Google)."),
            ("Sheet and row the portal read, so the value is easy to find inside the file.", "Hoja y fila que leyó el portal, para ubicar el valor dentro del archivo."),
            ("Who keeps that file.", "Quién lleva ese archivo."),
            ("Whether changing the file is the way to fix the number. The books are the accountants' output: a wrong figure there is corrected in CONTPAQi and arrives with the next monthly close.",
             "Si cambiar el archivo es la forma de corregir el número. Los libros son el resultado de contabilidad: una cifra equivocada se corrige en CONTPAQi y llega con el siguiente cierre mensual.")]
    head = [t("What it decides", "Qué decide"), t("File", "Archivo"), t("Where in the file", "Dónde en el archivo"), t("Kept by", "Lo lleva"), ""]
    note = t("every number on this page comes from one of these files; the portal only reads them and picks a change up at the next morning's read",
             "cada número de esta página viene de uno de estos archivos; el portal solo los lee y toma el cambio en la lectura de la mañana siguiente")
    return ('<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">'
            + t("Where to change this", "Dónde se cambia esto") + '</h2><span class="muted" style="font-size:12.5px">' + note + '</span></div>'
            + _table(head, trs, cols=cols, tools=False) + '</div>')


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
    irs = [f'<tr><td>{pill("ok" if it.get("paid_on") else "crit" if (it.get("status") or "").lower() == "delay" else "warn", (it.get("status") or ""))}</td><td>{_e(it["side"]).upper()}</td><td class="mono" style="font-size:12px">{_e(it.get("invoice"))}</td><td>{_nm(it.get("company"))}</td>'
           f'{_dt(it.get("issued"))}{_dt(it.get("final_due") or it.get("due"))}<td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td>{_dt(it.get("paid_on"))}'
           f'<td class="nw">{src_link(it, "Payment Due Day", str(it.get("final_due") or it.get("due") or ""))}</td></tr>' for it in items]
    pmo_html = ''
    if p.get('pmo_id'):
        ms = [x for x in tasks if x.get('is_milestone') in ('t', 'true', True)]
        mrs = [f'<tr><td class="mono" style="font-size:12px">{_e(x["task_id"])}</td><td>{_e(x["name"])}</td>{_dt(x.get("end_date"))}<td>{pill("ok" if (x.get("status") or "").lower() == "completed" else "warn" if (x.get("status") or "").lower() == "in progress" else "off", x.get("status") or "—")}</td>'
               f'<td class="nw">{src_link(x, "Task_Status", x.get("status") or "")}</td></tr>' for x in ms]
        by_status: Dict[str, Decimal] = {}
        for c in costs:
            by_status[c.get('cost_status') or '—'] = by_status.get(c.get('cost_status') or '—', D(0)) + _n(c['net'])
        crs = []
        for c in costs:
            crs.append(f'<tr><td class="mono" style="font-size:12px">{_e(c["cost_id"])}</td>{_dt(c.get("cost_date"))}<td>{_e(c["category"])}</td><td>{_nm(c["vendor"])}</td>'
                       f'<td>{_e(c["description"])}</td><td class="r">{_money(c["net"])}</td><td class="r">{_money(c["total"])}</td>'
                       f'<td>{pill("ok" if (c.get("cost_status") or "") == "Paid" else "warn" if c.get("cost_status") in ("Committed", "Incurred") else "off", c.get("cost_status") or "—")}</td>'
                       f'<td>{_e(c.get("approval") or "")} {_e(c.get("approved_by") or "")}</td>'
                       f'<td class="nw">{src_link(c, "Amount_Before_VAT", format(_n(c.get("net")), ",.2f"))}</td></tr>')
        phases = [x for x in tasks if x.get('is_phase') in ('t', 'true', True)]
        prs = [f'<tr><td class="mono" style="font-size:12px">{_e(x["wbs"])}</td><td>{_e(x["name"])}</td><td>{_e(x.get("start_date") or "")} → {_e(x.get("end_date") or "")}</td><td>{_e(x.get("resource") or "")}</td></tr>' for x in phases]
        vrs = [f'<tr><td>{_e(i["invoice_id"])}</td><td>{_e(i["milestone"])}</td><td>{_e(i["number"])}</td>{_dt(i.get("inv_date"))}{_dt(i.get("due"))}<td class="r">{_money(i["total"])}</td><td>{_e(i["status"])} / {_e(i["payment_status"])}</td>'
               f'<td class="nw">{src_link(i, "Total_Amount", format(_n(i.get("total")), ",.2f"))}</td></tr>' for i in invs]
        pmo_html = f'''
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("PMO sheet", "Hoja PMO")} · {_e(p.get("pmo_phase") or "")} · {_e(p.get("pmo_status") or "")}</h2><span class="muted" style="font-size:12.5px">{t("ARGIA PROJECT workbook: phases, milestones, costs, invoices", "libro ARGIA PROJECT: fases, hitos, costos, facturas")}</span></div>
 <div class="tiles" style="padding:12px 16px 0">{''.join(tile(f"Costs · {k}", f"Costos · {k}", _money(v), "net of IVA, from the Costs tab", "sin IVA, de la pestaña Costs") for k, v in sorted(by_status.items()))}</div>
 <div class="chead"><h3 class="ct">{t("Milestones", "Hitos")}</h3></div>{_table(["ID", t("Milestone", "Hito"), t("Date", "Fecha"), "Status", t("Source", "Origen")], mrs, cols=COLS('ms_id', 'ms_name', 'ms_date', 'ms_status') + [_COL_SRC])}
 <div class="chead"><h3 class="ct">{t("Phases", "Fases")}</h3></div>{_table(["WBS", t("Phase", "Fase"), t("Dates", "Fechas"), t("Resource", "Recurso")], prs, cols=COLS('ph_wbs', 'ph_name', 'ph_dates', 'ph_res'))}
 <div class="chead"><h3 class="ct">{t("Costs", "Costos")}</h3></div>{_table(["ID", t("Date", "Fecha"), t("Category", "Categoría"), t("Vendor", "Proveedor"), t("Description", "Descripción"), t("Net", "Neto"), "Total", "Status", t("Approval", "Aprobación"), t("Source", "Origen")], crs, cols=COLS('c_id', 'c_date', 'c_cat', 'c_vendor', 'c_desc', 'c_net', 'c_total', 'c_status', 'c_appr') + [_COL_SRC], sums=[5, 6])}
 {('<div class="chead"><h3 class="ct">' + t("Invoices", "Facturas") + '</h3></div>' + _table(["ID", t("Milestone", "Hito"), t("Number", "Número"), t("Date", "Fecha"), t("Due", "Vence"), "Total", "Status", t("Source", "Origen")], vrs, cols=COLS('iv_id', 'iv_ms', 'iv_no', 'iv_date', 'iv_due', 'iv_total', 'iv_status') + [_COL_SRC], sums=[5])) if vrs else ''}
</div>'''
    body = f'''<div class="kicker"><a href="/projects/">{t("Projects", "Proyectos")}</a> · <span class="mono">{_e(pid)}</span> · {t("books through", "libros al")} {period}</div>
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{_e(CC.label(code, p["name"]))}</h1>{_tools()}</div>
<div class="muted" style="font-size:14px">{phase_pill(p["phase"])} · {t("BM", "BM")} {_e(p.get("business_manager") or "—")} · PM {_e(p.get("project_manager") or p.get("pmo_manager") or "—")} · {_e(p.get("comment") or "")}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
{pmo_html}
{where_box([{"what": ("Value, planned cost, phase, dates, progress, invoiced and paid", "Valor, costo planeado, fase, fechas, avance, facturado y cobrado"), "pointer": SRC.pointer(p)},
            {"what": ("Project costs — add or correct a cost line here", "Costos del proyecto — aquí se agrega o corrige una línea de costo"), "pointer": SRC.pointer(_tab_of(costs[0]) if costs else _tab_of(tasks[0]) if tasks else None)},
            {"what": ("Milestones, phases and progress", "Hitos, fases y avance"), "pointer": SRC.pointer(_tab_of(tasks[0])) if tasks else None},
            {"what": ("Customer and supplier invoices, due dates, payment dates", "Facturas de cliente y proveedor, vencimientos, fechas de pago"), "pointer": SRC.pointer(_tab_of(items[0])) if items else SRC.pointer(q_source("tracker") or {})},
            {"what": ("Everything booked on this project (revenue, cost, margin)", "Todo lo contabilizado en este proyecto (ingresos, costo, margen)"), "pointer": SRC.pointer(q_source("polizas") or {})}])}
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("In the books", "En libros")} · {t("segment", "segmento")} {code}</h2><span class="muted" style="font-size:12.5px">{t("every posted journal line carrying this project's segment, grouped by account (2026 year to date)", "cada línea de póliza contabilizada con el segmento de este proyecto, agrupada por cuenta (2026 acumulado)")}</span></div>
{_table(["BS/PL", t("Account", "Cuenta"), t("Report line", "Línea de reporte"), t("Debit", "Cargo"), t("Credit", "Abono"), t("Net", "Neto"), "n", t("Dates", "Fechas")], grs, cols=COLS('gl_bspl', 'gl_account', 'gl_report', 'gl_debit', 'gl_credit', 'gl_net', 'gl_n', 'gl_dates'), sums=[3, 4, 5]) if grs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No journal lines carry this segment in 2026.", "Ninguna línea de póliza lleva este segmento en 2026.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Invoices in the tracker", "Facturas en seguimiento")}</h2></div>
{_table(["Status", t("Side", "Lado"), t("Invoice", "Factura"), t("Company", "Empresa"), t("Issued", "Emitida"), t("Due", "Vence"), "Total", t("Paid", "Pagada"), t("Source", "Origen")], irs, cols=COLS('oi_status', 'it_side', 'oi_invoice', 'it_company', 'oi_issued', 'oi_due', 'oi_total', 'oi_paid') + [_COL_SRC], sums=[6]) if irs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No open or recently paid invoice for this project.", "Sin factura abierta o pagada recientemente para este proyecto.")}</p>'}</div>'''
    return PC.page(CC.label(code, p['name']), body, 'projects', '', wide=True)


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


# ------------------------------------------------- display rate (v248)
def books_rate(period: str):
    """USD→MXN for the display conversion: a real fx_rate row when one
    exists (never the demo rows), else the accountants' own month-end
    valuation — the peso complement of the dollar accounts divided by
    their USD face value."""
    r = _rows('fx', "SELECT rate, to_char(rate_date, 'YYYY-MM-DD') AS d FROM fx_rate WHERE pair = 'USD/MXN' AND source <> 'demo' ORDER BY rate_date DESC LIMIT 1;")
    if r and _n(r[0].get('rate')) > 0:
        return _n(r[0]['rate']), ('fx', str(r[0].get('d')))
    if not period:
        return None, ''
    usd = mxn = D(0)
    for v in cash_view(q_cash(period)):
        if v.get('usd') and _n(v.get('usd_amount')) > 0:
            usd += _n(v['usd_amount'])
            mxn += _n(v['mxn'])
    if usd > 0 and mxn > 0:
        return (mxn / usd).quantize(Decimal('0.0001')), ('books', period)
    return None, ''


def _tools() -> str:
    """Print button + the currency switch, top right of every page."""
    return f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">{ccy_switch()}{PC.print_button()}</div>'


def ccy_switch() -> str:
    """The MXN · USD · as-issued switch shown in every finance kicker."""
    def a(v, label):
        return f'<a href="?ccy={v}"{" class=on" if _pref == v else ""}>{label}</a>'
    src = ''
    if _pref and _rate and _rate_src:
        kind, when = _rate_src if isinstance(_rate_src, tuple) else ('books', str(_rate_src))
        src = (t("month-end valuation in the books", "valuación de cierre en libros") if kind == 'books' else t("published rate", "tipo publicado")) + f" {when}"
    note = f' <span class="muted" style="font-size:12px">{t("at", "a")} {_rate:,.4f} MXN/USD · {src}</span>' if _pref and _rate else ''
    return f'<span class="ccysw noprint" title="show every amount in one currency / mostrar todos los importes en una moneda">{a("", t("as issued", "como emitido"))}{a("MXN", "MXN")}{a("USD", "USD")}</span>{note}'


# ------------------------------------------------- cost centres (v248)
def q_cost_by_segment(period: str):
    """Booked cost (PL cost accounts, debit − credit) and revenue per segment, YTD and the period's month."""
    return _rows('cost_seg', f"SELECT l.segment AS code,"
                             f" sum(CASE WHEN a.bs_pl = 'PL' AND a.a_p = 'C' THEN l.debit - l.credit ELSE 0 END) AS cost_ytd,"
                             f" sum(CASE WHEN a.bs_pl = 'PL' AND a.a_p = 'C' AND to_char(j.jdate, 'YYYY-MM') = {_q(period)} THEN l.debit - l.credit ELSE 0 END) AS cost_month,"
                             f" sum(CASE WHEN a.bs_pl = 'PL' AND a.a_p = 'R' THEN l.credit - l.debit ELSE 0 END) AS rev_ytd,"
                             f" count(*) AS n, max(j.jdate) AS last FROM gl_line l"
                             f" JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                             f" LEFT JOIN gl_account a ON a.entity_id = l.entity_id AND a.account = l.account"
                             f" WHERE l.entity_id = {_q(ENTITY)} AND l.segment IS NOT NULL AND j.jdate >= {_q(period[:4] + '-01-01')} GROUP BY 1 ORDER BY 2 DESC;")


def q_open_ap_by_code():
    return _rows('open_ap_code', f"SELECT project_code AS code, currency, sum(total) AS total, count(*) AS n,"
                                 f" sum(CASE WHEN coalesce(days_to_due, 0) < 0 THEN total ELSE 0 END) AS overdue FROM open_item"
                                 f" WHERE entity_id = {_q(ENTITY)} AND side = 'ap' AND paid_on IS NULL GROUP BY 1, 2;")


def q_segment_lines(code: int, limit: int = 600):
    return _rows('seg_lines', f"SELECT j.jdate, j.kind, j.number, j.concept, l.account, coalesce(a.name, l.account_name) AS account_name, a.bs_pl, a.a_p,"
                              f" l.reference, l.debit, l.credit FROM gl_line l"
                              f" JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                              f" LEFT JOIN gl_account a ON a.entity_id = l.entity_id AND a.account = l.account"
                              f" WHERE l.entity_id = {_q(ENTITY)} AND l.segment = {int(code)} ORDER BY j.jdate DESC, j.kind, j.number, l.line_no LIMIT {int(limit)};")


def _ap_cells(open_ap: Dict, code) -> str:
    """Open AP per currency for one code → one cell."""
    parts = [_money(v['total'], ccy) for ccy, v in sorted(open_ap.get(int(code), {}).items())] if code not in (None, '') else []
    return ' · '.join(parts)


def _open_ap_map():
    out: Dict[int, Dict[str, dict]] = {}
    for r in q_open_ap_by_code():
        if r.get('code') in (None, ''):
            continue
        out.setdefault(int(r['code']), {})[r.get('currency') or 'MXN'] = {'total': _n(r['total']), 'n': int(r['n'] or 0), 'overdue': _n(r.get('overdue'))}
    return out


def page_costs() -> str:
    period = q_period()
    segs = q_cost_by_segment(period) if period else []
    ccm = cc_map()
    open_ap = _open_ap_map()
    by_kind: Dict[str, Decimal] = {k: D(0) for k in CC.KINDS}
    total = D(0)
    sal = {'cost_ytd': D(0), 'cost_month': D(0), 'n': 0, 'codes': 0}
    rows_html = []
    seen = set()
    for r in segs:
        code = int(r['code'])
        seen.add(code)
        c = ccm.get(code)
        kind = c['kind'] if c else cc_kind(code)
        cy, cm = _n(r['cost_ytd']), _n(r['cost_month'])
        by_kind[kind] += cy
        total += cy
        if kind == CC.PAYROLL:
            sal['cost_ytd'] += cy
            sal['cost_month'] += cm
            sal['n'] += int(r['n'] or 0)
            sal['codes'] += 1
            continue
        rows_html.append((cy, code, c, kind, cm, _n(r['rev_ytd']), int(r['n'] or 0), r.get('last')))
    # open AP on codes with no booked cost yet (a fresh project, or a code the books do not use)
    for code, per in open_ap.items():
        if code in seen:
            continue
        c = ccm.get(code)
        kind = c['kind'] if c else cc_kind(code)
        if kind == CC.PAYROLL:
            continue
        rows_html.append((D(0), code, c, kind, D(0), D(0), 0, None))
    out_rows = []
    for cy, code, c, kind, cm, rv, n, last in rows_html:
        share = f"{cy / total * 100:.1f}%" if total else ''
        out_rows.append((cy, f'<tr><td style="min-width:280px">{_proj(code, c["name"] if c else "")}</td><td>{pill("ok" if kind == CC.PROJECT else "warn" if kind == CC.OVERHEAD else "off", *CC.KIND_LABEL[kind])}</td>'
                             f'<td class="r"><b>{_money(cy)}</b></td><td class="r">{_money(cm)}</td><td class="r">{share}</td>'
                             f'<td class="r">{_money(rv)}</td><td class="r">{_ap_cells(open_ap, code)}</td><td class="r">{n}</td>{_dt(last)}</tr>'))
    rows_html = out_rows
    if sal['codes']:
        sal_ap: Dict[str, Decimal] = {}
        for code, per in open_ap.items():
            if cc_kind(code) == CC.PAYROLL:
                for ccy, v in per.items():
                    sal_ap[ccy] = sal_ap.get(ccy, D(0)) + v['total']
        sal_share = f"{sal['cost_ytd'] / total * 100:.1f}%" if total else ''
        rows_html.append((sal['cost_ytd'], f'<tr><td style="min-width:280px"><a href="/finance/costs/salaries/"><b>{t("SALARIES & FEES", "SUELDOS Y HONORARIOS")}</b></a> <span class="muted">({sal["codes"]} {t("people", "personas")})</span></td><td>{pill("off", *CC.KIND_LABEL[CC.PAYROLL])}</td>'
                                            f'<td class="r"><b>{_money(sal["cost_ytd"])}</b></td><td class="r">{_money(sal["cost_month"])}</td><td class="r">{sal_share}</td>'
                                            f'<td class="r">{_money(0)}</td><td class="r">{" · ".join(_money(v, k) for k, v in sorted(sal_ap.items()))}</td><td class="r">{sal["n"]}</td><td class="nw"></td></tr>'))
    rows_html.sort(key=lambda x: -x[0])
    tiles = tile('Booked cost YTD', 'Costo contabilizado YTD', _money(total), f"every PL cost line with a segment · {period}", f"cada línea de costo con segmento · {period}")
    for k in (CC.PROJECT, CC.OVERHEAD, CC.PAYROLL, CC.WARRANTY, CC.OTHER):
        if by_kind[k]:
            tiles += tile(*CC.KIND_LABEL[k], _money(by_kind[k]), f"{by_kind[k] / total * 100:.0f}% of the booked cost" if total else '', f"{by_kind[k] / total * 100:.0f}% del costo contabilizado" if total else '',
                          tone=('warn' if k == CC.OVERHEAD else ''))
    cols = [("The cost centre: a project (ARGnnnn, from the projects overview) or an internal code the accountants use as the póliza segment — offices, operation costs, the warehouse, warranties. Salaries and fees are one line whatever the person's code.",
             "El centro de costo: un proyecto (ARGnnnn, del overview) o un código interno que contabilidad usa como segmento de póliza — oficinas, costos de operación, almacén, garantías. Sueldos y honorarios son una sola línea sin importar el código de la persona."),
            ("Project, overhead, salaries & fees, warranties, other — set automatically from the accountants' project list; fin_cost_centers.py --set fixes a wrong one.", "Proyecto, gastos generales, sueldos y honorarios, garantías, otros — asignado automáticamente de la lista de proyectos de contabilidad; fin_cost_centers.py --set corrige uno equivocado."),
            ("Cost booked on P&L cost accounts (classes 5–7, debit − credit) carrying this segment, year to date.", "Costo contabilizado en cuentas de resultados (clases 5–7, cargo − abono) con este segmento, acumulado del año."),
            ("The same, for the last closed month only.", "Lo mismo, solo el último mes cerrado."),
            ("Share of the total booked cost.", "Participación en el costo total contabilizado."),
            ("Revenue booked with the same segment (projects only).", "Ingresos contabilizados con el mismo segmento (solo proyectos)."),
            ("Supplier invoices still open in the tracker for this code, per currency.", "Facturas de proveedor aún abiertas en el seguimiento para este código, por moneda."),
            ("Posted journal lines carrying the segment.", "Líneas de póliza contabilizadas con el segmento."), ("Date of the last booked line.", "Fecha de la última línea contabilizada.")]
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Where the money goes", "A dónde va el dinero")}</h1></div>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Cost centres", "Centros de costo")}</h2><span class="muted" style="font-size:12.5px">{t("one line per CONTPAQi segment with booked cost or open supplier invoices; filter by kind", "una línea por segmento CONTPAQi con costo contabilizado o facturas de proveedor abiertas; filtre por tipo")}</span></div>
{_table([t("Cost centre", "Centro de costo"), t("Kind", "Tipo"), t("Cost YTD", "Costo YTD"), t("This month", "Este mes"), t("Share", "Parte"), t("Revenue YTD", "Ingresos YTD"), t("Open AP", "Por pagar"), t("Lines", "Líneas"), t("Last", "Última")], [r[1] for r in rows_html], cols=cols, sums=[2, 3, 5, 6])}</div>
<p class="note">{t("Cost = what the accountants booked on P&L cost accounts with the segment; it is not cash out. Projects link to their project page; internal codes to their own page with every journal line. The catalogue (kinds, the salaries group) is rebuilt from the accountants' project list at every books ingest; a kind set by hand survives.",
"Costo = lo que contabilidad registró en cuentas de resultados con el segmento; no es salida de efectivo. Los proyectos llevan a su página; los códigos internos a la suya con cada línea de póliza. El catálogo (tipos, el grupo de sueldos) se reconstruye de la lista de proyectos de contabilidad en cada carga; un tipo fijado a mano se conserva.")}</p>'''
    return PC.page('Cost centres', body, 'finance', 'costs', wide=True)


def _lines_table(lines, with_account=True, sums=None) -> str:
    trs = [f'<tr>{_dt(l["jdate"])}<td class="nw">{_e(l["kind"])} {_e(l["number"])}</td>' + (f'<td class="mono" style="font-size:12px">{_e(l["account"])}</td><td>{_nm(l.get("account_name"))}</td>' if with_account else '')
           + f'<td>{_e(l["concept"])}</td><td class="mono" style="font-size:11px">{_e(l.get("reference") or "")}</td>'
           f'<td class="r">{_money(l["debit"], "", 2) if _n(l["debit"]) else ""}</td><td class="r">{_money(l["credit"], "", 2) if _n(l["credit"]) else ""}</td></tr>' for l in lines]
    head = [t("Date", "Fecha"), t("Póliza", "Póliza")] + ([t("Account", "Cuenta"), t("Account name", "Nombre de cuenta")] if with_account else []) + [t("Concept", "Concepto"), t("Reference", "Referencia"), t("Debit", "Cargo"), t("Credit", "Abono")]
    cols = [_COL.get('bl_date'), _COL.get('bl_poliza')] + ([_COL.get('bk_account'), ("The account's name in the books.", "El nombre de la cuenta en libros.")] if with_account else []) + [_COL.get('bl_concept'), _COL.get('bl_ref'), _COL.get('gl_debit'), _COL.get('gl_credit')]
    n = len(head)
    return _table(head, trs, cols=cols, sums=sums or [n - 2, n - 1])


def page_cost_center(code_s: str) -> Optional[str]:
    period = q_period()
    ccm = cc_map()
    if code_s == 'salaries':
        segs = {int(r['code']): r for r in (q_cost_by_segment(period) if period else [])}
        open_ap = _open_ap_map()
        trs = []
        tot = D(0)
        for code, c in sorted(ccm.items()):
            if c['kind'] != CC.PAYROLL:
                continue
            r = segs.get(code, {})
            cy = _n(r.get('cost_ytd'))
            tot += cy
            trs.append(f'<tr><td>{_proj(code, c["name"])}</td><td class="r"><b>{_money(cy)}</b></td><td class="r">{_money(_n(r.get("cost_month")))}</td><td class="r">{_ap_cells(open_ap, code)}</td><td class="r">{_e(r.get("n") or 0)}</td>{_dt(r.get("last"))}</tr>')
        body = f'''<div class="kicker"><a href="/finance/costs/">{t("Cost centres", "Centros de costo")}</a> · {t("books through", "libros al")} {period}</div>
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{t("Salaries & fees", "Sueldos y honorarios")}</h1>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tile("Booked YTD", "Contabilizado YTD", _money(tot), f"{len(trs)} people with a segment", f"{len(trs)} personas con segmento")}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Per person", "Por persona")}</h2><span class="muted" style="font-size:12.5px">{t("payroll (706) and every personal segment the accountants use for fees; click a name for the journal lines", "nómina (706) y cada segmento personal que contabilidad usa para honorarios; clic en un nombre para las líneas de póliza")}</span></div>
{_table([t("Person / code", "Persona / código"), t("Cost YTD", "Costo YTD"), t("This month", "Este mes"), t("Open AP", "Por pagar"), t("Lines", "Líneas"), t("Last", "Última")], trs, cols=[("The person's segment in the books.", "El segmento de la persona en libros."), _COL.get('cos_ytd'), ("Last closed month only.", "Solo el último mes cerrado."), ("Open supplier invoices in the tracker on this code.", "Facturas abiertas en el seguimiento con este código."), ("Posted journal lines.", "Líneas contabilizadas."), ("Last booked line.", "Última línea contabilizada.")], sums=[1, 2, 3])}</div>'''
        return PC.page('Salaries', body, 'finance', 'costs', wide=True)
    if not code_s.isdigit():
        return None
    code = int(code_s)
    c = ccm.get(code)
    kind = c['kind'] if c else cc_kind(code)
    if kind == CC.PROJECT and (c or q_project(code)):
        return f'REDIRECT:/projects/ARG{code:04d}/'
    lines = q_segment_lines(code)
    if not lines and not c:
        return None
    seg = next((r for r in q_cost_by_segment(period) if int(r['code']) == code), {}) if period else {}
    items = q_project_items(code)
    open_ap = _open_ap_map()
    irs = [f'<tr><td>{pill("ok" if it.get("paid_on") else "crit" if (it.get("status") or "").lower() == "delay" else "warn", (it.get("status") or ""))}</td><td>{_e(it["side"]).upper()}</td><td class="mono" style="font-size:12px">{_e(it.get("invoice"))}</td><td>{_nm(it.get("company"))}</td>'
           f'{_dt(it.get("issued"))}{_dt(it.get("final_due") or it.get("due"))}<td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td>{_dt(it.get("paid_on"))}'
           f'<td class="nw">{src_link(it, "Payment Due Day", str(it.get("final_due") or it.get("due") or ""))}</td></tr>' for it in items]
    name = c['name'] if c else ''
    body = f'''<div class="kicker"><a href="/finance/costs/">{t("Cost centres", "Centros de costo")}</a> · <span class="mono">{code}</span> · {t("books through", "libros al")} {period}</div>
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{_e(CC.label(code, name, kind))}</h1>{_tools()}</div>
<div class="muted" style="font-size:14px">{pill("warn" if kind == CC.OVERHEAD else "off", *CC.KIND_LABEL.get(kind, ("Other", "Otros")))}{" · " + t("kind set by hand", "tipo fijado a mano") if c and c.get("manual") in (True, "t", "true") else ""}</div>
<div class="tiles" style="margin-top:16px">{tile("Cost YTD", "Costo YTD", _money(_n(seg.get("cost_ytd"))), f"{seg.get('n') or 0} journal lines", f"{seg.get('n') or 0} líneas de póliza")}{tile("This month", "Este mes", _money(_n(seg.get("cost_month"))), period, period)}{tile("Open AP", "Por pagar", _ap_cells(open_ap, code) or "—", "supplier invoices open in the tracker", "facturas de proveedor abiertas en el seguimiento")}</div>
{where_box([{"what": ("Supplier invoices on this code, due dates, payment dates", "Facturas de proveedor con este código, vencimientos, fechas de pago"), "pointer": SRC.pointer(_tab_of(items[0])) if items else SRC.pointer(q_source("tracker") or {})},
            {"what": ("Everything booked on this cost centre", "Todo lo contabilizado en este centro de costo"), "pointer": SRC.pointer(q_source("polizas") or {})},
            {"what": ("Which cost centre this code is, and its name", "Qué centro de costo es este código, y su nombre"), "pointer": SRC.pointer(q_source("acctbook") or {})}])}
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Journal lines", "Líneas de póliza")} · {t("segment", "segmento")} {code}</h2><span class="muted" style="font-size:12.5px">{t("every posted line carrying this segment (all accounts, newest first, up to 600)", "cada línea contabilizada con este segmento (todas las cuentas, la más reciente primero, hasta 600)")}</span></div>
{_lines_table(lines) if lines else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No journal lines carry this segment.", "Ninguna línea de póliza lleva este segmento.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Invoices in the tracker", "Facturas en seguimiento")}</h2></div>
{_table(["Status", t("Side", "Lado"), t("Invoice", "Factura"), t("Company", "Empresa"), t("Issued", "Emitida"), t("Due", "Vence"), "Total", t("Paid", "Pagada"), t("Source", "Origen")], irs, cols=COLS('oi_status', 'it_side', 'oi_invoice', 'it_company', 'oi_issued', 'oi_due', 'oi_total', 'oi_paid') + [_COL_SRC], sums=[6]) if irs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No invoice in the tracker for this code.", "Sin factura en seguimiento para este código.")}</p>'}</div>'''
    return PC.page(CC.label(code, name, kind), body, 'finance', 'costs', wide=True)


# ------------------------------------------------------ suppliers (v248)
def q_suppliers(period: str):
    return _rows('suppliers', f"SELECT b.account, b.name, b.opening, b.debits, b.credits, b.closing, b.movements,"
                              f" (SELECT max(j.jdate) FROM gl_line l JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                              f"   WHERE l.entity_id = b.entity_id AND l.account = b.account) AS last"
                              f" FROM gl_balance b WHERE b.entity_id = {_q(ENTITY)} AND b.period = {_q(period)} AND b.account LIKE '201-01-%'"
                              f" AND (b.closing <> 0 OR b.movements > 0) ORDER BY b.closing DESC;")


def q_account_lines(account: str, limit: int = 600):
    return _rows('acct_lines', f"SELECT j.jdate, j.kind, j.number, j.concept, l.reference, l.debit, l.credit, l.segment FROM gl_line l"
                               f" JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                               f" WHERE l.entity_id = {_q(ENTITY)} AND l.account = {_q(account)} ORDER BY j.jdate DESC, j.kind, j.number, l.line_no LIMIT {int(limit)};")


def supplier_index(suppliers: List[dict]) -> Dict[str, str]:
    """party key → the group's first account, for linking tracker companies to their supplier."""
    return {g['key']: g['accounts'][0] for g in supplier_groups(suppliers)}


def match_account(idx: Dict[str, str], company: str) -> str:
    k = party_key(company)
    if not k:
        return ''
    if k in idx:
        return idx[k]
    head = ' '.join(k.split()[:2])
    hits = [a for kk, a in idx.items() if kk.startswith(head) or head.startswith(kk)]
    return hits[0] if len(hits) == 1 else ''


def page_suppliers() -> str:
    period = q_period()
    sup = q_suppliers(period) if period else []
    idx = supplier_index(sup)
    open_items = [it for it in q_open_items('ap') if not it.get('paid_on')]
    per_acct: Dict[str, Dict[str, dict]] = {}
    unmatched: Dict[str, Dict[str, dict]] = {}
    for it in open_items:
        acct = match_account(idx, it.get('company') or '')
        tgt = per_acct.setdefault(acct, {}) if acct else unmatched.setdefault(CC.canon(it.get('company')), {})
        ccy = it.get('currency') or 'MXN'
        s_ = tgt.setdefault(ccy, {'total': D(0), 'overdue': D(0), 'n': 0})
        s_['total'] += _n(it['total'])
        s_['n'] += 1
        if int(it.get('days_to_due') or 0) < 0:
            s_['overdue'] += _n(it['total'])
    groups = supplier_groups(sup)
    tot: Dict[str, Dict[str, Decimal]] = {'closing': {}, 'credits': {}, 'debits': {}}
    for g in groups:
        for f in tot:
            for k, v in g[f].items():
                tot[f][k] = tot[f].get(k, D(0)) + v
    trs = []
    for g in groups:
        o = per_acct.get(g['accounts'][0], {})
        trs.append(f'<tr><td class="mono nw" style="font-size:12px">{"<br>".join(_e(a) for a in g["accounts"])}</td><td style="min-width:220px"><a href="/finance/suppliers/{_e(g["accounts"][0])}/"><b>{_e(g["name"])}</b></a></td>'
                   f'<td class="r">{_per_ccy(g["closing"], True)}</td><td class="r">{_per_ccy(g["credits"])}</td><td class="r">{_per_ccy(g["debits"])}</td>'
                   f'<td class="r">{" · ".join(_money(v["total"], k) for k, v in sorted(o.items()))}</td><td class="r">{" · ".join(_money(v["overdue"], k) for k, v in sorted(o.items()) if v["overdue"])}</td>'
                   f'<td class="r">{g["movements"]}</td>{_dt(g["last"])}</tr>')
    urs = [f'<tr><td>{_e(name)}</td><td class="r">{" · ".join(_money(v["total"], k) for k, v in sorted(per.items()))}</td><td class="r">{" · ".join(_money(v["overdue"], k) for k, v in sorted(per.items()) if v["overdue"])}</td><td class="r">{sum(v["n"] for v in per.values())}</td></tr>'
           for name, per in sorted(unmatched.items())]
    tiles = (tile('Owed to suppliers (books)', 'Debido a proveedores (libros)', _per_ccy(tot['closing']), f"{len(groups)} suppliers ({len(sup)} sub-accounts) with a balance or movements · {period} · USD at face value", f"{len(groups)} proveedores ({len(sup)} subcuentas) con saldo o movimientos · {period} · USD a valor nominal")
             + tile('Purchases booked YTD', 'Compras contabilizadas YTD', _per_ccy(tot['credits']), 'credits on the supplier sub-accounts', 'abonos en las subcuentas de proveedor')
             + tile('Paid YTD', 'Pagado YTD', _per_ccy(tot['debits']), 'debits on the supplier sub-accounts', 'cargos en las subcuentas de proveedor')
             + tile('Open in the tracker', 'Abierto en seguimiento', str(len(open_items)), f"{sum(sum(v['n'] for v in p.values()) for p in unmatched.values())} not matched to a sub-account", f"{sum(sum(v['n'] for v in p.values()) for p in unmatched.values())} sin subcuenta identificada"))
    cols = [("The supplier's sub-accounts under 201-01: a peso account, and for dollar suppliers a USD account (face value) plus its peso complement (COMP/COMPL).", "Las subcuentas del proveedor bajo 201-01: una en pesos y, para proveedores en dólares, una USD (valor nominal) más su complemento en pesos (COMP/COMPL)."),
            ("The supplier, one line for all its sub-accounts.", "El proveedor, una línea para todas sus subcuentas."),
            ("What ARGIA owes this supplier at the period end according to the books, per currency (the peso complement counts as MXN).", "Lo que ARGIA debe a este proveedor al cierre según libros, por moneda (el complemento en pesos cuenta como MXN)."),
            ("Invoices booked against the supplier year to date (credits).", "Facturas contabilizadas al proveedor en el año (abonos)."), ("Payments booked year to date (debits).", "Pagos contabilizados en el año (cargos)."),
            ("Open supplier invoices in the tracker, matched to this supplier by name, per currency.", "Facturas abiertas en el seguimiento, identificadas por nombre, por moneda."),
            ("Of which past the final due date.", "De ellas, vencidas."), ("Journal lines in the year.", "Líneas de póliza en el año."), ("Last booked line.", "Última línea contabilizada.")]
    body = f'''<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><div>{_kicker(period)}<h1 class="pt">{t("Suppliers", "Proveedores")}</h1></div>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tiles}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Balance and activity per supplier", "Saldo y actividad por proveedor")} · {period}</h2><span class="muted" style="font-size:12.5px">{t("books (CONTPAQi auxiliares 201-01) + the open items of the tracker; click a supplier for every transaction", "libros (auxiliares CONTPAQi 201-01) + partidas abiertas del seguimiento; clic en un proveedor para cada transacción")}</span></div>
{_table([t("Account", "Cuenta"), t("Supplier", "Proveedor"), t("Balance", "Saldo"), t("Purchases YTD", "Compras YTD"), t("Paid YTD", "Pagado YTD"), t("Open (tracker)", "Abierto (seguimiento)"), t("Overdue", "Vencido"), t("Lines", "Líneas"), t("Last", "Última")], trs, cols=cols, sums=[2, 3, 4, 5, 6])}</div>
{('<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">' + t("Open in the tracker, no sub-account found", "Abiertas en seguimiento, sin subcuenta identificada") + '</h2><span class="muted" style="font-size:12.5px">' + t("the tracker spells the company differently from the books, or the accountants have not booked it yet", "el seguimiento escribe la empresa distinto que libros, o contabilidad aún no la registra") + '</span></div>' + _table([t("Company (tracker)", "Empresa (seguimiento)"), t("Open", "Abierto"), t("Overdue", "Vencido"), t("Items", "Partidas")], urs, cols=[("The name as typed in the tracker.", "El nombre como está en el seguimiento."), _COL.get('oi_total'), ("Past the final due date.", "Vencidas."), ("Number of open invoices.", "Número de facturas abiertas.")], sums=[1, 2]) + '</div>') if urs else ''}'''
    return PC.page('Suppliers', body, 'finance', 'suppliers', wide=True)


def page_supplier(account: str) -> Optional[str]:
    if not re.match(r'^201-01-\d{3}$', account or ''):
        return None
    period = q_period()
    sup = q_suppliers(period) if period else []
    g = next((x for x in supplier_groups(sup) if account in x['accounts']), None)
    if g is None:
        return None
    lines = []
    for a in g['accounts']:
        for l in q_account_lines(a):
            lines.append(dict(l, account=a, ccy=_acct_ccy(next(s_['name'] for s_ in sup if s_['account'] == a))))
    lines.sort(key=lambda l: (str(l['jdate']), l['kind'], str(l['number'])), reverse=True)
    idx = supplier_index(sup)
    items = [it for it in q_open_items('ap') if match_account(idx, it.get('company') or '') == g['accounts'][0]]
    irs = []
    for it in items:
        days = '' if it.get('paid_on') else f"{int(it.get('days_to_due') or 0):+d}"
        irs.append(f'<tr><td>{pill("ok" if it.get("paid_on") else "crit" if (it.get("status") or "").lower() == "delay" else "warn", (it.get("status") or ""))}</td><td class="mono" style="font-size:12px">{_e(it.get("invoice"))}</td><td>{_proj(it.get("project_code"), it.get("project_name"))}</td>'
                   f'{_dt(it.get("issued"))}{_dt(it.get("final_due") or it.get("due"))}<td class="r">{days}</td><td class="r"><b>{_money(it.get("total"), it.get("currency") or "MXN")}</b></td>{_dt(it.get("paid_on"))}<td class="mono" style="font-size:11px">{_e((it.get("folio_fiscal") or "")[:8])}{doc_link(it)}</td></tr>')
    trs = [f'<tr>{_dt(l["jdate"])}<td class="nw">{_e(l["kind"])} {_e(l["number"])}</td><td class="mono nw" style="font-size:11px">{_e(l["account"])}</td><td>{_e(l["concept"])}</td><td class="mono" style="font-size:11px">{_e(l.get("reference") or "")}</td>'
           f'<td class="r">{_money(l["credit"], l["ccy"], 2) if _n(l["credit"]) else ""}</td><td class="r">{_money(l["debit"], l["ccy"], 2) if _n(l["debit"]) else ""}</td><td>{_proj(l.get("segment"))}</td></tr>' for l in lines]
    s_ = {'name': g['name']}
    body = f'''<div class="kicker"><a href="/finance/suppliers/">{t("Suppliers", "Proveedores")}</a> · <span class="mono">{_e(", ".join(g["accounts"]))}</span> · {t("books through", "libros al")} {period}</div>
<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap"><h1 class="pt">{_nm(s_["name"]) or _e(account)}</h1>{_tools()}</div>
<div class="tiles" style="margin-top:16px">{tile("Balance in the books", "Saldo en libros", _per_ccy(g["closing"]), f"at {period} · sub-accounts {', '.join(g['accounts'])}", f"al {period} · subcuentas {', '.join(g['accounts'])}")}{tile("Purchases YTD", "Compras YTD", _per_ccy(g["credits"]), "credits", "abonos")}{tile("Paid YTD", "Pagado YTD", _per_ccy(g["debits"]), "debits", "cargos")}{tile("Open in the tracker", "Abierto en seguimiento", str(len([i for i in items if not i.get("paid_on")])), f"{len(items)} items matched by name", f"{len(items)} partidas identificadas por nombre")}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Invoices in the tracker", "Facturas en seguimiento")}</h2></div>
{_table(["Status", t("Invoice", "Factura"), t("Project / cost centre", "Proyecto / centro de costo"), t("Issued", "Emitida"), t("Due", "Vence"), t("Days", "Días"), "Total", t("Paid", "Pagada"), "Folio"], irs, cols=COLS('oi_status', 'oi_invoice', 'oi_project', 'oi_issued', 'oi_due', 'oi_days', 'oi_total', 'oi_paid', 'oi_folio'), sums=[6]) if irs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No tracker row matches this supplier by name.", "Ninguna fila del seguimiento coincide con este proveedor por nombre.")}</p>'}</div>
<div class="card" style="margin-top:16px;overflow:hidden"><div class="chead"><h2 class="ct">{t("Every transaction in the books", "Cada transacción en libros")}</h2><span class="muted" style="font-size:12.5px">{t("all sub-accounts · invoices = credits, payments = debits; USD accounts at face value; newest first, up to 600 per account", "todas las subcuentas · facturas = abonos, pagos = cargos; cuentas USD a valor nominal; la más reciente primero, hasta 600 por cuenta")}</span></div>
{_table([t("Date", "Fecha"), t("Póliza", "Póliza"), t("Account", "Cuenta"), t("Concept", "Concepto"), t("Reference", "Referencia"), t("Invoiced", "Facturado"), t("Paid", "Pagado"), t("Project / cost centre", "Proyecto / centro de costo")], trs, cols=[_COL.get('bl_date'), _COL.get('bl_poliza'), _COL.get('bk_account'), _COL.get('bl_concept'), _COL.get('bl_ref'), ("Credit on the supplier account = an invoice booked.", "Abono en la cuenta del proveedor = factura registrada."), ("Debit = a payment (or credit note) booked.", "Cargo = pago (o nota de crédito) registrado."), _COL.get('bl_project')], sums=[5, 6]) if trs else f'<p class="muted" style="padding:16px 20px;margin:0">{t("No journal lines on this account.", "Sin líneas de póliza en esta cuenta.")}</p>'}</div>'''
    return PC.page(g['name'] or account, body, 'finance', 'suppliers', wide=True)
