"""ARGIA for Prologis - LED lighting programme (Prologis Essentials, formerly LightSmart) (v345).

Tomasz, 5 Nov 2026: a lighting page on prologis.argia.com.mx, built like
cpa.argia.com.mx's LED module and dressed like the platform: every building
ARGIA has relit for Prologis since 2019 by city, park and customer, a table
of all of them, statistics, a CO2 counter, the floor area covered, a picture
of each plant (satellite view), the customers' logos, and a separate page of
the Prologis buildings still not on LED (audited or not; from Prologis's
property database, locations of 5,000 m2 and more).

The data is server-only (/opt/argia/prologis/lighting/lighting.json, written
on Tomasz's laptop by build_lighting_json.py from ARGIA's project register):
tenants, buildings, WO and PO numbers are Prologis business data and this
repo is public. This module is pure and knows no project.

Rules (the same as the CPA LED module, v314):
* savings are DESIGN values from ARGIA's lighting studies (annual kWh before
  vs after) - estimated, never metered; a project without both figures has no
  savings figure, nothing is invented;
* avoided CO2 = saved kWh x the national grid factor of each calendar year
  (argia.core.co2, the one register); "since handover" counts each year at
  that year's factor;
* no price, cost or amount exists here (WO and PO *numbers* are references
  Prologis uses; amounts are not in the file and never shown);
* "not yet LED" potential is an ESTIMATE from the delivered buildings'
  median savings per m2 and is always labelled so.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from argia.core import co2 as co2reg

# market key -> (English, Spanish, display order)
MARKETS: Dict[str, Tuple[str, str]] = {
    "cdmx": ("Mexico City", "Ciudad de México"), "gdl": ("Guadalajara", "Guadalajara"),
    "mty": ("Monterrey", "Monterrey"), "rey": ("Reynosa", "Reynosa"), "tij": ("Tijuana", "Tijuana"),
    "cjs": ("Ciudad Juárez", "Ciudad Juárez"), "baj": ("Bajío", "Bajío"), "hmo": ("Hermosillo", "Hermosillo"),
    "nld": ("Nuevo Laredo", "Nuevo Laredo"), "mat": ("Matamoros", "Matamoros"), "nog": ("Nogales", "Nogales"),
    "sal": ("Saltillo", "Saltillo"),
}
# project status -> (English, Spanish, colour)
STATUSES: Dict[str, Tuple[str, str, str]] = {
    "delivered": ("Delivered", "Entregado", "#23B2A9"),
    "in_progress": ("In progress", "En proceso", "#e8a23a"),
    "on_hold": ("On hold", "En pausa", "#9aa4ad"),
}
KINDS: Dict[str, Tuple[str, str]] = {
    "tenant": ("Tenant space", "Espacio arrendado"), "vacant": ("Vacant space", "Espacio vacante"),
    "exterior": ("Exterior / yard", "Exterior / patio"), "common": ("Common areas", "Áreas comunes"),
    "stock": ("Replacement stock", "Stock de reemplazo"),
}
INTERIOR = ("tenant", "vacant")            # kinds whose area counts towards a building being converted
# building LED status (the "not yet LED" page) -> (English, Spanish, colour)
BSTATUS: Dict[str, Tuple[str, str, str]] = {
    "led": ("Converted to LED", "Convertido a LED", "#23B2A9"),
    "partial": ("Partly converted", "Convertido en parte", "#71BE45"),
    "in_progress": ("Project in progress", "Proyecto en proceso", "#e8a23a"),
    "audited": ("Audited, not converted", "Auditado, sin convertir", "#2CB5E5"),
    "not_started": ("No audit yet", "Sin auditoría", "#9aa4ad"),
}
FULL_SHARE = 0.8                   # delivered interior area / building area from which a building counts as converted
MIN_PIPELINE_M2 = 5000             # Tomasz: "interested only in locations over 5000 m2"


@dataclass(frozen=True)
class Project:
    id: int
    park: str
    market: str
    status: str
    kind: str
    customer: str
    yardi: Optional[str] = None
    bldg: Optional[str] = None
    group: Optional[str] = None
    year: Optional[int] = None
    wo_number: Optional[str] = None
    po_number: Optional[str] = None
    wo_date: Optional[str] = None
    handover_date: Optional[str] = None
    handover_planned: bool = False
    fixtures: Optional[int] = None
    area_m2: Optional[float] = None
    kwh_before: Optional[float] = None
    kwh_after: Optional[float] = None
    maker: Optional[str] = None
    controls: Optional[str] = None
    lux: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    geo: Optional[str] = None
    docs: Dict[str, bool] = field(default_factory=dict, compare=False, hash=False)

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"

    @property
    def kwh_saved(self) -> Optional[float]:
        if self.kwh_before and self.kwh_after is not None and self.kwh_before > self.kwh_after:
            return self.kwh_before - self.kwh_after
        return None

    @property
    def saving_share(self) -> Optional[float]:
        s = self.kwh_saved
        return s / self.kwh_before if s else None

    def co2_t_per_year(self, year: Optional[int] = None) -> Optional[float]:
        s = self.kwh_saved
        return s * co2reg.factor(year) / 1000 if s else None

    def co2_t_since(self, today: dt.date) -> Optional[float]:
        """t CO2e avoided from the handover day to ``today``, each calendar year at its own factor.
        No handover date, a planned one, or no savings figure = None (nothing is invented)."""
        s = self.kwh_saved
        if not s or not self.handover_date or self.handover_planned:
            return None
        d0 = _date(self.handover_date)
        if d0 is None or d0 >= today:
            return 0.0 if d0 else None
        tot, cur = 0.0, d0
        while cur < today:
            end = min(dt.date(cur.year + 1, 1, 1), today)
            days_in_year = 366 if _leap(cur.year) else 365
            tot += s * (end - cur).days / days_in_year * co2reg.factor(cur.year)
            cur = end
        return tot / 1000

    @property
    def place(self) -> str:
        return " · ".join(x for x in (self.park, self.bldg) if x)


@dataclass(frozen=True)
class Building:
    yardi: str
    market: Optional[str]
    park: Optional[str] = None
    building: Optional[int] = None
    name: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    area_m2: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    geo: Optional[str] = None


@dataclass(frozen=True)
class Opportunity:
    id: int
    yardi: Optional[str]
    customer: Optional[str] = None
    area_m2: Optional[float] = None
    audit: bool = False
    study: bool = False
    proforma: bool = False
    last_proforma: Optional[str] = None
    converted_project: Optional[int] = None


@dataclass(frozen=True)
class Customer:
    key: str
    name: str
    domain: Optional[str] = None
    logo: Optional[str] = None


@dataclass
class Dataset:
    generated: str
    projects: List[Project]
    buildings: Dict[str, Building]
    opportunities: List[Opportunity]
    customers: Dict[str, Customer]
    note: str = ""

    @property
    def delivered(self) -> List[Project]:
        return [p for p in self.projects if p.delivered]

    def project(self, pid: int) -> Optional[Project]:
        return next((p for p in self.projects if p.id == pid), None)


# ------------------------------------------------------------------ loading (fail closed: a bad file raises)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
YARDI_RE = re.compile(r"^[A-Z]{3}\d{5}$")
LOGO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.png$")


def load(path: str) -> Dataset:
    with open(path, encoding="utf-8") as f:
        return parse(json.load(f))


def parse(d: dict) -> Dataset:
    if not isinstance(d, dict) or d.get("meta", {}).get("schema") != 1:
        raise ValueError("lighting.json: unknown schema")
    projects, seen = [], set()
    for r in d.get("projects", []):
        p = Project(**{k: r.get(k) for k in Project.__dataclass_fields__ if k in r})
        if p.id in seen:
            raise ValueError(f"lighting.json: duplicate project {p.id}")
        seen.add(p.id)
        if p.status not in STATUSES:
            raise ValueError(f"lighting.json: project {p.id} has unknown status {p.status!r}")
        if p.kind not in KINDS:
            raise ValueError(f"lighting.json: project {p.id} has unknown kind {p.kind!r}")
        if p.market not in MARKETS:
            raise ValueError(f"lighting.json: project {p.id} has unknown market {p.market!r}")
        if p.yardi is not None and not YARDI_RE.match(p.yardi):
            raise ValueError(f"lighting.json: project {p.id} has a bad building code")
        if p.group is not None and not SLUG_RE.match(p.group):
            raise ValueError(f"lighting.json: project {p.id} has a bad customer key")
        if p.lat is not None and not (14 <= p.lat <= 33 and -119 <= p.lon <= -86):
            raise ValueError(f"lighting.json: project {p.id} lies outside Mexico")
        projects.append(p)
    buildings = {}
    for r in d.get("buildings", []):
        b = Building(**{k: r.get(k) for k in Building.__dataclass_fields__ if k in r})
        if not YARDI_RE.match(str(b.yardi)):
            raise ValueError(f"lighting.json: bad building code {b.yardi!r}")
        buildings[b.yardi] = b
    opps = [Opportunity(**{k: r.get(k) for k in Opportunity.__dataclass_fields__ if k in r})
            for r in d.get("opportunities", [])]
    customers = {}
    for k, r in (d.get("customers") or {}).items():
        if not SLUG_RE.match(k):
            raise ValueError(f"lighting.json: bad customer key {k!r}")
        logo = r.get("logo")
        if logo is not None and not LOGO_RE.match(logo):
            raise ValueError(f"lighting.json: bad logo file name for {k}")
        customers[k] = Customer(k, r.get("name") or k, r.get("domain"), logo)
    meta = d["meta"]
    return Dataset(meta.get("generated", ""), projects, buildings, opps, customers, meta.get("note", ""))


# ------------------------------------------------------------------ aggregates
@dataclass
class Totals:
    projects: int = 0
    buildings: int = 0            # distinct buildings (Prologis code, else park + building label)
    parks: int = 0
    markets: int = 0
    customers: int = 0
    fixtures: int = 0
    area_m2: float = 0.0
    mwh_saved: float = 0.0        # design, per year
    co2_t_year: float = 0.0       # at the current factor
    co2_t_since: float = 0.0      # since each handover, per-year factors
    co2_rate_t_s: float = 0.0     # for the live counter (t per second, current factor)
    with_savings: int = 0
    first_year: Optional[int] = None


def building_key(p: Project) -> str:
    return p.yardi or f"{p.market}|{p.park}|{p.bldg or p.id}"


def totals(projects: Iterable[Project], today: dt.date) -> Totals:
    ps = [p for p in projects if p.delivered]
    t = Totals(projects=len(ps))
    t.buildings = len({building_key(p) for p in ps})
    t.parks = len({(p.market, p.park) for p in ps})
    t.markets = len({p.market for p in ps})
    t.customers = len({p.group for p in ps if p.group})
    t.fixtures = sum(p.fixtures or 0 for p in ps)
    t.area_m2 = sum(p.area_m2 or 0 for p in ps)
    saved = [p.kwh_saved for p in ps if p.kwh_saved]
    t.with_savings = len(saved)
    t.mwh_saved = sum(saved) / 1000
    t.co2_t_year = sum(saved) * co2reg.factor(today.year) / 1000
    t.co2_t_since = sum(p.co2_t_since(today) or 0 for p in ps)
    t.co2_rate_t_s = sum(p.kwh_saved for p in ps if p.kwh_saved and p.co2_t_since(today) is not None) \
        * co2reg.factor(today.year) / 1000 / (365.25 * 86400)
    years = [p.year for p in ps if p.year]
    t.first_year = min(years) if years else None
    return t


def group_by(projects: Iterable[Project], key) -> Dict[str, List[Project]]:
    out: Dict[str, List[Project]] = {}
    for p in projects:
        out.setdefault(key(p), []).append(p)
    return out


def summary_rows(groups: Dict[str, List[Project]], today: dt.date) -> List[Tuple[str, Totals]]:
    """(key, totals) sorted by fixtures, largest first."""
    rows = [(k, totals(v, today)) for k, v in groups.items()]
    return sorted(rows, key=lambda kv: (-kv[1].fixtures, -kv[1].projects, kv[0]))


def per_year(projects: Iterable[Project]) -> List[Tuple[int, int, int]]:
    """(year, delivered projects, fixtures) for every year from the first to the last."""
    ps = [p for p in projects if p.delivered and p.year]
    if not ps:
        return []
    y0, y1 = min(p.year for p in ps), max(p.year for p in ps)
    return [(y, sum(1 for p in ps if p.year == y), sum(p.fixtures or 0 for p in ps if p.year == y))
            for y in range(y0, y1 + 1)]


def park_key(p: Project) -> str:
    return slug(f"{p.market}-{p.park}")


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "x"


# ------------------------------------------------------------------ buildings not yet on LED
@dataclass
class BuildingStatus:
    building: Building
    status: str
    delivered_m2: float
    share: Optional[float]              # delivered interior area / building area
    projects: List[Project]
    opportunities: List[Opportunity]

    @property
    def remaining_m2(self) -> Optional[float]:
        a = self.building.area_m2
        return None if not a else max(0.0, a - self.delivered_m2)


def building_status(ds: Dataset) -> List[BuildingStatus]:
    by_y = group_by([p for p in ds.projects if p.yardi], lambda p: p.yardi)
    op_y = {}
    for o in ds.opportunities:
        if o.yardi:
            op_y.setdefault(o.yardi, []).append(o)
    out = []
    for y, b in ds.buildings.items():
        ps = by_y.get(y, [])
        ops = op_y.get(y, [])
        dm2 = sum(p.area_m2 or 0 for p in ps if p.delivered and p.kind in INTERIOR)
        share = (min(1.0, dm2 / b.area_m2) if b.area_m2 else None)
        any_deliv = any(p.delivered and p.kind in INTERIOR for p in ps)
        if any_deliv and (share is None or share >= FULL_SHARE):
            st = "led"
        elif any_deliv:
            st = "partial"
        elif any(p.status in ("in_progress", "on_hold") for p in ps):
            st = "in_progress"
        elif any(o.audit or o.study or o.proforma for o in ops) or any(p.docs.get("audit") for p in ps):
            st = "audited"
        else:
            st = "not_started"
        out.append(BuildingStatus(b, st, dm2, share, ps, ops))
    return out


def pipeline(ds: Dataset, min_m2: float = MIN_PIPELINE_M2) -> List[BuildingStatus]:
    """Prologis buildings of ``min_m2`` and more that are not (fully) on LED: partly converted, in progress,
    audited or never audited - largest remaining area first. Unknown area = left out (cannot be sized)."""
    rows = [s for s in building_status(ds)
            if s.status != "led" and s.building.area_m2 and s.building.area_m2 >= min_m2]
    order = {k: i for i, k in enumerate(("in_progress", "audited", "partial", "not_started"))}
    return sorted(rows, key=lambda s: (order[s.status], -(s.remaining_m2 or 0)))


@dataclass
class Intensity:
    kwh_per_m2: float
    fixtures_per_1000m2: float
    n: int


def intensity(ds: Dataset) -> Optional[Intensity]:
    """Median design saving per m2 and fixtures per 1,000 m2 of the delivered interior projects that have both
    figures - the basis of the ESTIMATED potential of the buildings not yet on LED."""
    ps = [p for p in ds.delivered if p.kind in INTERIOR and p.area_m2 and p.area_m2 >= 500 and p.kwh_saved and p.fixtures]
    if len(ps) < 5:
        return None
    return Intensity(statistics.median(p.kwh_saved / p.area_m2 for p in ps),
                     statistics.median(1000 * p.fixtures / p.area_m2 for p in ps), len(ps))


def potential(rows: Iterable[BuildingStatus], it: Optional[Intensity], year: int) -> Dict[str, float]:
    """ESTIMATED: remaining m2 x median intensity."""
    m2 = sum(r.remaining_m2 or 0 for r in rows)
    if not it:
        return {"m2": m2, "mwh": 0.0, "co2_t": 0.0, "fixtures": 0.0}
    mwh = m2 * it.kwh_per_m2 / 1000
    return {"m2": m2, "mwh": mwh, "co2_t": mwh * co2reg.factor(year), "fixtures": m2 * it.fixtures_per_1000m2 / 1000}


# ------------------------------------------------------------------ CSV (no amounts exist to export)
CSV_COLUMNS = ("id", "market", "park", "bldg", "yardi", "customer", "kind", "status", "year", "wo_number",
               "po_number", "wo_date", "handover_date", "fixtures", "area_m2", "kwh_before", "kwh_after",
               "kwh_saved", "saving_pct", "co2_t_year", "maker", "controls", "lux")


def csv_rows(projects: Iterable[Project], year: int) -> List[List]:
    rows = [list(CSV_COLUMNS)]
    for p in projects:
        s = p.kwh_saved
        rows.append([p.id, MARKETS[p.market][0], p.park, p.bldg or "", p.yardi or "", p.customer, p.kind, p.status,
                     p.year or "", p.wo_number or "", p.po_number or "", p.wo_date or "",
                     ("" if p.handover_planned else p.handover_date or ""), p.fixtures or "", p.area_m2 or "",
                     p.kwh_before or "", p.kwh_after or "", round(s) if s else "",
                     round(100 * p.saving_share, 1) if s else "", round(p.co2_t_per_year(year), 1) if s else "",
                     p.maker or "", p.controls or "", p.lux or ""])
    return rows


# ------------------------------------------------------------------ satellite picture (Esri World Imagery)
def satellite_url(lat: float, lon: float, w: int = 640, h: int = 360, half_m: float = 220) -> str:
    """A static satellite picture centred on the point (Esri World Imagery export; the platform's CSP allows
    server.arcgisonline.com). ``half_m`` = half the picture width on the ground, in metres."""
    import math
    dlon = half_m / (111320 * math.cos(math.radians(lat)))
    dlat = dlon * h / w * math.cos(math.radians(lat))
    bbox = f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"
    return ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
            f"?bbox={bbox}&bboxSR=4326&imageSR=3857&size={w},{h}&format=jpg&f=image")


def google_maps_url(lat: float, lon: float) -> str:
    """Opens Google Maps' satellite view at the point (a link, not an embed - the CSP blocks frames)."""
    return f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},350m/data=!3m1!1e3"


# ------------------------------------------------------------------ helpers
def _date(s: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
