#!/usr/bin/env python3
"""Phase 3c: carry the Alerts ledger (sheet) into PostgreSQL alert_ledger
and prove both parse to the same ledger.

    PYTHONPATH=. python scripts/alerts_backfill_pg.py            # parity only
    PYTHONPATH=. python scripts/alerts_backfill_pg.py --apply    # upsert every sheet row, then parity

The sheet keeps changing until ARGIA_ALERTS_SOURCE=pg is set (every
alerts_snapshot tick may touch it), so the real sequence is: --apply,
parity CLEAN, flip — inside one alerts_snapshot interval.
EXIT: 0 clean   1 differences   3 config
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import fields
from typing import Dict, List, Tuple

TOL = 1e-6


def compare(sheet_ledger, pg_ledger) -> Dict:
    """Both are AlertsLedger objects. Every record, every field. PURE."""
    a = {r.alert_id: r for r in sheet_ledger.records}
    b = {r.alert_id: r for r in pg_ledger.records}
    only_s = sorted(set(a) - set(b))
    only_p = sorted(set(b) - set(a))
    diffs: List[Tuple[str, str, object, object]] = []
    for k in sorted(set(a) & set(b)):
        for f in fields(a[k]):
            x, y = getattr(a[k], f.name), getattr(b[k], f.name)
            if isinstance(x, float) and isinstance(y, float):
                if abs(x - y) > TOL:
                    diffs.append((k, f.name, x, y))
            elif x != y:
                diffs.append((k, f.name, x, y))
    return {"n_sheet": len(a), "n_pg": len(b), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs,
            "open_sheet": len(sheet_ledger.all_open()),
            "open_pg": len(pg_ledger.all_open()),
            "ok": not only_s and not diffs}


def render(rep: Dict) -> str:
    L = [f"Alerts ledger: sheet={rep['n_sheet']} pg={rep['n_pg']}"
         f"   OPEN: sheet={rep['open_sheet']} pg={rep['open_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:5]}   (must be 0)",
         f"  only in pg   : {len(rep['only_pg'])} {rep['only_pg'][:5]}   (allowed after the flip)",
         f"  field diffs  : {len(rep['diffs'])}"]
    for k, f, x, y in rep["diffs"][:10]:
        L.append(f"     {k} {f}: sheet={x!r} pg={y!r}")
    L.append("  VERDICT: " + ("CLEAN" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    from argia.core import alerts_pg as P
    from argia.core.alerts_state import record_to_row, records_from_rows
    from argia.core.sheets import SheetsClient

    sheet_rows = SheetsClient(sid).read_table("Alerts", "A1:O")
    sheet_ledger = records_from_rows(sheet_rows)
    P.ensure()
    if a.apply:
        n = P.write_rows([record_to_row(r) for r in sheet_ledger.records])
        print(f"backfill applied: {n} row(s) upserted into {P.TABLE}")
    pg_ledger = records_from_rows(P.read_records())
    rep = compare(sheet_ledger, pg_ledger)
    print(render(rep))
    print("OVERALL: " + ("CLEAN — safe to set ARGIA_ALERTS_SOURCE=pg"
                        if rep["ok"] else "DIFFERENT — do not flip"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
