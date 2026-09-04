#!/usr/bin/env python3
"""The phase-2a gate: is daily_production the same KPI_Daily the sheet has?

Reads both, keys rows by (date, plant), compares every one of the 24
columns with a storage-scale tolerance for numbers, and reports what is
only on one side. Nothing flips to ARGIA_KPI_SOURCE=pg until this is
IDENTICAL for the whole window kpi_pg_mirror maintains.

    PYTHONPATH=. python scripts/kpi_parity.py [--days 400]
EXIT: 0 identical   1 differences   3 config
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any, Dict, List, Tuple

from argia.core.cells import coerce_date
from argia.core.sheets import SheetsClient
from argia.kpi import pg_kpi_source as K

TOL = 0.0051
IGNORE = {"written_at_utc"}       # a timestamp of the write, not a KPI


def norm(name: str, v: Any):
    if name == "date_iso":
        d = coerce_date(v)
        return d.isoformat() if d else str(v)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    s = str(v).strip()
    try:
        return round(float(s), 6)
    except ValueError:
        return s


def index(grid) -> Dict[Tuple[str, str], Dict[str, Any]]:
    hdr = [str(h).strip() for h in grid[0]]
    out = {}
    for r in grid[1:]:
        rec = dict(zip(hdr, list(r) + [""] * (len(hdr) - len(r))))
        d = norm("date_iso", rec.get("date_iso"))
        pk = str(rec.get("plant_key") or "").strip().upper()
        if d and pk:
            out[(d, pk)] = rec
    return out


# Where PostgreSQL is the DESIGNED authority and the sheet is expected to
# lag (argia/store/kpi_mirror.py): on a vendor-corrected row the protected
# columns carry the billing-basis energy, the PR re-derived from it and
# the provenance note; on a CLOSED month nothing may be re-imported at
# all. A difference there is the design working, not a parity failure.
PG_WINS_COLS = {"energy_kwh", "billable_kwh", "pr", "pr_stc", "status_note"}


def classify(k, col, frozen, vendor) -> str:
    """'expected' when PG is the designed authority for this cell, else
    'unexpected'. Pure."""
    if col in PG_WINS_COLS and (vendor or frozen):
        return "expected"
    return "unexpected"


def compare(sheet_grid, pg_grid, min_date: str,
            frozen_months=frozenset(), vendor_rows=frozenset()) -> Dict:
    """``frozen_months``: {(plant, 'YYYY-MM')} with a closed reconciliation.
    ``vendor_rows``: {(date, plant)} whose PG status_note carries vendor
    provenance. ``ok`` means: rows present on the sheet are all in PG, and
    every remaining cell difference is one where PG is designed to win."""
    s = {k: v for k, v in index(sheet_grid).items() if k[0] >= min_date}
    p = {k: v for k, v in index(pg_grid).items() if k[0] >= min_date}
    only_s, only_p = sorted(set(s) - set(p)), sorted(set(p) - set(s))
    diffs: List[Tuple] = []
    for k in sorted(set(s) & set(p)):
        frozen = (k[1], k[0][:7]) in frozen_months
        vendor = k in vendor_rows
        for col in K.HEADER:
            if col in ("date_iso", "plant_key") or col in IGNORE:
                continue
            a, b = norm(col, s[k].get(col)), norm(col, p[k].get(col))
            if a == b:
                continue
            if isinstance(a, float) and isinstance(b, float) and abs(a - b) <= TOL:
                continue
            diffs.append((k, col, a, b, classify(k, col, frozen, vendor)))
    unexpected = [d for d in diffs if d[4] == "unexpected"]
    return {"n_sheet": len(s), "n_pg": len(p), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs, "unexpected": unexpected,
            "expected": [d for d in diffs if d[4] == "expected"],
            # PG may hold MORE history than the pruned sheet — that is fine
            "ok": not only_s and not unexpected}


def render(rep: Dict, min_date: str) -> str:
    L = [f"KPI parity since {min_date}: sheet rows={rep['n_sheet']} pg rows={rep['n_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:4]}   (must be 0)",
         f"  only in pg   : {len(rep['only_pg'])}   (PG keeps more history than the pruned sheet — fine)",
         f"  expected diffs (PG is the authority: vendor-corrected rows / closed months): {len(rep['expected'])}"]
    by_col: Dict[str, int] = {}
    for _, col, _, _, _ in rep["expected"]:
        by_col[col] = by_col.get(col, 0) + 1
    for col, n in sorted(by_col.items(), key=lambda x: -x[1]):
        L.append(f"     {col:<28} {n}")
    L.append(f"  UNEXPECTED diffs: {len(rep['unexpected'])}")
    by_col = {}
    for _, col, _, _, _ in rep["unexpected"]:
        by_col[col] = by_col.get(col, 0) + 1
    for col, n in sorted(by_col.items(), key=lambda x: -x[1]):
        L.append(f"     {col:<28} {n}")
    for k, col, a, b, _ in rep["unexpected"][:8]:
        L.append(f"     {k[0]} {k[1]} {col}: sheet={a!r} pg={b!r}")
    L.append("  VERDICT: " + ("IDENTICAL (up to PG-authoritative corrections)"
                              if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


def load_pg_context():
    """(frozen_months, vendor_rows) from PostgreSQL. Server-only."""
    from argia.store.pgq import psql_rows
    frozen = {(r[0], r[1][:7]) for r in psql_rows(
        "SELECT plant_key, ref_month::text FROM reconciliation_monthly "
        "WHERE closed_at IS NOT NULL;")}
    vendor = {(r[1], r[0]) for r in psql_rows(
        "SELECT plant_key, prod_date::text FROM daily_production "
        "WHERE status_note LIKE '%vendor daily counter%';")}
    return frozenset(frozen), frozenset(vendor)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=400)
    a = ap.parse_args(argv)
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    min_date = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    sheet_grid = SheetsClient(sid).read_range("KPI_Daily", "A1:ZZ")
    pg_grid = K.read_grid(min_date)
    frozen, vendor = load_pg_context()
    rep = compare(sheet_grid, pg_grid, min_date, frozen, vendor)
    print(render(rep, min_date))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
