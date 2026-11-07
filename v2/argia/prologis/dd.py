"""Due diligence of a prospect's property portfolio (v363, ARGIA for Grupo MRP).

A server-only JSON file (DATA_DIR/dd/properties.json) lists the prospect's
properties as researched from public sources, plus the assumptions used to
size the opportunities. This module validates it and computes every
estimate, so the page shows nothing that is not traceable to a fact or an
assumption written in the file. PURE: no I/O apart from ``load``.

Shape::

    {"schema": 1, "title": ..., "as_of": "YYYY-MM-DD",
     "title_es": optional, "company": {"name", "about", "about_es", "url"},
     "assumptions": {"roof_use", "m2_per_kwp", "tariff_mxn_kwh", "carport_share",
                     "kwp_per_space", "spaces_per_fixture", "led_w_saved",
                     "led_hours", "co2_kg_kwh", "yield_default"},
     (optional, needed when a property has "lighting": led_m2_per_fixture, led_w_before,
      led_w_after, led_controls_share, led_hours_interior)
     "properties": [{"code", "name", "kind", "status": "operating" | "development",
                     "city", "state", "address", "lat", "lon", "approx",
                     "gla_m2", "roof_m2", "roof_kwp", "units", "anchors": [...],
                     "parking", "carport", "opened", "pv_seen", "pv_share",
                     "yield", "notes", "sources": [...], "kind_es", "notes_es",
                     "lighting": {"status", "baseline", "area_m2", "m2_per_fixture", "w_before",
                                  "w_after", "hours", "note", "note_es"}}]}

Roof area = ``roof_m2`` when given, else ``gla_m2`` (single-level retail).
Existing PV (seen on satellite imagery) = roof x pv_share / m2_per_kwp.
New rooftop PV = roof x roof_use / m2_per_kwp minus the existing PV (never
below 0), or ``roof_kwp`` when given (towers and hotels). Carport = a share
of the parking spaces x kWp per space. LED = parking spaces / spaces per
fixture, each saving ``led_w_saved`` for ``led_hours`` a year.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SCHEMA = 1
STATUSES = ("operating", "development")
CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
ASSUMPTION_RANGES = {                      # name -> (low, high), both inclusive
    "roof_use": (0.05, 0.9),
    "m2_per_kwp": (3.0, 15.0),
    "tariff_mxn_kwh": (0.5, 10.0),
    "carport_share": (0.0, 1.0),
    "kwp_per_space": (0.5, 5.0),
    "spaces_per_fixture": (2.0, 50.0),
    "led_w_saved": (0.0, 2000.0),
    "led_hours": (0.0, 8760.0),
    "co2_kg_kwh": (0.0, 1.5),
    "yield_default": (800.0, 2400.0),
}
# v364: interior LED lighting (industrial buildings). Needed only when a property has a "lighting" block.
LED_RANGES = {
    "led_m2_per_fixture": (20.0, 400.0),    # floor area per high-bay fixture
    "led_w_before": (10.0, 2000.0),         # W per fixture today (metal halide 400 W + ballast = 458 W)
    "led_w_after": (5.0, 1000.0),           # W per LED fixture
    "led_controls_share": (0.0, 0.8),       # what occupancy and daylight sensors save on the LED load
    "led_hours_interior": (500.0, 8760.0),  # hours a year the interior lights are on
}
LIGHT_STATUSES = {
    "not_started": ("No audit yet", "Sin auditoría"),
    "audit": ("Audit scheduled", "Auditoría agendada"),
    "proposal": ("Proposal sent", "Propuesta enviada"),
    "installed": ("LED installed", "LED instalado"),
}
BASELINES = {
    "hid": ("Metal halide / HID today", "Aditivos metálicos / HID hoy"),
    "fluorescent": ("Fluorescent today", "Fluorescente hoy"),
    "plain_led": ("New build: compared with plain LED without controls", "Obra nueva: comparado con LED sin controles"),
}
MX_BOUNDS = ((14.0, -118.0), (33.0, -86.0))


@dataclass(frozen=True)
class Lighting:
    """Interior lighting of one property (v364). Any number left out uses the assumption."""
    status: str = "not_started"
    baseline: str = "hid"
    area_m2: Optional[float] = None             # lit floor area; default = the leasable area
    m2_per_fixture: Optional[float] = None
    w_before: Optional[float] = None
    w_after: Optional[float] = None
    hours: Optional[float] = None
    note: str = ""
    note_es: str = ""


@dataclass(frozen=True)
class Prop:
    code: str
    name: str
    kind: str
    status: str
    city: str
    state: str
    address: str
    lat: float
    lon: float
    approx: bool = False
    gla_m2: Optional[float] = None
    roof_m2: Optional[float] = None
    roof_kwp: Optional[float] = None
    units: str = ""
    anchors: tuple = ()
    parking: Optional[int] = None
    carport: bool = True
    opened: str = ""
    pv_seen: bool = False
    pv_share: float = 0.0
    yield_: Optional[float] = None
    notes: str = ""
    sources: tuple = ()
    kind_es: str = ""              # Spanish wording; empty = the English one is shown
    notes_es: str = ""
    lighting: Optional[Lighting] = None


@dataclass
class Portfolio:
    title: str
    as_of: str
    company: Dict[str, str]
    assumptions: Dict[str, float]
    props: List[Prop] = field(default_factory=list)
    title_es: str = ""

    def prop(self, code: str) -> Optional[Prop]:
        c = (code or "").upper()
        return next((p for p in self.props if p.code == c), None)


def _num(v, name: str, errs: List[str], where: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        errs.append(f"{where}: {name} is not a number")
        return None
    if not math.isfinite(x) or x < 0:
        errs.append(f"{where}: {name} must be a number >= 0")
        return None
    return x


def parse(data: dict) -> Portfolio:
    """Validate everything and raise ValueError listing all problems at once."""
    errs: List[str] = []
    if not isinstance(data, dict):
        raise ValueError("due diligence: the file must be a JSON object")
    if data.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    a_in = data.get("assumptions") or {}
    assumptions: Dict[str, float] = {}
    for k, (lo, hi) in ASSUMPTION_RANGES.items():
        x = _num(a_in.get(k), k, errs, "assumptions")
        if x is None:
            if k not in a_in:
                errs.append(f"assumptions: {k} missing")
            continue
        if not lo <= x <= hi:
            errs.append(f"assumptions: {k}={x:g} outside {lo:g}..{hi:g}")
        assumptions[k] = x
    led_in = {k: a_in[k] for k in LED_RANGES if k in a_in}
    for k, (lo, hi) in LED_RANGES.items():
        if k not in led_in:
            continue
        x = _num(led_in[k], k, errs, "assumptions")
        if x is not None and not lo <= x <= hi:
            errs.append(f"assumptions: {k}={x:g} outside {lo:g}..{hi:g}")
        elif x is not None:
            assumptions[k] = x
    props: List[Prop] = []
    seen = set()
    for i, r in enumerate(data.get("properties") or []):
        where = f"property #{i + 1}"
        if not isinstance(r, dict):
            errs.append(f"{where}: not an object")
            continue
        code = str(r.get("code") or "").upper()
        if not CODE_RE.match(code):
            errs.append(f"{where}: code must be 2-8 letters or digits")
            continue
        where = f"property {code}"
        if code in seen:
            errs.append(f"{where}: duplicate code")
        seen.add(code)
        name = str(r.get("name") or "").strip()
        if not name:
            errs.append(f"{where}: name missing")
        status = r.get("status")
        if status not in STATUSES:
            errs.append(f"{where}: status must be operating or development")
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"{where}: lat/lon missing")
            continue
        (s_, w_), (n_, e_) = MX_BOUNDS
        if not (s_ <= lat <= n_ and w_ <= lon <= e_):
            errs.append(f"{where}: coordinates outside Mexico")
        share = _num(r.get("pv_share"), "pv_share", errs, where) or 0.0
        if share > 1:
            errs.append(f"{where}: pv_share must be 0..1")
        pv_seen = bool(r.get("pv_seen"))
        if share and not pv_seen:
            errs.append(f"{where}: pv_share given but pv_seen is false")
        parking = _num(r.get("parking"), "parking", errs, where)
        anchors = r.get("anchors") or []
        sources = r.get("sources") or []
        if not (isinstance(anchors, list) and all(isinstance(x, str) for x in anchors)):
            errs.append(f"{where}: anchors must be a list of names")
            anchors = []
        if not (isinstance(sources, list) and all(isinstance(x, str) and x.startswith("https://") for x in sources)):
            errs.append(f"{where}: sources must be a list of https links")
            sources = []
        light = None
        lr = r.get("lighting")
        if lr is not None:
            if not isinstance(lr, dict):
                errs.append(f"{where}: lighting must be an object")
            else:
                ln = {k: _num(lr.get(k), k, errs, where + " lighting") for k in ("area_m2", "m2_per_fixture", "w_before", "w_after", "hours")}
                st, bl = str(lr.get("status") or "not_started"), str(lr.get("baseline") or "hid")
                if st not in LIGHT_STATUSES:
                    errs.append(f"{where}: lighting status must be one of {', '.join(LIGHT_STATUSES)}")
                if bl not in BASELINES:
                    errs.append(f"{where}: lighting baseline must be one of {', '.join(BASELINES)}")
                if ln["hours"] is not None and ln["hours"] > 8760:
                    errs.append(f"{where}: lighting hours above 8760")
                if ln["area_m2"] is None and not _num(r.get("gla_m2"), "gla_m2", [], where):
                    errs.append(f"{where}: lighting needs area_m2 or gla_m2")
                light = Lighting(status=st, baseline=bl, note=str(lr.get("note") or ""), note_es=str(lr.get("note_es") or ""), **ln)
        props.append(Prop(
            code=code, name=name, kind=str(r.get("kind") or ""), status=str(status), city=str(r.get("city") or ""),
            state=str(r.get("state") or ""), address=str(r.get("address") or ""), lat=lat, lon=lon,
            approx=bool(r.get("approx")), gla_m2=_num(r.get("gla_m2"), "gla_m2", errs, where),
            roof_m2=_num(r.get("roof_m2"), "roof_m2", errs, where), roof_kwp=_num(r.get("roof_kwp"), "roof_kwp", errs, where),
            units=str(r.get("units") or ""), anchors=tuple(anchors),
            parking=(int(parking) if parking is not None else None), carport=bool(r.get("carport", True)),
            opened=str(r.get("opened") or ""), pv_seen=pv_seen, pv_share=share,
            yield_=_num(r.get("yield"), "yield", errs, where), notes=str(r.get("notes") or ""), sources=tuple(sources),
            kind_es=str(r.get("kind_es") or ""), notes_es=str(r.get("notes_es") or ""), lighting=light))
    if not props and not errs:
        errs.append("no properties")
    if any(p.lighting for p in props):
        miss = [k for k in LED_RANGES if k not in led_in]
        if miss:
            errs.append("assumptions: lighting needs " + ", ".join(miss))
        for p in props:
            lt = p.lighting
            if lt and not errs:
                wb = lt.w_before if lt.w_before is not None else assumptions.get("led_w_before", 0)
                wa = lt.w_after if lt.w_after is not None else assumptions.get("led_w_after", 0)
                if wa > wb:
                    errs.append(f"property {p.code}: lighting w_after above w_before")
    if errs:
        raise ValueError("due diligence: " + "; ".join(errs))
    return Portfolio(title=str(data.get("title") or "Due diligence"), as_of=str(data.get("as_of") or ""),
                     company=dict(data.get("company") or {}), assumptions=assumptions, props=props,
                     title_es=str(data.get("title_es") or ""))


def load(path: str) -> Portfolio:
    with open(path, encoding="utf-8") as fh:
        return parse(json.load(fh))


# ------------------------------------------------------------------ estimates
OPPS = {
    "om": ("O&M for the existing PV", "O&M de la FV existente"),
    "roof": ("Rooftop PV", "FV en techo"),
    "carport": ("Solar carport", "Estacionamiento solar"),
    "led": ("LED parking lighting", "Iluminación LED del estacionamiento"),
    "design": ("PV and LED designed in before opening", "FV y LED desde el diseño, antes de abrir"),
    "led_interior": ("LED interior lighting with controls", "Iluminación LED interior con controles"),
}


def roof_area(p: Prop) -> Optional[float]:
    return p.roof_m2 if p.roof_m2 is not None else p.gla_m2


def estimate(p: Prop, a: Dict[str, float]) -> Dict[str, object]:
    """Every number shown for one property. Rounded for display only by the caller."""
    roof = roof_area(p)
    y = p.yield_ if p.yield_ else a["yield_default"]
    existing = (roof * p.pv_share / a["m2_per_kwp"]) if (p.pv_seen and roof) else 0.0
    if p.roof_kwp is not None:
        new_roof = p.roof_kwp
    elif roof:
        new_roof = max(0.0, roof * a["roof_use"] / a["m2_per_kwp"] - existing)
    else:
        new_roof = 0.0
    spaces = p.parking or 0
    carport = round(spaces * a["carport_share"]) * a["kwp_per_space"] if (p.carport and spaces) else 0.0
    new_kwp = new_roof + carport
    new_mwh = new_kwp * y / 1000.0
    fixtures = math.ceil(spaces / a["spaces_per_fixture"]) if spaces else 0
    led_mwh = fixtures * a["led_w_saved"] * a["led_hours"] / 1e6
    pv_mxn = new_mwh * 1000.0 * a["tariff_mxn_kwh"]
    led_mxn = led_mwh * 1000.0 * a["tariff_mxn_kwh"]
    opps = []
    if existing > 0:
        opps.append("om")
    if p.status == "development":
        opps.append("design")
    if new_roof >= 50:
        opps.append("roof")
    if carport > 0:
        opps.append("carport")
    if fixtures > 0:
        opps.append("led")
    li = light(p, a)
    if li["fixtures"]:
        opps.append("led_interior")
    return {
        "roof_m2": roof, "yield": y, "existing_kwp": existing, "existing_mwh": existing * y / 1000.0,
        "new_roof_kwp": new_roof, "carport_kwp": carport, "new_kwp": new_kwp, "new_mwh": new_mwh,
        "pv_mxn": pv_mxn, "led_fixtures": fixtures, "led_mwh": led_mwh, "led_mxn": led_mxn,
        "light_fixtures": li["fixtures"], "light_mwh": li["mwh"], "light_mxn": li["mwh"] * 1000.0 * a["tariff_mxn_kwh"],
        "value_mxn": pv_mxn + led_mxn + li["mwh"] * 1000.0 * a["tariff_mxn_kwh"],
        "co2_t": (new_mwh + led_mwh + li["mwh"]) * a["co2_kg_kwh"], "opps": opps,
    }


def light(p: Prop, a: Dict[str, float]) -> Dict[str, float]:
    """Interior LED of one property (v364): fixtures = lit area / area per fixture (rounded up);
    before = fixtures x W before x hours; after = fixtures x W after x hours x (1 - controls share)."""
    lt = p.lighting
    area = (lt.area_m2 if lt and lt.area_m2 is not None else p.gla_m2) if lt else None
    if not lt or not area:
        return {"area_m2": 0.0, "fixtures": 0, "hours": 0.0, "w_before": 0.0, "w_after": 0.0,
                "mwh_before": 0.0, "mwh_after": 0.0, "mwh": 0.0}
    per = lt.m2_per_fixture if lt.m2_per_fixture is not None else a["led_m2_per_fixture"]
    h = lt.hours if lt.hours is not None else a["led_hours_interior"]
    wb = lt.w_before if lt.w_before is not None else a["led_w_before"]
    wa = lt.w_after if lt.w_after is not None else a["led_w_after"]
    n = math.ceil(area / per)
    before = n * wb * h / 1e6
    after = n * wa * h * (1 - a["led_controls_share"]) / 1e6
    return {"area_m2": area, "fixtures": n, "hours": h, "w_before": wb, "w_after": wa,
            "mwh_before": before, "mwh_after": after, "mwh": before - after}


def totals(pf: Portfolio) -> Dict[str, float]:
    keys = ("existing_kwp", "existing_mwh", "new_kwp", "new_mwh", "pv_mxn", "led_fixtures", "led_mwh",
            "led_mxn", "light_fixtures", "light_mwh", "light_mxn", "value_mxn", "co2_t")
    out = {k: 0.0 for k in keys}
    for p in pf.props:
        e = estimate(p, pf.assumptions)
        for k in keys:
            out[k] += float(e[k])
    out["properties"] = len(pf.props)
    out["operating"] = sum(1 for p in pf.props if p.status == "operating")
    out["development"] = sum(1 for p in pf.props if p.status == "development")
    out["pv_seen"] = sum(1 for p in pf.props if p.pv_seen)
    out["gla_m2"] = sum(p.gla_m2 or 0 for p in pf.props)
    out["parking"] = sum(p.parking or 0 for p in pf.props)
    out["lighting"] = sum(1 for p in pf.props if p.lighting)
    return out


def ranked(pf: Portfolio) -> List[Prop]:
    """Existing PV first (an O&M contract can start now), then by estimated yearly value."""
    return sorted(pf.props, key=lambda p: (not p.pv_seen, -float(estimate(p, pf.assumptions)["value_mxn"]), p.code))
