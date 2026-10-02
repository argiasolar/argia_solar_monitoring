"""ARGIA for CPA - clean energy and CO2 figures for a partner's sites (v296).

CPA (an industrial real estate partner) and ARGIA deliver solar and LED
lighting to CPA's tenants. cpa.argia.com.mx shows CPA the energy those
sites produce and the CO2 it avoids, and gives its sustainability team a
clean energy report. Which plants belong to the programme is server-only
configuration (/opt/argia/cpa/cpa.json) - this module is pure and knows
no plant, name or price.

Rules (the same as every ARGIA page, so the numbers never disagree):

* energy = ``daily_production.energy_kwh`` (the KPI layer every report and
  invoice uses) for closed days, plus today's live counter from telemetry;
* avoided CO2 = energy x ``argia.core.co2.factor(year, plant)`` - the one
  register (SEMARNAT/CRE grid factor by year, or a factor a customer
  contracted), month by month;
* equivalences use the US EPA Greenhouse Gas Equivalencies figures named
  below, and are labelled as illustrations, never as measured values;
* the LED estimator is an estimate from the visitor's own inputs - no LED
  result is ever shown that was not measured.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from argia.core import co2 as co2reg

# US EPA, Greenhouse Gas Equivalencies Calculator (references page)
TREE_T_CO2 = 0.060          # t CO2 per urban tree seedling grown for 10 years
CAR_T_CO2_YEAR = 4.6        # t CO2 per typical passenger vehicle per year
GASOLINE_KG_CO2_L = 8.887 / 3.78541   # 8.887 kg CO2 per US gallon -> per litre (2.348)


@dataclass(frozen=True)
class Site:
    key: str                 # internal plant key - never shown
    name: str                # the tenant / customer name shown on the site
    city: str
    kwp: float
    lat: Optional[float] = None
    lon: Optional[float] = None
    approx: bool = False     # location is approximate (shown as such)
    slug: str = ""

    @property
    def page(self) -> str:
        return f"/sites/{self.slug}/"


@dataclass
class Live:
    kw: float = 0.0              # summed inverter power, last 30 min
    today_kwh: float = 0.0       # summed inverter day counters
    age_min: Optional[float] = None   # minutes since the newest reading (None = nothing today)
    curve: List[Tuple[str, float]] = field(default_factory=list)   # ("HH:MM", kW) every 5 min


def slugify(name: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "site"


def ym(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def months_between(first: str, last: str) -> List[str]:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def monthly(daily: Dict[str, Sequence[Tuple[dt.date, Optional[float]]]]) -> Dict[str, Dict[str, float]]:
    """{site: [(date, kWh)]} -> {site: {YYYY-MM: kWh}}; an unknown day (None) adds nothing."""
    out: Dict[str, Dict[str, float]] = {}
    for key, rows in daily.items():
        m = out.setdefault(key, {})
        for d, kwh in rows:
            if kwh is None:
                continue
            m[ym(d)] = m.get(ym(d), 0.0) + float(kwh)
    return out


def co2_t(key: str, month: str, kwh: float) -> float:
    return kwh * co2reg.factor_for_month(month, key) / 1000.0


def first_day(daily: Dict[str, Sequence[Tuple[dt.date, Optional[float]]]], key: str) -> Optional[dt.date]:
    days = [d for d, kwh in daily.get(key, ()) if kwh]
    return min(days) if days else None


@dataclass
class Totals:
    kwh: float = 0.0
    co2_t: float = 0.0

    def add(self, kwh: float, t: float) -> None:
        self.kwh += kwh
        self.co2_t += t


def period_totals(sites: Sequence[Site], mon: Dict[str, Dict[str, float]], first: str, last: str) -> Dict[str, Totals]:
    """kWh and t CO2 per site (and "_all") over the months first..last inclusive."""
    out = {s.key: Totals() for s in sites}
    out["_all"] = Totals()
    for s in sites:
        for m, kwh in mon.get(s.key, {}).items():
            if first <= m <= last:
                t = co2_t(s.key, m, kwh)
                out[s.key].add(kwh, t)
                out["_all"].add(kwh, t)
    return out


def live_totals(sites: Sequence[Site], live: Dict[str, Live], today: dt.date) -> Dict[str, Totals]:
    """Today's energy (not yet in daily_production) and its CO2."""
    out = {s.key: Totals() for s in sites}
    out["_all"] = Totals()
    for s in sites:
        x = live.get(s.key)
        if x and x.today_kwh:
            t = co2_t(s.key, ym(today), x.today_kwh)
            out[s.key].add(x.today_kwh, t)
            out["_all"].add(x.today_kwh, t)
    return out


