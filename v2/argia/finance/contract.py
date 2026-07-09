"""Contract_Monthly — the commercial expectations table.

One row per plant per month, covering the full contract horizon
(2024..2043 in the migration seed):

    plant_key | year | month | design_kwh | contract_kwh | tariff_mxn
              | fixed_income_ccy | ccy

Column semantics (settled 2026-07, after the GTO1 phase-2 analysis):

* ``design_kwh``       engineering expectation for the AS-BUILT plant
                       (818 kWp GTO1), degradation clocked from actual
                       COD. Feeds "% of design". Same values the old
                       Design_Monthly tab carried.
* ``contract_kwh``     what the PPA obliges (606 kWp GTO1 until the
                       extension is signed). Feeds maintenance-day
                       penalties ("energía compensada") and expected
                       income. The two columns diverge exactly in the
                       window between an expansion's commissioning and
                       its contract amendment.
* ``tariff_mxn``       the PPA tariff for THAT month — escalations are
                       row edits, so issued months stay immutable.
* ``fixed_income_ccy`` LaaS monthly fee in its native currency (both
                       current LaaS fees are USD-indexed); blank for
                       PPA rows. MXN conversion happens at query time
                       with the same XR the loan schedule uses.

Loaders degrade to empty on a missing tab, like every other config
reader in this codebase.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

LOG = logging.getLogger(__name__)

CONTRACT_TAB = "Contract_Monthly"
CONTRACT_HEADER = [
    "plant_key", "year", "month", "design_kwh", "contract_kwh",
    "tariff_mxn", "fixed_income_ccy", "ccy",
]

MonthKey = Tuple[str, int, int]


@dataclass(frozen=True)
class ContractMonth:
    plant_key: str
    year: int
    month: int
    design_kwh: Optional[float]
    contract_kwh: Optional[float]
    tariff_mxn: Optional[float]
    fixed_income_ccy: Optional[float]
    ccy: str                       # "" for PPA rows, "USD" for LaaS

    @property
    def is_laas(self) -> bool:
        return self.fixed_income_ccy is not None

    @property
    def days_in_month(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def contract_kwh_daily(self) -> Optional[float]:
        """The maintenance-day penalty basis: contracted monthly
        generation / calendar days. None for LaaS or unfilled rows."""
        if self.contract_kwh is None:
            return None
        return self.contract_kwh / self.days_in_month

    def expected_income_mxn(self, xr: Optional[float] = None
                            ) -> Optional[float]:
        """Contracted income for the month.

        PPA: contract_kwh × tariff (no FX involved; ``xr`` ignored).
        LaaS: fixed_income_ccy × xr — the caller supplies the rate,
        normally the one the loan schedule uses for the same month so
        income and debt service always share an FX basis. Returns None
        when the row lacks the needed inputs (including a LaaS row
        asked without a rate).
        """
        if self.is_laas:
            if xr is None:
                return None
            return self.fixed_income_ccy * xr
        if self.contract_kwh is None or self.tariff_mxn is None:
            return None
        return self.contract_kwh * self.tariff_mxn


def _f(value) -> Optional[float]:
    # comma-tolerant (Sheets FORMATTED values); see v64 incident
    from argia.core.normalize import safe_float
    return safe_float(value)


def load_contract_monthly(sheets) -> Dict[MonthKey, ContractMonth]:
    """Read Contract_Monthly into {(plant_key, year, month): row}.
    Missing tab → {} with a warning."""
    try:
        data = sheets.read_range(CONTRACT_TAB, "A1:H")
    except Exception:  # noqa: BLE001
        LOG.warning("%s tab not found — contract expectations, tariffs "
                    "and penalty bases will be unavailable", CONTRACT_TAB)
        return {}
    if not data or len(data) < 2:
        LOG.warning("%s tab is empty", CONTRACT_TAB)
        return {}

    header = [str(h or "").strip().lower() for h in data[0]]
    try:
        idx = {n: header.index(n) for n in CONTRACT_HEADER}
    except ValueError:
        LOG.warning("%s: header must be %s (got %s)", CONTRACT_TAB,
                    "|".join(CONTRACT_HEADER), header)
        return {}

    def cell(row, name):
        i = idx[name]
        return row[i] if i < len(row) else None

    out: Dict[MonthKey, ContractMonth] = {}
    bad = 0
    for row in data[1:]:
        try:
            pk = str(cell(row, "plant_key") or "").strip().upper()
            year = int(_f(cell(row, "year")))
            month = int(_f(cell(row, "month")))
        except (TypeError, ValueError):
            bad += 1
            continue
        if not pk or not (1 <= month <= 12):
            bad += 1
            continue
        out[(pk, year, month)] = ContractMonth(
            plant_key=pk, year=year, month=month,
            design_kwh=_f(cell(row, "design_kwh")),
            contract_kwh=_f(cell(row, "contract_kwh")),
            tariff_mxn=_f(cell(row, "tariff_mxn")),
            fixed_income_ccy=_f(cell(row, "fixed_income_ccy")),
            ccy=str(cell(row, "ccy") or "").strip().upper(),
        )
    if bad:
        LOG.warning("%s: skipped %d malformed row(s)", CONTRACT_TAB, bad)
    LOG.info("Contract_Monthly loaded: %d plant-month(s)", len(out))
    return out


def tariff_for_month(contracts: Dict[MonthKey, ContractMonth],
                     plant_key: str, year: int, month: int,
                     fallback: Optional[float] = None) -> Optional[float]:
    """The tariff in force for a plant-month. ``fallback`` supports the
    transition period while the legacy Cleaning_Costs scalar still
    exists; pass ``plant.tariff_mxn_per_kwh`` there."""
    row = contracts.get((str(plant_key).upper(), year, month))
    if row is not None and row.tariff_mxn is not None:
        return row.tariff_mxn
    return fallback
