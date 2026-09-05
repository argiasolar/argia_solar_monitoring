"""KPI_Daily from PostgreSQL, in the sheet's own shape (v190, phase 2a).

Every reader of the ``KPI_Daily`` tab parses a grid (header + rows) or
``read_table`` dicts and normalises dates through ``date_key`` /
``coerce_date``, which accept both sheet serials and ISO text. This
module produces that grid from ``daily_production`` with the SHEET's
column names and order, ISO dates, numbers typed, blanks as '' — so
alerts_daily, finance.income, finance.annex, report.daily,
dashboard_update and archive.load_kpi_daily run unchanged.

Selection:  ARGIA_KPI_SOURCE = sheet | pg     (v190 default: sheet)
Gate:       scripts/kpi_parity.py must be IDENTICAL before flipping.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List, Optional

from argia.store.kpi_mirror import COLMAP, INTEGER, NUMERIC

SOURCE_ENV = "ARGIA_KPI_SOURCE"

# The live tab's header, in its order (the 14 kpi_eod-owned columns then
# the analytics columns stamped to the right). Kept explicit so a PG
# grid is column-compatible with every "A1:ZZ" reader.
HEADER: List[str] = [
    "date_iso", "plant_key",
    "energy_kwh", "irradiance_kwh_m2", "irradiance_source",
    "pr", "pr_confidence", "capacity_factor", "capacity_factor_confidence",
    "inverters_reporting", "inverters_with_reboot",
    "notes", "written_at_utc", "pr_stc",
    "specific_yield", "availability", "soiling_loss_pct", "data_class",
    "cloud_coverage_pct", "expected_kwh", "production_pct", "status_note",
    "design_kwh", "billable_kwh",
]
# sheet name -> PG column
_PG_OF = dict(COLMAP)
_PG_OF.update({"date_iso": "prod_date", "plant_key": "plant_key"})


def source(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "pg")).strip().lower()
    return "pg" if v == "pg" else "sheet"


def select_sql(min_date: Optional[str] = None) -> str:
    cols = ", ".join(f"{_PG_OF[h]} AS {h}" for h in HEADER)
    where = f" WHERE prod_date >= DATE '{min_date}'" if min_date else ""
    return (f"SELECT {cols} FROM daily_production{where}"
            " ORDER BY prod_date, plant_key;")


def _cell(name: str, raw: str):
    s = (raw or "").strip()
    if s == "":
        return ""
    pg = _PG_OF[name]
    if pg in INTEGER:
        try:
            return int(float(s))
        except ValueError:
            return s
    if pg in NUMERIC:
        try:
            f = float(s)
        except ValueError:
            return s
        return int(f) if f.is_integer() and "." not in s else f
    return s


def csv_to_grid(text: str) -> List[List[Any]]:
    """psql --csv -> [HEADER, row, ...]. Pure."""
    grid: List[List[Any]] = [list(HEADER)]
    for rec in csv.DictReader(io.StringIO(text)):
        if not rec.get("date_iso") or not rec.get("plant_key"):
            continue
        grid.append([_cell(h, rec.get(h, "")) for h in HEADER])
    return grid


def grid_to_records(grid) -> List[Dict[str, Any]]:
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


def read_grid(min_date: Optional[str] = None) -> List[List[Any]]:
    return csv_to_grid(_fetch_csv(select_sql(min_date)))


def read_records(min_date: Optional[str] = None) -> List[Dict[str, Any]]:
    return grid_to_records(read_grid(min_date))


# ---------------------------------------------------------------- readers
# One door for every reader: same call, the switch decides.

def kpi_grid(sheets, a1: str = "A1:ZZ") -> List[List[Any]]:
    """What ``sheets.read_range(KPI_DAILY_TAB, a1)`` used to return."""
    if source() == "pg":
        return read_grid()
    return sheets.read_range("KPI_Daily", a1)


def kpi_records(sheets, a1: str = "A1:ZZ") -> List[Dict[str, Any]]:
    """What ``sheets.read_table(KPI_DAILY_TAB, a1)`` used to return."""
    if source() == "pg":
        return read_records()
    return sheets.read_table("KPI_Daily", a1)
