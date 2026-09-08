#!/usr/bin/env python3
"""Fill a plant's missing daily irradiance from the plant that SHARES its
weather device (v241).

    irradiance_proxy_backfill.py --plant TAM1                       # dry run: what would change
    irradiance_proxy_backfill.py --plant TAM1 --from 2026-05-13 --to 2026-09-07 --apply

Why: Ryder (TAM1) was onboarded from the vendor's month chart, so its
May-August rows carry energy but no irradiance (the KPI job never ran
on them), and since it went dark on Sep 2 the KPI job skipped it every
morning. Its weather device is Plastic Omnium's ShineMaster
(plant.weather_plant_id + datalogger_sn are NL1's), and NL1's stored
daily irradiance IS that device's integral — so the honest value for
a TAM1 day is the NL1 value of the same day, labelled as such.

Rules (all enforced in the pure functions below, tested):
  * only rows whose irradiance_kwh_m2 is NULL — a stored value is never
    overwritten (use the KPI job for that);
  * the donor is the plant with the same weather_plant_id AND
    datalogger_sn, never a plant of our choosing;
  * expected_kwh = kwp_dc x irradiance x expected_factor (the KPI job's
    own formula), only where expected_kwh is NULL too;
  * writes go through kpi_write.stamp — existing rows only, CLOSED
    months frozen, protected columns untouched;
  * irradiance_source = 'proxy:<donor>' so the provenance is on the row.
"""
from __future__ import annotations

import argparse
import sys
from typing import Dict, Iterable, List, Optional, Tuple

Row = Tuple[str, str, str]           # (prod_date, irradiance | '', expected | '')


def donor_for(plant_key: str, plants: Iterable[Tuple[str, str, str]]) -> Optional[str]:
    """The other plant on the same weather device: rows of
    (plant_key, weather_plant_id, datalogger_sn). None when the plant
    has no device or nobody else shares it."""
    mine = None
    rows = list(plants)
    for pk, wpid, sn in rows:
        if pk == plant_key:
            mine = ((wpid or "").strip(), (sn or "").strip())
    if not mine or not mine[0] or not mine[1]:
        return None
    for pk, wpid, sn in rows:
        if pk != plant_key and ((wpid or "").strip(), (sn or "").strip()) == mine:
            return pk
    return None


def plan(plant_key: str, target: Iterable[Row], donor_irr: Dict[str, float],
         kwp_dc: float, expected_factor: float) -> Dict[str, Dict[str, float]]:
    """{date: {'irradiance_kwh_m2': x, 'expected_kwh': y?}} for every
    target row whose irradiance is NULL and whose donor has a value
    that day. expected_kwh only where it is NULL too. Pure."""
    out: Dict[str, Dict[str, float]] = {}
    for d, irr, exp in target:
        if str(irr).strip() != "":
            continue                      # stored value: never overwritten
        v = donor_irr.get(d)
        if not v or v <= 0:
            continue
        rec = {"irradiance_kwh_m2": round(float(v), 4)}
        if str(exp).strip() == "" and kwp_dc and expected_factor:
            rec["expected_kwh"] = round(kwp_dc * float(v) * expected_factor, 3)
        out[d] = rec
    return out


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def select_target_sql(plant_key: str, d0: str, d1: str) -> str:
    return ("SELECT prod_date::text, coalesce(irradiance_kwh_m2::text,''), coalesce(expected_kwh::text,'')"
            f" FROM daily_production WHERE plant_key = {_q(plant_key)}"
            f" AND prod_date BETWEEN DATE {_q(d0)} AND DATE {_q(d1)} ORDER BY prod_date;")


def select_donor_sql(donor: str, d0: str, d1: str) -> str:
    return ("SELECT prod_date::text, irradiance_kwh_m2::text FROM daily_production"
            f" WHERE plant_key = {_q(donor)} AND irradiance_kwh_m2 > 0"
            f" AND prod_date BETWEEN DATE {_q(d0)} AND DATE {_q(d1)};")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fill a plant's NULL daily irradiance from its weather-device twin")
    ap.add_argument("--plant", required=True)
    ap.add_argument("--from", dest="d0", default=None, help="first day (default: the plant's first row)")
    ap.add_argument("--to", dest="d1", default=None, help="last day (default: yesterday MX)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a = ap.parse_args(argv)
    import datetime as dt
    from argia.core.time_utils import now_mx
    from argia.store import kpi_write
    from argia.store.pgq import psql_rows

    pk = a.plant.strip().upper()
    plants = [tuple(r[:3]) for r in psql_rows(
        "SET statement_timeout='10s'; SELECT plant_key, coalesce(weather_plant_id,''), coalesce(datalogger_sn,'') FROM plant;")]
    donor = donor_for(pk, plants)
    if donor is None:
        print(f"{pk}: no other plant shares its weather device — nothing to copy from")
        return 2
    cfg = psql_rows(f"SET statement_timeout='10s'; SELECT kwp_dc::text, expected_factor::text FROM plant WHERE plant_key = {_q(pk)};")
    if not cfg:
        print(f"{pk}: not in plant")
        return 2
    kwp, ef = float(cfg[0][0] or 0), float(cfg[0][1] or 0)
    first = psql_rows(f"SET statement_timeout='10s'; SELECT min(prod_date)::text FROM daily_production WHERE plant_key = {_q(pk)};")
    d0 = a.d0 or (first[0][0] if first and first[0] and first[0][0] else None)
    d1 = a.d1 or (now_mx().date() - dt.timedelta(days=1)).isoformat()
    if not d0:
        print(f"{pk}: no daily_production rows")
        return 2
    target = [tuple(r[:3]) for r in psql_rows("SET statement_timeout='20s'; " + select_target_sql(pk, d0, d1))]
    donor_irr = {r[0]: float(r[1]) for r in psql_rows("SET statement_timeout='20s'; " + select_donor_sql(donor, d0, d1)) if len(r) >= 2 and r[1]}
    todo = plan(pk, target, donor_irr, kwp, ef)
    null_days = sum(1 for _, irr, _e in target if str(irr).strip() == "")
    print(f"{pk}: donor {donor} (same weather device), {len(target)} row(s) {d0}..{d1}, "
          f"{null_days} without irradiance, {len(todo)} fillable, {null_days - len(todo)} with no donor value")
    for d in sorted(todo):
        rec = todo[d]
        print(f"  {d}: irradiance {rec['irradiance_kwh_m2']}" + (f"  expected {rec['expected_kwh']}" if 'expected_kwh' in rec else ""))
    if not todo:
        return 0
    if not a.apply:
        print("dry run — add --apply to write")
        return 0
    date_key = lambda s: s    # noqa: E731 — ISO in, ISO out
    n = kpi_write.stamp("irradiance_kwh_m2", {(d, pk): r["irradiance_kwh_m2"] for d, r in todo.items()}, date_key)
    kpi_write.stamp("irradiance_source", {(d, pk): f"proxy:{donor}" for d in todo}, date_key)
    exp = {(d, pk): r["expected_kwh"] for d, r in todo.items() if "expected_kwh" in r}
    m = kpi_write.stamp("expected_kwh", exp, date_key) if exp else 0
    print(f"applied: irradiance on {n} row(s), expected_kwh on {m} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
