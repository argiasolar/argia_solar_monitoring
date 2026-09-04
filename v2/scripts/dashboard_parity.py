#!/usr/bin/env python3
"""Phase 3d gate: after a dashboard_update run in ARGIA_DASHBOARD_SOURCE=both
(both targets written from the SAME matrices), do the Dashboard tabs and
the PostgreSQL tables read back as the same buckets?

Rows are keyed (date_mx, plant_key, bucket_ts[, inverter_sn]); every
column is compared the way the readers see it — date_key on dates,
safe_float on numbers (0.0051), text otherwise.

    PYTHONPATH=. python scripts/dashboard_parity.py
EXIT: 0 identical   1 differences   3 config
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

TOL = 0.0051


def norm(name: str, v: Any, numeric: set):
    from argia.core.cells import coerce_date
    from argia.core.normalize import safe_float
    if name == "date_mx":
        d = coerce_date(v)
        return d.isoformat() if d else str(v or "")
    if name in numeric:
        return safe_float(v)
    return str(v if v is not None else "").strip()


def compare(sheet_rows: List[Dict], pg_rows: List[Dict], columns: List[str],
            key_cols: List[str], numeric: set) -> Dict:
    def key(r):
        return tuple(norm(c, r.get(c), numeric) for c in key_cols)
    a = {key(r): r for r in sheet_rows}
    b = {key(r): r for r in pg_rows}
    only_s = sorted(set(a) - set(b))
    only_p = sorted(set(b) - set(a))
    diffs = []
    for k in sorted(set(a) & set(b)):
        for c in columns:
            x, y = norm(c, a[k].get(c), numeric), norm(c, b[k].get(c), numeric)
            if isinstance(x, float) and isinstance(y, float):
                if abs(x - y) > TOL:
                    diffs.append((k, c, x, y))
            elif x != y:
                diffs.append((k, c, x, y))
    return {"n_sheet": len(a), "n_pg": len(b), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs,
            "ok": not only_s and not only_p and not diffs}


def render(name: str, rep: Dict) -> str:
    L = [f"{name}: sheet={rep['n_sheet']} pg={rep['n_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:3]}",
         f"  only in pg   : {len(rep['only_pg'])} {rep['only_pg'][:3]}",
         f"  field diffs  : {len(rep['diffs'])}"]
    for k, c, x, y in rep["diffs"][:8]:
        L.append(f"     {k} {c}: sheet={x!r} pg={y!r}")
    L.append("  VERDICT: " + ("IDENTICAL" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.parse_args(argv)
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    from argia.core.sheets import SheetsClient
    from argia.report import dashboard_pg as DP
    from argia.report.dashboard import INVERTER_COLUMNS, PLANT_COLUMNS
    s = SheetsClient(sid)
    ok = True
    rep = compare(s.read_table("Dashboard_Plant", "A1:ZZ"), DP.read_plant_records(),
                  PLANT_COLUMNS, ["date_mx", "plant_key", "bucket_ts"], DP.NUMERIC)
    ok &= rep["ok"]; print(render("Dashboard_Plant", rep))
    rep = compare(s.read_table("Dashboard_Inverter", "A1:ZZ"), DP.read_inverter_records(),
                  INVERTER_COLUMNS, ["date_mx", "plant_key", "inverter_sn", "bucket_ts"],
                  DP.NUMERIC)
    ok &= rep["ok"]; print(render("Dashboard_Inverter", rep))
    print("OVERALL: " + ("IDENTICAL — safe to set ARGIA_DASHBOARD_SOURCE=pg"
                        if ok else "DIFFERENT — do not flip"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
