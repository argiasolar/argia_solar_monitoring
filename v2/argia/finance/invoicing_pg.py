"""Invoiced history from PostgreSQL, in Invoicing_Overview's shape
(v192, Sheets retirement phase 3b).

The ARGIA Solar workbook's ``Invoicing_Overview`` tab is the last thing
the server reads from that workbook: what was ACTUALLY invoiced per
plant-month (Year | Month | Month_No | Plant_Key | Total_kWh |
Penalty_kWh | Total_Income | Expected_kWh). The annex renders invoiced
months from it (they win over KPI atoms) and invoice_publish records the
``invoicing`` register from it.

The register already exists on pio06 (invoice_publish creates it) but
stored only the SUM (billable_kwh = produced + penalty) and the amount.
This module adds the split the annex needs — produced_kwh, penalty_kwh,
expected_kwh — plus a ``source`` tag, and serves the register as the
sheet's grid so ``annex.parse_invoicing_overview`` runs unchanged.

Selection:  ARGIA_INVOICING_SOURCE = sheet | pg     (v192 default: sheet)
Backfill:   scripts/invoicing_backfill_pg.py --apply (fills NULLs only,
            never changes a stored value), then its parity must be CLEAN.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List, Optional

SOURCE_ENV = "ARGIA_INVOICING_SOURCE"

HEADER = ["Year", "Month", "Month_No", "Plant_Key", "Total_kWh",
          "Penalty_kWh", "Total_Income", "Expected_kWh"]

ENSURE_SQL = (
    "CREATE TABLE IF NOT EXISTS invoicing ("
    " plant_key text NOT NULL, ref_month date NOT NULL,"
    " billable_kwh numeric(14,3), tariff_mxn numeric(10,4),"
    " amount_mxn numeric(14,2), billing_kwh numeric(14,3),"
    " delta_kwh numeric(14,3), delta_pct numeric(9,4),"
    " check_status text NOT NULL,"
    " published_at timestamptz NOT NULL DEFAULT now(),"
    " PRIMARY KEY (plant_key, ref_month));\n"
    "ALTER TABLE invoicing ADD COLUMN IF NOT EXISTS produced_kwh numeric(14,3);\n"
    "ALTER TABLE invoicing ADD COLUMN IF NOT EXISTS penalty_kwh numeric(14,3);\n"
    "ALTER TABLE invoicing ADD COLUMN IF NOT EXISTS expected_kwh numeric(14,3);\n"
    "ALTER TABLE invoicing ADD COLUMN IF NOT EXISTS source text;\n"
)

SELECT_SQL = (
    "SELECT extract(year FROM ref_month)::int AS \"Year\","
    " to_char(ref_month, 'FMMonth') AS \"Month\","
    " extract(month FROM ref_month)::int AS \"Month_No\","
    " plant_key AS \"Plant_Key\","
    " produced_kwh AS \"Total_kWh\", penalty_kwh AS \"Penalty_kWh\","
    " amount_mxn AS \"Total_Income\", expected_kwh AS \"Expected_kWh\""
    " FROM invoicing WHERE produced_kwh IS NOT NULL OR expected_kwh IS NOT NULL"
    " ORDER BY ref_month, plant_key;")


def source(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "sheet")).strip().lower()
    return "pg" if v == "pg" else "sheet"


def _cell(name: str, raw) -> Any:
    s = ("" if raw is None else str(raw)).strip()
    if s == "" or name in ("Month", "Plant_Key"):
        return s
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in s else f


def csv_to_grid(text: str) -> List[List[Any]]:
    """psql --csv -> [HEADER, row, ...] in the sheet's shape. Pure."""
    grid: List[List[Any]] = [list(HEADER)]
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("Plant_Key") or "").strip():
            continue
        grid.append([_cell(h, rec.get(h, "")) for h in HEADER])
    return grid


def _fetch_csv(sql: str) -> str:
    import subprocess
    db = os.environ.get("ARGIA_PG_DB", "argia_mont")
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", db,
                        "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("psql failed: %s" % r.stderr.strip()[:300])
    return r.stdout


def read_grid() -> List[List[Any]]:
    """Every invoiced plant-month the register holds, as the sheet grid."""
    return csv_to_grid(_fetch_csv(SELECT_SQL))


# ---------------------------------------------------------------- backfill

def _num(v) -> Optional[float]:
    from argia.core.normalize import safe_float
    return safe_float(v)


