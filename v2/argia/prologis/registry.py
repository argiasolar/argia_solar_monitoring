"""The Prologis site and project registry (v292).

A server-only JSON file (ARGIA_PL_REGISTRY, default
/opt/argia/prologis/registry.json) - customer data never goes into the
public repo. Shape::

    {"portfolio": {"name": ..., "market": ...},
     "sites": [{"code", "name", "park", "city", "state", "address", "kwp",
                "pto", "status": "operating" | "pre_pto", "lat", "lon",
                "geo_approx": true, "monitoring": "SolarEdge + Hark",
                "solaredge_site_id": null,
                # v369, from Prologis's PM package (Nov 2026), all optional:
                "solar_id", "customer", "market", "scheme" (QS+FULL EXPORT /
                NET METERING / ZERO EXPORT), "in_operation" (YYYY-MM-DD),
                "pl_priority" (Prologis's score), "last_cleaning" (YYYY-MM-DD),
                "scope": ["pm", "cleaning", "thermography"], "geo_note"}],
     "projects": [{"id", "name", "kind": "EPC" | "O&M onboarding",
                   "site_code", "city", "kwp", "stage", "next", "next_date",
                   "notes", "lat", "lon"}]}

``load`` validates every row and raises ValueError with all problems at
once, so a bad edit never half-renders the platform.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

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
    # v369: Prologis's PM package (Nov 2026) - who is in the building and what Prologis asks ARGIA to do there
    solar_id: str = ""
    customer: str = ""
    market: str = ""
    scheme: str = ""
    in_operation: str = ""
    pl_priority: Optional[float] = None
    last_cleaning: str = ""
    scope: Tuple[str, ...] = ()
    geo_note: str = ""
    pinned_by: str = ""                 # set when a person placed the pin in the portal (site_pins)

    @property
    def operating(self) -> bool:
        return self.status == "operating"

    def months_since_cleaning(self, today: dt.date) -> Optional[int]:
        """Whole months from the last cleaning to ``today``; None when unknown. Pure."""
        if not self.last_cleaning:
            return None
        d = dt.date.fromisoformat(self.last_cleaning)
        return max(0, (today.year - d.year) * 12 + today.month - d.month - (1 if today.day < d.day else 0))


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


MX_BOUNDS = ((14.0, -118.0), (33.0, -86.0))     # [[south, west], [north, east]]
SCOPES = ("pm", "cleaning", "thermography")     # v369: services Prologis requests per site


def _iso(v, what: str, code: str) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"{what} must be YYYY-MM-DD, got {s!r}") from None


def region_bounds(data: dict):
    """v337: the area the sites must lie in. Mexico unless the registry says
    "bounds": [[south, west], [north, east]] (a portfolio in other countries)."""
    b = data.get("bounds")
    if b is None:
        return MX_BOUNDS
    (s_, w_), (n_, e_) = b
    s_, w_, n_, e_ = float(s_), float(w_), float(n_), float(e_)
    if not (-90 <= s_ < n_ <= 90 and -180 <= w_ < e_ <= 180):
        raise ValueError("bounds must be [[south, west], [north, east]] inside -90..90 / -180..180")
    return (s_, w_), (n_, e_)


def parse(data: dict) -> Registry:
    errs: List[str] = []
    try:
        (b_s, b_w), (b_n, b_e) = region_bounds(data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"registry: {e}") from None
    sites: List[Site] = []
    seen = set()
    for i, r in enumerate(data.get("sites") or []):
        try:
            s = Site(code=str(r["code"]).upper(), name=r["name"], park=r.get("park", ""),
                     city=r["city"], state=r.get("state", ""), address=r.get("address", ""),
                     kwp=float(r["kwp"]), pto=str(r.get("pto", "")), status=r.get("status", "operating"),
                     lat=float(r["lat"]), lon=float(r["lon"]), geo_approx=bool(r.get("geo_approx", True)),
                     monitoring=r.get("monitoring", "SolarEdge + Hark"),
                     solaredge_site_id=(str(r["solaredge_site_id"]) if r.get("solaredge_site_id") else None),
                     solar_id=str(r.get("solar_id") or ""), customer=str(r.get("customer") or ""),
                     market=str(r.get("market") or ""), scheme=str(r.get("scheme") or ""),
                     in_operation=_iso(r.get("in_operation"), "in_operation", str(r.get("code"))),
                     pl_priority=(float(r["pl_priority"]) if r.get("pl_priority") not in (None, "") else None),
                     last_cleaning=_iso(r.get("last_cleaning"), "last_cleaning", str(r.get("code"))),
                     scope=tuple(r.get("scope") or ()), geo_note=str(r.get("geo_note") or ""))
        except (KeyError, TypeError, ValueError) as e:
            errs.append(f"site #{i + 1}: {e!r}")
            continue
        if s.code in seen:
            errs.append(f"site {s.code}: duplicate code")
        if s.status not in ("operating", "pre_pto"):
            errs.append(f"site {s.code}: status must be operating or pre_pto")
        if not (0 < s.kwp < 100000):
            errs.append(f"site {s.code}: kwp out of range")
        if any(x not in SCOPES for x in s.scope):
            errs.append(f"site {s.code}: scope must be a list of {', '.join(SCOPES)}")
        if not (b_s <= s.lat <= b_n and b_w <= s.lon <= b_e):
            mx = (b_s, b_w, b_n, b_e) == (*MX_BOUNDS[0], *MX_BOUNDS[1])
            errs.append(f"site {s.code}: coordinates outside " + ("Mexico" if mx else "the registry bounds"))
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


def apply_pins(rg: Registry, pins: Dict[str, Dict]) -> Registry:
    """v369: the registry with the positions placed by hand in the portal (site_pins): a placed pin is the
    building, so the site is no longer approximate. The same object when nothing was placed. Pure."""
    if not pins:
        return rg
    sites = [replace(s, lat=float(pins[s.code]["lat"]), lon=float(pins[s.code]["lon"]), geo_approx=False,
                     pinned_by=str(pins[s.code]["username"])) if s.code in pins else s for s in rg.sites]
    return Registry(portfolio=rg.portfolio, sites=sites, projects=rg.projects)


def load(path: Optional[str] = None) -> Registry:
    p = path or os.environ.get("ARGIA_PL_REGISTRY", DEFAULT_PATH)
    with open(p, encoding="utf-8") as fh:
        return parse(json.load(fh))