def equivalents(t_co2: float) -> Dict[str, float]:
    return {"trees": t_co2 / TREE_T_CO2, "cars_year": t_co2 / CAR_T_CO2_YEAR,
            "gasoline_l": t_co2 * 1000.0 / GASOLINE_KG_CO2_L}


def specific_yield(site: Site, mon: Dict[str, Dict[str, float]], last: str) -> Optional[float]:
    """kWh per kWp over the 12 closed months ending with ``last``; None
    when the site has not produced for 12 full months yet."""
    months = months_between(_shift(last, -11), last)
    m = mon.get(site.key, {})
    if any(x not in m for x in months) or not site.kwp:
        return None
    return sum(m[x] for x in months) / site.kwp


def _shift(month: str, n: int) -> str:
    y, mm = int(month[:4]), int(month[5:7]) - 1 + n
    return f"{y + mm // 12:04d}-{mm % 12 + 1:02d}"


def previous_month(today: dt.date) -> str:
    return _shift(ym(today), -1)


def status(x: Optional[Live], in_window: bool) -> str:
    """'live' (reading in the last 30 min), 'stale', 'dark' (nothing today
    in daylight) or 'night'."""
    if not in_window:
        return "night"
    if x is None or x.age_min is None:
        return "dark"
    return "live" if x.age_min <= 30 else "stale"


def csv_monthly(sites: Sequence[Site], mon: Dict[str, Dict[str, float]], months: Sequence[str]) -> str:
    """One row per site and month: the data behind the report, for the
    sustainability team's own tools (GRESB, GHG inventory)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["site", "month", "solar_energy_kwh", "grid_factor_kg_co2e_per_kwh", "avoided_t_co2e", "installed_kwp"])
    for s in sites:
        for m in months:
            kwh = mon.get(s.key, {}).get(m)
            if kwh is None:
                continue
            f = co2reg.factor_for_month(m, s.key)
            w.writerow([s.name, m, f"{kwh:.1f}", f"{f:.3f}", f"{kwh * f / 1000:.3f}", f"{s.kwp:.2f}"])
    return buf.getvalue()


# ------------------------------------------------------------------ LED estimator
def led_estimate(fixtures: float, w_old: float, w_new: float, hours_day: float, days_year: float,
                 mxn_kwh: float, kg_co2_kwh: float) -> Dict[str, float]:
    """Yearly savings of replacing ``fixtures`` lamps of ``w_old`` W by
    ``w_new`` W, on for ``hours_day`` x ``days_year``. Pure; the page's
    estimator runs LED_JS, which tests keep identical to this."""
    vals = (fixtures, w_old, w_new, hours_day, days_year, mxn_kwh, kg_co2_kwh)
    if any(v is None or v < 0 for v in vals) or hours_day > 24 or days_year > 366 or w_new > w_old:
        raise ValueError("inputs out of range")
    kwh = fixtures * (w_old - w_new) * hours_day * days_year / 1000.0
    return {"kwh_year": kwh, "mxn_year": kwh * mxn_kwh, "t_co2_year": kwh * kg_co2_kwh / 1000.0,
            "kw_saved": fixtures * (w_old - w_new) / 1000.0,
            "pct": (w_old - w_new) / w_old * 100.0 if w_old else 0.0}


LED_JS = """function ledEst(n,wo,wn,h,d,p,f){var kwh=n*(wo-wn)*h*d/1000;
return {kwh_year:kwh,mxn_year:kwh*p,t_co2_year:kwh*f/1000,kw_saved:n*(wo-wn)/1000,pct:wo?(wo-wn)/wo*100:0};}"""


def months_with_data(mon: Dict[str, Dict[str, float]], keys: Iterable[str]) -> List[str]:
    ms = sorted({m for k in keys for m in mon.get(k, {})})
    return months_between(ms[0], ms[-1]) if ms else []
