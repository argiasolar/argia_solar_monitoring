"""ARGIA for CPA - LED lighting projects in CPA's parks (v314).

Tomasz, 6 Oct 2026: show CPA the LED retrofits delivered and in the
pipeline, with the CO2 they avoid, on the map with their status, from the
park owner's point of view, without any financial figure, and in the
reports.

The project list is server-only (/opt/argia/cpa/led.json, written by
scripts/cpa_led_import.py from ARGIA's project sheet) - tenants and
buildings are CPA business data and the repo is public. This module is
pure and knows no project.

Rules:

* savings are ESTIMATED, never metered: (installed kW before - kW after)
  x the building's operating hours per year - the same arithmetic as
  ARGIA's project sheet; the pages say "estimated" every time;
* a building without a "before" load (a new building, lit efficiently
  from day one) has no savings figure - nothing is invented;
* avoided CO2 = saved kWh x the current national grid factor from
  argia.core.co2 (the one register; no customer override applies to LED);
* no price, cost or MXN figure exists here.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from argia.core import co2 as co2reg

# status key -> (English, Spanish, colour). Order = display order.
STATUSES: Dict[str, Tuple[str, str, str]] = {
    "delivered": ("Delivered", "Entregado", "#05b1a9"),
    "installing": ("To be installed", "Por instalar", "#e8a23a"),
    "to_confirm": ("To be confirmed", "Por confirmar", "#8b7fd6"),
    "proposal": ("Proposed", "Propuesta", "#9aa3c7"),
}
SHEET_STATUS = {"entregado": "delivered", "por instalar": "installing", "por confirmar": "to_confirm",
                "propuesta": "proposal"}
KINDS: Dict[str, Tuple[str, str]] = {
    "exterior": ("Yard and exterior", "Patios y exterior"),
    "logistic": ("Warehouse", "Nave logística"),
    "production": ("Production", "Producción"),
    "office": ("Offices", "Oficinas"),
}


@dataclass(frozen=True)
class Park:
    id: str
    name: str
    city: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Project:
    id: str
    status: str
    building: str                  # CPA's building name, e.g. "ADN B004"
    park: str                      # Park.id
    tenant: Optional[str] = None
    area_m2: Optional[float] = None
    fixtures: Optional[int] = None
    kw_before: Optional[float] = None
    kw_after: Optional[float] = None
    hours: Optional[float] = None
    kind: Optional[str] = None     # KINDS key
    sensors: bool = False
    lux_before: Optional[str] = None
    lux_after: Optional[str] = None
    lat: Optional[float] = None    # the building itself, when known
    lon: Optional[float] = None

    @property
    def exact(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def new_build(self) -> bool:
        return not self.kw_before

    @property
    def saved_kwh(self) -> Optional[float]:
        """kWh per year; None when there is no 'before' to compare with."""
        if not self.kw_before or self.kw_after is None or not self.hours:
            return None
        return max(0.0, (self.kw_before - self.kw_after) * self.hours)

    @property
    def cut_pct(self) -> Optional[float]:
        if not self.kw_before or self.kw_after is None:
            return None
        return max(0.0, (1 - self.kw_after / self.kw_before) * 100.0)

    def co2_t(self, factor: Optional[float] = None) -> Optional[float]:
        s = self.saved_kwh
        return None if s is None else s * (co2reg.CURRENT if factor is None else factor) / 1000.0


@dataclass
class Summary:
    projects: int = 0
    fixtures: int = 0
    area_m2: float = 0.0
    kw_before: float = 0.0          # only projects with a before AND an after
    kw_after: float = 0.0
    saved_kwh: float = 0.0
    co2_t: float = 0.0
    parks: int = 0

    @property
    def cut_pct(self) -> Optional[float]:
        return (1 - self.kw_after / self.kw_before) * 100.0 if self.kw_before else None


def summarise(projects: Sequence[Project], factor: Optional[float] = None) -> Dict[str, Summary]:
    """{status: Summary, '_all': Summary, '_pipeline': Summary (installing +
    to confirm + proposed)}. Pure."""
    out = {k: Summary() for k in list(STATUSES) + ["_all", "_pipeline"]}
    parks: Dict[str, set] = {k: set() for k in out}
    for p in projects:
        for k in (p.status, "_all") + (("_pipeline",) if p.status != "delivered" else ()):
            s = out[k]
            s.projects += 1
            s.fixtures += p.fixtures or 0
            s.area_m2 += p.area_m2 or 0.0
            if p.saved_kwh is not None:
                s.kw_before += p.kw_before or 0.0
                s.kw_after += p.kw_after or 0.0
                s.saved_kwh += p.saved_kwh
                s.co2_t += p.co2_t(factor) or 0.0
            parks[k].add(p.park)
    for k, s in out.items():
        s.parks = len(parks[k])
    return out


def by_park(projects: Sequence[Project], parks: Sequence[Park]) -> List[Tuple[Park, List[Project]]]:
    """Parks that have projects, most delivered first, then by name. Pure."""
    order = list(STATUSES)
    groups = []
    for pk in parks:
        mine = sorted((p for p in projects if p.park == pk.id), key=lambda p: (order.index(p.status), p.building))
        if mine:
            groups.append((pk, mine))
    groups.sort(key=lambda g: (-sum(p.status == "delivered" for p in g[1]), -len(g[1]), g[0].name))
    return groups


# ------------------------------------------------------------------ the server-only file
def _num(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path: str) -> Tuple[List[Park], List[Project]]:
    """Read led.json; raises ValueError on anything that would put a wrong
    pin or a wrong number on CPA's site."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    parks = []
    for p in doc.get("parks") or []:
        lat, lon = _num(p.get("lat")), _num(p.get("lon"))
        if not p.get("id") or not str(p.get("name", "")).strip() or lat is None or lon is None \
                or not (14 <= lat <= 33 and -118 <= lon <= -86):
            raise ValueError(f"led.json: bad park {p!r}")
        parks.append(Park(str(p["id"]), str(p["name"]).strip(), str(p.get("city", "")).strip(), lat, lon))
    ids = {p.id for p in parks}
    projects = []
    for r in doc.get("projects") or []:
        if r.get("status") not in STATUSES or r.get("park") not in ids or not str(r.get("building", "")).strip():
            raise ValueError(f"led.json: bad project {r.get('id')!r} ({r.get('building')!r})")
        kb, ka, h = _num(r.get("kw_before")), _num(r.get("kw_after")), _num(r.get("hours"))
        if (kb is not None and kb < 0) or (ka is not None and ka < 0) or (h is not None and not 0 < h <= 8784):
            raise ValueError(f"led.json: project {r.get('id')!r} has impossible loads or hours")
        projects.append(Project(
            id=str(r.get("id")), status=r["status"], building=str(r["building"]).strip(), park=r["park"],
            tenant=(str(r["tenant"]).strip() or None) if r.get("tenant") else None,
            area_m2=_num(r.get("area_m2")), fixtures=int(r["fixtures"]) if _num(r.get("fixtures")) is not None else None,
            kw_before=kb, kw_after=ka, hours=h, kind=r.get("kind") if r.get("kind") in KINDS else None,
            sensors=bool(r.get("sensors")), lux_before=r.get("lux_before") or None, lux_after=r.get("lux_after") or None,
            lat=_num(r.get("lat")), lon=_num(r.get("lon"))))
    return parks, projects


