#!/usr/bin/env python3
"""v314: ARGIA's CPA LED project sheet -> /opt/argia/cpa/led.json.

    python3 scripts/cpa_led_import.py CPA_Status_YYYYMMDD.xlsx \
        --parks /opt/argia/cpa/cpa_parks.json --out /opt/argia/cpa/led.json

* Columns are found by their header text (row with "Status" and
  "Customer [CPA Park]"), so a moved column cannot shift a figure.
* Every project must land in a park of cpa_parks.json (locations taken
  from CPA's own developments and available-buildings maps); an
  unmatched building stops the import and is named.
* The sheet's own summary block is reconciled: projects, fixtures and
  area per status, and the delivered retrofits' yearly savings. Any
  difference is printed and the import fails (--force writes anyway).
* No price, cost or currency column is read.

v315: --answers applies the field team's answers (cpa_led_answers.json:
handover and planned dates, a missing "before" load, tenant / vacant,
controls share, design light level, exact location, status) AFTER the
sheet has been reconciled, each one keyed by project id AND building name
(a moved row cannot take someone else's answer); every change is printed.

Both files are server-only (tenants and buildings are CPA business data;
the repo is public). Nothing is written unless every check passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.cpa import led as LED  # noqa: E402

HEADERS = {
    "id": "id", "status": "status", "building": "customer [cpa park]", "market": "city/market",
    "address": "park / address", "tenant": "tenant", "area_m2": "area [m2]", "kw_before": "before kw",
    "kw_after": "after kw", "fixtures": "fixtures", "lux_before": "lux level before", "lux_after": "actual lux level",
    "kind": "type", "automation": "automation", "hours": "operating hours [h/yr]",
}
EMPTY = {"", "-", "na", "n/a", "none", chr(0x2014), chr(0x2013)}


def norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_" + chr(0x2013) + chr(0x2014) + "]", " ", str(s or ""))).strip().upper()


def building_name(raw) -> str:
    n = norm(raw)
    return n[4:] if n.startswith("CPA ") else n


def text(v) -> Optional[str]:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return None if s.lower() in EMPTY else s


def num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = text(v)
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def lux(v) -> Optional[str]:
    s = text(v)
    return re.sub(r"(\d)\s*(lx|fc)\b", r"\1 \2", s) if s else None


def find_header(rows: List[Tuple]) -> Tuple[int, Dict[str, int]]:
    for i, r in enumerate(rows):
        cells = [str(c).strip().lower() if c is not None else "" for c in r]
        if "status" in cells and HEADERS["building"] in cells:
            cols = {}
            for k, h in HEADERS.items():
                if h not in cells:
                    raise ValueError(f"column {h!r} not found in the header row")
                cols[k] = cells.index(h)
            return i, cols
    raise ValueError("header row (Status / Customer [CPA Park]) not found")


def sheet_summary(rows: List[Tuple], upto: int) -> Tuple[Dict[str, Tuple[float, float, float]], Optional[float]]:
    """The sheet's own summary: {status: (projects, fixtures, area)} and the
    delivered retrofits' 'Estimated annual savings [kWh]'."""
    per, saved = {}, None
    for r in rows[:upto]:
        cells = list(r)
        for j, c in enumerate(cells):
            lab = str(c).strip().lower() if c is not None else ""
            if lab in LED.SHEET_STATUS and j + 3 < len(cells):
                per[LED.SHEET_STATUS[lab]] = tuple(num(x) or 0.0 for x in cells[j + 1:j + 4])
            if lab.startswith("estimated annual savings"):
                saved = next((num(x) for x in cells[j + 1:] if num(x) is not None), None)
    return per, saved


def match_park(name: str, parks: List[dict]) -> Optional[dict]:
    padded = f" {name} "
    for p in parks:
        if any(f" {norm(t)} " in padded for t in p.get("match", [])):
            return p
    return None


def convert(rows: List[Tuple], parks_doc: dict) -> Tuple[dict, List[str]]:
    """(led.json document, problems). Pure."""
    hi, col = find_header(rows)
    parks = parks_doc.get("parks") or []
    exact = [(norm(b["match"]), b) for b in parks_doc.get("buildings") or []]
    problems, projects, used = [], [], set()
    for r in rows[hi + 1:]:
        cell = lambda k: r[col[k]] if col[k] < len(r) else None       # noqa: E731
        raw_status = text(cell("status"))
        if not raw_status and not text(cell("building")):
            continue
        status = LED.SHEET_STATUS.get((raw_status or "").lower())
        name = building_name(cell("building"))
        if not status:
            problems.append(f"{name or '?'}: unknown status {raw_status!r}")
            continue
        park = match_park(name, parks)
        if not park:
            problems.append(f"{name}: no park in cpa_parks.json matches this building")
            continue
        used.add(park["id"])
        hit = next((b for m, b in exact if f" {m} " in f" {name} "), None)
        auto = (text(cell("automation")) or "").lower()
        kind = (text(cell("kind")) or "").lower()
        projects.append({
            "id": f"LED-{int(num(cell('id')) or len(projects) + 1):02d}", "status": status, "building": name,
            "park": park["id"], "tenant": text(cell("tenant")), "area_m2": num(cell("area_m2")),
            "fixtures": int(num(cell("fixtures"))) if num(cell("fixtures")) is not None else None,
            "kw_before": num(cell("kw_before")), "kw_after": num(cell("kw_after")), "hours": num(cell("hours")),
            "kind": kind if kind in LED.KINDS else None, "sensors": "sensor" in auto or "nlight" in auto,
            "lux_before": lux(cell("lux_before")), "lux_after": lux(cell("lux_after")),
            "lat": hit["lat"] if hit else None, "lon": hit["lon"] if hit else None,
        })
    doc = {"imported": dt.date.today().isoformat(),
           "parks": [{k: p[k] for k in ("id", "name", "city", "lat", "lon")} for p in parks if p["id"] in used],
           "projects": projects}
    return doc, problems


