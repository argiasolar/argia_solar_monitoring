"""Contract design baseline (PVsyst/Helioscope monthly kWh).

WHY (2026-07-08): the weather-adjusted "expected" goes blind exactly when
measured irradiance fails (block days), and contracts are written against
the DESIGN estimate anyway — the Prologis-style triple is actual vs
expected (weather-adjusted) vs estimated (design). The `Design_Monthly`
tab carries the per-plant monthly design kWh (extracted from the
ARGIA_Solar `ExpectedkWh` contract data, per-year rows because the source
degrades ~0.5%/yr):

    plant_key | year | month | design_kwh

This module reads it and prorates to a daily figure (month total /
calendar days). Static data — no reliability gating needed; that is its
whole value.
"""

from __future__ import annotations

import calendar
import logging
from typing import Dict, Optional, Tuple

LOG = logging.getLogger(__name__)

# Contract_Monthly (v61) carries design_kwh as its 4th column with the
# same header names, so it is a drop-in primary source; the legacy
# Design_Monthly names remain as fallbacks during the transition.
DESIGN_TAB_CANDIDATES = ("Contract_Monthly", "Design_Monthly",
                         "design_monthly")
DesignMap = Dict[Tuple[str, int, int], float]


def load_design_monthly(sheets) -> DesignMap:
    """Read the design tab into {(plant_key, year, month): kwh}.

    Missing tab or malformed rows degrade to an empty/partial map with a
    log line — the baseline is an enhancement, never a failure mode."""
    # v191: the grid comes through the finance door (sheet tab or
    # PostgreSQL contract_monthly.design_kwh, per ARGIA_FINANCE_SOURCE).
    from argia.finance.pg_source import design_grid
    data, used = design_grid(sheets, DESIGN_TAB_CANDIDATES)
    return parse_design_grid(data, used)


def parse_design_grid(data, used=None) -> DesignMap:
    """The parser, source-agnostic (PURE): header + rows -> map."""
    if not data or len(data) < 2:
        LOG.warning("Design_Monthly tab not found or empty — 'vs design' "
                    "will be blank until it is filled")
        return {}

    header = [str(h or "").strip().lower() for h in data[0]]
    try:
        idx = {n: header.index(n)
               for n in ("plant_key", "year", "month", "design_kwh")}
    except ValueError:
        LOG.warning("%s: header must be plant_key|year|month|design_kwh "
                    "(got %s)", used, header)
        return {}

    out: DesignMap = {}
    bad = 0
    blank = 0
    for row in data[1:]:
        # Contract_Monthly carries design_kwh on a minority of its rows
        # (the PPA horizon rows have none) — a blank is "no design", not a
        # malformed row (v191.1: it used to log 1,163 'malformed' rows).
        i = idx["design_kwh"]
        if i >= len(row) or row[i] is None or str(row[i]).strip() == "":
            blank += 1
            continue
        try:
            pk = str(row[idx["plant_key"]]).strip().upper()
            year = int(float(row[idx["year"]]))
            month = int(float(row[idx["month"]]))
            kwh = float(row[idx["design_kwh"]])
            if pk and 1 <= month <= 12 and kwh > 0:
                out[(pk, year, month)] = kwh
            else:
                bad += 1
        except (TypeError, ValueError, IndexError):
            bad += 1
    if bad:
        LOG.warning("%s: skipped %d malformed row(s)", used, bad)
    LOG.info("Design baseline loaded: %d plant-month(s) from %s (%d rows "
             "without design_kwh)", len(out), used, blank)
    return out


def design_kwh_for_day(design: DesignMap, plant_key: str,
                       date_iso: str) -> Optional[float]:
    """Month design / calendar days for that month. None when the
    plant+year+month has no row (unfilled year, inactive plant)."""
    try:
        year, month, _day = (int(x) for x in date_iso.split("-"))
    except (ValueError, AttributeError):
        return None
    total = design.get((str(plant_key).upper(), year, month))
    if total is None:
        return None
    return round(total / calendar.monthrange(year, month)[1], 1)
