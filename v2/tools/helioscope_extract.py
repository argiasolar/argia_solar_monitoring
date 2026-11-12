#!/usr/bin/env python3
"""HelioScope (Aurora Solar) "Annual Production Report" PDF -> the plant's entry in the design file (v370).

    helioscope_extract.py REPORT.pdf --plant SLP1 --sensor ghi --cod 2024-08-01 --into ppa_designs.json \
        [--degradation 0.0055] [--caveat TEXT]          # dry run: prints the entry, writes nothing
    ... --apply                                         # upsert it into the file (0600, atomic)

What it reads (pdftotext -raw): the system metrics (Module DC / Inverter AC nameplate, annual production,
PR, kWh/kWp), Energy to Grid, the 12-month table (GHI, POA, Shaded, Nameplate, Grid), the condition set name,
weather dataset, the monthly soiling row and the loss tree. The result is validated with the same rules the
monitoring uses (argia.kpi.design_model.parse) before anything is written.

``--sensor`` is NOT in the report: it is the plane the plant's pyranometer measures ("ghi" horizontal,
"poa" in the module plane). Nov 2026: Quimica Coyoacan and Plastic Omnium are proven horizontal (sensor vs
satellite stays flat through the winter, where a tilted sensor reads +14 to +27 %); the 2-3 deg roofs read
the same either way.

The PDFs and the output are customer data: server-only, never in the repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.kpi import design_model as DM            # noqa: E402

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
LOSS_NAMES = ("AC System", "Shading", "Inverters", "Clipping", "Wiring", "Reflection", "Electrical Mismatch",
              "Mismatch", "Soiling", "Temperature", "Irradiance", "Optimizers")
_NUM = r"([\d,]+(?:\.\d+)?)"


class Unreadable(ValueError):
    """The text is not an Aurora annual production report (or its layout changed)."""


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def parse_report(text: str) -> Dict:
    """The design entry (without plant/sensor/cod) from the report's ``pdftotext -raw`` text. Pure."""
    lines = [ln.strip() for ln in text.splitlines()]
    flat = " ".join(x for x in lines if x)
    missing: List[str] = []

    def one(rx: str, what: str):
        m = re.search(rx, flat)
        if not m:
            missing.append(what)
        return m

    design = one(r"\bDesign (.+?) Module DC", "design name")
    kwp = one(r"Module DC Nameplate " + _NUM + r" kW", "Module DC nameplate")
    kwac = one(r"Inverter AC Nameplate " + _NUM + r" kW", "Inverter AC nameplate")
    pr = one(r"Performance Ratio ([\d.]+)%", "performance ratio")
    grid = one(r"Energy to Grid " + _NUM, "Energy to Grid")
    rdate = re.search(r"Aurora Solar \d+ / \d+ ([A-Z][a-z]+ \d{1,2}, \d{4})", flat)
    author = re.search(r"Annual Production Report produced by (.+?)(?: ©|$)", flat)
    weather = re.search(r"Weather Dataset (T[MDG]Y.+?NREL \(\w+\))", flat)
    cset = re.search(r"Description (?!Output)(.+?) Weather Dataset", flat)
    months = []
    for name in MONTHS:
        m = re.search(rf"^{name} {_NUM} {_NUM} {_NUM} {_NUM} {_NUM}$", text, re.M)
        if not m:
            missing.append(f"month {name}")
            continue
        ghi, poa, _shaded, _nameplate, kwh = (_f(m.group(i)) for i in range(1, 6))
        months.append({"ghi": ghi, "poa": poa, "grid_kwh": kwh})
    soil = re.search(r"Soiling \(%\) J F M A M J J A S O N D " + " ".join([r"([\d.]+)"] * 12), flat)
    if soil and len(months) == 12:
        for i, m in enumerate(months):
            m["soiling_pct"] = float(soil.group(i + 1))
    if missing:
        raise Unreadable("not found in the report: " + ", ".join(missing))
    losses = {}
    for name in LOSS_NAMES:
        m = re.search(rf"(?<![A-Za-z]){re.escape(name)}: ([\d.]+)%", flat)
        if m and name not in losses:
            losses[name] = float(m.group(1))
    if "Electrical Mismatch" in losses:
        losses.pop("Mismatch", None)
    out = {"source": f"HelioScope '{design.group(1).strip()}'"
                     + (f", {author.group(1).strip()}" if author else ""),
           "kwp_dc": _f(kwp.group(1)), "kwp_ac": _f(kwac.group(1)), "annual_kwh": _f(grid.group(1)),
           "pr": round(float(pr.group(1)) / 100.0, 4), "months": months, "losses": losses,
           "weather": weather.group(1) if weather else "", "condition_set": cset.group(1).strip() if cset else ""}
    if rdate:
        import datetime as dt
        out["report_date"] = dt.datetime.strptime(rdate.group(1), "%B %d, %Y").date().isoformat()
    return out


def entry(text: str, sensor: str, cod: str, degradation: float = DM.DEFAULT_DEGRADATION,
          caveat: str = "") -> Dict:
    e = parse_report(text)
    e.update({"sensor": sensor, "cod": cod, "degradation": degradation})
    if caveat:
        e["caveat"] = caveat
    return e


def upsert(doc: Dict, plant: str, e: Dict) -> Dict:
    """A new design file dict with ``plant`` replaced; validated as a whole (raises DM.Invalid). Pure."""
    new = {"version": 1, "plants": dict((doc or {}).get("plants") or {})}
    new["plants"][plant.upper()] = e
    DM.parse(new)
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf")
    ap.add_argument("--plant", required=True)
    ap.add_argument("--sensor", required=True, choices=DM.BASES)
    ap.add_argument("--cod", required=True, help="interconnection date YYYY-MM-DD (ageing starts there)")
    ap.add_argument("--degradation", type=float, default=DM.DEFAULT_DEGRADATION)
    ap.add_argument("--caveat", default="")
    ap.add_argument("--into", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    text = subprocess.run(["pdftotext", "-raw", a.pdf, "-"], check=True, capture_output=True,
                          text=True).stdout
    e = entry(text, a.sensor, a.cod, a.degradation, a.caveat)
    doc = json.load(open(a.into, encoding="utf-8")) if os.path.exists(a.into) else {}
    new = upsert(doc, a.plant, e)
    d = DM.parse(new)[a.plant.upper()]
    print(f"{a.plant.upper()}: {d.source} {d.report_date} - {d.kwp_dc} kWp, {d.annual_kwh:,.1f} kWh/yr "
          f"({d.annual_kwh / d.kwp_dc:,.1f} kWh/kWp), PR {d.pr}, {d.condition_set}, {d.weather}, sensor {d.sensor}")
    if not a.apply:
        print("(dry run, nothing written)")
        return 0
    tmp = a.into + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(new, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, a.into)
    print(f"written {a.into}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
