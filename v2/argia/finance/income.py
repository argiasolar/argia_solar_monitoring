"""Income, opex, debt service and DSCR over arbitrary date ranges.

The computation layer behind the investor/shareholder report. Pure
functions over already-loaded data (Contract_Monthly, Loan_Schedule,
KPI_Daily, PlantConfig) — the report renderer formats, this module
decides the numbers. Rules it encodes:

* Actual PPA income = Σ daily billable energy × the tariff in force
  THAT month. ``billable_kwh`` is preferred when the KPI has stamped it
  (maintenance-day compensada); until then ``energy_kwh`` is the
  billable quantity.
* LaaS income (expected and accrued) = USD fee × the XR the loan
  schedule uses for the same plant-month, so income and debt service
  always share an FX basis. If a plant's schedule rows disagree on the
  month's rate (possible once a second loan exists), the first is used
  and a warning is logged — divergence means someone updated one loan's
  projection and not the other.
* Debt service and O&M are monthly lumps; any partial period prorates
  them by elapsed calendar days (the "don't look bankrupt on the 9th"
  rule). Revenue is never prorated — it accrues daily by nature.
* DSCR = income / debt service, no IVA anywhere.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from argia.core.normalize import safe_float
from argia.finance.contract import ContractMonth, MonthKey
from argia.finance.loans import ScheduleRow
from argia.report.daily import date_key

LOG = logging.getLogger(__name__)

KPI_DAILY_TAB = "KPI_Daily"


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    """Inclusive date range."""
    start: date
    end: date

    @classmethod
    def from_iso(cls, start_iso: str, end_iso: str) -> "Period":
        s = date.fromisoformat(start_iso)
        e = date.fromisoformat(end_iso)
        if e < s:
            raise ValueError("period end before start")
        return cls(s, e)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def month_overlaps(self) -> List[Tuple[int, int, int, int]]:
        """[(year, month, overlap_days, days_in_month)] for every
        calendar month the period touches."""
        out = []
        cursor = date(self.start.year, self.start.month, 1)
        while cursor <= self.end:
            dim = calendar.monthrange(cursor.year, cursor.month)[1]
            m_start = cursor
            m_end = date(cursor.year, cursor.month, dim)
            lo = max(m_start, self.start)
            hi = min(m_end, self.end)
            out.append((cursor.year, cursor.month,
                        (hi - lo).days + 1, dim))
            cursor = m_end + timedelta(days=1)
        return out

    def contains_iso(self, date_iso) -> bool:
        iso = date_key(date_iso)
        if not iso:
            return False
        d = date.fromisoformat(iso)
        return self.start <= d <= self.end


# ---------------------------------------------------------------------------
# KPI_Daily energy (actuals)
# ---------------------------------------------------------------------------

def load_kpi_energy(sheets, period: Period) -> Dict[Tuple[str, str], float]:
    """{(plant_key, 'YYYY-MM-DD'): billable kWh} for days inside the
    period. Prefers the ``billable_kwh`` column (stamped once the
    maintenance/penalty feature lands); falls back to ``energy_kwh``.
    Missing tab → {}."""
    try:
        data = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
    except Exception:  # noqa: BLE001
        LOG.warning("%s not readable — actual income unavailable",
                    KPI_DAILY_TAB)
        return {}
    if not data or len(data) < 2:
        return {}
    header = [str(h or "").strip().lower() for h in data[0]]
    idx = {n: i for i, n in enumerate(header) if n}
    if "date_iso" not in idx or "plant_key" not in idx:
        LOG.warning("%s: header lacks date_iso/plant_key", KPI_DAILY_TAB)
        return {}
    bill_col = idx.get("billable_kwh")
    energy_col = idx.get("energy_kwh")
    if bill_col is None and energy_col is None:
        LOG.warning("%s: no billable_kwh/energy_kwh column", KPI_DAILY_TAB)
        return {}

    out: Dict[Tuple[str, str], float] = {}
    for row in data[1:]:
        try:
            d_iso = date_key(row[idx["date_iso"]])
            pk = str(row[idx["plant_key"]]).strip().upper()
        except IndexError:
            continue
        if not pk or not d_iso or not period.contains_iso(d_iso):
            continue
        # v91: prefer billable_kwh PER ROW, but a blank billable cell must
        # NOT zero a day — fall back to energy_kwh for that row. The
        # migration back-fills history and kpi_eod stamps billable going
        # forward, so this is belt-and-suspenders: a manually inserted or
        # half-stamped row silently dropped its income before this.
        kwh = None
        if bill_col is not None and bill_col < len(row):
            kwh = safe_float(row[bill_col])   # comma-tolerant; v64 incident
        if kwh is None and energy_col is not None and energy_col < len(row):
            kwh = safe_float(row[energy_col])
        if kwh is None:
            continue
        out[(pk, d_iso)] = kwh
    return out


# ---------------------------------------------------------------------------
# FX rate resolution (LaaS)
# ---------------------------------------------------------------------------

def xr_for_month(schedule: Iterable[ScheduleRow], plant_key: str,
                 year: int, month: int) -> Optional[float]:
    """The XR the loan schedule uses for this plant-month; None when the
    plant has no USD rows that month. Divergent rates (multiple loans,
    inconsistent projections) log a warning and return the first."""
    ym = "%04d-%02d" % (year, month)
    rates = [r.xr for r in schedule
             if r.plant_key == plant_key and r.ref_month == ym
             and r.xr is not None]
    if not rates:
        return None
    first = rates[0]
    if any(abs(r - first) > first * 0.005 for r in rates[1:]):
        LOG.warning("xr_for_month(%s %s): loan rows disagree on the rate "
                    "(%s) — using %.4f; align the projections",
                    plant_key, ym, rates, first)
    return first


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------

def expected_income_month(contracts: Dict[MonthKey, ContractMonth],
                          schedule: Iterable[ScheduleRow],
                          plant_key: str, year: int,
                          month: int) -> Optional[float]:
    """Contracted income for one plant-month, MXN. LaaS fees convert at
    the loan schedule's XR for that month."""
    row = contracts.get((plant_key.upper(), year, month))
    if row is None:
        return None
    if row.is_laas:
        return row.expected_income_mxn(
            xr=xr_for_month(schedule, plant_key.upper(), year, month))
    return row.expected_income_mxn()


