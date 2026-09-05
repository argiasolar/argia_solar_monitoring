"""Telemetry from PostgreSQL, in the sheet's own shape (v189, 2026-09-04).

Phase 1 of the Sheets retirement. Every consumer of ``Telemetry_Argia``
— ``kpi.reader`` (kpi_eod), ``alerts_snapshot``, ``dashboard_update`` —
parses a *grid* (header row + cell rows in ARGIA_SCHEMA order) or a list
of dicts keyed by that header. This module produces exactly that from the
``telemetry`` table, with cells typed the way ``SheetsClient.read_range``
returns them (UNFORMATTED_VALUE: numbers as numbers, blanks as ''), so the
existing parsers run unchanged and the parity test compares like with
like.

Selection is by env:
    ARGIA_TELEMETRY_SOURCE = sheet | pg      (v189 default: sheet)
The readers ask ``source()`` and take the PG path only when it says
``pg``. Flip it on the server after ``scripts/telemetry_parity.py`` is
green for the day; flip the code default in the follow-up release.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from argia.core.time_utils import MX_TZ, UTC, parse_pg_ts
from argia.telemetry.schema import ARGIA_SCHEMA

SOURCE_ENV = "ARGIA_TELEMETRY_SOURCE"
HEADER: List[str] = list(ARGIA_SCHEMA.columns)          # 16 columns

# PG columns in the order they map onto HEADER (timestamp_mx is derived)
_PG_COLS = ("ts_utc", "vendor", "plant_key", "inverter_sn", "inverter_label",
            "status", "power_w", "etoday_kwh", "temperature_c", "fault_code",
            "irradiance_wm2", "irradiance_kwh_m2_5m", "cloud_cover_pct",
            "ambient_temp_c", "module_temp_c")
_NUMERIC = {"power_w", "etoday_kwh", "temperature_c", "irradiance_wm2",
            "irradiance_kwh_m2_5m", "cloud_cover_pct", "ambient_temp_c",
            "module_temp_c"}


SHEET_WRITE_ENV = "ARGIA_SHEET_TELEMETRY"


def sheet_write_enabled(env=None) -> bool:
    """Should telemetry_5m still upsert Telemetry_Argia? v189 default: yes
    (the readers may still be on the sheet). Set ARGIA_SHEET_TELEMETRY=0
    once ARGIA_TELEMETRY_SOURCE=pg has proven itself; the follow-up
    release flips the default."""
    env = os.environ if env is None else env
    return str(env.get(SHEET_WRITE_ENV, "0")).strip().lower() in (
        "1", "true", "yes", "on")


def source(env=None) -> str:
    """'sheet' or 'pg'. Anything unrecognised is 'sheet' — never guess
    towards the new path."""
    env = os.environ if env is None else env
    v = str(env.get(SOURCE_ENV, "pg")).strip().lower()
    return "pg" if v == "pg" else "sheet"


# ---------------------------------------------------------------- pure

def _num(s: str):
    s = (s or "").strip()
    if s == "":
        return ""
    try:
        f = float(s)
    except ValueError:
        return s
    return int(f) if f.is_integer() and "." not in s else f


def _status(s: str):
    s = (s or "").strip()
    if s == "":
        return ""
    try:
        return int(float(s))
    except ValueError:
        return s


def record_to_cells(rec: Dict[str, str]) -> List[Any]:
    """One psql CSV record -> one grid row in HEADER order. Pure."""
    ts = parse_pg_ts(rec["ts_utc"])
    out: List[Any] = [
        ts.isoformat(),                                   # timestamp_utc
        ts.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),  # timestamp_mx
    ]
    for col in _PG_COLS[1:]:
        v = rec.get(col, "")
        if col == "status":
            out.append(_status(v))
        elif col in _NUMERIC or col == "fault_code":
            # fault_code: the sheet stores '0' as the number 0 (USER_ENTERED)
            # and 'IS=40960,RS=1' as text — mirror that
            out.append(_num(v))
        else:
            out.append(v if v is not None else "")
    return out


def csv_to_grid(text: str) -> List[List[Any]]:
    """psql --csv output -> [HEADER, row, row, ...]. Pure; skips records
    without the natural key."""
    grid: List[List[Any]] = [list(HEADER)]
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("ts_utc") and rec.get("plant_key")
                and rec.get("inverter_sn")):
            continue
        grid.append(record_to_cells(rec))
    return grid


def grid_to_records(grid: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Grid -> list of dicts keyed by header (what read_table returns)."""
    if not grid:
        return []
    hdr = [str(h) for h in grid[0]]
    return [dict(zip(hdr, list(r) + [""] * (len(hdr) - len(r))))
            for r in grid[1:]]


def where_clause(date_iso: Optional[str] = None,
                 since_utc: Optional[dt.datetime] = None,
                 plants: Optional[Iterable[str]] = None) -> str:
    """SQL WHERE for one MX calendar day and/or a UTC lower bound. Pure."""
    parts: List[str] = []
    if date_iso:
        dt.date.fromisoformat(date_iso)          # validate
        parts.append("(ts_utc AT TIME ZONE 'America/Mexico_City')::date "
                     f"= DATE '{date_iso}'")
    if since_utc is not None:
        s = since_utc.astimezone(UTC).isoformat()
        parts.append(f"ts_utc >= '{s}'::timestamptz")
    if plants:
        keys = ",".join("'" + str(p).strip().upper().replace("'", "''") + "'"
                        for p in plants)
        parts.append(f"plant_key IN ({keys})")
    return (" WHERE " + " AND ".join(parts)) if parts else ""


def select_sql(date_iso=None, since_utc=None, plants=None) -> str:
    return ("SELECT " + ",".join(_PG_COLS) + " FROM telemetry"
            + where_clause(date_iso, since_utc, plants)
            + " ORDER BY ts_utc, plant_key, inverter_sn;")


# -------------------------------------------------------------- server

def _fetch_csv(sql: str) -> str:
    """v207.2: one psql wrapper (argia.store.pgq); the name stays as
    the seam tests monkeypatch."""
    from argia.store.pgq import psql_csv
    return psql_csv(sql, timeout=180)


def read_grid(date_iso: Optional[str] = None,
              since_utc: Optional[dt.datetime] = None,
              plants: Optional[Iterable[str]] = None) -> List[List[Any]]:
    """Header + rows, like ``sheets.read_range('Telemetry_Argia','A1:P')``."""
    return csv_to_grid(_fetch_csv(select_sql(date_iso, since_utc, plants)))


def read_records(date_iso=None, since_utc=None, plants=None) -> List[Dict[str, Any]]:
    """Dicts keyed by header, like ``sheets.read_table('Telemetry_Argia')``."""
    return grid_to_records(read_grid(date_iso, since_utc, plants))
