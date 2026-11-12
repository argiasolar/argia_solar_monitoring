"""HelioScope as-connected design baseline for O&M monitoring (v370).

Tomasz, 12 Nov 2026, with Arturo's Nov 2026 HelioScope runs of the PPA plants: "we should use them as our
baseline for all calculations and monitoring, change expectations and calibrate better, as these designs are
exactly showing how the systems are built and how we should approach soiling, angles etc". Decided the same day:
INTERNAL ONLY - the customer invoice annex keeps the contract curve ("expectativa") and today's "teorica", so
``contract_monthly`` and ``daily_production.expected_kwh`` are never touched by this module. Ageing: 0.55 %/yr
from each plant's interconnection date (the rate the contract curves use).

One design per plant, server-only JSON (customer data, never in the repo)::

    {"version": 1, "plants": {"SLP1": {
        "source": "...", "report_date": "YYYY-MM-DD", "kwp_dc": 187.88, "kwp_ac": 150.0,
        "sensor": "ghi" | "poa",          # the plane the plant's pyranometer measures
        "cod": "YYYY-MM-DD", "degradation": 0.0055,
        "annual_kwh": 321216.2,           # the report's Energy to Grid (checked against the months)
        "months": [{"ghi": .., "poa": .., "grid_kwh": .., "soiling_pct": ..} x 12],
        optional: "weather", "condition_set", "pr", "losses": {name: pct}, "caveat"}}}

The figures (pure):

* design kWh of a month  = the report's Grid kWh x ageing at mid-month
* design-expected kWh    = Grid kWh / design irradiance of that month (on the sensor's plane) x measured
                           irradiance x ageing: what the design predicts for the sun that actually came
* EPI (energy performance index) = actual / design-expected; in line = within IN_LINE (3 %)
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

LOG = logging.getLogger(__name__)

PATH_ENV = "ARGIA_DESIGN_FILE"
DEFAULT_PATH = "/opt/argia/design/ppa_designs.json"
BASES = ("ghi", "poa")
DEFAULT_DEGRADATION = 0.0055
IN_LINE = 0.03            # "In line means within 3%" (design base preview, Nov 2026)
PR_MAX = 1.05             # a day above this raw PR has an undercounted sun: never averaged (as report_gen)
ANNUAL_TOL = 0.005        # the months must add up to the report's annual figure


class Invalid(ValueError):
    """The design file is wrong; the message lists every problem at once."""


@dataclass(frozen=True)
class Month:
    ghi: float
    poa: float
    grid_kwh: float
    soiling_pct: Optional[float] = None


@dataclass(frozen=True)
class Design:
    plant_key: str
    source: str
    report_date: str
    kwp_dc: float
    kwp_ac: Optional[float]
    sensor: str
    cod: dt.date
    degradation: float
    months: Tuple[Month, ...]
    weather: str = ""
    condition_set: str = ""
    pr: Optional[float] = None
    losses: Tuple[Tuple[str, float], ...] = ()
    caveat: str = ""

    @property
    def annual_kwh(self) -> float:
        return sum(m.grid_kwh for m in self.months)

    def ageing(self, day: dt.date) -> float:
        """(1 - degradation) ** years since interconnection; 1.0 before it."""
        years = max(0.0, (day - self.cod).days / 365.25)
        return (1.0 - self.degradation) ** years

    def per_irradiance(self, month: int) -> float:
        """Year-1 design kWh per kWh/m2 measured on the sensor's plane, for a calendar month."""
        m = self.months[month - 1]
        basis = m.ghi if self.sensor == "ghi" else m.poa
        return m.grid_kwh / basis

    def design_kwh_month(self, year: int, month: int) -> float:
        return self.months[month - 1].grid_kwh * self.ageing(dt.date(year, month, 15))

    def design_kwh_year(self, year: int) -> float:
        return sum(self.design_kwh_month(year, m) for m in range(1, 13))

    def design_kwh_day(self, day: dt.date) -> float:
        return self.design_kwh_month(day.year, day.month) / calendar.monthrange(day.year, day.month)[1]

    def expected_kwh(self, day: dt.date, irradiance_kwh_m2: float) -> float:
        return self.per_irradiance(day.month) * irradiance_kwh_m2 * self.ageing(day)

    def factor(self, day: dt.date) -> float:
        """Design kWh per kWh/m2 of measured sun on ``day`` (aged) - the design's 'expected factor x kWp'."""
        return self.per_irradiance(day.month) * self.ageing(day)

    def clean_expected_kwh(self, day: dt.date, irradiance_kwh_m2: float) -> float:
        """The design's expectation with clean modules (its own soiling loss of the month put back)."""
        soil = self.months[day.month - 1].soiling_pct or 0.0
        return self.expected_kwh(day, irradiance_kwh_m2) / (1.0 - soil / 100.0)

    def resource(self, year: int, month: int, measured_kwh_m2: float, days_measured: int) -> Optional[float]:
        """Measured sun vs the design year's sun for the same days (1.0 = a typical month). Pure."""
        if days_measured <= 0:
            return None
        m = self.months[month - 1]
        basis = m.ghi if self.sensor == "ghi" else m.poa
        typical = basis * days_measured / calendar.monthrange(year, month)[1]
        return measured_kwh_m2 / typical if typical > 0 else None