def actual_income(kpi_energy: Dict[Tuple[str, str], float],
                  contracts: Dict[MonthKey, ContractMonth],
                  schedule: Iterable[ScheduleRow],
                  plant_key: str, period: Period,
                  tariff_fallback: Optional[float] = None
                  ) -> Optional[float]:
    """Accrued income for the period, MXN.

    PPA: Σ daily billable × that month's tariff (fallback supports the
    Cleaning_Costs scalar during transition). LaaS: fee × XR, prorated
    by elapsed days. None when nothing is computable (e.g. PPA plant
    with no KPI rows in range)."""
    pk = plant_key.upper()
    laas_row = None
    for (y, m, _, _) in period.month_overlaps():
        r = contracts.get((pk, y, m))
        if r is not None and r.is_laas:
            laas_row = True
            break
    if laas_row:
        total = 0.0
        found = False
        for (y, m, overlap, dim) in period.month_overlaps():
            r = contracts.get((pk, y, m))
            if r is None or not r.is_laas:
                continue
            inc = r.expected_income_mxn(xr=xr_for_month(schedule, pk, y, m))
            if inc is None:
                continue
            total += inc * overlap / dim
            found = True
        return total if found else None

    total = 0.0
    found = False
    for (pk2, d_iso), kwh in kpi_energy.items():
        if pk2 != pk:
            continue
        y, m = int(d_iso[:4]), int(d_iso[5:7])
        row = contracts.get((pk, y, m))
        tariff = (row.tariff_mxn if row is not None and
                  row.tariff_mxn is not None else tariff_fallback)
        if tariff is None:
            continue
        total += kwh * tariff
        found = True
    return total if found else None


# ---------------------------------------------------------------------------
# Debt service & opex over periods
# ---------------------------------------------------------------------------

def debt_service_for_period(schedule: Iterable[ScheduleRow],
                            plant_key: str, period: Period,
                            prorate: bool = True) -> float:
    """Σ installments over the period's months, prorated by day overlap
    (unless ``prorate=False`` for whole-month reporting)."""
    pk = plant_key.upper()
    total = 0.0
    for (y, m, overlap, dim) in period.month_overlaps():
        ym = "%04d-%02d" % (y, m)
        month_sum = sum(r.payment_mxn for r in schedule
                        if r.plant_key == pk and r.ref_month == ym)
        total += month_sum * (overlap / dim if prorate else 1.0)
    return total


def om_cost_for_period(om_monthly: Optional[float],
                       period: Period) -> float:
    """Monthly O&M scalar prorated across the period. None → 0.0 (the
    report footnotes plants without a figure)."""
    if not om_monthly:
        return 0.0
    return sum(om_monthly * overlap / dim
               for (_, _, overlap, dim) in period.month_overlaps())


def dscr(income: Optional[float], service: float) -> Optional[float]:
    """income / debt service; None when either side is absent. A plant
    with no debt has no DSCR (not infinity)."""
    if income is None or service <= 0:
        return None
    return income / service