def reconcile(doc: dict, rows: List[Tuple]) -> List[str]:
    """Differences between the converted projects and the sheet's own summary."""
    hi, _ = find_header(rows)
    per, saved = sheet_summary(rows, hi)
    s = LED.summarise([LED.Project(**r) for r in doc["projects"]])
    out = []
    for st, (n, fx, area) in per.items():
        got = s[st]
        if (got.projects, got.fixtures) != (int(n), int(fx)) or abs(got.area_m2 - area) > 0.5:
            out.append(f"{st}: sheet {int(n)} projects / {int(fx)} fixtures / {area:,.1f} m2, "
                       f"import {got.projects} / {got.fixtures} / {got.area_m2:,.1f}")
    if saved is not None and abs(s["delivered"].saved_kwh - saved) > 1.0:
        out.append(f"delivered savings: sheet {saved:,.0f} kWh/yr, import {s['delivered'].saved_kwh:,.0f}")
    if not per:
        out.append("the sheet's summary block was not found - nothing to reconcile against")
    return out


ANSWER_KEYS = {"kw_before", "tenant", "vacant", "delivered", "planned", "controls_pct", "lux_before", "lux_after",
               "lat", "lon", "park", "status"}


def apply_answers(doc: dict, answers: dict, parks_doc: dict) -> Tuple[List[str], List[str]]:
    """(changes, problems). Pure apart from mutating ``doc``."""
    by_id = {r["id"]: r for r in doc["projects"]}
    park_ids = {p["id"] for p in parks_doc.get("parks") or []}
    changes, problems = [], []
    for pid, a in (answers.get("projects") or {}).items():
        r = by_id.get(pid)
        if r is None:
            problems.append(f"{pid}: no such project in the sheet")
            continue
        if building_name(a.get("building", "")) != r["building"]:
            problems.append(f"{pid}: answer is for {a.get('building')!r}, the sheet row is {r['building']!r}")
            continue
        for k, v in (a.get("set") or {}).items():
            if k not in ANSWER_KEYS:
                problems.append(f"{pid}: unknown field {k!r}")
            elif k == "status" and v not in LED.STATUSES:
                problems.append(f"{pid}: unknown status {v!r}")
            elif k == "park" and v not in park_ids:
                problems.append(f"{pid}: unknown park {v!r}")
            elif r.get(k) != v:
                changes.append(f"{pid} {r['building']}: {k} {r.get(k)!r} -> {v!r}")
                r[k] = v
    used = {r["park"] for r in doc["projects"]}
    doc["parks"] = [{k: p[k] for k in ("id", "name", "city", "lat", "lon")} for p in parks_doc.get("parks") or [] if p["id"] in used]
    return changes, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xlsx")
    ap.add_argument("--parks", default="/opt/argia/cpa/cpa_parks.json")
    ap.add_argument("--out", default="/opt/argia/cpa/led.json")
    ap.add_argument("--force", action="store_true", help="write even if the sheet summary disagrees")
    ap.add_argument("--answers", help="field team's answers applied after the reconcile (cpa_led_answers.json)")
    a = ap.parse_args(argv)
    import openpyxl
    wb = openpyxl.load_workbook(a.xlsx, data_only=True, read_only=True)
    rows = [tuple(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    with open(a.parks, encoding="utf-8") as fh:
        parks_doc = json.load(fh)
    doc, problems = convert(rows, parks_doc)
    doc["source"] = os.path.basename(a.xlsx)
    for p in problems:
        print("PROBLEM", p)
    if problems:
        print("not written")
        return 2
    diffs = reconcile(doc, rows)
    for d in diffs:
        print("MISMATCH", d)
    if a.answers:
        with open(a.answers, encoding="utf-8") as fh:
            answers = json.load(fh)
        changes, bad = apply_answers(doc, answers, parks_doc)
        for c in changes:
            print("ANSWER", c)
        for b in bad:
            print("PROBLEM", b)
        if bad:
            print("not written")
            return 2
        doc["answers"] = os.path.basename(a.answers)
    import datetime as _dt
    s = LED.summarise([LED.Project(**r) for r in doc["projects"]], today=_dt.date.today())
    for st in list(LED.STATUSES) + ["_all"]:
        x = s[st]
        print(f"{st:11s} {x.projects:3d} projects {x.fixtures:6d} fixtures {x.area_m2:12,.1f} m2 "
              f"{x.saved_kwh:12,.0f} kWh/yr {x.co2_t:9,.1f} t CO2e/yr  parks {x.parks}"
              + (f"  to date {x.co2_to_date:,.1f} t since {x.first}" if x.first else ""))
    if diffs and not a.force:
        print("not written (use --force to write anyway)")
        return 3
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    LED.load(tmp)                                   # the generator must be able to read it
    os.replace(tmp, a.out)
    print(f"written {a.out}: {len(doc['projects'])} projects in {len(doc['parks'])} parks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
