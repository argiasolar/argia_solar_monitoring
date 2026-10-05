#!/usr/bin/env python3
"""Recompute daily availability from evidence for past days (v310).

    availability_recompute.py [--days 31] [--plant KEY] [--apply] [--csv out.csv]

Reads the stored telemetry, the configured inverters (rated kW) and each
plant's coordinates and expected energy from PostgreSQL, computes the day
with argia/kpi/availability.py and prints old -> new for every plant-day.
Dry run by default; --apply writes ONLY daily_production.availability and
daily_production.avail_coverage (never energy, billable or any money
column). Server only (runuser psql); every query carries a statement_timeout.

v311: two passes - the first learns each plant's usual energy / weather
ratio from its well-covered days, the second judges plant-wide silences
against that normal energy and the day energy the vendor counter finally
reported. kpi_eod runs it every morning over the last 35 days, so data
that arrives late (internet back after an outage) reaches its days.
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


TYPICAL_MIN_DAYS = 5
TYPICAL_BOUNDS = (0.5, 1.3)


def typical_ratio(days) -> float:
    """The plant's own usual energy / weather-expectation ratio, from its
    well-covered days (coverage >= 0.95, nothing down): the weather model is
    off by plant (Ryder's model reads high), so 'normal energy' is judged
    against the plant's own normal. Bounded; 1.0 with too few days. Pure.
    ``days``: [(coverage, down_steps_total, energy, expected)]."""
    import statistics
    r = [e / x for c, d, e, x in days if c is not None and c >= 0.95 and not d and e and x and x > 0]
    if len(r) < TYPICAL_MIN_DAYS:
        return 1.0
    return max(TYPICAL_BOUNDS[0], min(TYPICAL_BOUNDS[1], statistics.median(r)))


def spread_catchup(days) -> Dict[str, float]:
    """v311: energy that arrived late (the lifetime counter jumping when the
    internet came back - loss_daily.catchup_kwh on the return day) belongs to
    the days the plant was blind. ``days``: [(date, energy, typical, catchup)]
    of one plant, sorted. The catch-up is spread back over the run of short
    days just before it (energy below PLANT_PROOF of typical, at most 31
    days), in proportion to each day's shortfall and never above it. Returns
    {date: extra kWh}. Pure."""
    extra: Dict[str, float] = {}
    for i, (d, _e, _t, c) in enumerate(days):
        if not c or c <= 0:
            continue
        run_ = []
        j = i - 1
        while j >= 0 and len(run_) < 31:
            dd, e, t, _c = days[j]
            if not t or t <= 0 or (e or 0.0) + extra.get(dd, 0.0) >= AV.PLANT_PROOF * t:
                break
            run_.append((dd, t - (e or 0.0) - extra.get(dd, 0.0)))
            j -= 1
        need = sum(short for _d, short in run_)
        if need <= 0:
            continue
        share = min(1.0, c / need)
        for dd, short in run_:
            extra[dd] = extra.get(dd, 0.0) + short * share
    return extra


def catchups(first: dt.date, last: dt.date, only: Optional[str]) -> Dict[Tuple[str, str], float]:
    """loss_daily.catchup_kwh per (plant, day) - the lifetime counter's late energy."""
    try:
        rows = psql_rows(TIMEOUT + "SELECT plant_key, prod_date::text, catchup_kwh FROM loss_daily"
                         f" WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}' AND catchup_kwh > 0"
                         + (f" AND plant_key = '{only}'" if only else "") + ";")
    except Exception:  # noqa: BLE001 - no loss table yet: no catch-up to spread
        return {}
    return {(r[0], r[1]): f(r[2]) or 0.0 for r in rows}


def run(days: int = 31, only: Optional[str] = None, apply: bool = False, csv_path: Optional[str] = None,
        quiet: bool = False) -> int:
    """Recompute the last ``days`` MX days. Returns the plant-days computed."""
    today = (dt.datetime.now(dt.timezone.utc) + AV.MX_OFFSET).date()
    first, last = today - dt.timedelta(days=days), today - dt.timedelta(days=1)
    geo = plants(only)
    rows = [r for r in stored(first, last, only) if r[0] in geo]
    data = {}
    for pk, day, old_av, old_cov, exp, energy in rows:
        d = dt.date.fromisoformat(day)
        data[(pk, day)] = (samples(pk, d), inverters(pk, d))
    # pass 1: the raw days, to learn each plant's usual ratio to its weather model
    first_pass = {}
    for pk, day, old_av, old_cov, exp, energy in rows:
        d = dt.date.fromisoformat(day)
        s, inv = data[(pk, day)]
        first_pass[(pk, day)] = AV.compute(s, inv, d, geo[pk][0], geo[pk][1], expected_kwh=f(exp))
    ratio = {}
    for pk in geo:
        ratio[pk] = typical_ratio([(first_pass[(p, dd)].coverage, sum(first_pass[(p, dd)].down_steps.values()),
                                    f(e), f(x)) for p, dd, _a, _c, x, e in rows if p == pk])
    # late energy: the lifetime counter's catch-up, spread back over the blind days
    cu = catchups(first, today, only)
    late: Dict[Tuple[str, str], float] = {}
    for pk in geo:
        seq = [(day, f(energy), (f(exp) * ratio[pk]) if f(exp) else None, cu.get((pk, day), 0.0))
               for p, day, _a, _c, exp, energy in rows if p == pk]
        seq += [(dd, None, None, c) for (p, dd), c in sorted(cu.items()) if p == pk and dd > last.isoformat()]
        for dd, x in spread_catchup(seq).items():
            late[(pk, dd)] = x
    # pass 2: with the plant's normal energy and the vendor counter's day energy
    changes, sql = [], []
    for pk, day, old_av, old_cov, exp, energy in rows:
        d = dt.date.fromisoformat(day)
        s, inv = data[(pk, day)]
        typical = f(exp) * ratio[pk] if f(exp) else None
        day_energy = (f(energy) or 0.0) + late.get((pk, day), 0.0)
        res = AV.compute(s, inv, d, geo[pk][0], geo[pk][1], expected_kwh=typical, plant_energy_kwh=day_energy)
        changes.append({"plant": pk, "date": day, "old": f(old_av), "new": res.availability, "coverage": res.coverage,
                        "energy_kwh": f(energy), "late_kwh": round(late.get((pk, day), 0.0), 1),
                        "expected_kwh": f(exp), "typical_kwh": round(typical, 1) if typical else None,
                        "down": ";".join(f"{sn}:{n}" for sn, n in sorted(res.down_steps.items())),
                        "unknown": ";".join(f"{sn}:{n}" for sn, n in sorted(res.unknown_steps.items())),
                        "proven": ";".join(res.proven), "plant_proven_steps": res.plant_proven_steps})
        sql.append(update_sql(pk, day, res))
    if not quiet:
        print("usual energy / weather expectation: " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(ratio.items())))
        print(f"{'plant':6} {'date':10} {'old':>7} {'new':>7} {'cover':>6}  down / unknown (15-min steps) / plant-proven")
        for c in changes:
            o = "  -  " if c["old"] is None else f"{100 * c['old']:6.1f}%"
            n = "  -  " if c["new"] is None else f"{100 * c['new']:6.1f}%"
            if (c["old"] is None or c["new"] is None or abs((c["old"] or 0) - (c["new"] or 0)) >= 0.005
                    or c["coverage"] < 0.95 or c["plant_proven_steps"]):
                print(f"{c['plant']:6} {c['date']:10} {o:>7} {n:>7} {100 * c['coverage']:5.0f}%  "
                      f"{c['down']} / {c['unknown']} / {c['plant_proven_steps']}")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(changes[0].keys()) if changes else ["plant"])
            w.writeheader()
            w.writerows(changes)
    if apply and sql:
        psql_exec(TIMEOUT + AV.ENSURE_SQL + "\nBEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n")
        if not quiet:
            print(f"applied: {len(sql)} plant-day(s) - availability and avail_coverage only")
    elif not quiet:
        print(f"dry run: {len(sql)} plant-day(s) computed, nothing written (--apply to write)")
    return len(sql)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--plant")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv")
    a = ap.parse_args(argv)
    run(a.days, a.plant, a.apply, a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
