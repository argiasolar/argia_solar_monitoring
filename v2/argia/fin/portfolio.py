"""v245 — readers for the two business-side workbooks the portal joins
to the books through the business-case number:

* ``REALIZATIONS/RUNNING PROJECTS OVERVIEW/Argia_Projects_Overview_MX.xlsx``
  sheet ``Data`` — Marcela's portfolio: one row per business case since
  2018 (510 rows), phase 0_closing … 6_done, value, planned cost, dates,
  PM, installation progress, invoiced, paid. Updated weekly; the phase and
  progress are the PM truth until the PMO sheets carry them.
* ``ACCOUNTING/Argia Mexico Payables and receivables 2026_V2.xlsx`` sheet
  ``Payables and Receivables.`` — Tania's open-item tracker: every open
  customer / supplier invoice with due dates, folio fiscal and status.
  The freshest AR/AP view there is (the books close ~3 weeks after month
  end; this is kept daily).

Both readers take the sheet's rows and return dataclasses; pure.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from .money import D

PHASES = {"0_closing": 0, "1_specification": 1, "2_preparation": 2, "3_execution": 3, "4_finalization": 4,
          "5_review": 5, "6_done": 6, "7_warranty claim": 7, "8_on hold": 8}
ACTIVE_PHASES = (0, 1, 2, 3, 4, 5, 8)


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _d(v) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = _s(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def _n(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").replace("%", "").strip()
        if not re.match(r"^-?\d*\.?\d+$", s):
            return None
        v = s
    try:
        return D(v)
    except Exception:            # noqa: BLE001
        return None


def _code_and_name(text: str):
    m = re.match(r"^\s*(\d{3,4})\s+(.*)$", text)
    return (int(m.group(1)), m.group(2).strip()) if m else (None, text.strip())


# ---------------------------------------------------------------- overview
@dataclass
class OverviewRow:
    code: int
    name: str
    phase: str                  # '3_execution'
    status: str                 # 'ok' | 'warning' | 'critical' | ''
    country: str
    business_manager: str
    project_manager: str
    value_usd: Optional[Decimal]
    value_mxn: Optional[Decimal]
    planned_cost_mxn: Optional[Decimal]
    margin_planned_pct: Optional[Decimal]
    contract_start: Optional[dt.date]
    contract_end: Optional[dt.date]
    planned_start: Optional[dt.date]
    planned_finish: Optional[dt.date]
    subcontractor: str
    progress: Optional[Decimal]         # 0..1
    handover: Optional[dt.date]
    invoiced_mxn: Optional[Decimal]
    paid_mxn: Optional[Decimal]
    po: str
    comment: str

    @property
    def phase_no(self) -> Optional[int]:
        return PHASES.get(self.phase)

    @property
    def active(self) -> bool:
        return self.phase_no in ACTIVE_PHASES

    @property
    def project_id(self) -> str:
        return f"ARG{self.code:04d}"


def read_overview(rows: Sequence[Sequence]) -> List[OverviewRow]:
    """Sheet ``Data``: the header row is the one whose 6th cell is 'Id';
    the 'Project Name' cell carries '<code> <name>'."""
    hdr: Optional[Dict[str, int]] = None
    out: List[OverviewRow] = []
    for r in rows:
        if hdr is None:
            cells = [_s(c).replace("\n", " ") for c in r]
            # the sheet carries a partial header block at the top (Id, name, value…) and the full
            # one above the data — the full one names the invoiced / paid / cost columns
            if "Id" in cells and "Project Name" in cells and "Invoiced MXN" in cells:
                hdr = {c: i for i, c in enumerate(cells) if c}
            continue
        g = lambda k: (r[hdr[k]] if k in hdr and hdr[k] < len(r) else None)   # noqa: E731
        name_cell = _s(g("Project Name"))
        code, name = _code_and_name(name_cell)
        if code is None:
            code = int(_n(g("Id")) or 0) if _n(g("Id")) else None
        if code is None or not name:
            continue
        phase = _s(g("Phase"))
        if phase not in PHASES:
            continue
        status_map = {"1": "ok", "2": "warning", "3": "critical"}
        out.append(OverviewRow(
            code=code, name=name, phase=phase, status=status_map.get(_s(g("Status")).split(".")[0], _s(g("Status2")).lower()),
            country=_s(g("Country")), business_manager=_s(g("Business Manager")), project_manager=_s(g("Project Manager")),
            value_usd=_n(g("Value [USD]")), value_mxn=_n(g("Value [MXN]")), planned_cost_mxn=_n(g("Planned Cost [MXN]")),
            margin_planned_pct=_n(g("Margin Planned [%]")), contract_start=_d(g("Contract Start")), contract_end=_d(g("Contract End")),
            planned_start=_d(g("Planned Start")), planned_finish=_d(g("Planned Finish")), subcontractor=_s(g("Subcontractor")),
            progress=_n(g("Installation progress")), handover=_d(g("Handover protocol Date")),
            invoiced_mxn=_n(g("Invoiced MXN")), paid_mxn=_n(g("Paid [MXN]")), po=_s(g("PO")), comment=_s(g("Comment"))))
    return out


# ------------------------------------------------------------- AR/AP tracker
@dataclass
class OpenItem:
    side: str                   # 'ar' | 'ap'
    status: str                 # Delay | On time | Paid | …
    invoice: str
    company: str
    project_code: Optional[int]
    project_name: str
    po: str
    issued: Optional[dt.date]
    due: Optional[dt.date]
    new_due: Optional[dt.date]
    final_due: Optional[dt.date]
    days_to_due: Optional[int]
    total_mxn: Decimal
    net_mxn: Decimal
    total_usd: Decimal
    net_usd: Decimal
    mxn_equiv_net: Decimal
    folio_fiscal: str
    paid_on: Optional[dt.date]
    kind: str
    comment: str

    @property
    def currency(self) -> str:
        return "USD" if self.total_usd and not self.total_mxn else "MXN"

    @property
    def total(self) -> Decimal:
        return self.total_usd if self.currency == "USD" else self.total_mxn

    @property
    def is_paid(self) -> bool:
        return self.status.lower() == "paid" or self.paid_on is not None

    @property
    def open_amount(self) -> Decimal:
        return D(0) if self.is_paid else self.total


def read_tracker(rows: Sequence[Sequence]) -> List[OpenItem]:
    """Sheet ``Payables and Receivables.``: a RECEIVABLES block and a
    PAYABLES block, each with its own header row starting 'Status'."""
    side = ""
    hdr: Optional[Dict[str, int]] = None
    out: List[OpenItem] = []
    for r in rows:
        cells = [_s(c) for c in r]
        joined = " ".join(cells).upper()
        if "RECEIVABLES" in joined and "PAYABLES" not in joined and len([c for c in cells if c]) <= 2:
            side, hdr = "ar", None
            continue
        if "PAYABLES" in joined and len([c for c in cells if c]) <= 2:
            side, hdr = "ap", None
            continue
        if side and hdr is None and cells[:1] == ["Status"]:
            hdr = {c: i for i, c in enumerate(cells) if c}
            continue
        if not side or hdr is None:
            continue
        def g(k):
            i = hdr.get(k)
            if i is None:                      # header text varies a little between versions
                i = next((v for h, v in hdr.items() if h.startswith(k)), None)
            return r[i] if i is not None and i < len(r) else None
        real = _s(g("Real Status"))
        if not real or not _s(g("Company")):
            continue
        code, pname = _code_and_name(_s(g("Project")))
        days = _n(g("Days to Due (Real)"))
        out.append(OpenItem(
            side=side, status=real, invoice=_s(g("Invoice")), company=_s(g("Company")), project_code=code, project_name=pname,
            po=_s(g("PO")), issued=_d(g("Invoice Issued")), due=_d(g("Payment Due Day")), new_due=_d(g("New Payment Date")),
            final_due=_d(g("Final Due Date")), days_to_due=int(days) if days is not None else None,
            total_mxn=_n(g("Total Amount MXN")) or D(0), net_mxn=_n(g("Amount without VAT")) or D(0),
            total_usd=_n(g("Total Amount USD")) or D(0), net_usd=_n(g("Amount without VAT2")) or D(0), mxn_equiv_net=_n(g("MXN w/o VAT")) or D(0),
            folio_fiscal=_s(g("Folio Fiscal")).replace("‐", "-"), paid_on=_d(g("Confirmation Payment")),
            kind=_s(g("Type")), comment=_s(g("Comments"))))
    return out
