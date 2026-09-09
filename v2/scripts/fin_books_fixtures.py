#!/usr/bin/env python3
"""v245 — synthetic BOOKS in the exact layouts the accountants export, so
the readers (argia/fin/contpaq, acctbook, portfolio, pmo_sheet) and the
Drive ingest are tested without a byte of the real books in the public
repository. Called from scripts/fin_fixtures.py (build/--check); can be
run alone to write tests/fixtures/fin/books/.

Layouts reproduced (row for row, as openpyxl yields them):
  polizas_rows.json     CONTPAQi 'Diarios y Pólizas' print   (header block, póliza headers, lines, control rows, day totals)
  auxiliares_rows.json  CONTPAQi 'Movimientos auxiliares'    (derived FROM the pólizas: per account, opening, running balance)
  acctbook_sheets.json  the accountants' workbook              (Cover, Setup, Mapping, Projects, PL, BS, PL_Budget, GM_per_Projects, Bank Loans, Balanza)
  overview_rows.json    Argia_Projects_Overview_MX.xlsx / Data (partial header block, then the full header, then rows)
  tracker_rows.json     Payables and receivables / the tracker (RECEIVABLES block, PAYABLES block)
  pmo_tabs.json         one ARGIA PROJECT workbook             ({tab: rows} as the Sheets API returns them)

One póliza (2026-02-25 Egresos 99) is in the pólizas print but NOT in the
auxiliares — the real books had exactly that (an unposted entry) and the
cross-check must report it, never crash.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Dict, List

V2 = Path(__file__).resolve().parents[1]
OUT = V2 / "tests" / "fixtures" / "fin" / "books"

ENTITY = "ARGIA DEMO"
RFC = "DEM150101AB1"
MONTHS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

ACCOUNTS = {   # account -> (name, BS/PL, A/P/R/C, report code, report account)
    "102-01-001": ("Banorte MXN 9001", "BS", "A", "BS_100", "Cash"),
    "102-01-002": ("Banorte DLLS 9002", "BS", "A", "BS_100", "Cash"),
    "102-01-003": ("Banorte DLLS 9002 Compl", "BS", "A", "BS_100", "Cash"),
    "105-01-001": ("CLIENTE ALFA SA DE CV", "BS", "A", "BS_060", "Receivables"),
    "105-01-002": ("CLIENTE BETA USD", "BS", "A", "BS_060", "Receivables"),
    "119-01-000": ("IVA acreditable", "BS", "A", "BS_080", "VAT Receivables"),
    "201-01-001": ("PROVEEDOR GAMMA SA DE CV", "BS", "P", "BS_120", "Trade Payables"),
    "201-01-002": ("PROVEEDOR DELTA SA DE CV", "BS", "P", "BS_120", "Trade Payables"),
    "209-01-000": ("IVA trasladado", "BS", "P", "BS_140", "VAT Payables"),
    "401-03-000": ("Ventas proyectos", "PL", "R", "PL_040", "Revenues"),
    "501-11-000": ("Costo de ventas materiales", "PL", "C", "PL_050", "Cost of Sales"),
    "601-49-001": ("Viáticos", "PL", "C", "PL_120", "Travel"),
}
PROJECTS = {701: ("OPERATION COSTS", "_", "_"), 9001: ("Solar Capex Roof 300 kWp Demo Uno", "GM", "Tomasz Zemelka"),
            9002: ("Lighting Capex Demo Dos", "GM", "Eduardo Fraga"), 9003: ("Solar PPA Demo Tres", "LAAS", "Daniel Mascareño")}

# (date, kind, number, concept, [(account, reference, segment, debit, credit)])
POLIZAS = [
    ("2026-01-15", "Diario", 1, "FACTURA 2001 CLIENTE ALFA", [("105-01-001", "F-2001", 9001, "1160000.00", ""), ("401-03-000", "F-2001", 9001, "", "1000000.00"), ("209-01-000", "F-2001", 9001, "", "160000.00")]),
    ("2026-01-20", "Diario", 2, "FACTURA A-77 PROVEEDOR GAMMA", [("501-11-000", "A-77", 9001, "600000.00", ""), ("119-01-000", "A-77", 9001, "96000.00", ""), ("201-01-001", "A-77", 9001, "", "696000.00")]),
    ("2026-01-28", "Ingresos", 1, "COBRO CLIENTE ALFA", [("102-01-001", "SPEI 4411", 9001, "580000.00", ""), ("105-01-001", "SPEI 4411", 9001, "", "580000.00")]),
    ("2026-02-03", "Egresos", 1, "PAGO PROVEEDOR GAMMA", [("201-01-001", "SPEI 5120", 9001, "348000.00", ""), ("102-01-001", "SPEI 5120", 9001, "", "348000.00")]),
    ("2026-02-10", "Diario", 3, "FACTURA 2002 CLIENTE BETA USD", [("105-01-002", "F-2002", 9002, "20000.00", ""), ("401-03-000", "F-2002", 9002, "", "20000.00")]),
    ("2026-02-14", "Egresos", 2, "VIATICOS LEON", [("601-49-001", "UBER", 701, "1450.50", ""), ("102-01-001", "UBER", 701, "", "1450.50")]),
    ("2026-02-20", "Ingresos", 2, "COBRO CLIENTE BETA USD", [("102-01-002", "WIRE 77", 9002, "20000.00", ""), ("102-01-003", "WIRE 77", 9002, "348000.00", ""),
                                                          ("105-01-002", "WIRE 77", 9002, "", "20000.00"), ("401-03-000", "TC 18.40", 9002, "", "348000.00")]),
    ("2026-02-25", "Egresos", 99, "PAGO DUPLICADO (NO AFECTADA)", [("201-01-002", "SPEI 9999", 9002, "50000.00", ""), ("102-01-001", "SPEI 9999", 9002, "", "50000.00")]),
]
UNPOSTED = {"2026-02-25:Egresos:99"}
OPENING = {"102-01-001": "250000.00", "102-01-002": "5000.00", "102-01-003": "87000.00", "105-01-001": "120000.00", "201-01-002": "50000.00"}
PERIOD_FROM, PERIOD_TO, PRINTED = "01/Ene/2026", "28/Feb/2026", "21/Mar/2026"


def _es(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day:02d}/{MONTHS_ES[d.month - 1]}/{d.year}"


def _f(s: str):
    return float(s) if s else None


def polizas_rows() -> List[list]:
    rows = [["CONTPAQi®", None, None, ENTITY, None, None, None, "Hoja:      1"],
            [f"Impreso de pólizas del {PERIOD_FROM} al {PERIOD_TO}", None, None, None, None, None, None, f"Fecha: {PRINTED}"],
            ["Moneda: Peso Mexicano"], ["Dirección:  "], [f"Reg. Fed.: {RFC}"], [None],
            ["Fecha", "Tipo", "Número", "C o n c e p t o", "Clase", "Diario"],
            ["No.", "Refer.", "C u e n t a", "N o m b r e", "Diario", "Seg.", "C a r g o s", "A b o n o s"], [None]]
    for date, kind, num, concept, lines in POLIZAS:
        rows.append([_es(date), kind, float(num), concept, None, None, None])
        td = tc = Decimal(0)
        for i, (acct, ref, seg, dr, cr) in enumerate(lines, 1):
            rows.append([float(i), ref, acct, ACCOUNTS[acct][0], None, float(seg) if seg else None, _f(dr), _f(cr)])
            td += Decimal(dr or 0)
            tc += Decimal(cr or 0)
        rows.append([None, "Cifra de Control", 20402008.0, None, None, "Total póliza :", float(td), float(tc)])
        rows.append([" "])
        rows.append([None, None, None, None, None, f"Total al {_es(date)} :", float(td), float(tc)])
        rows.append(["      Total de pólizas impresas: 1"])
        rows.append([" "])
    return rows


def auxiliares_rows() -> List[list]:
    rows = [["CONTPAQ i", None, None, ENTITY, None, None, None, "Hoja:      1"],
            ["Movimientos, Auxiliares del catálogo", None, None, None, None, None, None, f"Fecha: {PRINTED}"],
            [f"del {PERIOD_FROM} al {PERIOD_TO}"], ["Moneda: Peso Mexicano"], [None],
            ["C u e n ta", "N o m b r e", None, None, None, None, None, "Saldo Inicial"],
            ["Fecha", "Tipo", "Número ", "Concepto", "Referencia", "Cargos", "Abonos", "Saldo"], [None]]
    per_acct: Dict[str, list] = {a: [] for a in ACCOUNTS}
    for date, kind, num, concept, lines in POLIZAS:
        if f"{date}:{kind}:{num}" in UNPOSTED:
            continue
        for acct, ref, _seg, dr, cr in lines:
            per_acct[acct].append((date, kind, num, concept, ref, dr, cr))
    for acct, (name, bs_pl, ap, _rc, _ra) in ACCOUNTS.items():
        movs = per_acct[acct]
        opening = Decimal(OPENING.get(acct, "0"))
        if not movs and not opening:
            continue
        rows.append([acct, name, None, None, None, None, "Saldo inicial :", float(opening)])
        bal = opening
        sign = -1 if ap in ("P", "R") else 1
        for date, kind, num, concept, ref, dr, cr in sorted(movs):
            bal += sign * (Decimal(dr or 0) - Decimal(cr or 0))
            rows.append([_es(date), kind, float(num), concept, ref, _f(dr), _f(cr), float(bal)])
        rows.append([None, None, None, None, "Total:", float(sum(Decimal(m[5] or 0) for m in movs)), float(sum(Decimal(m[6] or 0) for m in movs)), float(bal)])
        rows.append([" "])
    return rows


def balanza_rows() -> List[list]:
    rows = [["CONTPAQ i", None, None, ENTITY, None, None, None, "Hoja:      1"], [f"Balanza de comprobación al {PERIOD_TO}"], [None], [None],
            ["C u e n t a", "N o m b r e", "Saldos", "Iniciales", None, None, "Saldos", "Actuales"],
            [None, None, "Deudor", "Acreedor", "Cargos", "Abonos", "Deudor", "Acreedor"], [None]]
    for acct, (name, bs_pl, ap, _rc, _ra) in ACCOUNTS.items():
        dr = cr = Decimal(0)
        for date, kind, num, _c, lines in POLIZAS:
            if f"{date}:{kind}:{num}" in UNPOSTED:
                continue
            for a, _r, _s, d, c in lines:
                if a == acct:
                    dr += Decimal(d or 0)
                    cr += Decimal(c or 0)
        opening = Decimal(OPENING.get(acct, "0"))
        credit_nature = ap in ("P", "R")
        open_d, open_c = (0, opening) if credit_nature else (opening, 0)
        close = (opening - dr + cr) if credit_nature else (opening + dr - cr)
        close_d, close_c = (0, close) if credit_nature else (close, 0)
        rows.append([acct.replace("-", ""), name, float(open_d) or None, float(open_c) or None, float(dr) or None, float(cr) or None, float(close_d) or None, float(close_c) or None])
    return rows


def _report(kind: str) -> List[list]:
    codes = [("PL_010", "LAAS and PPA  Revenues"), ("PL_020", "LAAS and PPA COS"), ("label", "LAAS and PPA  EBITDA"), ("PL_040", "Revenues"),
             ("PL_050", "Cost of Sales"), ("label", "Gross Margin"), ("label", "% Gross Margin"), ("PL_120", "Travel"), ("label", "OPEX Total"),
             ("label", "EBITDA incl. LAAS"), ("label", "NET Income")]
    actual = {"PL_010": [80, 85], "PL_020": [-30, -32], "PL_040": [1000, 368], "PL_050": [-600, 0], "PL_120": [0, -1.4505]}
    budget = {"PL_010": [70] * 12, "PL_020": [-30] * 12, "PL_040": [900] * 12, "PL_050": [-585] * 12, "PL_120": [-5] * 12}
    src = actual if kind == "PL" else budget
    hdr_year = "26" if kind == "PL" else "23"           # the budget sheet's stale labels, as in the real workbook
    rows = [[None] * 9 + [ENTITY, None, "FEBRUARY-2026" if kind == "PL" else "BUDGET 2025"], [None],
            [None, "2) Profit & Loss Statement", None, None, None, None] + [f"{r}-{hdr_year}" for r in ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")] + [None, "YTD"]]

    def line(code, label, vals):
        ytd = math.fsum(vals)          # correctly rounded on every Python (3.12 changed sum() of floats)
        return [None, label, None, None, None, None] + [float(v) for v in vals] + [None, float(ytd), None, None, None, None, None, code if code != "label" else None]
    vals = {}
    for code, label in codes:
        if code in src:
            v = src[code] + [0] * (12 - len(src[code]))
        elif label == "LAAS and PPA  EBITDA":
            v = [a + b for a, b in zip(vals["PL_010"], vals["PL_020"])]
        elif label == "Gross Margin":
            v = [a + b for a, b in zip(vals["PL_040"], vals["PL_050"])]
        elif label == "% Gross Margin":
            v = [(g / r if r else 0) for g, r in zip(vals["Gross Margin"], vals["PL_040"])]
        elif label == "OPEX Total":
            v = vals["PL_120"]
        elif label == "EBITDA incl. LAAS":
            v = [a + b + c for a, b, c in zip(vals["LAAS and PPA  EBITDA"], vals["Gross Margin"], vals["OPEX Total"])]
        else:
            v = vals["EBITDA incl. LAAS"]
        vals[code if code != "label" else label] = v
        rows.append(line(code, label, v))
    return rows


def bs_rows() -> List[list]:
    rows = [[None] * 9 + [ENTITY, None, "FEBRUARY-2026"], [None],
            [None, "3) Balance Sheet", None, None, None, None, "OB", "I-26", "II-26", "III-26", "IV-26", "V-26", "VI-26", "VII-26", "VIII-26", "IX-26", "X-26", "XI-26", "XII-26"]]
    for code, label, ob, jan, feb in (("BS_100", "Cash", 342.0, 922.0, 942.55), ("BS_060", "Receivables", 120.0, 700.0, 700.0), ("BS_120", "Trade Payables", 50.0, 746.0, 398.0)):
        rows.append([None, label, None, None, None, None, ob, jan, feb] + [0.0] * 10 + [None] * 6 + [code])
    return rows


def acctbook_sheets() -> Dict[str, List[list]]:
    cover = [[None], [None, None, None, None, None, ENTITY], [None], [None, None, "MANAGEMENT REPORTING 2026", None, None, None, None, "FEBRUARY-2026"], [None], [None],
             [None, None, None, "select month >", "M_02"], [None], [None, None, "Actual Year", None, 2026.0], [None, None, "Actual Month", None, 2.0]]
    setup = [[None], [None, None, "SETUP"], [None], [None, None, "UNITS", 1000.0]] + [[None, None, f"M_{m:02d}", float(m), 2026.0] for m in range(1, 13)]
    mapping = [[None], [None, "MAPPING OF ACCOUNTS"], [None], [None, "Account", "Account_Name", "Account_Name_ENG", "BS_PL", "A_P", "Report_Code", "Report_Account", "Account_Preference"]]
    for acct, (name, bs_pl, ap, rc, ra) in ACCOUNTS.items():
        mapping.append([None, acct, name, name, bs_pl, ap, rc, ra])
    projects = [[None], [None, "MAPPING OF PROJECTS"], [None], [None, "Project", "Project_Name", "Project_Type", "Business_Manager"]]
    for code, (name, ptype, bm) in PROJECTS.items():
        projects.append([None, float(code), name, ptype, bm])
    gm = [[None], [None, "e"], [None, None, "Total"], [None], [None, None, None, None, "   2019 - 2025", "   2019 - 2025", None, "YTD 2026", "YTD 2026"], [None],
          [None, "Project", "Project name", "x", "Revenues", "COS", "x", "Revenues", "COS", "x", "Revenues", "COS", "GM", "%GM", "x", "Planned Value [MXN]", "Planned Cost [MXN]", None, "Margin Planned [MXN]", "Margin Planned [%]", "Business Manager"]]
    for code, prior, ytd_r, ytd_c, pv, pc in ((9001, (0, 0), 1000000, -600000, 1200000, -840000), (9002, (250000, -180000), 368000, 0, 400000, -280000), (9003, (0, 0), 0, 0, 900000, -700000)):
        rev_t, cos_t = prior[0] + ytd_r, prior[1] + ytd_c
        gmv = rev_t + cos_t
        gm.append([None, float(code), PROJECTS[code][0], None, float(prior[0]), float(prior[1]), None, float(ytd_r), float(ytd_c), None, float(rev_t), float(cos_t), float(gmv),
                   (gmv / rev_t) if rev_t else 0.0, None, float(pv), float(pc), None, float(pv + pc), (pv + pc) / pv, PROJECTS[code][2]])
    loans = [["Holder ", "Bank ", "Co-signer", "Credit Line ", "Currency ", "Interest Rate ", "Other Fees ", "Duration ", "Signature Date ", None, None, None, None, None, "Payment type ", "Account ", "Bank "],
             ["Argia ", "BancoDemo ", "VK", 900000.0, "USD ", "SOFR+5", "0.8%", "81 Months ", "2023-08-03", None, None, None, None, None, "Direct Debit ", "000 111 222", "BancoDemo "]]
    return {"Cover": cover, "Setup": setup, "Mapping": mapping, "Projects": projects, "PL": _report("PL"), "BS": bs_rows(), "PL_Budget": _report("PL_Budget"),
            "GM_per_Projects": gm, "Bank Loans": loans, "Balanza": balanza_rows()}


def overview_rows() -> List[list]:
    head = [None, "Status", "Status2", "Country", "Phase", "Id", "Project Name", "Business Manager", "Value [USD]", "Value [MXN]", "Planned Cost [MXN]", "Margin Planned [%]",
            "Contract Start", "Month", "Year", "Contract\nEnd", "Project Manager", "Delivery of luminaires", "Delivery of lighting control", "Delivery of material", "Planned Start",
            "Planned Finish", "Subcontractor", "Installation progress", "Handover protocol Date", "Invoiced MXN", "%", "Paid [MXN]", "%2", "Payment Pending", "PO", "Columna2",
            "Invoicing vs execution MXN", "Pending to collect ", "Comment "]
    rows = [[None, "Status", None, "Country", "Phase", "Id", "Project Name", "Business Manager", "Value [USD]", "Value [MXN]", None, None, "Contract Start"],
            [None], [None, None, None, None, None, None, "RUNNING PROJECTS:", dt.datetime(2026, 3, 1)], [None], [None, "TOTAL REVENUES:", None, None, 1600000.0], [None], [None], [None], [None], [None], [None],
            head]

    def r(status, phase, code, name, bm, usd, mxn, cost, start, end, pm, prog, inv, paid, comment=""):
        return [None, status, "ok", "MX", phase, float(phase[0]), f"{code} {name}", bm, float(usd), float(mxn), float(cost), (mxn - cost) / mxn, dt.datetime.fromisoformat(start), float(int(start[5:7])), 2026.0,
                dt.datetime.fromisoformat(end), pm, "TBC", "TBC", "TBC", None, None, "SIGA", float(prog), None, float(inv), inv / mxn, float(paid), paid / mxn, None, "PO-1", None, None, None, comment]
    rows.append(r("1", "3_execution", 9001, "Solar Capex Roof 300 kWp Demo Uno", "Tomasz Zemelka", 65000, 1200000, 840000, "2026-01-10", "2026-05-30", "Eduardo Fraga ", 0.7, 1000000, 580000))
    rows.append(r("1", "2_preparation", 9002, "Lighting Capex Demo Dos", "Eduardo Fraga", 22000, 400000, 280000, "2026-02-01", "2026-06-15", "Eduardo Fraga ", 0.05, 0, 0))
    rows.append(r("2", "0_closing", 9003, "Solar PPA Demo Tres", "Daniel Mascareño", 49000, 900000, 700000, "2026-03-15", "2026-09-30", "Eduardo Fraga ", 0, 0, 0, "pending signature"))
    rows.append(r("1", "6_done", 8001, "Old Lighting Demo Cero", "Tomasz Zemelka", 5000, 90000, 60000, "2024-01-10", "2024-03-30", "Luis Juaristi", 1, 90000, 90000))
    rows.append([None, "Legend:"])
    return rows


def tracker_rows() -> List[list]:
    hdr = ["Status", "Real Status", "Invoice", "Company", "Project", "%", "PO", "Invoice Issued", "Payment Due Day", "New Payment Date", "Final Due Date", "Days to Due (Real)",
           "Total Amount MXN", "Amount without VAT", "Total Amount USD", "Amount without VAT2", "MXN w/o VAT", "Folio Fiscal", "Confirmation Payment date", "Type ", "Comments"]
    d = dt.datetime.fromisoformat
    rows = [["Expected Incomes and Payments", None, None, None, None, None, None, None, d("2026-03-01")], [None, "Bank", "Currency", "Amount MXN", "Amount USD"], [None], [None, None, None, None, "RECEIVABLES  "], hdr,
            ["1", "Delay", "2001", "CLIENTE ALFA", "9001 Solar Capex Roof 300 kWp Demo Uno", 1.0, "PO-1", d("2026-01-15"), d("2026-02-14"), None, d("2026-02-14"), -15.0, 580000.0, 500000.0, 0.0, 0.0, 500000.0, "11111111-2222-3333-4444-555555555555", None, "Sales", "50% balance"],
            ["3", "On time", "2003", "CLIENTE BETA", "9002 Lighting Capex Demo Dos", None, "PO-2", d("2026-02-25"), d("2026-03-27"), None, d("2026-03-27"), 26.0, 0.0, 0.0, 12000.0, 10344.83, 190345.0, "22222222-3333-4444-5555-666666666666", None, "Sales", ""],
            ["4", "Paid", "2002", "CLIENTE BETA", "9002 Lighting Capex Demo Dos", None, "PO-2", d("2026-02-10"), d("2026-02-20"), None, d("2026-02-20"), -9.0, 0.0, 0.0, 20000.0, 17241.38, 317241.0, "33333333-4444-5555-6666-777777777777", d("2026-02-20"), "Sales", ""],
            [None] * 12 + [580000.0, 500000.0, 32000.0], [None, None, None, None, "PAYABLES "], hdr,
            ["1", "Delay", "A-77", "PROVEEDOR GAMMA", "9001 Solar Capex Roof 300 kWp Demo Uno", 1.0, None, d("2026-01-20"), d("2026-02-19"), None, d("2026-02-19"), -10.0, 348000.0, 300000.0, 0.0, 0.0, 300000.0, "44444444-5555-6666-7777-888888888888", None, "Material", "balance after 50%"],
            ["4", "Paid", "Permanent", "ARRENDADORA DEMO", "701 OPERATION COSTS", 1.0, None, d("2026-02-04"), d("2026-02-19"), None, d("2026-02-19"), -10.0, 25000.0, 21552.0, 0.0, 0.0, 21552.0, "55555555-6666-7777-8888-999999999999", d("2026-02-10"), "Permanent", "Leasing"],
            ["4", "Paid", "Permanent", "ARRENDADORA DEMO", "701 OPERATION COSTS", 1.0, None, d("2026-02-04"), d("2026-02-19"), None, d("2026-02-19"), -10.0, 25000.0, 21552.0, 0.0, 0.0, 21552.0, "55555555-6666-7777-8888-999999999999", d("2026-02-10"), "Permanent", "Leasing"],
            ["3", "On time", "B-12", "PROVEEDOR DELTA", "9002 Lighting Capex Demo Dos", None, "PO-9", d("2026-02-26"), d("2026-03-28"), None, d("2026-03-28"), 27.0, 0.0, 0.0, 4500.0, 3879.31, 71379.0, "66666666-7777-8888-9999-000000000000", None, "Material", ""]]
    return rows


def pmo_tabs() -> Dict[str, List[list]]:
    info = [[""], ["", "PROJECT SUMMARY"], [""],
            ["", "Project_ID", "ARG9001", "", "Project_Status", "Project_Phase", "Project_Contract_Type", "Project Manager", "Project_Supervisor", "Project_Location"],
            ["", "Project_Name", "9001 Solar Capex Roof 300 kWp Demo Uno", "", "Ok", "0 - Closing", "Lighting", "Project Manager", "Project Supervisor", "Project Location"],
            ["", "Project_Customer", "Cliente Alfa", "", "Warning", "1 - Preparation", "LAAS", "Vit Kovarik", "Roberto Alvarado (ARGIA)", "Mexico City"],
            ["", "Project_Location", "León", "", "Critical", "2 - Specification", "Solar", "Eduardo Fraga", "Emilio Perez (ARGIA)", "Monterrey"],
            ["", "GPS", "21.12, -101.68"], ["", "Project_Manager", "Eduardo Fraga"], ["", "Project_Supervisor", "Emilio Cardiel (ARGIA)"],
            ["", "Project_Status", "In Progress"], ["", "Project_Phase", "3 - Execution"], ["", "Project_Contract_Type", "Solar"],
            ["", "Project_Start_Date", "1/10/2026"], ["", "Project_End_Date", "5/30/2026", "140"], ["", "Project_Value", "$1,200,000.00"], ["", "Project_Cost", "$840,000.00"],
            ["", "Project_Margin", "30.00%"], ["", "Project_Shifts", "Mon-Sat"]]
    tasks = [["WBS", "Task_ID", "Task_Name", "Project_Phase", "Resource_ID", "Task_Start_Date", "Task_End_Date", "Project_Task_Start_Date", "Project_Task_End_Date", "Task_Duration", "Task_Priority", "Task_Status", "Task_Complete%", "Milestone"],
             ["1", "P001", "Aviso de Inicio (NTP)", "Yes", "RES012 - ARGIA Design", "1/10/2026", "1/10/2026", "1/10/2026", "1/10/2026", "", "", "", "100.00%", "No"],
             ["1.1", "T001", "Aceptación de Orden de Trabajo", "No", "RES012 - ARGIA Design", "1/10/2026", "1/10/2026", "1/10/2026", "1/10/2026", "1", "", "Completed", "100.00%", "No"],
             ["1.9", "M001", "NTP Completado", "No", "", "1/10/2026", "1/10/2026", "1/10/2026", "1/10/2026", "1", "", "Completed", "100.00%", "Yes"],
             ["2", "P002", "Ingeniería", "Yes", "RES012 - ARGIA Design", "1/12/2026", "1/30/2026", "1/12/2026", "1/30/2026", "", "", "", "50.00%", "No"],
             ["2.1", "T002", "Diseño fotovoltaico", "No", "RES012 - ARGIA Design", "1/12/2026", "1/30/2026", "1/12/2026", "1/30/2026", "14", "High", "In Progress", "50.00%", "No"],
             ["2.9", "M002", "Ingeniería Completada", "No", "", "1/30/2026", "1/30/2026", "1/30/2026", "1/30/2026", "1", "", "Not Started", "0.00%", "Yes"],
             ["3", "P003", "Instalación", "Yes", "RES001 - Electro Experts", "2/15/2026", "5/20/2026", "2/15/2026", "5/20/2026", "", "", "", "0.00%", "No"],
             ["3.1", "T003", "Montaje de estructura", "No", "RES001 - Electro Experts", "2/15/2026", "3/15/2026", "2/15/2026", "3/15/2026", "24", "", "Not Started", "0.00%", "No"],
             ["3.9", "M003", "Instalación Completada", "No", "", "5/20/2026", "5/20/2026", "5/20/2026", "5/20/2026", "1", "", "Not Started", "0.00%", "Yes"]]
    costs = [["Cost_ID", "Project_ID", "Cost_Date", "Cost_Category", "Vendor", "Description", "Amount_Before_VAT", "VAT_Amount", "Total_Amount", "Cost_Status", "Approval_Status", "Approved_By", "Approval_Date", "Amount_Paid", "Payment_Status"],
             ["COST0001", "ARG9001", "20/01/26", "Solar Panels", "Proveedor Gamma", "Paneles", "$600,000.00", "$96,000.00", "$696,000.00", "Paid", "Approved", "Eduardo Fraga", "", "$348,000.00", "Partial"],
             ["COST0002", "ARG9001", "15/02/26", "Installation", "Electro Experts", "Instalación", "$150,000.00", "$24,000.00", "$174,000.00", "Committed", "Approved", "Eduardo Fraga", "", "", "Open"],
             ["COST0003", "ARG9001", "1/3/2026", "Inverters", "Solarever", "Inversores", "$90,000.00", "$14,400.00", "$104,400.00", "Planned", "", "", "", "", "Open"]]
    invoices = [["Invoice_ID", "Project_ID", "Customer_Name", "Invoice_Milestone", "Invoice_Number", "Invoice_Date", "Due_Date", "Invoice_Amount", "VAT_Amount", "Total_Amount", "Invoice_Status", "Payment_Status", "Payment_Date", "Amount_Received"],
                ["INV0001", "ARG9001", "Cliente Alfa", "Engineering", "2001", "1/15/2026", "2/14/2026", "$1,000,000.00", "$160,000.00", "$1,160,000.00", "Approved", "Partial", "1/28/2026", "$580,000.00"]]
    logs = [["Log_ID", "Log_Date", "Project_ID", "Task_ID", "Resource_ID", "Hours_Worked", "Progress_Entry_%", "Status_Update", "Issue_Flag", "Notes", "Photo_Link", "Entered_By", "Timestamp"],
            ["LOG0001", "1/20/2026", "ARG9001", "T002", "RES012", "8", "50.00%", "In Progress", "No", "diseño en revisión", "", "Eduardo Fraga", ""]]
    summary = [[""], ["", "PROJECT SUMMARY"], [""], ["", "Project_ID", "ARG9001", "", "Project_Status", "Project_Phase"], ["", "Project_Name", "9001 Solar Capex Roof 300 kWp Demo Uno", "", "In Progress", "3 - Execution"],
               ["", "Project_Progress", "41.67%"]]
    return {"Project_Info": info, "Resources": [["Resource_ID", "Resoruce_Type", "Resource_Name"], ["RES001", "Electro Installation", "Electro Experts"]], "Tasks": tasks,
            "Daily_Logs": logs, "Costs": costs, "Invoices": invoices, "Summary": summary}


def build() -> Dict[str, str]:
    j = lambda o: json.dumps(o, indent=0, ensure_ascii=False, default=str)   # noqa: E731
    return {"books/polizas_rows.json": j(polizas_rows()), "books/auxiliares_rows.json": j(auxiliares_rows()), "books/acctbook_sheets.json": j(acctbook_sheets()),
            "books/overview_rows.json": j(overview_rows()), "books/tracker_rows.json": j(tracker_rows()), "books/pmo_tabs.json": j(pmo_tabs()),
            "books/README.md": ("# Synthetic books (v245)\n\nThe accountants' exports in their exact layouts, generated by `scripts/fin_books_fixtures.py` (seedless, deterministic; "
                                "`scripts/fin_fixtures.py --check` fails when a file drifts). `polizas_rows.json` is a CONTPAQi 'Diarios y Pólizas' print; `auxiliares_rows.json` the "
                                "'Movimientos auxiliares' print DERIVED from it (one póliza, 2026-02-25 Egresos 99, is deliberately unposted — the cross-check must report it); "
                                "`acctbook_sheets.json` the sheets of `Argia_Accounting_Data_MM_YY` (mapping, projects, PL/BS/budget, GM per project, bank loans, balanza); "
                                "`overview_rows.json` the projects overview `Data` sheet; `tracker_rows.json` the AR/AP tracker; `pmo_tabs.json` one ARGIA PROJECT workbook as the "
                                "Sheets API returns it. Every name, RFC, folio and amount is invented; the entity is ARGIA DEMO / DEM150101AB1.\n")}


def to_xlsx(rows: List[list], path: Path, sheet: str = "Sheet1", dates_as_datetime: bool = False):
    """A fixture as an .xlsx (what the Drive ingest reads); needs openpyxl."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append([(dt.datetime.fromisoformat(c) if dates_as_datetime and isinstance(c, str) and len(c) == 19 and c[4] == "-" else c) for c in r])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def to_workbook(sheets: Dict[str, List[list]], path: Path):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def mirror(root: Path):
    """Write a local Drive mirror the ingest can read: the books as .xlsx
    under the accountants' folder names, the overview, the tracker, the
    PMO workbook as a JSON tab dump."""
    month = root / "ACCOUNTING" / "Accounting Reporting" / "2026" / "2.- February"
    to_xlsx(polizas_rows(), month / "0226 Polizas Argia.xlsx", "Diarios y Pólizas")
    to_xlsx(auxiliares_rows(), month / "0226 Auxiliares Argia.xlsx", "Reporte de Compac")
    to_workbook(acctbook_sheets(), month / "Argia_Accounting_Data_02_26_V1.xlsx")
    ov = overview_rows()
    to_workbook({"Legend": [["ID", "Project Phase"]], "Data": ov}, root / "REALIZATIONS" / "RUNNING PROJECTS OVERVIEW" / "Argia_Projects_Overview_MX.xlsx")
    to_workbook({"Payables and Receivables.": tracker_rows()}, root / "ACCOUNTING" / "Argia Mexico Payables and receivables 2026_V2.xlsx")
    pm = root / "PROJECT MANAGEMENT" / "Project ARG9001 - Demo Uno - 300 kWp"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "ARGIA_PROJECT_DEMO_UNO.json").write_text(json.dumps(pmo_tabs(), ensure_ascii=False), encoding="utf-8")
    return root


if __name__ == "__main__":
    files = build()
    for rel, content in files.items():
        p = OUT.parent / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
    print(f"books fixtures: {len(files)} files under {OUT}")
