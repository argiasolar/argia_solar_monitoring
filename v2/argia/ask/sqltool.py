"""Ask ARGIA — read-only SQL for the assistant (v215, "access to all
data"). The fixed tools stay the first choice (validated inputs, the
numbers the reports use); this is the escape hatch for questions no
tool covers, with guards that make it read-only whatever the model
writes:

* one statement, SELECT or WITH only — no semicolons, no comments, no
  DML/DDL/utility keywords, no ``pg_`` catalog or admin functions;
* only allow-listed tables (the business tables — never ask_log);
* wrapped in a READ ONLY transaction with a statement timeout, rows
  capped, columns returned by name (row_to_json);
* internal users only — customer-scoped accounts never reach it.

Pure: ``guard`` and ``wrap`` build strings; the execution goes through
``rows()`` like every other tool.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set

ALLOWED_TABLES: Set[str] = {
    "plant", "inverter", "telemetry", "telemetry_detail", "daily_production",
    "alert_state", "maintenance_event", "reconciliation_daily", "reconciliation_monthly",
    "vendor_counter_snapshot", "contract_monthly", "loan", "loan_schedule",
    "invoicing", "cfe_tariff", "cfe_pipeline_status", "sync_run", "knowledge",
    "finance_audit", "string_daily", "satellite_check", "thermal_daily", "thermal_bins",
}
# never: users/sessions (auth), ask_log (other people's questions), usage_daily, _tele_stage
# what the model may read about each table, in one line each
TABLE_NOTES: Dict[str, str] = {
    "plant": "one row per plant: plant_key, customer, brand, kwp_dc/kwp_ac, portfolio (PPA/CAPEX), tariff_mxn_per_kwh, lat/lon, active",
    "inverter": "configured inverters: plant_key, inverter_sn, label, rated_kw, active",
    "telemetry": "5-minute samples: ts_utc, plant_key, inverter_sn, power_w, etoday_kwh (the inverter's own day counter), temperature_c, irradiance_wm2 — LARGE, always filter by plant and a short date range",
    "telemetry_detail": "per-sample string/MPPT detail (wide) — LARGE",
    "daily_production": "the KPI day: plant_key, prod_date, energy_kwh (reference), expected_kwh, billable_kwh, pr, availability, irradiance, status_note",
    "reconciliation_daily": "nightly check per plant-day: interval_kwh vs vendor_daily_kwh vs kpi_kwh, completeness_pct, variance_pct, status PASS/REVIEW/FAIL/NO_DATA, note, reference_kwh, reference_basis",
    "reconciliation_monthly": "monthly close per plant: ref_month, billing_kwh, basis, status, closed_at, closed_by, note",
    "vendor_counter_snapshot": "the vendor platform's own counters captured nightly: snap_date, daily_kwh, monthly_kwh, lifetime_kwh, note",
    "alert_state": "alert engine state: key (rule+plant+inverter), severity, since, last_seen, message",
    "maintenance_event": "logged maintenance / customer events: plant_key, start, end, category, approved, note",
    "contract_monthly": "PPA contract per plant-month: year, month, contract_kwh, design_kwh, tariff_mxn, fixed_income_ccy",
    "loan": "bank loans per asset; loan_schedule = monthly installments (payment_mxn / payment_ccy, fx)",
    "invoicing": "invoice register: month, plant, kWh billed, MXN, status, annex file",
    "cfe_tariff": "CFE tariffs: tariff_code (GDMTH...), region, month, charge_type, unit, value_mxn, source (cfe_scrape = CFE-verified)",
    "knowledge": "ARGIA Golden Standard slides (doc='AGS', lang en/es/cz, n, title, body) — prefer search_standard",
    "sync_run": "every job run: script, status, started_at, error",
    "thermal_daily": "nightly inverter thermal health per inverter-day: peak_c, minutes_over_65/70, events, dt_peer_peak_c (vs plant peers), dt_ambient_peak_c, derating_minutes, lost_kwh (suspected thermal derating vs cooler peers), cooling_health GOOD/WATCH/POOR — prefer get_thermal_health",
    "thermal_bins": "temperature-binned actual/expected ratios behind the derating curve (bin_c, n, ratio_sum)",
}
MAX_ROWS = 200
TIMEOUT_MS = 10000

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|analyze|"
    r"reindex|cluster|lock|call|do|execute|prepare|deallocate|listen|notify|set|reset|show|"
    r"begin|commit|rollback|savepoint|into|returning|refresh|comment|security|role|"
    r"pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|"
    r"dblink|current_setting|set_config|pg_terminate_backend|pg_cancel_backend)\b", re.I)
_IDENT = re.compile(r"\b(?:from|join)\s+(?:only\s+)?([a-zA-Z_][\w.]*)", re.I)


class SqlRejected(ValueError):
    """The statement is not a plain read — returned to the model."""


def guard(sql: str) -> str:
    """Validate and normalise; returns the bare SELECT or raises."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise SqlRejected("empty statement")
    if ";" in s:
        raise SqlRejected("one statement only (no semicolons)")
    if "--" in s or "/*" in s:
        raise SqlRejected("comments are not allowed")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise SqlRejected("only SELECT (or WITH ... SELECT) is allowed")
    m = _FORBIDDEN.search(s)
    if m:
        raise SqlRejected(f"keyword not allowed in a read-only query: {m.group(0).upper()}")
    if re.search(r"\bpg_[a-z_]+\b|\binformation_schema\b", s, re.I):
        raise SqlRejected("system catalogs are not readable here — use describe_tables")
    ctes = {c.lower() for c in re.findall(r"(?:with|,)\s*([a-zA-Z_]\w*)\s+as\s*\(", s, re.I)}
    for ident in _IDENT.findall(s):
        name = ident.split(".")[-1].lower()
        if name in ctes:
            continue
        if name not in ALLOWED_TABLES:
            raise SqlRejected(f"table not allowed: {ident} (allowed: {', '.join(sorted(ALLOWED_TABLES))})")
    if len(s) > 4000:
        raise SqlRejected("statement too long")
    return s