def sheet_rows(grid) -> List[Dict[str, Any]]:
    """Typed rows from the sheet grid: {plant_key, ref_month, produced,
    penalty, income, expected}. A row needs a year/month/plant and at
    least a produced OR an expected figure: the sheet carries
    Expected_kWh for months not yet invoiced, and the annex uses that
    expectation (rollup_month), so those rows must travel too. Pure."""
    out: List[Dict[str, Any]] = []
    for r in (grid[1:] if grid else []):
        if len(r) < 5:
            continue
        try:
            y = int(float(r[0]))
            m = int(float(r[2]))
        except (TypeError, ValueError):
            continue
        pk = str(r[3] or "").strip().upper()
        produced = _num(r[4])
        expected = _num(r[7]) if len(r) > 7 else None
        if not pk or not (1 <= m <= 12):
            continue
        if produced is None and expected is None:
            continue
        out.append({
            "plant_key": pk, "ref_month": "%04d-%02d-01" % (y, m),
            "produced": produced,
            "penalty": _num(r[5]) if len(r) > 5 else None,
            "income": _num(r[6]) if len(r) > 6 else None,
            "expected": expected,
        })
    return out


def _lit(v) -> str:
    return "NULL" if v is None else repr(float(v))


def build_backfill_sql(rows: List[Dict[str, Any]]) -> str:
    """One statement per row. A missing register row is inserted with
    check_status 'SHEET_IMPORT' (or 'EXPECTED_ONLY' when the month is
    not invoiced yet and only carries an expectation); an existing row
    only gets its NULL columns filled (COALESCE(stored, sheet)) — a
    stored value is never changed, never lowered. billable_kwh =
    produced + penalty. Pure."""
    stmts: List[str] = []
    for r in rows:
        if r["produced"] is None:
            penalty = billable = None
            status = "EXPECTED_ONLY"
        else:
            penalty = r["penalty"] or 0.0
            billable = r["produced"] + penalty
            status = "SHEET_IMPORT"
        stmts.append(
            "INSERT INTO invoicing (plant_key, ref_month, billable_kwh,"
            " amount_mxn, produced_kwh, penalty_kwh, expected_kwh,"
            " check_status, source) VALUES ("
            f"'{r['plant_key']}', DATE '{r['ref_month']}', {_lit(billable)},"
            f" {_lit(r['income'])}, {_lit(r['produced'])}, {_lit(penalty)},"
            f" {_lit(r['expected'])}, '{status}', 'Invoicing_Overview')"
            " ON CONFLICT (plant_key, ref_month) DO UPDATE SET"
            " billable_kwh = COALESCE(invoicing.billable_kwh, EXCLUDED.billable_kwh),"
            " amount_mxn = COALESCE(invoicing.amount_mxn, EXCLUDED.amount_mxn),"
            " produced_kwh = COALESCE(invoicing.produced_kwh, EXCLUDED.produced_kwh),"
            " penalty_kwh = COALESCE(invoicing.penalty_kwh, EXCLUDED.penalty_kwh),"
            " expected_kwh = COALESCE(invoicing.expected_kwh, EXCLUDED.expected_kwh),"
            " source = COALESCE(invoicing.source, EXCLUDED.source);")
    return "\n".join(stmts) + ("\n" if stmts else "")


def compare_history(sheet_hist: Dict, pg_hist: Dict, tol: float = 0.0051
                    ) -> Dict:
    """Both sides are ``annex.parse_invoicing_overview`` output for one
    year: {PLANT: {ym: {kwh, penalty, income, expected}}}. Pure."""
    ks = {(p, ym) for p, ms in sheet_hist.items() for ym in ms}
    kp = {(p, ym) for p, ms in pg_hist.items() for ym in ms}
    diffs = []
    for p, ym in sorted(ks & kp):
        a, b = sheet_hist[p][ym], pg_hist[p][ym]
        for f in ("kwh", "penalty", "income", "expected"):
            x, y = a.get(f), b.get(f)
            if x is None and y is None:
                continue
            if x is None or y is None or abs(float(x) - float(y)) > tol:
                diffs.append(((p, ym), f, x, y))
    return {"only_sheet": sorted(ks - kp), "only_pg": sorted(kp - ks),
            "diffs": diffs, "n_sheet": len(ks), "n_pg": len(kp),
            "ok": not (ks - kp) and not diffs}
