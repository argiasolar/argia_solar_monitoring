#!/usr/bin/env python3
"""v380: read-only export of one period for the EPI-600 engine (scripts/epi600_month.py).

Runs on pio06 as root: every query goes through the one psql wrapper
(argia.store.pgq.psql_csv) and starts with SESSION ... READ ONLY plus a statement
timeout, so nothing can be written. Output files (the format epi600_month.py reads):
  telemetry.csv, strings.csv, detail.csv, daily.csv, layouts/<KEY>.json
Period: local days [from, to) in Mexico City time (UTC-6, no DST since 2022).

  python3 scripts/epi600_export.py --from 2026-10-01 --to 2026-11-01 \\
      --plants /opt/argia/epi/plants_v1.json --out /tmp/epi_oct
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.store import pgq     # noqa: E402

LAYOUTS = "/opt/argia/layouts"
READ_ONLY = ("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY; SET statement_timeout = '300s'; "
             "SET TIME ZONE 'UTC'; ")
TABLES = {
    "telemetry.csv": "SELECT plant_key, inverter_sn, ts_utc, status, power_w, etoday_kwh, irradiance_wm2, "
                     "module_temp_c, ambient_temp_c FROM telemetry WHERE {w} ORDER BY 1,2,3",
    "strings.csv": "SELECT plant_key, inverter_sn, ts_utc, str_a, mppt_a, mppt_v FROM string_sample WHERE {w} ORDER BY 1,2,3",
    "detail.csv": "SELECT plant_key, inverter_sn, ts_utc, ppv_w, pac_w, pf, derating_mode FROM telemetry_detail "
                  "WHERE {w} ORDER BY 1,2,3",
    "daily.csv": "SELECT plant_key, prod_date, energy_kwh, irradiance_kwh_m2, availability, avail_coverage, "
                 "inverters_reporting, data_class FROM daily_production WHERE prod_date >= '{d0}' AND prod_date < '{d1}' "
                 "AND plant_key IN ({keys}) ORDER BY 1,2",
}


def utc_bounds(d0: dt.date, d1: dt.date):
    """Local MX midnights as UTC timestamps (UTC-6)."""
    return (f"{d0.isoformat()} 06:00+00", f"{d1.isoformat()} 06:00+00")


def queries(d0: dt.date, d1: dt.date, keys) -> dict:
    if not keys or any(not k.isalnum() for k in keys):
        raise ValueError(f"plant keys must be alphanumeric: {keys}")
    a, b = utc_bounds(d0, d1)
    klist = ",".join(f"'{k}'" for k in sorted(keys))
    w = f"ts_utc >= '{a}' AND ts_utc < '{b}' AND plant_key IN ({klist})"
    return {f: READ_ONLY + q.format(w=w, d0=d0.isoformat(), d1=d1.isoformat(), keys=klist)
            for f, q in TABLES.items()}


def run_query(sql: str, path: str) -> int:
    text = pgq.psql_csv(sql, timeout=330)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return os.path.getsize(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d0", required=True)
    ap.add_argument("--to", dest="d1", required=True, help="first day NOT included")
    ap.add_argument("--plants", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--layouts", default=LAYOUTS)
    a = ap.parse_args(argv)
    d0, d1 = dt.date.fromisoformat(a.d0), dt.date.fromisoformat(a.d1)
    if d1 <= d0:
        raise SystemExit("--to must be after --from")
    plants = json.load(open(a.plants, encoding="utf-8"))
    keys = [p["key"] for p in plants.values()]
    os.makedirs(os.path.join(a.out, "layouts"), exist_ok=True)
    for f, sql in queries(d0, d1, keys).items():
        print(f"{f}: {run_query(sql, os.path.join(a.out, f))} bytes", flush=True)
    for k in keys:
        shutil.copy(os.path.join(a.layouts, f"{k}.json"), os.path.join(a.out, "layouts", f"{k}.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