def wrap(sql: str, max_rows: int = MAX_ROWS, timeout_ms: int = TIMEOUT_MS) -> str:
    """The guarded statement inside a read-only transaction, rows as
    JSON objects (one per line) so the columns come back by name."""
    s = guard(sql)
    return (f"BEGIN READ ONLY; SET LOCAL statement_timeout = {int(timeout_ms)};"
            f" SET LOCAL search_path = public; /*tag:query_database*/ "
            f"SELECT row_to_json(q)::text FROM ({s}) q LIMIT {int(max_rows)}; COMMIT;")


def parse_rows(raw_rows: Sequence[Sequence[str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        if not r or not r[0]:
            continue
        try:
            out.append(json.loads("\t".join(r)))     # a JSON value never splits on tab, but be safe
        except ValueError:
            continue
    return out


def describe(rows_fn, tables: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Columns of the allow-listed tables from the catalog (one query),
    plus the one-line notes above."""
    wanted = sorted(set(tables or ALLOWED_TABLES) & ALLOWED_TABLES)
    lst = ",".join("'" + t + "'" for t in wanted) or "''"
    raw = rows_fn("/*tag:describe_tables*/ SELECT table_name, column_name, data_type"
                  " FROM information_schema.columns WHERE table_schema='public'"
                  f" AND table_name IN ({lst}) ORDER BY table_name, ordinal_position;")
    cols: Dict[str, List[str]] = {}
    for r in raw:
        if len(r) >= 3:
            cols.setdefault(r[0], []).append(f"{r[1]} {r[2]}")
    return {"tables": [{"table": t, "columns": cols[t], "note": TABLE_NOTES.get(t, "")}
                       for t in wanted if t in cols],
            "rules": "read-only; one SELECT; rows capped at %d; times are UTC unless the column"
                     " says MX; energy in kWh; money in MXN without IVA." % MAX_ROWS}
