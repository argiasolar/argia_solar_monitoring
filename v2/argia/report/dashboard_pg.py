"""Dashboard_Plant / Dashboard_Inverter in PostgreSQL (v195, phase 3d).

The two Dashboard tabs are a DERIVED dataset: dashboard_update rebuilds
the rolling 7-day window of hourly buckets every 10 minutes from
telemetry + KPI + config, and rewrites both tabs in place. Readers:
dashboard_html_publish (the live dashboard page), report.daily (live
"expected so far", intraday buckets, conditions) and kpi_eod /
maintenance.deemed (measured energy inside a partial-day maintenance
window). Nothing is entered by hand and nothing is history — the window
is recomputed from its sources every tick.

So the PostgreSQL twin is two tables with the tabs' exact columns
(``dashboard.PLANT_COLUMNS`` / ``INVERTER_COLUMNS``), rewritten
atomically per run (DELETE + INSERT in one transaction: a reader never
sees an empty table), and served back as ``read_table`` dicts — dates as
ISO text, bucket_ts as 'YYYY-MM-DD HH:MM:SS', numbers typed — which the
readers already parse through date_key / safe_float.

    ARGIA_DASHBOARD_SOURCE = sheet | both | pg      (v195 default: sheet)

  sheet  write the tabs, readers read the tabs — today's behaviour
  both   write BOTH from the same matrices (the parity run), readers
         read PostgreSQL
  pg     PostgreSQL only; the tabs are no longer written
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List

from argia.report.dashboard import INVERTER_COLUMNS, PLANT_COLUMNS

SOURCE_ENV = "ARGIA_DASHBOARD_SOURCE"
MODES = ("sheet", "both", "pg")

PLANT_TABLE = "dashboard_plant"
INVERTER_TABLE = "dashboard_inverter"

# column -> SQL type; everything not listed is text
_TYPES = {
    "date_mx": "date", "bucket_ts": "timestamp",
    "kwp_dc": "numeric", "tariff_mxn_per_kwh": "numeric",
    "total_kwh": "numeric", "theoretical_kwh": "numeric",
    "irradiance_kwh_m2": "numeric", "irradiance_wm2": "numeric",
    "cloud_cover_pct": "numeric", "module_temp_c": "numeric",
    "ambient_temp_c": "numeric", "inverters_total": "integer",
    "inverters_reporting": "integer", "inverters_faulted": "integer",
    "production_pct": "numeric",
    "energy_kwh": "numeric", "power_w": "numeric", "temperature_c": "numeric",
    "peer_median_kwh": "numeric", "expected_share_kwh": "numeric",
    "est_loss_kwh": "numeric", "fault_events": "integer",
}
NUMERIC = {c for c, t in _TYPES.items() if t in ("numeric", "integer")}


def _ddl(table: str, columns: List[str]) -> str:
    cols = ",\n    ".join(f"{c} {_TYPES.get(c, 'text')}" for c in columns)
    return (f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols},\n"
            f"    written_at timestamptz NOT NULL DEFAULT now()\n);\n"
            f"CREATE INDEX IF NOT EXISTS {table}_day ON {table} (date_mx, plant_key);\n")


ENSURE_SQL = _ddl(PLANT_TABLE, PLANT_COLUMNS) + _ddl(INVERTER_TABLE, INVERTER_COLUMNS)


def mode(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "sheet")).strip().lower()
    return v if v in MODES else "sheet"


def writes_sheet(env=None) -> bool:
    return mode(env) in ("sheet", "both")


def writes_pg(env=None) -> bool:
    return mode(env) in ("both", "pg")


def reads_pg(env=None) -> bool:
    return mode(env) in ("both", "pg")


# ---------------------------------------------------------------- pure

def _select(table: str, columns: List[str]) -> str:
    parts = []
    for c in columns:
        t = _TYPES.get(c, "text")
        if t == "date":
            parts.append(f"{c}::text AS {c}")
        elif t == "timestamp":
            parts.append(f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS') AS {c}")
        else:
            parts.append(c)
    return (f"SELECT {', '.join(parts)} FROM {table}"
            " ORDER BY date_mx, plant_key, bucket_ts;")


PLANT_SELECT = _select(PLANT_TABLE, PLANT_COLUMNS)
INVERTER_SELECT = _select(INVERTER_TABLE, INVERTER_COLUMNS)


def _cell(name: str, raw) -> Any:
    s = ("" if raw is None else str(raw)).strip()
    if s == "" or name not in NUMERIC:
        return s
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in s else f


def csv_to_records(text: str, columns: List[str]) -> List[Dict[str, Any]]:
    """psql --csv -> read_table-style dicts. Pure."""
    out: List[Dict[str, Any]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("plant_key") or "").strip():
            continue
        out.append({c: _cell(c, rec.get(c, "")) for c in columns})
    return out


def _lit(c: str, v: Any) -> str:
    s = "" if v is None else str(v).strip()
    t = _TYPES.get(c, "text")
    if s == "":
        return "NULL"
    if t in ("numeric", "integer"):
        try:
            f = float(s)
        except ValueError:
            return "NULL"
        return repr(int(f)) if t == "integer" else repr(f)
    return "'" + s.replace("'", "''") + "'"


def build_rewrite_sql(table: str, columns: List[str],
                      matrix: List[List[Any]]) -> str:
    """The tab rewrite as ONE transaction: delete everything, insert the
    new window. ``matrix`` is dashboard_update.to_matrix output (header +
    rows). Pure."""
    hdr = [str(h) for h in (matrix[0] if matrix else [])]
    if hdr != list(columns):
        raise ValueError(f"{table}: matrix header {hdr[:5]}... != {columns[:5]}...")
    rows = matrix[1:]
    parts = ["BEGIN;", f"DELETE FROM {table};"]
    if rows:
        tuples = ["(" + ", ".join(_lit(c, r[i] if i < len(r) else None)
                                  for i, c in enumerate(columns)) + ")"
                  for r in rows]
        parts.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n"
                     + ",\n".join(tuples) + ";")
    parts.append("COMMIT;")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- I/O

def _fetch_csv(sql: str) -> str:
    import subprocess
    db = os.environ.get("ARGIA_PG_DB", "argia_mont")
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", db,
                        "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("psql failed: %s" % r.stderr.strip()[:300])
    return r.stdout


def ensure() -> None:
    from argia.store.pgq import psql_exec
    psql_exec(ENSURE_SQL)


def read_plant_records() -> List[Dict[str, Any]]:
    return csv_to_records(_fetch_csv(PLANT_SELECT), PLANT_COLUMNS)


def read_inverter_records() -> List[Dict[str, Any]]:
    return csv_to_records(_fetch_csv(INVERTER_SELECT), INVERTER_COLUMNS)


def rewrite(table: str, columns: List[str], matrix: List[List[Any]]) -> int:
    from argia.store.pgq import psql_exec
    ensure()
    psql_exec(build_rewrite_sql(table, columns, matrix))
    return max(len(matrix) - 1, 0)


# ---------------------------------------------------------------- doors

def plant_records(sheets) -> List[Dict[str, Any]]:
    """What ``sheets.read_table("Dashboard_Plant", "A1:ZZ")`` returned."""
    if reads_pg():
        return read_plant_records()
    return sheets.read_table("Dashboard_Plant", "A1:ZZ")


def inverter_records(sheets) -> List[Dict[str, Any]]:
    if reads_pg():
        return read_inverter_records()
    return sheets.read_table("Dashboard_Inverter", "A1:ZZ")
