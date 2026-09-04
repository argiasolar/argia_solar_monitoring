"""Grid emission factor for "avoided CO2" claims — the single source.

The factor is published per year by SEMARNAT/CRE for the Mexican grid
(kg CO2e per kWh delivered). Tomasz set the register on 2026-09-04:

    2020  0.494
    2021  0.423
    2022  0.435
    2023  0.438
    2024  0.444   <- currently applicable, so 2025+ uses it too

A year later than the newest entry uses the newest entry (the factor
stays in force until CRE publishes the next one); a year earlier than
the oldest uses the oldest. Neither ever raises — a report must never
fail to render over a CO2 line.

PLANT OVERRIDES: a customer may contract a different factor. SAG (MEX1)
asked for 0.202 kg/kWh across their whole history, so their invoices and
pages use that for every year. An override is not a fallback — it wins
over the year table outright.

Everything here is pure. The server bundle cannot import this package,
so report_gen.py carries a literal copy that test_constants.py keeps
identical — change this table and that test tells you what else to edit.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

# year -> kg CO2e per kWh (SEMARNAT/CRE, Mexican national grid)
FACTOR_BY_YEAR: Dict[int, float] = {
    2020: 0.494,
    2021: 0.423,
    2022: 0.435,
    2023: 0.438,
    2024: 0.444,
}

# plant_key -> kg CO2e per kWh, all years. Customer-contracted values.
PLANT_OVERRIDE: Dict[str, float] = {
    "MEX1": 0.202,        # SAG — customer asked for this across all history
}

_FIRST_YEAR = min(FACTOR_BY_YEAR)
_LAST_YEAR = max(FACTOR_BY_YEAR)

#: The currently applicable national factor (the newest published year).
CURRENT = FACTOR_BY_YEAR[_LAST_YEAR]


def factor(year: Optional[int] = None,
           plant_key: Optional[str] = None) -> float:
    """kg CO2e per kWh for one year and (optionally) one plant.

    ``year`` None means the current calendar year. ``plant_key`` with a
    contracted override returns that override regardless of year.
    """
    if plant_key:
        override = PLANT_OVERRIDE.get(str(plant_key).strip().upper())
        if override is not None:
            return override
    if year is None:
        year = dt.date.today().year
    year = int(year)
    if year < _FIRST_YEAR:
        return FACTOR_BY_YEAR[_FIRST_YEAR]
    if year > _LAST_YEAR:
        return FACTOR_BY_YEAR[_LAST_YEAR]
    return FACTOR_BY_YEAR[year]


def factor_for_month(ym: str, plant_key: Optional[str] = None) -> float:
    """Same, addressed by an 'YYYY-MM' month string."""
    try:
        return factor(int(str(ym)[:4]), plant_key)
    except (TypeError, ValueError):
        return factor(None, plant_key)


def factors_by_year(plant_key: Optional[str] = None,
                    first: int = _FIRST_YEAR,
                    last: Optional[int] = None) -> Dict[str, float]:
    """{'2024': 0.444, ...} for handing to a page's JavaScript, so a
    browser-side range sum can be exact per year instead of applying one
    scalar across a multi-year range."""
    last = last or max(_LAST_YEAR, dt.date.today().year + 1)
    return {str(y): factor(y, plant_key) for y in range(first, last + 1)}


def label(plant_key: Optional[str] = None,
          year: Optional[int] = None) -> str:
    """Human sub-label for a CO2 tile, e.g. '0.444 kg CO2/kWh (CRE)' or
    the contracted note when the plant has an override."""
    f = factor(year, plant_key)
    if plant_key and str(plant_key).strip().upper() in PLANT_OVERRIDE:
        return "%.3f kg CO2/kWh (contracted)" % f
    return "%.3f kg CO2/kWh (SEMARNAT/CRE)" % f
