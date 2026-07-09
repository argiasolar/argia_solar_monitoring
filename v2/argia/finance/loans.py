"""Loans and amortization schedules — loaders and derived queries.

Design rules (learned from v1's failure modes):

* A plant can have any number of loans (SLP1 already has two). The unit
  of financial identity is the ``loan_id``, never the plant.
* Monthly debt service is DERIVED by summing schedule rows for a month,
  never stored per plant. v1 stored it on the Credit tab and reported a
  stale 24,622.50 for SLP1 after its refinance; the real figure was
  12,500.00.
* USD-denominated loans (LaaS) carry ``payment_ccy`` (the sum of the
  facility's USD components) and ``xr`` (the FX rate used). For months
  already paid, ``xr`` is the historical rate; for future months v1
  projected the last known rate forward, so future MXN figures are
  projections, not commitments. ``ScheduleRow.fx_projected`` makes that
  distinction queryable.
* Missing tabs degrade to empty results with a log line, mirroring
  ``argia.kpi.design`` — the finance layer must never take down a job
  that only wanted production KPIs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from argia.core.normalize import safe_float
from argia.report.daily import date_key

LOG = logging.getLogger(__name__)

LOANS_TAB = "Loans"
SCHEDULE_TAB = "Loan_Schedule"

LOANS_HEADER = [
    "loan_id", "plant_key", "project_name", "bank", "currency",
    "principal_mxn", "total_installments", "first_month", "last_month",
]
SCHEDULE_HEADER = [
    "loan_id", "plant_key", "ref_month", "installment_no",
    "total_installments", "payment_mxn", "payment_ccy", "xr",
    "due_after_mxn",
]


@dataclass(frozen=True)
class Loan:
    loan_id: str
    plant_key: str
    project_name: str
    bank: str
    currency: str            # "MXN" | "USD"
    principal_mxn: float     # MXN value at booking (USD loans: restated)
    total_installments: int
    first_month: str         # "YYYY-MM"
    last_month: str          # "YYYY-MM"


@dataclass(frozen=True)
class ScheduleRow:
    loan_id: str
    plant_key: str
    ref_month: str           # "YYYY-MM"
    installment_no: int
    total_installments: int
    payment_mxn: float
    payment_ccy: Optional[float]   # None for MXN loans
    xr: Optional[float]            # None for MXN loans
    due_after_mxn: float

    @property
    def is_usd(self) -> bool:
        return self.payment_ccy is not None and self.xr is not None

    def fx_projected(self, as_of: str) -> bool:
        """True when this is a USD row for a month after ``as_of``
        ("YYYY-MM") — its MXN figure uses a projected FX rate."""
        return self.is_usd and self.ref_month > as_of


def _f(value) -> Optional[float]:
    # safe_float strips thousands commas — the Sheets API returns
    # FORMATTED values ("94,668.89"), which plain float() rejects.
    # That parsing gap made the live schedule load empty (v64 incident).
    return safe_float(value)


_YM = re.compile(r"^\d{4}-\d{2}$")


def _month_str(value) -> str:
    """Normalize a sheet cell to 'YYYY-MM'.

    The live sheet auto-parsed the migration's "2024-10" strings into
    date cells, which read back as US-formatted strings ("10/1/2024"),
    serial numbers, or datetimes depending on render option. date_key
    handles all of those; plain 'YYYY-MM' strings pass through."""
    s = str(value or "").strip()
    if _YM.match(s):
        return s
    iso = date_key(value)
    return iso[:7] if iso else s[:7]


def current_month(today: Optional[date] = None) -> str:
    d = today or date.today()
    return "%04d-%02d" % (d.year, d.month)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_loans(sheets) -> Dict[str, Loan]:
    """Read the Loans tab into {loan_id: Loan}. Missing tab → {}."""
    try:
        records = sheets.read_table(LOANS_TAB)
    except Exception:  # noqa: BLE001 — degrade, never fail the caller
        LOG.warning("%s tab not found — finance queries will be empty",
                    LOANS_TAB)
        return {}
    out: Dict[str, Loan] = {}
    for rec in records:
        lid = str(rec.get("loan_id") or "").strip()
        if not lid:
            continue
        principal = _f(rec.get("principal_mxn"))
        total = _f(rec.get("total_installments"))
        if principal is None or total is None:
            LOG.warning("%s: malformed row for %s — skipped", LOANS_TAB, lid)
            continue
        out[lid] = Loan(
            loan_id=lid,
            plant_key=str(rec.get("plant_key") or "").strip(),
            project_name=str(rec.get("project_name") or "").strip(),
            bank=str(rec.get("bank") or "").strip(),
            currency=str(rec.get("currency") or "MXN").strip().upper(),
            principal_mxn=principal,
            total_installments=int(total),
            first_month=_month_str(rec.get("first_month")),
            last_month=_month_str(rec.get("last_month")),
        )
    return out


def load_loan_schedule(sheets) -> List[ScheduleRow]:
    """Read the Loan_Schedule tab. Missing tab → []."""
    try:
        records = sheets.read_table(SCHEDULE_TAB)
    except Exception:  # noqa: BLE001
        LOG.warning("%s tab not found — finance queries will be empty",
                    SCHEDULE_TAB)
        return []
    rows: List[ScheduleRow] = []
    for rec in records:
        lid = str(rec.get("loan_id") or "").strip()
        month = _month_str(rec.get("ref_month"))
        pay = _f(rec.get("payment_mxn"))
        inst = _f(rec.get("installment_no"))
        total = _f(rec.get("total_installments"))
        due = _f(rec.get("due_after_mxn"))
        if not lid or not month or pay is None or inst is None:
            continue
        rows.append(ScheduleRow(
            loan_id=lid,
            plant_key=str(rec.get("plant_key") or "").strip(),
            ref_month=month,
            installment_no=int(inst),
            total_installments=int(total) if total is not None else 0,
            payment_mxn=pay,
            payment_ccy=_f(rec.get("payment_ccy")),
            xr=_f(rec.get("xr")),
            due_after_mxn=due if due is not None else 0.0,
        ))
    rows.sort(key=lambda r: (r.loan_id, r.ref_month))
    return rows


# ---------------------------------------------------------------------------
# Derived queries — the replacement for v1's stored Credit columns
# ---------------------------------------------------------------------------

def monthly_debt_service(schedule: List[ScheduleRow],
                         ref_month: str) -> Dict[str, float]:
    """{plant_key: Σ payment_mxn} for one month. Plants with no
    installment due that month are absent (service = 0)."""
    out: Dict[str, float] = {}
    for row in schedule:
        if row.ref_month == ref_month:
            out[row.plant_key] = out.get(row.plant_key, 0.0) + row.payment_mxn
    return out


def loan_payments_for_month(schedule: List[ScheduleRow],
                            ref_month: str) -> List[ScheduleRow]:
    """The individual installments due in a month (for report line
    items such as 'SLP1-L2 2/12: 12,500.00')."""
    return [r for r in schedule if r.ref_month == ref_month]


def outstanding_balance(schedule: List[ScheduleRow],
                        as_of_month: str) -> Dict[str, float]:
    """{loan_id: due_after_mxn of the latest row <= as_of_month}.

    Loans whose schedule starts after ``as_of_month`` report their
    earliest known balance (nothing has amortized yet). USD balances
    are MXN-restated at each row's rate.
    """
    latest: Dict[str, Tuple[str, float]] = {}
    earliest: Dict[str, Tuple[str, float]] = {}
    for row in schedule:
        if row.loan_id not in earliest or row.ref_month < earliest[row.loan_id][0]:
            earliest[row.loan_id] = (row.ref_month, row.due_after_mxn)
        if row.ref_month <= as_of_month:
            cur = latest.get(row.loan_id)
            if cur is None or row.ref_month > cur[0]:
                latest[row.loan_id] = (row.ref_month, row.due_after_mxn)
    out = {lid: bal for lid, (_, bal) in latest.items()}
    for lid, (_, bal) in earliest.items():
        out.setdefault(lid, bal)
    return out


def fx_exposure(schedule: List[ScheduleRow],
                ref_month: str) -> Tuple[float, float]:
    """(usd_denominated_mxn, total_mxn) of debt service in a month —
    the share of service that moves with the exchange rate."""
    usd = total = 0.0
    for row in schedule:
        if row.ref_month == ref_month:
            total += row.payment_mxn
            if row.is_usd:
                usd += row.payment_mxn
    return usd, total


def portfolio_debt_service(schedule: List[ScheduleRow],
                           ref_month: str) -> float:
    return sum(monthly_debt_service(schedule, ref_month).values())
