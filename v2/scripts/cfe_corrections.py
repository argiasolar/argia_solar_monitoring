#!/usr/bin/env python3
"""cfe_tariff corrections register vs the table (v239).

    cfe_corrections.py            # check: exit 2 when a confirmed cell differs
    cfe_corrections.py --sql      # print the UPDATEs --apply would run
    cfe_corrections.py --apply    # write the confirmed corrections (idempotent), then re-check

The register is data/cfe_corrections.json; drift_check runs the check
every morning and a difference reaches the administrator's digest.
Consumers of cfe_tariff stay read-only — this is the one writer besides
cfe_load, and it only ever moves a cell to the value the register names.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Tuple

from argia.core import cfe_corrections as CC


def table_rows(rows, reg) -> List[Tuple[str, str, str, str, str]]:
    return [tuple(r[:5]) for r in rows("SET statement_timeout='10s'; " + CC.select_sql(reg)) if len(r) >= 5]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cfe_tariff corrections register vs the table")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sql", action="store_true")
    a = ap.parse_args(argv)
    from argia.store.pgq import psql_exec, psql_rows
    reg = CC.load()
    bad = CC.validate(reg)
    if bad:
        print("register invalid:")
        for b in bad:
            print("  - " + b)
        return 3
    rows = table_rows(psql_rows, reg)
    if a.sql:
        for s in CC.apply_sql(reg, rows):
            print(s)
        return 0
    if a.apply:
        for s in CC.apply_sql(reg, rows):
            print("apply: " + s)
            psql_exec(s)
        rows = table_rows(psql_rows, reg)
    findings = CC.compare(reg, rows)
    n = len(CC.wanted(reg))
    print(f"cfe_corrections: {n} confirmed cell(s), {len(findings)} differ")
    for f in findings:
        print("  - " + f)
    for q in reg.get("open_questions") or []:
        print("  ? " + q)
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