# ------------------------------------------------------------------ parse / load
_CODE = re.compile(r"^[A-Z0-9]{2,8}$")


def _num(v, what: str, errs: list, positive: bool = True) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        errs.append(f"{what}: not a number ({v!r})")
        return None
    if positive and not x > 0:
        errs.append(f"{what}: must be above 0 ({x})")
        return None
    return x


def _date(v, what: str, errs: list) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        errs.append(f"{what}: not a YYYY-MM-DD date ({v!r})")
        return None


def parse(obj: Mapping) -> Dict[str, Design]:
    """The design file as {plant_key: Design}; raises Invalid listing every problem. Pure."""
    errs: list = []
    out: Dict[str, Design] = {}
    plants = (obj or {}).get("plants") if isinstance(obj, Mapping) else None
    if not isinstance(plants, Mapping):
        raise Invalid('the file needs {"plants": {...}}')
    for code, p in plants.items():
        n0 = len(errs)
        key = str(code).upper()
        w = f"{key}"
        if not _CODE.match(key):
            errs.append(f"{w}: bad plant key")
        if not isinstance(p, Mapping):
            errs.append(f"{w}: not an object")
            continue
        kwp = _num(p.get("kwp_dc"), f"{w} kwp_dc", errs)
        kwp_ac = _num(p.get("kwp_ac"), f"{w} kwp_ac", errs) if p.get("kwp_ac") is not None else None
        sensor = str(p.get("sensor", "")).lower()
        if sensor not in BASES:
            errs.append(f"{w} sensor: must be one of {BASES} (the plane the pyranometer measures)")
        cod = _date(p.get("cod"), f"{w} cod", errs)
        rdate = _date(p.get("report_date"), f"{w} report_date", errs)
        deg = _num(p.get("degradation", DEFAULT_DEGRADATION), f"{w} degradation", errs, positive=False)
        if deg is not None and not 0.0 <= deg < 0.05:
            errs.append(f"{w} degradation: {deg} is not a yearly fraction (0.0055 = 0.55 %/yr)")
        if not str(p.get("source", "")).strip():
            errs.append(f"{w} source: say which report this is")
        ms = p.get("months")
        months = []
        if not isinstance(ms, Sequence) or len(ms) != 12:
            errs.append(f"{w} months: need exactly 12")
        else:
            for i, m in enumerate(ms, 1):
                if not isinstance(m, Mapping):
                    errs.append(f"{w} month {i}: not an object")
                    continue
                ghi = _num(m.get("ghi"), f"{w} month {i} ghi", errs)
                poa = _num(m.get("poa"), f"{w} month {i} poa", errs)
                grid = _num(m.get("grid_kwh"), f"{w} month {i} grid_kwh", errs)
                soil = m.get("soiling_pct")
                soil = _num(soil, f"{w} month {i} soiling_pct", errs, positive=False) if soil is not None else None
                months.append(Month(ghi or 0.0, poa or 0.0, grid or 0.0, soil))
        ann = p.get("annual_kwh")
        if ann is not None and len(months) == 12:
            a = _num(ann, f"{w} annual_kwh", errs)
            tot = sum(m.grid_kwh for m in months)
            if a and abs(tot - a) > ANNUAL_TOL * a:
                errs.append(f"{w}: the months add up to {tot:,.0f} kWh, the report says {a:,.0f}")
        if kwp and len(months) == 12 and months[0].grid_kwh:
            yld = sum(m.grid_kwh for m in months) / kwp
            if not 800 <= yld <= 2600:
                errs.append(f"{w}: {yld:,.0f} kWh/kWp a year is not a Mexican PV plant - kWp or kWh wrong")
        pr = p.get("pr")
        pr = _num(pr, f"{w} pr", errs) if pr is not None else None
        losses = tuple((str(k), float(v)) for k, v in (p.get("losses") or {}).items()
                       if isinstance(v, (int, float)))
        if len(errs) == n0:
            out[key] = Design(key, str(p["source"]).strip(), rdate.isoformat(), kwp, kwp_ac, sensor, cod,
                              deg, tuple(months), str(p.get("weather", "")), str(p.get("condition_set", "")),
                              pr, losses, str(p.get("caveat", "")))
    if errs:
        raise Invalid("; ".join(errs))
    return out


