#!/usr/bin/env python3
"""Swap the HelioScope design of a PPA string layout already on the server (v371).

    string_layout_helio.py /opt/argia/layouts MEX2 PPA_Vitalmex.pdf --src "HelioScope 'PPA Vitalmex' (A. Gonzalez, 11.11.2026)"
        # dry run: writes <dir>/_try/MEX2.json + MEX2_helio.jpg, prints the fit, the live files untouched
    ... --apply     # backs the live MEX2.json + MEX2_helio.jpg up to <dir>/_v371/ (once) and writes in place

Tomasz, 12 Nov 2026: "have you replaced the helioscope design inside monitoring for each plant drawing?" -
the Nov 2026 as-connected HelioScope runs replace the old ones on every plant's string-layout page.

Only the HelioScope view changes: the installation drawing, its strings and inverter areas stay exactly as
built (the drawing's module rectangles are read back from the layout file, so the drawing PDF and the
workbook are not needed). The markers are placed by the same registration as the full build
(tools/helio_layout.py); below its thresholds no marker is placed and the page says why.
The layout files are customer data: server-only, never in the repo.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helio_layout as HL        # noqa: E402


def strip_helio(doc: Dict) -> Dict:
    """The layout without any HelioScope part (view, positions, areas, misfit note). Pure."""
    d = copy.deepcopy(doc)
    d["drawings"] = [x for x in d.get("drawings", []) if x.get("kind") != "helioscope"]
    d["areas"] = [a for a in d.get("areas", []) if a.get("d") != "helio"]
    for st in d.get("strings", []):
        st["pos"] = [p for p in st.get("pos", []) if p.get("d") != "helio"]
    d["notes"] = [n for n in d.get("notes", []) if not str(n[0]).startswith(HL.MISFIT_EN)]
    return d


def drawing_inputs(doc: Dict) -> Tuple[List[tuple], List[int], int, List[Dict]]:
    """(module shapes x0,y0,x1,y1,inv in drawing points, drawing box, drawing px width, drawing areas). Pure."""
    dr = next(x for x in doc["drawings"] if x.get("id") == "drawing")
    areas = [a for a in doc["areas"] if a.get("d") == "drawing"]
    shapes = [(x, y, x + w, y + h, a["inv"]) for a in areas for x, y, w, h in a["rects"]]
    if not shapes:
        raise ValueError(f"{doc.get('plant')}: the layout has no drawing module areas to register")
    return shapes, dr["box"], int(dr["px"][0]), areas


def swap(doc: Dict, img, counts: Dict, src: str, out: str) -> Dict:
    """The layout with the new HelioScope view (writes <out>/<plant>_helio.jpg)."""
    d = strip_helio(doc)
    pk = d["plant"]
    shapes, box, px_w, areas = drawing_inputs(d)
    hd, hareas = HL.place(img, counts, pk, src, out, shapes, box, px_w, d["strings"], areas)
    d["drawings"].append(hd)
    d["areas"] += hareas
    if not hd["markers"]:
        d["notes"].append([HL.MISFIT_EN + f' (overlap {hd["fit"]:.0%}, blocks paired {hd["paired"]:.0%}), so the strings are '
                           'only marked on the installation drawing.',
                           HL.MISFIT_ES + f' (traslape {hd["fit"]:.0%}, bloques emparejados {hd["paired"]:.0%}), por eso las '
                           'cadenas solo se marcan en el plano de instalación.'])
    chk = d.setdefault("checks", {})
    chk.setdefault("inverters", {})["helioscope"] = hd["counts"]["inverters"]
    chk.setdefault("strings", {})["helioscope"] = hd["counts"]["strings"]
    return d


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("layouts")
    ap.add_argument("plant")
    ap.add_argument("pdf")
    ap.add_argument("--src", required=True, help="the report's name as the page shows it")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    pk = a.plant.upper()
    live = os.path.join(a.layouts, f"{pk}.json")
    doc = json.load(open(live, encoding="utf-8"))
    out = a.layouts if a.apply else os.path.join(a.layouts, "_try")
    os.makedirs(out, exist_ok=True)
    img, counts = HL.helio_image(a.pdf, os.path.join(a.layouts, "_helio_tmp"))
    old = next((x for x in doc["drawings"] if x.get("kind") == "helioscope"), {})
    if a.apply:
        bak = os.path.join(a.layouts, "_v371")
        os.makedirs(bak, exist_ok=True)
        for f in (f"{pk}.json", f"{pk}_helio.jpg"):
            if os.path.exists(os.path.join(a.layouts, f)) and not os.path.exists(os.path.join(bak, f)):
                shutil.copy2(os.path.join(a.layouts, f), os.path.join(bak, f))
    new = swap(doc, img, counts, a.src, out)
    hd = next(x for x in new["drawings"] if x.get("kind") == "helioscope")
    tmp = os.path.join(out, f"{pk}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(out, f"{pk}.json"))
    shutil.rmtree(os.path.join(a.layouts, "_helio_tmp"), ignore_errors=True)
    print(f"{pk}: {old.get('src', 'none')} (fit {old.get('fit')}, markers {old.get('markers')}) -> {hd['src']} "
          f"(fit {hd['fit']}, paired {hd['paired']}, markers {hd['markers']}, counts {hd['counts']}); "
          f"checks {new['checks']}; written to {out}{'' if a.apply else ' (dry run, live files untouched)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
