#!/usr/bin/env python3
"""Phase 3b: carry Invoicing_Overview (ARGIA Solar workbook) into the
PostgreSQL ``invoicing`` register, then prove the register serves the
annex the same history the sheet did.

    PYTHONPATH=. python scripts/invoicing_backfill_pg.py           # parity only
    PYTHONPATH=. python scripts/invoicing_backfill_pg.py --apply   # backfill, then parity

Backfill rule: a register row the sheet has and PG lacks is inserted
(check_status SHEET_IMPORT); an existing row only gets NULL columns
filled — no stored value is ever changed or lowered. Parity: every year
in the sheet, both sides through ``annex.parse_invoicing_overview``;
a plant-month only in the sheet, or any field difference, fails.
EXIT: 0 clean   1 differences   3 config
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict


def render(year: int, rep: Dict) -> str:
    L = [f"Invoicing_Overview {year}: sheet={rep['n_sheet']} pg={rep['n_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:5]}   (must be 0)",
         f"  only in pg   : {len(rep['only_pg'])} {rep['only_pg'][:5]}   (allowed)",
         f"  field diffs  : {len(rep['diffs'])}"]
    for k, f, a, b in rep["diffs"][:10]:
        L.append(f"     {k} {f}: sheet={a!r} pg={b!r}")
    L.append("  VERDICT: " + ("CLEAN" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true",
                    help="write the backfill (default: parity only)")
    a = ap.parse_args(argv)
    sid = os.environ.get("ARGIA_SOLAR_SHEET_ID", "").strip()
    if not sid:
        print("ARGIA_SOLAR_SHEET_ID not set"); return 3

    from argia.core.sheets import SheetsClient
    from argia.finance import invoicing_pg as I
    from argia.finance.annex import parse_invoicing_overview
    from argia.store.pgq import psql_exec

    grid = SheetsClient(sheet_id=sid).read_range("Invoicing_Overview", "A1:H2000")
    rows = I.sheet_rows(grid)
    years = sorted({int(r["ref_month"][:4]) for r in rows})
    print(f"sheet: {len(grid) - 1} rows, {len(rows)} invoiced plant-months, years {years}")

    psql_exec(I.ENSURE_SQL)
    if a.apply:
        psql_exec(I.build_backfill_sql(rows))
        print(f"backfill applied: {len(rows)} upserts (NULL-fill only)")

    pg_grid = I.read_grid()
    ok = True
    for y in years:
        rep = I.compare_history(parse_invoicing_overview(grid, y),
                                parse_invoicing_overview(pg_grid, y))
        ok &= rep["ok"]
        print(render(y, rep))
    print("OVERALL: " + ("CLEAN — safe to set ARGIA_INVOICING_SOURCE=pg"
                        if ok else "DIFFERENT — do not flip"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