def load(path: Optional[str] = None, strict: bool = False) -> Dict[str, Design]:
    """The designs on this server. No file -> {} (nothing changes anywhere). A broken file is logged and
    ignored by the pages (strict=False) - the deploy checks it with strict=True first."""
    path = path or os.environ.get(PATH_ENV) or DEFAULT_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return parse(json.load(fh))
    except (OSError, ValueError) as ex:                 # Invalid is a ValueError, so is bad JSON
        if strict:
            raise
        LOG.error("design file %s ignored: %s", path, ex)
        return {}


# ------------------------------------------------------------------ EPI
@dataclass(frozen=True)
class Epi:
    epi: Optional[float]
    actual_kwh: float
    expected_kwh: float
    days: int
    skipped: int            # days with a sun the plant could not have used (raw PR > PR_MAX)


def epi(design: Design, days: Iterable[Tuple[dt.date, Optional[float], Optional[float]]],
        kwp_dc: Optional[float] = None) -> Epi:
    """EPI over (day, energy kWh, irradiance kWh/m2) rows. A day counts when both are measured and the
    sun is above zero; a day whose raw PR (energy / kWp / sun) is above PR_MAX is skipped - its
    irradiance was undercounted, and it would inflate the index. Pure."""
    kwp = kwp_dc or design.kwp_dc
    act = exp = 0.0
    n = skipped = 0
    for day, e, irr in days:
        if e is None or irr is None or irr <= 0:
            continue
        if e / (kwp * irr) > PR_MAX:
            skipped += 1
            continue
        act += e
        exp += design.expected_kwh(day, irr)
        n += 1
    return Epi(act / exp if exp > 0 else None, act, exp, n, skipped)


def band(x: Optional[float], tol: float = IN_LINE) -> Optional[str]:
    """'in line' within tol, else 'above' / 'below'; None without a figure. Pure."""
    if x is None:
        return None
    if x > 1.0 + tol:
        return "above"
    if x < 1.0 - tol:
        return "below"
    return "in line"


def design_vs_contract(design: Design, year: int, contract_kwh: Mapping[int, float]) -> Optional[float]:
    """Aged design of the year / contracted energy of the year - only with all 12 contract months. Pure."""
    if len([m for m in range(1, 13) if (contract_kwh.get(m) or 0) > 0]) < 12:
        return None
    return design.design_kwh_year(year) / sum(contract_kwh[m] for m in range(1, 13))


# ------------------------------------------------------------------ v372: one internal expectation
def internal_expected(designs: Mapping[str, Design], plant_key: str, day, irradiance, stored) -> Tuple[Optional[float], str]:
    """(expected kWh, basis) for INTERNAL readers (v372, Tomasz 12 Nov 2026: "Separate internal figure"):
    the plant's design for the sun that was measured ('design'), else the stored kWp x sun x factor
    ('factor'). The stored daily_production.expected_kwh itself is never changed - it is the invoice
    annex 'teorica'. Pure."""
    d = designs.get(str(plant_key).upper()) if designs else None
    try:
        irr = float(irradiance) if irradiance not in (None, "") else None
    except (TypeError, ValueError):
        irr = None
    if d is not None and irr is not None and irr > 0:
        if not isinstance(day, dt.date):
            day = dt.date.fromisoformat(str(day)[:10])
        if day >= d.cod:
            return round(d.expected_kwh(day, irr), 2), "design"
    try:
        return (float(stored) if stored not in (None, "") else None), "factor"
    except (TypeError, ValueError):
        return None, "factor"


def soiling_drift(design: Design, day: dt.date, energy, irradiance) -> Optional[float]:
    """1 - actual / clean-module design expectation, never negative: soiling plus every other loss the
    design does not foresee - against the design's own month (seasonal), not a flat baseline. Pure."""
    try:
        e, irr = float(energy), float(irradiance)
    except (TypeError, ValueError):
        return None
    if irr <= 0 or day < design.cod:
        return None
    clean = design.clean_expected_kwh(day, irr)
    if clean <= 0 or e / clean > 1.5:              # an undercounted sun, not a clean plant
        return None
    return round(max(0.0, 1.0 - e / clean), 4)
