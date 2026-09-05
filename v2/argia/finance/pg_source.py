"""Finance inputs from PostgreSQL, in the sheets' own shape (v191, phase 3a).

The five finance tabs of ARGIA_MONT_V2 already have PostgreSQL twins
on pio06 (the /setup/finance editor writes THERE since 2026-09-01):

    Contract_Monthly   -> contract_monthly   (same 8 columns)
    Loans              -> loan               (same 9 columns)
    Loan_Schedule      -> loan_schedule      (+ plant_key / total_installments
                                              joined from loan; PG also holds
                                              the /finance/extend rows the sheet
                                              never received)
    Design_Monthly     -> contract_monthly.design_kwh (Contract_Monthly has
                                              been the primary design source
                                              since v61; the tab is a fallback)
    Maintenance_Events -> maintenance_event  (argia.maintenance.events already
                                              has the PG loader)

This module serves those tables as the GRID (header + rows) or the
``read_table`` dicts the sheet readers parse, with numbers typed and
blanks as '' — exactly what ``SheetsClient.read_range(UNFORMATTED)``
returned — so ``contract.load_contract_monthly``, ``design
.load_design_monthly`` and ``loans.load_loans / load_loan_schedule``
parse it unchanged.

Selection:  ARGIA_FINANCE_SOURCE = sheet | pg     (v191 default: sheet)
Gate:       scripts/finance_parity.py must be clean before flipping.
Off-server (no psql) the pg mode raises on read, like every other
PG source: a misconfigured Pi/CI run fails loudly, never silently
returns an empty finance picture.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List

SOURCE_ENV = "ARGIA_FINANCE_SOURCE"

CONTRACT_HEADER = ["plant_key", "year", "month", "design_kwh",
                   "contract_kwh", "tariff_mxn", "fixed_income_ccy", "ccy"]
DESIGN_HEADER = ["plant_key", "year", "month", "design_kwh"]
LOANS_HEADER = ["loan_id", "plant_key", "project_name", "bank", "currency",
                "principal_mxn", "total_installments", "first_month",
                "last_month"]
SCHEDULE_HEADER = ["loan_id", "plant_key", "ref_month", "installment_no",
                   "total_installments", "payment_mxn", "payment_ccy", "xr",
                   "due_after_mxn"]

# Columns that come back typed as numbers (the sheet's UNFORMATTED_VALUE
# behaviour). Everything else stays text; '' for NULL.
NUMERIC = {
    "year", "month", "design_kwh", "contract_kwh", "tariff_mxn",
    "fixed_income_ccy", "principal_mxn", "total_installments",
    "installment_no", "payment_mxn", "payment_ccy", "xr", "due_after_mxn",
}

CONTRACT_SQL = (
    "SELECT plant_key, year, month, design_kwh, contract_kwh, tariff_mxn,"
    " fixed_income_ccy, ccy FROM contract_monthly"
    " ORDER BY plant_key, year, month;")
DESIGN_SQL = (
    "SELECT plant_key, year, month, design_kwh FROM contract_monthly"
    " WHERE design_kwh IS NOT NULL ORDER BY plant_key, year, month;")
LOANS_SQL = (
    "SELECT loan_id, plant_key, project_name, bank, currency, principal_mxn,"
    " total_installments, to_char(first_month, 'YYYY-MM') AS first_month,"
    " to_char(last_month, 'YYYY-MM') AS last_month"
    " FROM loan ORDER BY loan_id;")
SCHEDULE_SQL = (
    "SELECT s.loan_id, l.plant_key, to_char(s.ref_month, 'YYYY-MM') AS"
    " ref_month, s.installment_no, l.total_installments, s.payment_mxn,"
    " s.payment_ccy, s.xr, s.due_after_mxn"
    " FROM loan_schedule s JOIN loan l ON l.loan_id = s.loan_id"
    " ORDER BY s.loan_id, s.ref_month;")


def source(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "pg")).strip().lower()
    return "pg" if v == "pg" else "sheet"


def _cell(name: str, raw) -> Any:
    s = ("" if raw is None else str(raw)).strip()
    if s == "":
        return ""
    if name in NUMERIC:
        try:
            f = float(s)
        except ValueError:
            return s
        return int(f) if f.is_integer() and "." not in s else f
    return s


def csv_to_grid(text: str, header: List[str]) -> List[List[Any]]:
    """psql --csv -> [header, row, ...] in the sheet's shape. Pure."""
    grid: List[List[Any]] = [list(header)]
    for rec in csv.DictReader(io.StringIO(text)):
        if not any((rec.get(h) or "").strip() for h in header):
            continue
        grid.append([_cell(h, rec.get(h, "")) for h in header])
    return grid


def grid_to_records(grid) -> List[Dict[str, Any]]:
    """What ``SheetsClient.read_table`` builds from a grid."""
    if not grid:
        return []
    hdr = [str(h) for h in grid[0]]
    return [dict(zip(hdr, list(r) + [""] * (len(hdr) - len(r))))
            for r in grid[1:]]


def _fetch_csv(sql: str) -> str:
    import subprocess
    db = os.environ.get("ARGIA_PG_DB", "argia_mont")
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", db,
                        "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("psql failed: %s" % r.stderr.strip()[:300])
    return r.stdout


# ---------------------------------------------------------------- PG reads

def read_contract_grid() -> List[List[Any]]:
    return csv_to_grid(_fetch_csv(CONTRACT_SQL), CONTRACT_HEADER)


def read_design_grid() -> List[List[Any]]:
    return csv_to_grid(_fetch_csv(DESIGN_SQL), DESIGN_HEADER)


def read_loans_records() -> List[Dict[str, Any]]:
    return grid_to_records(csv_to_grid(_fetch_csv(LOANS_SQL), LOANS_HEADER))


def read_schedule_records() -> List[Dict[str, Any]]:
    return grid_to_records(csv_to_grid(_fetch_csv(SCHEDULE_SQL),
                                       SCHEDULE_HEADER))


# ---------------------------------------------------------------- doors
# One door per tab: same call, the switch decides. The sheet branch is
# byte-for-byte what the readers used to do themselves.

def contract_grid(sheets) -> List[List[Any]]:
    if source() == "pg":
        return read_contract_grid()
    return sheets.read_range("Contract_Monthly", "A1:H")


def design_grid(sheets, candidates=("Contract_Monthly", "Design_Monthly",
                                    "design_monthly")):
    """(grid, source_name). In sheet mode the first existing tab wins,
    as design.py has always done."""
    if source() == "pg":
        return read_design_grid(), "contract_monthly (PG)"
    for tab in candidates:
        try:
            return sheets.read_range(tab, "A1:D"), tab
        except Exception:  # noqa: BLE001 — try next name
            continue
    return None, None


def loans_records(sheets) -> List[Dict[str, Any]]:
    if source() == "pg":
        return read_loans_records()
    return sheets.read_table("Loans")


def schedule_records(sheets) -> List[Dict[str, Any]]:
    if source() == "pg":
        return read_schedule_records()
    return sheets.read_table("Loan_Schedule")
