"""The Prologis site and project registry (v292).

A server-only JSON file (ARGIA_PL_REGISTRY, default
/opt/argia/prologis/registry.json) - customer data never goes into the
public repo. Shape::

    {"portfolio": {"name": ..., "market": ...},
     "sites": [{"code", "name", "park", "city", "state", "address", "kwp",
                "pto", "status": "operating" | "pre_pto", "lat", "lon",
                "geo_approx": true, "monitoring": "SolarEdge + Hark",
                "solaredge_site_id": null}],
     "projects": [{"id", "name", "kind": "EPC" | "O&M onboarding",
                   "site_code", "city", "kwp", "stage", "next", "next_date",
                   "notes", "lat", "lon"}]}

``load`` validates every row and raises ValueError with all problems at
once, so a bad edit never half-renders the platform.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_PATH = "/opt/argia/prologis/registry.json"
STAGES = ["Audit", "Design", "Permits", "Procurement", "Construction",
          "Commissioning", "PTO", "O&M"]
STAGES_ES = {"Audit": "Auditoría", "Design": "Diseño", "Permits": "Permisos",
             "Procurement": "Compras", "Construction": "Construcción",
             "Commissioning": "Puesta en marcha", "PTO": "PTO", "O&M": "O&M"}


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    park: str
    city: str
    state: str
    address: str
    kwp: float
    pto: str
    status: str
    lat: float
    lon: float
    geo_approx: bool = True
    monitoring: str = "SolarEdge + Hark"
    solaredge_site_id: Optional[str] = None

    @property
    def operating(self) -> bool:
        return self.status == "operating"


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    kind: str
    site_code: str
    city: str
    kwp: Optional[float]
    stage: str
    next: str = ""
    next_date: str = ""
    notes: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None

    @property
    def stage_index(self) -> int:
        return STAGES.index(self.stage) if self.stage in STAGES else 0


@dataclass
class Registry:
    portfolio: Dict[str, str]
    sites: List[Site]
    projects: List[Project] = field(default_factory=list)

    def site(self, code: str) -> Optional[Site]:
        c = (code or "").upper()
        return next((s for s in self.sites if s.code == c), None)

    @property
    def operating(self) -> List[Site]:
        return [s for s in self.sites if s.operating]

    @property
    def kwp_total(self) -> float:
        return sum(s.kwp for s in self.sites)


def parse(data: dict) -> Registry:
    errs: List[str] = []
    sites: List[Site] = []
    seen = set()
    for i, r in enumerate(data.get("sites") or []):
        try:
            s = Site(code=str(r["code"]).upper(), name=r["name"], park=r.get("park", ""),
                     city=r["city"], state=r.get("state", ""), address=r.get("address", ""),
                     kwp=float(r["kwp"]), pto=str(r.get("pto", "")), status=r.get("status", "operating"),
                     lat=float(r["lat"]), lon=float(r["lon"]), geo_approx=bool(r.get("geo_approx", True)),
                     monitoring=r.get("monitoring", "SolarEdge + Hark"),
                     solaredge_site_id=(str(r["solaredge_site_id"]) if r.get("solaredge_site_id") else None))
        except (KeyError, TypeError, ValueError) as e:
            errs.append(f"site #{i + 1}: {e!r}")
            continue
        if s.code in seen:
            errs.append(f"site {s.code}: duplicate code")
        if s.status not in ("operating", "pre_pto"):
            errs.append(f"site {s.code}: status must be operating or pre_pto")
        if not (0 < s.kwp < 100000):
            errs.append(f"site {s.code}: kwp out of range")
        if not (14 <= s.lat <= 33 and -118 <= s.lon <= -86):
            errs.append(f"site {s.code}: coordinates outside Mexico")
        seen.add(s.code)
        sites.append(s)
    projects: List[Project] = []
    for i, r in enumerate(data.get("projects") or []):
        try:
            p = Project(id=str(r["id"]), name=r["name"], kind=r.get("kind", "EPC"),
                        site_code=str(r.get("site_code", "")).upper(), city=r.get("city", ""),
                        kwp=(float(r["kwp"]) if r.get("kwp") not in (None, "") else None),
                        stage=r.get("stage", "Audit"), next=r.get("next", ""),
                        next_date=r.get("next_date", ""), notes=r.get("notes", ""),
                        lat=(float(r["lat"]) if r.get("lat") is not None else None),
                        lon=(float(r["lon"]) if r.get("lon") is not None else None))
        except (KeyError, TypeError, ValueError) as e:
            errs.append(f"project #{i + 1}: {e!r}")
            continue
        if p.stage not in STAGES:
            errs.append(f"project {p.id}: unknown stage {p.stage!r}")
        projects.append(p)
    if not sites:
        errs.append("no sites")
    if errs:
        raise ValueError("; ".join(errs))
    return Registry(portfolio=dict(data.get("portfolio") or {}), sites=sites, projects=projects)


def load(path: Optional[str] = None) -> Registry:
    p = path or os.environ.get("ARGIA_PL_REGISTRY", DEFAULT_PATH)
    with open(p, encoding="utf-8") as fh:
        return parse(json.load(fh))
