#!/usr/bin/env python3
"""Phase 5a: carry the Plants / Inverters tabs into PostgreSQL plant /
inverter (new columns only) and prove load_portfolio sees the same
portfolio from both sources.

    PYTHONPATH=. python scripts/config_backfill_pg.py            # parity only
    PYTHONPATH=. python scripts/config_backfill_pg.py --apply    # fill new columns, then parity

Rules:
  * the 16 plant / 8 inverter columns PG already had are PostgreSQL's:
    they are never written from the sheet. The sheet only FILLS the
    columns added by config_pg.ENSURE_SQL (COALESCE(stored, sheet)).
  * parity runs load_portfolio's parser over both sources and compares
    every PlantConfig / InverterConfig field. Expected (PG-authoritative)
    differences: (plant, field) pairs edited through /setup and audited
    in finance_audit — pr_baseline -> pr_baseline, kwp_fix -> kwp_dc,
    om -> om_cost_monthly_mxn; date fields are compared as dates (the
    sheet stores Excel serials, PG stores dates); numbers within 0.0051
    (coordinates within 1e-5). Anything else fails.
  * one data fix on the way (--apply): plant.site_id '9309575.0' (a float
    export artefact) -> '9309575', what the sheet and the vendor APIs use.
EXIT: 0 clean   1 differences   3 config
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import fields
from typing import Any, Dict, FrozenSet, List, Tuple

AUDIT_FIELD = {"pr_baseline": "pr_baseline", "kwp_fix": "kwp_dc",
               "om": "om_cost_monthly_mxn"}
DATE_FIELDS = {"installation_date", "commissioning_date"}
TOL = 0.0051
COORD_TOL = 1e-5

SITE_ID_FIX_SQL = ("UPDATE plant SET site_id = regexp_replace(site_id, '\\.0$', '')"
                   " WHERE site_id ~ '\\.0$';")


def _as_date(v):
    """PlantConfig keeps the raw cell as text: an Excel serial ('45589')
    on the sheet, ISO from PG. coerce_date wants the serial as a number."""
    from argia.core.cells import coerce_date
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():
        return coerce_date(float(s))
    return coerce_date(s)


def _same(name: str, a: Any, b: Any) -> bool:
    if name in DATE_FIELDS:
        return _as_date(a) == _as_date(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        tol = COORD_TOL if name in ("lat", "lon") else TOL
        return abs(float(a) - float(b)) <= tol
    return a == b


def compare_plants(sheet: Dict, pg: Dict, audited: FrozenSet[Tuple[str, str]]) -> Dict:
    """{plant_key: PlantConfig} x2. PURE."""
    only_s = sorted(set(sheet) - set(pg)); only_p = sorted(set(pg) - set(sheet))
    diffs: List[Tuple[str, str, Any, Any]] = []
    expected: List[Tuple[str, str, Any, Any]] = []
    for k in sorted(set(sheet) & set(pg)):
        for f in fields(sheet[k]):
            a, b = getattr(sheet[k], f.name), getattr(pg[k], f.name)
            if _same(f.name, a, b):
                continue
            (expected if (k, f.name) in audited else diffs).append((k, f.name, a, b))
    return {"n_sheet": len(sheet), "n_pg": len(pg), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs, "expected": expected,
            "ok": not only_s and not only_p and not diffs}


def compare_inverters(sheet: Dict, pg: Dict) -> Dict:
    """{(plant_key, sn): InverterConfig} x2. PURE."""
    only_s = sorted(set(sheet) - set(pg)); only_p = sorted(set(pg) - set(sheet))
    diffs = []
    for k in sorted(set(sheet) & set(pg)):
        for f in fields(sheet[k]):
            a, b = getattr(sheet[k], f.name), getattr(pg[k], f.name)
            if not _same(f.name, a, b):
                diffs.append((k, f.name, a, b))
    return {"n_sheet": len(sheet), "n_pg": len(pg), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs, "expected": [],
            "ok": not only_s and not only_p and not diffs}


def render(name: str, rep: Dict) -> str:
    L = [f"{name}: sheet={rep['n_sheet']} pg={rep['n_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:5]}   (must be 0)",
         f"  only in pg   : {len(rep['only_pg'])} {rep['only_pg'][:5]}   (must be 0)",
         f"  field diffs  : {len(rep['diffs'])}"]
    for k, f, a, b in rep["diffs"][:12]:
        L.append(f"     {k} {f}: sheet={a!r} pg={b!r}")
    if rep["expected"]:
        L.append(f"  expected (PG authoritative, audited): {len(rep['expected'])}")
        for k, f, a, b in rep["expected"]:
            L.append(f"     {k} {f}: sheet={a!r} pg={b!r}")
    L.append("  VERDICT: " + ("CLEAN" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def audited_pairs() -> FrozenSet[Tuple[str, str]]:
    from argia.store.pgq import psql_rows
    out = set()
    for r in psql_rows("SELECT plant_key, action FROM finance_audit"
                       " WHERE plant_key IS NOT NULL AND plant_key <> '';"):
        if len(r) >= 2 and r[1] in AUDIT_FIELD:
            out.add((r[0], AUDIT_FIELD[r[1]]))
    return frozenset(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    from argia.core import config_pg as C
    from argia.core.config import load_portfolio
    from argia.core.sheets import SheetsClient
    from argia.store.pgq import psql_exec

    sheets = SheetsClient(sid)
    C.ensure()
    if a.apply:
        prow = sheets.read_table("Plants", "A1:ZZ")
        irow = sheets.read_table("Inverters", "A1:ZZ")
        psql_exec(SITE_ID_FIX_SQL)
        psql_exec(C.build_fill_sql("plant", C.PLANTS_HEADER, C.PLANT_EXISTING,
                                   ["plant_key"], prow))
        psql_exec(C.build_fill_sql("inverter", C.INVERTERS_HEADER,
                                   C.INVERTER_EXISTING,
                                   ["plant_key", "inverter_sn"], irow))
        print(f"backfill applied: {len(prow)} plant row(s), {len(irow)} inverter row(s)"
              " (new columns filled, existing columns untouched)")

    saved = os.environ.pop(C.SOURCE_ENV, None)
    try:
        os.environ[C.SOURCE_ENV] = "sheet"
        ps = load_portfolio(sheets)
        os.environ[C.SOURCE_ENV] = "pg"
        pp = load_portfolio(sheets)
    finally:
        if saved is None:
            os.environ.pop(C.SOURCE_ENV, None)
        else:
            os.environ[C.SOURCE_ENV] = saved

    ok = True
    rep = compare_plants(ps.plants, pp.plants, audited_pairs())
    ok &= rep["ok"]; print(render("Plants -> plant", rep))
    inv_s = {(i.plant_key, i.inverter_sn): i for v in ps.inverters_by_plant.values() for i in v}
    inv_p = {(i.plant_key, i.inverter_sn): i for v in pp.inverters_by_plant.values() for i in v}
    rep = compare_inverters(inv_s, inv_p)
    ok &= rep["ok"]; print(render("Inverters -> inverter", rep))
    print("OVERALL: " + ("CLEAN — safe to set ARGIA_CONFIG_SOURCE=pg"
                        if ok else "DIFFERENT — do not flip"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