def csv_projects(projects: Sequence[Project], parks: Sequence[Park], factor: Optional[float] = None) -> str:
    """One row per project for the sustainability team. No financial column."""
    f = co2reg.CURRENT if factor is None else factor
    pk = {p.id: p for p in parks}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["park", "city", "building", "tenant", "status", "use", "area_m2", "fixtures", "lighting_kw_before",
                "lighting_kw_after", "operating_hours_per_year", "estimated_kwh_saved_per_year",
                "grid_factor_kg_co2e_per_kwh", "estimated_t_co2e_avoided_per_year", "light_level_before", "light_level_after"])
    order = list(STATUSES)
    for p in sorted(projects, key=lambda p: (order.index(p.status), pk[p.park].name, p.building)):
        s = p.saved_kwh
        w.writerow([pk[p.park].name, pk[p.park].city, p.building, p.tenant or "", STATUSES[p.status][0],
                    KINDS[p.kind][0] if p.kind else "", "" if p.area_m2 is None else f"{p.area_m2:.0f}",
                    "" if p.fixtures is None else p.fixtures, "" if p.kw_before is None else f"{p.kw_before:.2f}",
                    "" if p.kw_after is None else f"{p.kw_after:.2f}", "" if p.hours is None else f"{p.hours:.0f}",
                    "" if s is None else f"{s:.0f}", f"{f:.3f}", "" if s is None else f"{s * f / 1000:.2f}",
                    p.lux_before or "", p.lux_after or ""])
    return buf.getvalue()
