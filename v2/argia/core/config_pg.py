"""Plants / Inverters configuration from PostgreSQL (v198, phase 5a).

The two config tabs are the last live READ of the ARGIA_MONT_V2
workbook: ``load_portfolio`` (every job), ``dashboard_update`` and
``dashboard_html_publish``. pio06 already has ``plant`` (16 columns:
identity, kWp, coordinates, portfolio, tariff, pr_baseline, contracted
kWh, active, O&M, investment, SLA — the /setup/ editors write THESE and
audit every change) and ``inverter`` (8 columns) — but the tabs carry
42 and 12 columns respectively. This module:

* extends both tables with the tabs' remaining columns — TEXT unless the
  value is a date or a flag, because the sheet holds mixed content
  ('590+650', 'n/a') that every reader parses through safe_float /
  normalize_text anyway; typed columns come with the admin drawer once
  the values are cleaned;
* serves both tables as the tabs' grids (the live 42/12-column headers,
  pinned), flags as 'TRUE'/'FALSE', dates as ISO text, numbers typed,
  so ``load_portfolio`` and the dashboard parsers run unchanged;
* one door per tab behind  ARGIA_CONFIG_SOURCE = sheet | pg  (default sheet).

Authority: the 16 existing plant columns are PostgreSQL's (audited
edits since 2026-09-01: pr_baseline, kwp_fix, om); the sheet only ever
FILLS the new columns (COALESCE(stored, sheet)) — see
scripts/config_backfill_pg.py, whose parity is authority-aware.
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List

SOURCE_ENV = "ARGIA_CONFIG_SOURCE"

PLANTS_HEADER: List[str] = [
    "plant_key", "customer", "brand", "site_id", "kwp_dc", "kwp_ac", "lat",
    "lon", "expected_factor", "pr_target", "installation_date",
    "secret_api_name", "secret_user_name", "secret_pass_name",
    "weather_plant_id", "datalogger_sn", "datalogger_addr", "active",
    "module_count", "module_wp", "string_count", "tilt_deg", "azimuth_deg",
    "notes", "tariff_mxn_per_kwh", "kwp_dc_override", "kwp_dc_check",
    "pr_stc_model", "gamma_pmax", "monitoring_class", "p90_annual_kwh",
    "contracted_kwh", "date_interconnection", "billing_scheme",
    "module_model", "pr_baseline", "om_cost_monthly_mxn", "portfolio",
    "show_dashboard", "show_daily_report", "show_financial", "client_channel",
]
INVERTERS_HEADER: List[str] = [
    "plant_key", "inverter_sn", "inverter_label", "rated_kw", "active",
    "mppt_count", "strings_total", "rated_kw_dc", "phase", "date_producing",
    "date_decommissioned", "in_service_today",
]

# columns the tables already had (PostgreSQL-authoritative, never filled
# from the sheet)
PLANT_EXISTING = {"plant_key", "customer", "brand", "site_id", "kwp_dc",
                  "kwp_ac", "lat", "lon", "portfolio", "tariff_mxn_per_kwh",
                  "pr_baseline", "contracted_kwh", "active",
                  "om_cost_monthly_mxn"}
INVERTER_EXISTING = {"plant_key", "inverter_sn", "inverter_label",
                     "rated_kw", "phase", "date_producing",
                     "date_decommissioned", "active"}

BOOL_COLS = {"active", "show_dashboard", "show_daily_report",
             "show_financial", "in_service_today"}
DATE_COLS = {"installation_date", "date_interconnection", "date_producing",
             "date_decommissioned"}
# served as numbers (typed columns in PG)
NUMERIC_COLS = {"kwp_dc", "kwp_ac", "lat", "lon", "tariff_mxn_per_kwh",
                "pr_baseline", "contracted_kwh", "om_cost_monthly_mxn",
                "rated_kw"}


def _ddl(table: str, header: List[str], existing: set) -> str:
    out = []
    for c in header:
        if c in existing:
            continue
        t = ("boolean" if c in BOOL_COLS else "date" if c in DATE_COLS
             else "text")
        out.append(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {c} {t};")
    return "\n".join(out) + "\n"


ENSURE_SQL = (_ddl("plant", PLANTS_HEADER, PLANT_EXISTING)
              + _ddl("inverter", INVERTERS_HEADER, INVERTER_EXISTING))


def source(env=None) -> str:
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "pg")).strip().lower()
    return "pg" if v == "pg" else "sheet"


# ---------------------------------------------------------------- pure

def _select(table: str, header: List[str], order: str) -> str:
    parts = []
    for c in header:
        if c in BOOL_COLS:
            parts.append(f"CASE WHEN {c} THEN 'TRUE' WHEN {c} IS NULL THEN ''"
                         f" ELSE 'FALSE' END AS {c}")
        elif c in DATE_COLS:
            parts.append(f"{c}::text AS {c}")
        else:
            parts.append(c)
    return f"SELECT {', '.join(parts)} FROM {table} ORDER BY {order};"


PLANTS_SELECT = _select("plant", PLANTS_HEADER, "plant_key")
INVERTERS_SELECT = _select("inverter", INVERTERS_HEADER,
                           "plant_key, inverter_sn")


def _cell(name: str, raw) -> Any:
    s = ("" if raw is None else str(raw)).strip()
    if s == "" or name not in NUMERIC_COLS:
        return s
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in s else f


def csv_to_records(text: str, header: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("plant_key") or "").strip():
            continue
        out.append({c: _cell(c, rec.get(c, "")) for c in header})
    return out


def _lit(c: str, v: Any) -> str:
    s = "" if v is None else str(v).strip()
    if c in BOOL_COLS:
        if s == "":
            return "NULL"
        return "TRUE" if s.lower() in ("true", "1", "yes", "y", "x") else "FALSE"
    if c in DATE_COLS:
        from argia.core.cells import coerce_date
        d = coerce_date(v) if s != "" else None
        return f"DATE '{d.isoformat()}'" if d else "NULL"
    if s == "":
        return "NULL"
    if c in NUMERIC_COLS:
        try:
            return repr(float(s.replace(",", "")))
        except ValueError:
            return "NULL"
    return "'" + s.replace("'", "''") + "'"


def build_fill_sql(table: str, header: List[str], existing: set,
                   key_cols: List[str], rows: List[Dict[str, Any]]) -> str:
    """Sheet rows -> INSERT ... ON CONFLICT DO UPDATE that FILLS the
    new columns only (COALESCE(stored, sheet)); an existing column is
    never touched on an existing row. A row unknown to PG is inserted
    whole. Pure."""
    if not rows:
        return ""
    new_cols = [c for c in header if c not in existing]
    tuples = []
    for r in rows:
        tuples.append("(" + ", ".join(_lit(c, r.get(c)) for c in header) + ")")
    sets = ", ".join(f"{c} = COALESCE({table}.{c}, EXCLUDED.{c})" for c in new_cols)
    return (f"INSERT INTO {table} ({', '.join(header)}) VALUES\n"
            + ",\n".join(tuples)
            + f"\nON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {sets};")


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


def read_plants_records() -> List[Dict[str, Any]]:
    return csv_to_records(_fetch_csv(PLANTS_SELECT), PLANTS_HEADER)


def read_inverters_records() -> List[Dict[str, Any]]:
    return csv_to_records(_fetch_csv(INVERTERS_SELECT), INVERTERS_HEADER)


# ---------------------------------------------------------------- doors

def plants_records(sheets, a1: str = "A1:ZZ") -> List[Dict[str, Any]]:
    """What ``sheets.read_table("Plants", a1)`` returned. In pg mode a
    read failure raises: a job must not run on an empty portfolio."""
    if source() == "pg":
        return read_plants_records()
    return sheets.read_table("Plants", a1)


def inverters_records(sheets, a1: str = "A1:Z") -> List[Dict[str, Any]]:
    if source() == "pg":
        return read_inverters_records()
    return sheets.read_table("Inverters", a1)
