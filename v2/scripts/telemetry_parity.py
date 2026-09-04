#!/usr/bin/env python3
"""The Phase-1 gate: does PostgreSQL give kpi_eod the same day as the sheet?

Builds the DayBundle for one MX date twice — from ``Telemetry_Argia`` and
from the ``telemetry`` table — through the SAME parser, then compares:
row counts per plant, the set of natural keys (timestamp_utc, plant,
inverter), and every field of every row the two sources share. Nothing
flips to ``ARGIA_TELEMETRY_SOURCE=pg`` until this exits 0.

USAGE (server):
    PYTHONPATH=. python scripts/telemetry_parity.py --date 2026-09-03
EXIT: 0 identical   1 differences   3 config error
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from typing import Dict, List, Tuple

from argia.core.sheets import SheetsClient
from argia.kpi.reader import DayBundle, InverterRow, filter_to_date, parse_rows
from argia.telemetry import pg_source

LOG = logging.getLogger("argia.telemetry.parity")


def key_of(r: InverterRow) -> Tuple[str, str, str]:
    return (r.timestamp_utc.isoformat(), r.plant_key, r.inverter_sn)


# PG columns have fixed scales (temperature 2 dp, etoday 3 dp, irradiance
# 4 dp) while the sheet keeps the vendor's float32 noise (37.100002). Half
# a unit of the coarsest scale is storage rounding, not a data difference.
TOL = 0.0051


def _close(a, b, tol=TOL) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False
    return a == b


def compare(sheet_rows: List[InverterRow], pg_rows: List[InverterRow]) -> Dict:
    """Pure. Returns a report dict; ``ok`` is the verdict."""
    s = {key_of(r): r for r in sheet_rows}
    p = {key_of(r): r for r in pg_rows}
    only_sheet = sorted(set(s) - set(p))
    only_pg = sorted(set(p) - set(s))
    field_diffs = []
    for k in sorted(set(s) & set(p)):
        a, b = s[k], p[k]
        for f in dataclasses.fields(InverterRow):
            va, vb = getattr(a, f.name), getattr(b, f.name)
            if not _close(va, vb):
                field_diffs.append((k, f.name, va, vb))
    per_plant = {}
    for name, rows in (("sheet", sheet_rows), ("pg", pg_rows)):
        for r in rows:
            per_plant.setdefault(r.plant_key, {"sheet": 0, "pg": 0})[name] += 1
    max_abs = 0.0
    for k in set(s) & set(p):
        for f in dataclasses.fields(InverterRow):
            va, vb = getattr(s[k], f.name), getattr(p[k], f.name)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                max_abs = max(max_abs, abs(float(va) - float(vb)))
    return {
        "max_abs_numeric_diff": max_abs,
        "n_sheet": len(sheet_rows), "n_pg": len(pg_rows),
        "only_sheet": only_sheet, "only_pg": only_pg,
        "field_diffs": field_diffs, "per_plant": per_plant,
        "ok": not only_sheet and not only_pg and not field_diffs,
    }


def render(rep: Dict, date_iso: str) -> str:
    L = [f"telemetry parity {date_iso}: sheet={rep['n_sheet']} pg={rep['n_pg']}"]
    L.append("  %-6s %7s %7s" % ("plant", "sheet", "pg"))
    for k in sorted(rep["per_plant"]):
        c = rep["per_plant"][k]
        flag = "" if c["sheet"] == c["pg"] else "   <-- differs"
        L.append("  %-6s %7d %7d%s" % (k, c["sheet"], c["pg"], flag))
    L.append(f"  only in sheet : {len(rep['only_sheet'])}")
    for k in rep["only_sheet"][:5]:
        L.append("     " + " ".join(k))
    L.append(f"  only in pg    : {len(rep['only_pg'])}")
    for k in rep["only_pg"][:5]:
        L.append("     " + " ".join(k))
    L.append(f"  max |numeric diff| on shared rows: {rep['max_abs_numeric_diff']:.6f}")
    L.append(f"  field diffs   : {len(rep['field_diffs'])}  (tolerance {TOL})")
    for k, f, a, b in rep["field_diffs"][:10]:
        L.append(f"     {k[1]} {k[2]} {k[0]} {f}: sheet={a!r} pg={b!r}")
    L.append("  VERDICT: " + ("IDENTICAL" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", required=True)
    ap.add_argument("--log-level", default="WARNING")
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper())
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    sheet_grid = SheetsClient(sid).read_range("Telemetry_Argia", "A1:P")
    pg_grid = pg_source.read_grid(date_iso=a.date)
    sheet_rows = filter_to_date(parse_rows(sheet_grid), a.date)
    pg_rows = filter_to_date(parse_rows(pg_grid), a.date)
    rep = compare(sheet_rows, pg_rows)
    print(render(rep, a.date))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
