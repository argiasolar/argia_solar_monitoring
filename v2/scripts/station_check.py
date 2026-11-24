#!/usr/bin/env python3
"""Daily weather-station check per plant (v396, Arturo's methodology R10 + the R2 station gate).

    station_check.py [--days 35] [--apply]

Reads the stored 5-minute station data (telemetry), the MPPT voltages (string_sample), the
satellite cross-check and the daily irradiance, judges each station-day with
argia.analytics.station_check and prints it; --apply upserts station_check_daily. kpi_eod runs
it every morning over the last 35 days. Read-only without --apply. Server only (psql).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.analytics import station_check as SC          # noqa: E402
from argia.store.pgq import psql_exec, psql_rows         # noqa: E402

MXT = "AT TIME ZONE 'America/Mexico_City'"
TIMEOUT = "SET statement_timeout = '300s';\n"
REF_START = "2026-06-01"     # the module-minus-ambient reference needs the stored history


def _f(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def collect(first: dt.date, last: dt.date):
    rng = (f"ts_utc >= (DATE '{first}')::timestamp {MXT} AND ts_utc < (DATE '{last}' + 1)::timestamp {MXT}")
    cov = {}
    for r in psql_rows(TIMEOUT + "SELECT plant_key, d, "
                       "count(DISTINCT b) FILTER (WHERE irr), count(DISTINCT b) FILTER (WHERE tm),"
                       " count(DISTINCT b) FILTER (WHERE ta) FROM (SELECT plant_key,"
                       f" (ts_utc {MXT})::date::text d, date_trunc('hour', ts_utc) + floor(extract(minute FROM ts_utc) / 5)"
                       " * interval '5 min' b, irradiance_wm2 IS NOT NULL irr, module_temp_c IS NOT NULL tm,"
                       f" ambient_temp_c IS NOT NULL ta FROM telemetry WHERE {rng}"
                       f" AND extract(hour FROM ts_utc {MXT}) BETWEEN 8 AND 16) x GROUP BY 1, 2;"):
        if len(r) >= 5:
            cov[(r[0], r[1])] = min(int(r[2]), int(r[3]), int(r[4])) / SC.EXPECTED_STEPS
    dt_daily: Dict[str, Dict[str, float]] = {}
    for r in psql_rows(TIMEOUT + f"SELECT plant_key, (ts_utc {MXT})::date::text, avg(module_temp_c - ambient_temp_c), count(*)"
                       f" FROM telemetry WHERE ts_utc >= DATE '{REF_START}' AND ts_utc < (DATE '{last}' + 1)::timestamp {MXT}"
                       " AND irradiance_wm2 BETWEEN 700 AND 950 AND module_temp_c IS NOT NULL AND ambient_temp_c IS NOT NULL"
                       f" AND extract(hour FROM ts_utc {MXT}) BETWEEN 11 AND 14 GROUP BY 1, 2;"):
        if len(r) >= 4 and int(r[3]) >= SC.MIN_DT_SAMPLES:
            dt_daily.setdefault(r[0], {})[r[1]] = float(r[2])
    coup = {}
    for r in psql_rows(TIMEOUT + "WITH s AS (SELECT plant_key, date_trunc('hour', ts_utc) + floor(extract(minute FROM ts_utc) / 5)"
                       " * interval '5 min' b, avg(v) v FROM string_sample, unnest(mppt_v) v"
                       f" WHERE {rng} AND v > 100 GROUP BY 1, 2),"
                       " w AS (SELECT plant_key, date_trunc('hour', ts_utc) + floor(extract(minute FROM ts_utc) / 5)"
                       " * interval '5 min' b, avg(module_temp_c) tm, avg(irradiance_wm2) g FROM telemetry"
                       f" WHERE {rng} AND module_temp_c IS NOT NULL GROUP BY 1, 2),"
                       f" j AS (SELECT s.plant_key, (s.b {MXT})::date::text d, s.v, w.tm FROM s JOIN w USING (plant_key, b)"
                       " WHERE w.g >= 600),"
                       " m AS (SELECT plant_key, d, percentile_cont(0.5) WITHIN GROUP (ORDER BY v) md FROM j GROUP BY 1, 2)"
                       " SELECT j.plant_key, j.d, regr_slope(100 * j.v / m.md, j.tm), count(*), max(j.tm) - min(j.tm)"
                       " FROM j JOIN m USING (plant_key, d) GROUP BY 1, 2;"):
        if len(r) >= 5:
            coup[(r[0], r[1])] = (_f(r[2]), int(r[3]), _f(r[4]))
    sat = {(r[0], r[1]): r[2] for r in psql_rows(
        TIMEOUT + f"SELECT plant_key, check_date::text, status FROM satellite_check WHERE check_date BETWEEN DATE '{first}'"
        f" AND DATE '{last}';") if len(r) >= 3}
    irr: Dict[str, Dict[str, float]] = {}
    for r in psql_rows(TIMEOUT + "SELECT plant_key, prod_date::text, irradiance_kwh_m2 FROM daily_production"
                       f" WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}' AND irradiance_kwh_m2 > 0"
                       " AND irradiance_source NOT LIKE 'cloud%';"):
        if len(r) >= 3:
            irr.setdefault(r[0], {})[r[1]] = float(r[2])
    return cov, dt_daily, coup, sat, irr


def build(first: dt.date, last: dt.date, data) -> list:
    cov, dt_daily, coup, sat, irr = data
    shared = SC.shared_groups(irr)
    drift = SC.drift_series(dt_daily, shared)
    coup_by: Dict[str, Dict[str, tuple]] = {}
    for (pk, day), v in coup.items():
        coup_by.setdefault(pk, {})[day] = v
    plants = sorted({pk for pk, _d in cov} | set(dt_daily))
    out, d = [], first
    while d <= last:
        day = d.isoformat()
        for pk in plants:
            if (pk, day) not in cov and day not in dt_daily.get(pk, {}):
                continue
            slope, ndays = SC.coupling_window(coup_by.get(pk, {}), day)
            out.append(SC.judge(pk, day, cov.get((pk, day)), slope, ndays, sat.get((pk, day)),
                                dt_daily.get(pk, {}).get(day), drift.get((pk, day)), shared.get(pk, ())))
        d += dt.timedelta(days=1)
    return out


def run(days: int = 35, apply: bool = False, quiet: bool = False) -> int:
    last = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).date() - dt.timedelta(days=1)
    first = last - dt.timedelta(days=days - 1)
    checks = build(first, last, collect(first, last))
    if not quiet:
        for c in checks:
            if c.status != "ok":
                print(f"{c.plant_key:5} {c.day} {c.status:7} " + "; ".join(c.reasons)
                      + (f" (shared with {', '.join(c.shared_with)})" if c.shared_with else ""))
        print(f"{len(checks)} station-days, {sum(c.status == 'fail' for c in checks)} failed"
              + ("" if apply else " - dry run, nothing written (--apply to write)"))
    if apply and checks:
        psql_exec(TIMEOUT + SC.upsert_sql(checks), timeout=330)
    return len(checks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    run(a.days, a.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
