#!/usr/bin/env python3
"""v248 — the cost-centre catalogue: list it, or fix a kind by hand.

    fin_cost_centers.py                       # list: code, kind, group, manual flag, name
    fin_cost_centers.py --set 772=project     # a manual kind (kept by every future ingest)
    fin_cost_centers.py --set 1259=overhead --set 704=payroll
    fin_cost_centers.py --auto 772            # back to the automatic rule

Kinds: project | overhead | payroll | warranty | other. A payroll code
joins the SALARIES group (one line on /finance/costs/). Env: ARGIA_PG_DB,
ARGIA_FIN_ENTITY (ARGIA-MX). Exit 0, or 2 when the database is unreachable.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.fin import costcenter as CC      # noqa: E402
from argia.fin.ingest import _lit           # noqa: E402

ENTITY = os.environ.get("ARGIA_FIN_ENTITY", "ARGIA-MX")


def _rows(sql: str):
    from argia.store.pgq import psql_rows
    return psql_rows("SET statement_timeout='20s'; " + sql)


def _exec(sql: str):
    from argia.store.pgq import psql_exec
    psql_exec("SET statement_timeout='20s';\n" + sql)


def set_sql(code: int, kind: str, entity: str = ENTITY) -> str:
    if kind not in CC.KINDS:
        raise ValueError(f"kind must be one of {', '.join(CC.KINDS)}")
    grp = CC.SALARIES if kind == CC.PAYROLL else ""
    return (f"UPDATE cost_center SET kind = {_lit(kind)}, grp = {_lit(grp)}, manual = true"
            f" WHERE entity_id = {_lit(entity)} AND code = {int(code)};")


def auto_sql(code: int, entity: str = ENTITY) -> str:
    return f"UPDATE cost_center SET manual = false WHERE entity_id = {_lit(entity)} AND code = {int(code)};"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", action="append", default=[], metavar="CODE=KIND")
    ap.add_argument("--auto", action="append", default=[], type=int, metavar="CODE")
    a = ap.parse_args(argv)
    stmts = []
    for x in a.set:
        code, _, kind = x.partition("=")
        try:
            stmts.append(set_sql(int(code), kind.strip().lower()))
        except ValueError as e:
            print(f"FAILED: --set {x}: {e}")
            return 1
    stmts += [auto_sql(c) for c in a.auto]
    try:
        if stmts:
            _exec("BEGIN;\n" + "\n".join(stmts) + "\nCOMMIT;")
            print(f"updated {len(stmts)} row(s); the automatic rule re-applies to non-manual codes at the next ingest")
        rows = _rows(f"SELECT code, kind, grp, manual, name FROM cost_center WHERE entity_id = {_lit(ENTITY)} ORDER BY kind, code;")
    except Exception as e:                    # noqa: BLE001
        print(f"FAILED: database ({type(e).__name__}: {str(e)[:120]})")
        return 2
    print(f"cost centres ({ENTITY}): {len(rows)}")
    for r in rows:
        print(f"  {r[0]:>5}  {r[1]:<9} {r[2]:<9} {'manual' if r[3] == 't' else '':<7} {r[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
