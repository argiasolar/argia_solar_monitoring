"""v245 — reader for the accountants' monthly workbook
``Argia_Accounting_Data_MM_YY_Vn.xlsx/xlsm`` (ACCOUNTING/Accounting
Reporting/<year>/<n>.- <Month>/).

The workbook is the management-reporting layer on top of CONTPAQi: the
account → report-line mapping, the business-case (project) list, the
P&L and balance sheet by month in thousands, the 2026 budget on the same
lines, gross margin per project (2019-2025 vs YTD vs plan), bank loans.
Only the sheets the portal needs are read; each reader takes the sheet's
rows (openpyxl ``iter_rows(values_only=True)``) and returns plain
dataclasses / dicts — pure, so the tests run on a synthetic workbook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from .money import D

CODE_RE = re.compile(r"^(PL|BS)_\d{3}$")
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
_MONTH_HDR = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII)-(\d{2})$")

# sheet names as they are in the workbook (keys of the dict `read_workbook` expects)
SHEETS = ("Cover", "Setup", "Mapping", "Projects", "PL", "BS", "PL_Budget", "GM_per_Projects", "Bank Loans", "Balanza")


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _num(v) -> Optional[Decimal]:
    if v is None or v == "" or isinstance(v, str) and not re.match(r"^-?[\d,]*\.?\d+$", v.strip()):
        return None
    if isinstance(v, str):
        v = v.replace(",", "")
    try:
        return D(v)
    except Exception:            # noqa: BLE001
        return None


# ----------------------------------------------------------------- mapping
@dataclass
class AccountMap:
    account: str            # '102-01-001'
    name: str
    name_en: str
    bs_pl: str              # 'BS' | 'PL'
    a_p: str                # 'A'ctivo/'P'asivo or 'R'evenue/'C'ost
    report_code: str        # 'BS_100'
    report_account: str     # 'Cash'


def read_mapping(rows: Sequence[Sequence]) -> Dict[str, AccountMap]:
    out: Dict[str, AccountMap] = {}
    for r in rows:
        acct = _s(r[1] if len(r) > 1 else None)
        if not re.match(r"^\d{3}-\d{2}-\d{3}$", acct):
            continue
        out[acct] = AccountMap(acct, _s(r[2]), _s(r[3]) or _s(r[2]), _s(r[4]), _s(r[5]), _s(r[6]), _s(r[7]))
    return out


# ---------------------------------------------------------------- projects
@dataclass
class ProjectCode:
    code: int               # business-case number (the CONTPAQi segment)
    name: str
    project_type: str       # 'GM' (projects) | 'LAAS' | 'PL_xxx' (cost centre) | '_'
    business_manager: str

    @property
    def is_business_case(self) -> bool:
        return self.project_type in ("GM", "LAAS") and self.name != ""


def read_projects(rows: Sequence[Sequence]) -> Dict[int, ProjectCode]:
    out: Dict[int, ProjectCode] = {}
    for r in rows:
        c = r[1] if len(r) > 1 else None
        if not isinstance(c, (int, float)):
            continue
        code = int(c)
        out[code] = ProjectCode(code, _s(r[2]), _s(r[3]), _s(r[4]) if len(r) > 4 else "")
    return out


# ------------------------------------------------------------ report sheets
@dataclass
class ReportLine:
    code: str               # 'PL_040' or 'label:Gross Margin' for the workbook's subtotals
    label: str
    months: List[Optional[Decimal]]      # 12 values (thousands MXN), None where blank
    ytd: Optional[Decimal]
    year: int
    order: int              # row order in the sheet

    @property
    def is_subtotal(self) -> bool:
        return self.code.startswith("label:")

    def value(self, month: int) -> Optional[Decimal]:
        return self.months[month - 1]

    def ytd_through(self, month: int) -> Decimal:
        return sum((v for v in self.months[:month] if v is not None), D(0))


def read_report(rows: Sequence[Sequence], year_hint: Optional[int] = None) -> List[ReportLine]:
    """PL / BS / PL_Budget sheets → lines keyed by report code.

    The month columns are found from the header row ('I-26' … 'XII-26');
    the YTD column is the one headed 'YTD' (BS: no YTD, the OB column is
    ignored); the report code is the last cell matching PL_nnn/BS_nnn.
    The sheet's own year label wins unless it is obviously stale (the
    budget sheet still says I-23) — then ``year_hint`` is used.
    """
    month_cols: Dict[int, int] = {}
    ytd_col: Optional[int] = None
    label_col: Optional[int] = None
    year = year_hint or 0
    lines: List[ReportLine] = []
    for r in rows:
        if not month_cols:
            for i, c in enumerate(r):
                m = _MONTH_HDR.match(_s(c))
                if m:
                    month_cols[ROMAN[m.group(1)]] = i
                    y = 2000 + int(m.group(2))
                    if not year_hint:
                        year = y
                if _s(c).upper() == "YTD":
                    ytd_col = i
            if month_cols:
                label_col = min(month_cols.values()) - 5 if min(month_cols.values()) >= 5 else 1
                for i, c in enumerate(r):
                    if _s(c).startswith("Profit") or _s(c).startswith("Balance") or _s(c).startswith("2)") or _s(c).startswith("3)"):
                        label_col = i
            continue
        label = ""
        for i in range(0, min(month_cols.values())):
            if _s(r[i]) and i >= (label_col or 0):
                label = _s(r[i])
                break
        if not label:
            continue
        vals = [(_num(r[month_cols[m]]) if month_cols[m] < len(r) else None) for m in range(1, 13)]
        if all(v is None for v in vals):
            continue
        code = ""
        for c in reversed(r):
            if CODE_RE.match(_s(c)):
                code = _s(c)
                break
        ytd = _num(r[ytd_col]) if ytd_col is not None and ytd_col < len(r) else None
        lines.append(ReportLine(code or f"label:{label}", label, vals, ytd, year, len(lines)))
    return lines


# ---------------------------------------------------------- GM per project
@dataclass
class ProjectMargin:
    code: int
    name: str
    revenue_prior: Decimal          # 2019-2025 cumulative
    cos_prior: Decimal
    revenue_ytd: Decimal
    cos_ytd: Decimal
    revenue_total: Decimal
    cos_total: Decimal
    gm: Decimal
    gm_pct: Optional[Decimal]
    planned_value: Decimal
    planned_cost: Decimal            # negative in the sheet, kept as-is (cost)
    planned_margin: Decimal
    planned_margin_pct: Optional[Decimal]
    business_manager: str

    @property
    def variance_gm(self) -> Decimal:
        return self.gm - self.planned_margin


def read_gm_projects(rows: Sequence[Sequence]) -> Dict[int, ProjectMargin]:
    out: Dict[int, ProjectMargin] = {}
    for r in rows:
        c = r[1] if len(r) > 1 else None
        if not isinstance(c, (int, float)) or len(r) < 21:
            continue
        g = lambda i: _num(r[i]) if i < len(r) else None   # noqa: E731
        z = lambda i: g(i) or D(0)                          # noqa: E731
        out[int(c)] = ProjectMargin(int(c), _s(r[2]), z(4), z(5), z(7), z(8), z(10), z(11), z(12), g(13),
                                    z(15), z(16), z(18), g(19), _s(r[20]))
    return out


# --------------------------------------------------------------- bank loans
@dataclass
class BankLoan:
    holder: str
    bank: str
    credit_line: str
    currency: str
    rate: str
    duration: str
    signed: str
    payment_type: str
    account: str


def read_bank_loans(rows: Sequence[Sequence]) -> List[BankLoan]:
    out: List[BankLoan] = []
    for r in rows:
        if not _s(r[0]) or _s(r[0]).lower().startswith("holder"):
            continue
        g = lambda i: _s(r[i]) if i < len(r) else ""       # noqa: E731
        out.append(BankLoan(g(0), g(1), g(3), g(4), g(5), g(7), g(8), g(14), g(15)))
    return out


# ------------------------------------------------------------------- cover
@dataclass
class Period:
    year: int
    month: int
    entity: str

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


def read_cover(rows: Sequence[Sequence]) -> Period:
    year = month = 0
    entity = ""
    for r in rows:
        cells = [_s(c) for c in r]
        text = [c for c in cells if c]
        if not text:
            continue
        if "ARGIA MEXICO" in text and not entity:
            entity = "ARGIA MEXICO"
        for i, c in enumerate(cells):
            if c == "Actual Year" and i + 2 < len(r) and _num(r[i + 2]) is not None:
                year = int(_num(r[i + 2]))
            if c == "Actual Month" and i + 2 < len(r) and _num(r[i + 2]) is not None:
                month = int(_num(r[i + 2]))
    return Period(year, month, entity)


# ------------------------------------------------------------- whole book
@dataclass
class Workbook:
    period: Period
    mapping: Dict[str, AccountMap]
    projects: Dict[int, ProjectCode]
    pl: List[ReportLine]
    bs: List[ReportLine]
    budget: List[ReportLine]
    gm: Dict[int, ProjectMargin]
    loans: List[BankLoan]

    def line(self, code: str, which: str = "pl") -> Optional[ReportLine]:
        for l in getattr(self, which):
            if l.code == code:
                return l
        return None


def read_workbook(sheets: Dict[str, Sequence[Sequence]]) -> Workbook:
    """``sheets`` maps sheet name → rows; missing optional sheets are
    tolerated (empty results)."""
    period = read_cover(sheets.get("Cover", []))
    return Workbook(period=period,
                    mapping=read_mapping(sheets.get("Mapping", [])),
                    projects=read_projects(sheets.get("Projects", [])),
                    pl=read_report(sheets.get("PL", []), period.year or None),
                    bs=read_report(sheets.get("BS", []), period.year or None),
                    budget=read_report(sheets.get("PL_Budget", []), period.year or None),
                    gm=read_gm_projects(sheets.get("GM_per_Projects", [])),
                    loans=read_bank_loans(sheets.get("Bank Loans", [])))
