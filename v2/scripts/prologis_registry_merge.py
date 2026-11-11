#!/usr/bin/env python3
"""Merge a Prologis site package into the portal's site registry (v369).

    prologis_registry_merge.py PACKAGE.json             # dry run: what would change, nothing written
    prologis_registry_merge.py PACKAGE.json --apply     # backup registry.json, write the merged file

PACKAGE (server-only, customer data - never in the repo)::

    {"source": "...", "portfolio": {"name", "market"},
     "update": {"CODE": {pm fields..., optional "lat", "lon", "geo_approx": false, "geo_note",
                         report-only "rfp_kwp", "rfp_name"}},
     "add": [full site rows]}

Rules: an existing site keeps its name, kWp, PTO, address and status - the package only adds the PM fields
(tenant, solar ID, interconnection, in operation since, Prologis priority, last cleaning, requested services), and
moves the pin only when the registry's point is approximate and the package has the building. Differences in kWp or
name are reported, never applied. A site already in the registry is never added twice. The result must pass the
same validation the portal uses (registry.parse) before anything is written.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.prologis import registry as R            # noqa: E402

PM_FIELDS = ("solar_id", "customer", "market", "scheme", "in_operation", "pl_priority", "last_cleaning", "scope")


def _words(name: str) -> List[str]:
    """A building name without its spelling: 'Alamos, Building 1' == 'Alamos Bldg #1' == 'ALAMOS 1'. Pure."""
    import re
    w = re.sub(r"[^a-z0-9 ]", " ", str(name).lower().replace("building", "bldg")).split()
    return [x for x in w if x not in ("bldg", "prologis", "park")]


def merge(reg: Dict, pkg: Dict) -> Tuple[Dict, List[str]]:
    """(new registry dict, report lines). Pure: the inputs are not changed."""
    out = copy.deepcopy(reg)
    rep: List[str] = []
    by = {s["code"].upper(): s for s in out["sites"]}
    for code, u in sorted((pkg.get("update") or {}).items()):
        s = by.get(code.upper())
        if s is None:
            rep.append(f"update {code}: not in the registry - skipped")
            continue
        changed = [k for k in PM_FIELDS if k in u and s.get(k) != u[k]]
        for k in changed:
            s[k] = u[k]
        if "lat" in u and s.get("geo_approx", True) and u.get("geo_approx") is False:
            s["lat"], s["lon"], s["geo_approx"], s["geo_note"] = u["lat"], u["lon"], False, u.get("geo_note", "")
            changed.append("position")
        if changed:
            rep.append(f"update {code}: {', '.join(changed)}")
        if u.get("rfp_kwp") is not None and abs(float(u["rfp_kwp"]) - float(s["kwp"])) > 0.5:
            rep.append(f"DIFF   {code}: kWp registry {s['kwp']} vs package {u['rfp_kwp']} (kept)")
        if u.get("rfp_name") and _words(u["rfp_name"]) != _words(s["name"]):
            rep.append(f"name   {code}: registry {s['name']!r} / package {u['rfp_name']!r} (kept)")
    for row in pkg.get("add") or []:
        code = str(row["code"]).upper()
        if code in by:
            rep.append(f"add    {code}: already in the registry - not added")
            continue
        new = {k: v for k, v in row.items() if v is not None}
        out["sites"].append(new)
        by[code] = new
        rep.append(f"add    {code}: {row['name']} ({row['kwp']} kWp, {row.get('city', '')}, "
                   f"{'building' if row.get('geo_approx') is False else 'approximate'} position)")
    if pkg.get("portfolio"):
        out["portfolio"] = dict(out.get("portfolio") or {}, **pkg["portfolio"])
    R.parse(out)                                       # the portal's own validation, all problems at once
    return out, rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("package")
    ap.add_argument("--registry", default=os.environ.get("ARGIA_PL_REGISTRY", R.DEFAULT_PATH))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    reg = json.load(open(a.registry, encoding="utf-8"))
    pkg = json.load(open(a.package, encoding="utf-8"))
    out, rep = merge(reg, pkg)
    for line in rep:
        print(line)
    print(f"sites: {len(reg['sites'])} -> {len(out['sites'])}{'' if a.apply else ' (dry run, nothing written)'}")
    if a.apply:
        bak = a.registry + ".bak_v369"
        if not os.path.exists(bak):
            shutil.copy2(a.registry, bak)
        tmp = a.registry + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, a.registry)
        print(f"written {a.registry} (backup {bak})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
