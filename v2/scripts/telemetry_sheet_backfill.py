#!/usr/bin/env python3
"""Replay one MX day of Telemetry_Argia into the sheet from PostgreSQL.

Why this exists (2026-09-04): the workbook hit its 10,000,000-cell cap on
2026-09-03 and every append failed for eight hours — 11:00..18:00 MX, the
peak production window. Postgres kept receiving the same rows the whole
time, so nothing was lost; but ``Telemetry_Argia`` is what kpi_eod reads,
and a KPI computed from a half-day is a wrong KPI in a billing chain.

This replays a day from Postgres into the sheet. The sheet writer upserts
on the natural key (timestamp_utc, plant_key, inverter_sn), so replaying a
day that is already complete is a no-op, and replaying a partial day fills
exactly the holes. Safe to re-run.

USAGE
    PYTHONPATH=. python scripts/telemetry_sheet_backfill.py --date 2026-09-03
    PYTHONPATH=. python scripts/telemetry_sheet_backfill.py --date 2026-09-03 --apply

ENV: GOOGLE_SHEET_ID_V2, GOOGLE_CREDENTIALS, ARGIA_PG_DB (default argia_mont)
EXIT: 0 ok   2 nothing in Postgres for that date   3 config error
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import logging
import os
import subprocess
import sys

from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ, UTC
from argia.telemetry.schema import ARGIA_SCHEMA, ARGIA_TAB_NAME
from argia.telemetry.sheets_writer import ensure_telemetry_tab, write_telemetry_rows

LOG = logging.getLogger("argia.telemetry.backfill")

# Selected in this exact order; index-matched to ARGIA_COMMON_COLS below.
PG_COLS = ("ts_utc", "vendor", "plant_key", "inverter_sn", "inverter_label",
           "status", "power_w", "etoday_kwh", "temperature_c", "fault_code",
           "irradiance_wm2", "irradiance_kwh_m2_5m", "cloud_cover_pct",
           "ambient_temp_c", "module_temp_c")

SELECT_SQL = (
    "SELECT " + ",".join(PG_COLS) + " FROM telemetry"
    " WHERE (ts_utc AT TIME ZONE 'America/Mexico_City')::date = DATE '%s'"
    "%s ORDER BY ts_utc, plant_key, inverter_sn;"
)


def parse_pg_ts(raw: str) -> dt.datetime:
    """PostgreSQL prints '2026-09-03 19:00:10+00'; make it a real UTC datetime.

    Pure. Accepts the '+00' short offset that ``datetime.fromisoformat``
    rejects on older Pythons, and any already-ISO spelling.
    """
    s = (raw or "").strip().replace(" ", "T")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        s += ":00"                      # '+00' -> '+00:00'
    d = dt.datetime.fromisoformat(s)
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


def sheet_row(rec: dict) -> list:
    """One PG record -> one ARGIA_SCHEMA row, in the sheet's own spelling.

    Pure. timestamp_utc/timestamp_mx are formatted exactly as the live
    collector writes them, so the natural key matches and the upsert
    updates instead of duplicating.
    """
    ts = parse_pg_ts(rec["ts_utc"])
    return [
        ts.isoformat(),
        ts.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        rec.get("vendor", ""), rec.get("plant_key", ""),
        rec.get("inverter_sn", ""), rec.get("inverter_label", ""),
        rec.get("status", ""), rec.get("power_w", ""),
        rec.get("etoday_kwh", ""), rec.get("temperature_c", ""),
        rec.get("fault_code", ""), rec.get("irradiance_wm2", ""),
        rec.get("irradiance_kwh_m2_5m", ""), rec.get("cloud_cover_pct", ""),
        rec.get("ambient_temp_c", ""), rec.get("module_temp_c", ""),
    ]


def rows_from_csv(text: str) -> list:
    """psql --csv output -> ARGIA_SCHEMA rows. Pure; tolerates a trailing
    newline and the embedded commas in fault_code ('IS=40960,RS=1')."""
    out = []
    for rec in csv.DictReader(io.StringIO(text)):
        if not (rec.get("ts_utc") and rec.get("plant_key")
                and rec.get("inverter_sn")):
            continue
        out.append(sheet_row(rec))
    return out


def fetch(date_iso: str, plant: str | None = None, db: str | None = None) -> str:
    clause = ""
    if plant:
        clause = " AND plant_key = '%s'" % plant.replace("'", "''")
    sql = SELECT_SQL % (date_iso, clause)
    r = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d",
         db or os.environ.get("ARGIA_PG_DB", "argia_mont"),
         "-v", "ON_ERROR_STOP=1", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("psql failed: %s" % r.stderr.strip()[:300])
    return r.stdout


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--date", required=True, help="MX date, YYYY-MM-DD")
    p.add_argument("--plant", default=None)
    p.add_argument("--apply", action="store_true",
                   help="write to the sheet. Default is a dry run.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    dt.date.fromisoformat(args.date)          # fail fast on a bad date
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 is not set")
        return 3

    rows = rows_from_csv(fetch(args.date, args.plant))
    if not rows:
        LOG.error("no Postgres telemetry for %s — nothing to replay", args.date)
        return 2
    hours = sorted({r[1][11:13] for r in rows})
    LOG.info("%s: %d row(s) in Postgres across MX hours %s..%s",
             args.date, len(rows), hours[0], hours[-1])

    if not args.apply:
        LOG.info("DRY RUN — re-run with --apply to write %s", ARGIA_TAB_NAME)
        return 0

    sheets = SheetsClient(sheet_id)
    ensure_telemetry_tab(sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA)
    stats = write_telemetry_rows(sheets, ARGIA_TAB_NAME, ARGIA_SCHEMA, rows)
    LOG.info("%s: %s", ARGIA_TAB_NAME, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
