#!/usr/bin/env python3
"""Recompute daily availability from evidence for past days (v310).

    availability_recompute.py [--days 31] [--plant KEY] [--apply] [--csv out.csv]

Reads the stored telemetry, the configured inverters (rated kW) and each
plant's coordinates and expected energy from PostgreSQL, computes the day
with argia/kpi/availability.py and prints old -> new for every plant-day.
Dry run by default; --apply writes ONLY daily_production.availability and
daily_production.avail_coverage (never energy, billable or any money
column). Server only (runuser psql); every query carries a statement_timeout.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.kpi import availability as AV                      # noqa: E402
from argia.store.pgq import psql_exec, psql_rows              # noqa: E402

TIMEOUT = "SET statement_timeout = '120s';\n"


def f(x: str) -> Optional[float]:
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def plants(only: Optional[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    rows = psql_rows(TIMEOUT + "SELECT plant_key, lat, lon FROM plant WHERE active ORDER BY 1;")
    return {r[0]: (f(r[1]), f(r[2])) for r in rows if not only or r[0] == only}


def inverters(pk: str, day: dt.date) -> List[Tuple[str, Optional[float]]]:
    rows = psql_rows(TIMEOUT + f"SELECT inverter_sn, rated_kw FROM inverter WHERE plant_key = '{pk}' AND active"
                     f" AND (date_decommissioned IS NULL OR date_decommissioned > DATE '{day}')"
                     f" AND (date_producing IS NULL OR date_producing <= DATE '{day}') ORDER BY 1;")
    return [(r[0], f(r[1])) for r in rows]


def samples(pk: str, day: dt.date) -> List[AV.Sample]:
    start = dt.datetime.combine(day, dt.time()) - AV.MX_OFFSET        # MX midnight in UTC
    rows = psql_rows(TIMEOUT + "SELECT to_char(ts_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS'), inverter_sn,"
                     " status, power_w, etoday_kwh FROM telemetry"
                     f" WHERE plant_key = '{pk}' AND ts_utc >= TIMESTAMPTZ '{start.isoformat()}+00'"
                     f" AND ts_utc < TIMESTAMPTZ '{(start + dt.timedelta(days=1)).isoformat()}+00' ORDER BY 1;")
    out = []
    for r in rows:
        st = int(r[2]) if r[2] not in ("", None) else None
        out.append((dt.datetime.fromisoformat(r[0]).replace(tzinfo=dt.timezone.utc), r[1], st, f(r[3]), f(r[4])))
    return out


def stored(first: dt.date, last: dt.date, only: Optional[str]):
    has_cov = psql_rows(TIMEOUT + "SELECT count(*) FROM information_schema.columns"
                        " WHERE table_name = 'daily_production' AND column_name = 'avail_coverage';")[0][0] != "0"
    cov = "avail_coverage" if has_cov else "NULL"
    rows = psql_rows(TIMEOUT + f"SELECT plant_key, prod_date::text, availability, {cov}, expected_kwh, energy_kwh"
                     f" FROM daily_production WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}'"
                     + (f" AND plant_key = '{only}'" if only else "") + " ORDER BY 1, 2;")
    return rows


def update_sql(pk: str, day: str, res: AV.AvailabilityDay) -> str:
    av = "NULL" if res.availability is None else f"{res.availability:.4f}"
    return (f"UPDATE daily_production SET availability = {av}, avail_coverage = {res.coverage:.4f}"
            f" WHERE plant_key = '{pk}' AND prod_date = DATE '{day}';")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--plant")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args(argv)
    today = (dt.datetime.now(dt.timezone.utc) + AV.MX_OFFSET).date()
    first, last = today - dt.timedelta(days=a.days), today - dt.timedelta(days=1)
    geo = plants(a.plant)
    changes, sql = [], []
    for pk, day, old_av, old_cov, exp, energy in stored(first, last, a.plant):
        if pk not in geo:
            continue
        d = dt.date.fromisoformat(day)
        res = AV.compute(samples(pk, d), inverters(pk, d), d, geo[pk][0], geo[pk][1], expected_kwh=f(exp))
        o = f(old_av)
        changes.append({"plant": pk, "date": day, "old": o, "new": res.availability, "coverage": res.coverage,
                        "energy_kwh": f(energy), "expected_kwh": f(exp),
                        "down": ";".join(f"{sn}:{n}" for sn, n in sorted(res.down_steps.items())),
                        "unknown": ";".join(f"{sn}:{n}" for sn, n in sorted(res.unknown_steps.items())),
                        "proven": ";".join(res.proven)})
        sql.append(update_sql(pk, day, res))
    print(f"{'plant':6} {'date':10} {'old':>7} {'new':>7} {'cover':>6}  down / unknown (15-min steps)")
    for c in changes:
        o = "  -  " if c["old"] is None else f"{100 * c['old']:6.1f}%"
        n = "  -  " if c["new"] is None else f"{100 * c['new']:6.1f}%"
        if c["old"] is None or c["new"] is None or abs((c["old"] or 0) - (c["new"] or 0)) >= 0.005 or c["coverage"] < 0.95:
            print(f"{c['plant']:6} {c['date']:10} {o:>7} {n:>7} {100 * c['coverage']:5.0f}%  {c['down']} / {c['unknown']}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(changes[0].keys()) if changes else ["plant"])
            w.writeheader()
            w.writerows(changes)
    if a.apply and sql:
        psql_exec(TIMEOUT + AV.ENSURE_SQL + "\nBEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n")
        print(f"applied: {len(sql)} plant-day(s) - availability and avail_coverage only")
    else:
        print(f"dry run: {len(sql)} plant-day(s) computed, nothing written (--apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
