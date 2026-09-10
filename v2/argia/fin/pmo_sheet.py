"""v245 — reader for one ARGIA PROJECT workbook (the V8.1 PMO template the
PMs keep per project under ``PROJECT MANAGEMENT/Project ARGnnnn - …/``).

The reader is tab-name agnostic: every tab's rows are scanned and
recognised by the header they carry (``Task_ID`` + ``WBS`` = tasks,
``Cost_ID`` = costs, ``Invoice_ID`` = invoices, ``Log_ID`` = daily logs,
the ``Project_ID`` / ``Project_Name`` label-value block = summary). A
renamed tab therefore still parses; an unknown tab is ignored.

Input: ``{tab_name: rows}`` where rows are lists of cell values as the
Sheets API returns them (strings; dates 'M/D/YYYY' or 'DD/MM/YY', money
'$1,027,040.00', percentages '70.00%'). Pure.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from .money import D


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _n(v) -> Optional[Decimal]:
    s = _s(v).replace("$", "").replace(",", "").replace("%", "")
    if not re.match(r"^-?\d*\.?\d+$", s):
        return None
    return D(s)


def _pct(v) -> Optional[Decimal]:
    """'70.00%' → 0.70; '0.7' → 0.7; '70' → 0.70."""
    s = _s(v)
    n = _n(s)
    if n is None:
        return None
    if "%" in s or n > 1:
        return n / 100
    return n


def _d(v) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = _s(v)
    if not s or s.startswith("12/30/1899"):
        return None
    for fmt in ("%m/%d/%Y", "%d/%m/%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _yes(v) -> bool:
    return _s(v).lower() in ("yes", "true", "1", "sí", "si")


@dataclass
class PmoTask:
    wbs: str
    task_id: str
    name: str
    is_phase: bool
    is_milestone: bool
    resource: str
    start: Optional[dt.date]
    end: Optional[dt.date]
    duration_days: Optional[int]
    priority: str
    status: str
    progress: Optional[Decimal]        # 0..1
    sheet: str = ""                    # v250: the tab this row lives in…
    row: int = 0                       # …1-based sheet row

    @property
    def phase_no(self) -> str:
        return self.wbs.split(".")[0]


@dataclass
class PmoCost:
    cost_id: str
    date: Optional[dt.date]
    category: str
    vendor: str
    description: str
    net: Decimal
    vat: Decimal
    total: Decimal
    cost_status: str                    # Planned | Committed | Incurred | Paid
    approval: str
    approved_by: str
    paid: Decimal
    payment_status: str
    sheet: str = ""                    # v250
    row: int = 0


@dataclass
class PmoInvoice:
    invoice_id: str
    customer: str
    milestone: str
    number: str
    date: Optional[dt.date]
    due: Optional[dt.date]
    net: Decimal
    vat: Decimal
    total: Decimal
    status: str
    payment_status: str
    paid_on: Optional[dt.date]
    received: Decimal
    sheet: str = ""                    # v250
    row: int = 0


@dataclass
class PmoLog:
    log_id: str
    date: Optional[dt.date]
    task_id: str
    resource: str
    hours: Optional[Decimal]
    progress: Optional[Decimal]
    status: str
    issue: bool
    notes: str
    by: str


@dataclass
class PmoProject:
    project_id: str                      # 'ARG1473'
    name: str
    customer: str
    location: str
    status: str
    phase: str
    contract_type: str
    manager: str
    supervisor: str
    start: Optional[dt.date]
    end: Optional[dt.date]
    value: Optional[Decimal]
    cost: Optional[Decimal]
    tasks: List[PmoTask] = field(default_factory=list)
    costs: List[PmoCost] = field(default_factory=list)
    invoices: List[PmoInvoice] = field(default_factory=list)
    logs: List[PmoLog] = field(default_factory=list)

    @property
    def code(self) -> Optional[int]:
        m = re.match(r"^ARG(\d{4})", self.project_id)
        return int(m.group(1)) if m else None

    @property
    def milestones(self) -> List[PmoTask]:
        return [t for t in self.tasks if t.is_milestone]

    def cost_by_status(self) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for c in self.costs:
            out[c.cost_status or "unknown"] = out.get(c.cost_status or "unknown", D(0)) + c.net
        return out

    @property
    def progress(self) -> Optional[Decimal]:
        """Mean progress of the leaf tasks (phases excluded), 0..1."""
        leaves = [t.progress for t in self.tasks if not t.is_phase and t.progress is not None]
        return (sum(leaves) / len(leaves)) if leaves else None


def _hdr_index(row: Sequence) -> Dict[str, int]:
    return {_s(c): i for i, c in enumerate(row) if _s(c)}


def _find_header(rows: Sequence[Sequence], *must: str) -> Optional[int]:
    for i, r in enumerate(rows):
        cells = {_s(c) for c in r}
        if all(m in cells for m in must):
            return i
    return None


def _summary(rows: Sequence[Sequence]) -> Dict[str, str]:
    """The PROJECT SUMMARY block: vertical label/value pairs ('Project_Status'
    in one cell, the value in the next non-empty cell of the same row). The
    horizontal header rows beside the block carry dropdown LISTS (every
    status, every phase) — they are not values and are never read."""
    out: Dict[str, str] = {}
    for r in rows:
        cells = [_s(c) for c in r]
        for i, c in enumerate(cells):
            if c.startswith("Project_"):
                if c not in out:
                    val = next((x for x in cells[i + 1:] if x), "")
                    if val and not val.startswith("Project_"):
                        out[c] = val
                break
    return out


def read_project(tabs: Dict[str, Sequence[Sequence]]) -> Optional[PmoProject]:
    summary: Dict[str, str] = {}
    tasks: List[PmoTask] = []
    costs: List[PmoCost] = []
    invoices: List[PmoInvoice] = []
    logs: List[PmoLog] = []
    for _name, rows in tabs.items():
        if not rows:
            continue
        hi = _find_header(rows, "Task_ID", "WBS")
        if hi is not None and not tasks:
            h = _hdr_index(rows[hi])
            for rn, r in enumerate(rows[hi + 1:], hi + 2):
                g = lambda k: (r[h[k]] if k in h and h[k] < len(r) else None)   # noqa: E731
                if not _s(g("Task_ID")):
                    continue
                dur = _n(g("Task_Duration"))
                tasks.append(PmoTask(wbs=_s(g("WBS")), task_id=_s(g("Task_ID")), name=_s(g("Task_Name")),
                                     is_phase=_yes(g("Project_Phase")) if "Project_Phase" in h else _yes(g("Is_Phase")),
                                     is_milestone=_yes(g("Milestone")) if "Milestone" in h else _yes(g("Is_Milestone")),
                                     resource=_s(g("Resource_ID")), start=_d(g("Task_Start_Date")) or _d(g("Planned_Start")),
                                     end=_d(g("Task_End_Date")) or _d(g("Planned_End")), duration_days=int(dur) if dur is not None else None,
                                     priority=_s(g("Task_Priority")), status=_s(g("Task_Status")) or _s(g("Status")),
                                     progress=_pct(g("Task_Complete%")) if "Task_Complete%" in h else _pct(g("Progress_Pct")), sheet=_name, row=rn))
            continue
        hi = _find_header(rows, "Cost_ID", "Vendor")
        if hi is not None and not costs:
            h = _hdr_index(rows[hi])
            for rn, r in enumerate(rows[hi + 1:], hi + 2):
                g = lambda k: (r[h[k]] if k in h and h[k] < len(r) else None)   # noqa: E731
                if not _s(g("Cost_ID")):
                    continue
                costs.append(PmoCost(cost_id=_s(g("Cost_ID")), date=_d(g("Cost_Date")), category=_s(g("Cost_Category")),
                                     vendor=_s(g("Vendor")), description=_s(g("Description")), net=_n(g("Amount_Before_VAT")) or D(0),
                                     vat=_n(g("VAT_Amount")) or D(0), total=_n(g("Total_Amount")) or D(0), cost_status=_s(g("Cost_Status")),
                                     approval=_s(g("Approval_Status")), approved_by=_s(g("Approved_By")), paid=_n(g("Amount_Paid")) or D(0),
                                     payment_status=_s(g("Payment_Status")), sheet=_name, row=rn))
            continue
        hi = _find_header(rows, "Invoice_ID", "Invoice_Amount")
        if hi is not None and not invoices:
            h = _hdr_index(rows[hi])
            for rn, r in enumerate(rows[hi + 1:], hi + 2):
                g = lambda k: (r[h[k]] if k in h and h[k] < len(r) else None)   # noqa: E731
                if not _s(g("Invoice_ID")):
                    continue
                invoices.append(PmoInvoice(invoice_id=_s(g("Invoice_ID")), customer=_s(g("Customer_Name")), milestone=_s(g("Invoice_Milestone")),
                                           number=_s(g("Invoice_Number")), date=_d(g("Invoice_Date")), due=_d(g("Due_Date")),
                                           net=_n(g("Invoice_Amount")) or D(0), vat=_n(g("VAT_Amount")) or D(0), total=_n(g("Total_Amount")) or D(0),
                                           status=_s(g("Invoice_Status")), payment_status=_s(g("Payment_Status")), paid_on=_d(g("Payment_Date")),
                                           received=_n(g("Amount_Received")) or D(0), sheet=_name, row=rn))
            continue
        hi = _find_header(rows, "Log_ID", "Hours_Worked")
        if hi is not None and not logs:
            h = _hdr_index(rows[hi])
            for r in rows[hi + 1:]:
                g = lambda k: (r[h[k]] if k in h and h[k] < len(r) else None)   # noqa: E731
                if not _s(g("Log_ID")):
                    continue
                logs.append(PmoLog(log_id=_s(g("Log_ID")), date=_d(g("Log_Date")), task_id=_s(g("Task_ID")), resource=_s(g("Resource_ID")),
                                   hours=_n(g("Hours_Worked")), progress=_pct(g("Progress_Entry_%")), status=_s(g("Status_Update")),
                                   issue=_yes(g("Issue_Flag")), notes=_s(g("Notes")), by=_s(g("Entered_By"))))
            continue
        sm = _summary(rows)
        if "Project_ID" in sm:
            for k, v in sm.items():
                summary.setdefault(k, v)
    if not summary.get("Project_ID"):
        return None
    return PmoProject(project_id=summary.get("Project_ID", ""), name=summary.get("Project_Name", ""),
                      customer=summary.get("Project_Customer", ""), location=summary.get("Project_Location", ""),
                      status=summary.get("Project_Status", ""), phase=summary.get("Project_Phase", ""),
                      contract_type=summary.get("Project_Contract_Type", ""), manager=summary.get("Project_Manager", ""),
                      supervisor=summary.get("Project_Supervisor", ""), start=_d(summary.get("Project_Start_Date")),
                      end=_d(summary.get("Project_End_Date")), value=_n(summary.get("Project_Value")), cost=_n(summary.get("Project_Cost")),
                      tasks=tasks, costs=costs, invoices=invoices, logs=logs)
