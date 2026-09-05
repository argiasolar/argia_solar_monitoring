"""PostgreSQL ``telemetry_detail`` — the wide vendor row, kept (v203).

Every collector builds a 143-column PLANT_SCHEMA row per inverter sample
(grid voltages and frequency, AC current, power factor, per-MPPT
voltages and powers, per-string currents, warning/fault words, derating
mode, PV isolation, bus voltages, string flags, GFCI). Since v184 those
rows went to the per-plant sheet tabs — switched OFF after the cell-cap
incidents — so the fleet has been throwing away ~130 measurements per
sample and keeping 11. Tomasz 2026-09-05: "verify if we collect all
data, maybe we are missing something … patterns related to temperature
before going stale, or something related to voltage".

This module mirrors the wide row into ``telemetry_detail``: the scalar
diagnostics as typed columns, the per-channel families as numeric
arrays (vpv[16], ppv[9], istring[29], epv_today[15]). ~7k rows/day,
~300 bytes each — a few MB a day. Same upsert semantics as
``pg_mirror`` (key = plant, sn, ts_utc), same fail-soft rule: a failed
mirror is a warning, never a collection error.

Switch: ``ARGIA_PG_DETAIL`` (default on whenever ``ARGIA_PG_MIRROR`` is).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence

from argia.store import pg_mirror
from argia.telemetry.schema import PLANT_SCHEMA

LOG = logging.getLogger("argia.pg_detail")

DETAIL_ENV = "ARGIA_PG_DETAIL"

# scalar columns copied 1:1 from PLANT_SCHEMA (name -> SQL type)
SCALARS = [
    ("pac_w", "numeric(12,2)"), ("iac_a", "numeric(8,2)"), ("pf", "numeric(6,3)"),
    ("pacr_w", "numeric(12,2)"), ("pacs_w", "numeric(12,2)"), ("pact_w", "numeric(12,2)"),
    ("vacr_v", "numeric(7,2)"), ("vacs_v", "numeric(7,2)"), ("vact_v", "numeric(7,2)"),
    ("vac_rs_v", "numeric(7,2)"), ("vac_st_v", "numeric(7,2)"), ("vac_tr_v", "numeric(7,2)"),
    ("fac_hz", "numeric(6,3)"), ("ppv_w", "numeric(12,2)"),
    ("warn_code", "integer"), ("warn_code_1", "integer"),
    ("fault_code_1", "integer"), ("fault_code_2", "integer"), ("fault_type", "integer"),
    ("pid_status", "integer"), ("pid_fault_code", "integer"),
    ("apf_status", "integer"), ("afci_status", "integer"),
    ("derating_mode", "integer"), ("real_op_percent", "integer"),
    ("pv_iso", "numeric(10,2)"), ("p_bus_voltage_v", "numeric(7,2)"),
    ("n_bus_voltage_v", "numeric(7,2)"),
    ("str_unmatch", "integer"), ("str_unblance", "integer"), ("str_break", "integer"),
    ("gfci_ma", "numeric(8,2)"),
]
INT_COLS = {n for n, t in SCALARS if t == "integer"}
ARRAYS = [("vpv_v", "vpv", "_v"), ("ppv_mppt_w", "ppv", "_w"),
          ("istring_a", "istring", "_a"), ("epv_today_kwh", "epv", "_today_kwh")]

_IDX = {c: i for i, c in enumerate(PLANT_SCHEMA.columns)}


def _array_indices(prefix: str, suffix: str) -> List[int]:
    out = []
    for i, c in enumerate(PLANT_SCHEMA.columns):
        if c.startswith(prefix) and c.endswith(suffix) and c[len(prefix):-len(suffix)].isdigit():
            out.append((int(c[len(prefix):-len(suffix)]), i))
    return [i for _, i in sorted(out)]


_ARRAY_IDX = {name: _array_indices(p, s) for name, p, s in ARRAYS}

ENSURE_SQL = (
    "CREATE TABLE IF NOT EXISTS telemetry_detail (\n"
    "    ts_utc timestamptz NOT NULL,\n    plant_key text NOT NULL,\n"
    "    inverter_sn text NOT NULL,\n"
    + "".join(f"    {n} {t},\n" for n, t in SCALARS)
    + "".join(f"    {n} numeric[],\n" for n, _, _ in ARRAYS)
    + "    PRIMARY KEY (plant_key, inverter_sn, ts_utc)\n);\n"
    "CREATE INDEX IF NOT EXISTS idx_tdetail_ts ON telemetry_detail (ts_utc);\n"
)

COLUMNS = ["ts_utc", "plant_key", "inverter_sn"] + [n for n, _ in SCALARS] + [n for n, _, _ in ARRAYS]


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    if not pg_mirror.enabled():
        return False
    return str(env.get(DETAIL_ENV, "1")).strip().lower() in ("1", "true", "yes", "on")


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _lit(v: Optional[float], integer: bool = False) -> str:
    if v is None:
        return "NULL"
    return str(int(v)) if integer else repr(float(v))


def _array_lit(vals: Sequence[Optional[float]]) -> str:
    """Trailing NULLs trimmed (unused MPPTs/strings); all-empty -> NULL."""
    vals = list(vals)
    while vals and vals[-1] is None:
        vals.pop()
    if not vals:
        return "NULL"
    return "ARRAY[" + ",".join("NULL" if v is None else repr(float(v)) for v in vals) + "]::numeric[]"


def row_values(plant_key: str, plant_row: Sequence[Any]) -> Optional[List[str]]:
    """One PLANT_SCHEMA row -> SQL literals in COLUMNS order. Pure."""
    if len(plant_row) < len(PLANT_SCHEMA.columns):
        return None
    ts = str(plant_row[_IDX["timestamp_utc"]] or "").strip()
    sn = str(plant_row[_IDX["inverter_sn"]] or "").strip()
    if not ts or not sn:
        return None
    vals = [pg_mirror._sql_lit(ts, False), pg_mirror._sql_lit(plant_key, False),
            pg_mirror._sql_lit(sn, False)]
    for n, _t in SCALARS:
        vals.append(_lit(_num(plant_row[_IDX[n]]), n in INT_COLS))
    for name, _p, _s in ARRAYS:
        vals.append(_array_lit([_num(plant_row[i]) for i in _ARRAY_IDX[name]]))
    return vals


def build_upsert_sql(plant_key: str, plant_rows: Sequence[Sequence[Any]]) -> Optional[str]:
    tuples = []
    for r in plant_rows:
        vals = row_values(plant_key, r)
        if vals is not None:
            tuples.append("(" + ", ".join(vals) + ")")
    if not tuples:
        return None
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[3:])
    return (f"INSERT INTO telemetry_detail ({', '.join(COLUMNS)}) VALUES\n"
            + ",\n".join(tuples)
            + f"\nON CONFLICT (plant_key, inverter_sn, ts_utc) DO UPDATE SET {upd};")


def mirror_plant_rows(plant_key: str, plant_rows: Sequence[Sequence[Any]],
                      dry_run: bool = False, log: Optional[logging.Logger] = None) -> int:
    """Upsert one plant's wide rows. Returns rows attempted (0 = no-op).
    Never raises: a failure is logged as a warning (collection unaffected)."""
    lg = log or LOG
    if not enabled() or not plant_rows:
        return 0
    sql = build_upsert_sql(plant_key, plant_rows)
    if sql is None:
        return 0
    n = sql.count("\n(")
    if dry_run:
        lg.info("[PG] DRY RUN: would upsert %d detail rows for %s", n, plant_key)
        return n
    from argia.store import pgq                      # v207.3: one wrapper
    try:
        pgq.psql_exec(ENSURE_SQL + sql, timeout=60)
    except Exception as e:  # noqa: BLE001
        lg.warning("[PG] detail write failed for %s (telemetry rows unaffected): %s",
                   plant_key, str(e)[-300:])
        return 0
    lg.info("[PG] wrote %d detail rows for %s", n, plant_key)
    return n


# ------------------------------------------------------------- v207 reader
STRING_FLAG_COLS = ("str_break", "str_unmatch", "str_unblance")


def read_string_flags(first_mx_date: str, last_mx_date: str) -> List[tuple]:
    """(ts_utc, plant_key, inverter_sn, {str_break, str_unmatch,
    str_unblance}) for every wide row whose MX day is within the window
    (inclusive). Only rows that carry at least one flag column are
    returned: non-Growatt inverters have no string bits and contribute
    nothing, exactly like their empty sheet columns used to.

    v207: the daily string-flag alert read the per-plant sheet tabs; from
    v199 that read raised SheetsRetired and the rule silently produced
    nothing while the bits sat in this table (v203)."""
    import datetime as _dt
    from argia.store.pgq import psql_rows
    cols = ", ".join(STRING_FLAG_COLS)
    sql = (
        "SELECT ts_utc, plant_key, inverter_sn, " + cols +
        " FROM telemetry_detail"
        " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date"
        f" BETWEEN DATE '{first_mx_date}' AND DATE '{last_mx_date}'"
        " AND (" + " OR ".join(c + " IS NOT NULL" for c in STRING_FLAG_COLS) + ")"
        " ORDER BY ts_utc;")
    out = []
    for r in psql_rows(sql):
        if len(r) < 3 + len(STRING_FLAG_COLS):
            continue
        raw = r[0].replace(" ", "T")
        # psql prints "+00"; Python < 3.11 wants "+00:00"
        if len(raw) >= 3 and raw[-3] in "+-" and raw[-2:].isdigit():
            raw += ":00"
        try:
            ts = _dt.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        flags = {c: (r[3 + i] if r[3 + i] != "" else None)
                 for i, c in enumerate(STRING_FLAG_COLS)}
        out.append((ts, r[1], r[2], flags))
    return out
