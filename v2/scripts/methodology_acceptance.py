#!/usr/bin/env python3
"""Regression check of the report methodology against real worked examples (v392).

    methodology_acceptance.py [--file /opt/argia/acceptance/methodology.json]

Arturo's "Reporting methodology corrections" v0.1 gives worked examples with customer figures
(availability of named inverters, contract percentages, temperatures). Those numbers never go
in the repo (public): they live in a server-only JSON file, a list of checks such as

    {"name": "R1 Vitalmex inverter 1", "type": "inverter_availability", "plant": "MEX2",
     "serial": "GR...", "from": "2026-11-01", "to": "2026-11-22", "expect": 94.0, "tol": 1.0}

Types: inverter_availability / plant_availability (the capacity-weighted inverters, which must
also equal the plant's own daily values over the window) / month_vs_contract (the partial month,
prorated) / closed_months_vs_contract (sum of closed months) / thermal_dt_peer_max (worst peer
deviation in the window must stay at or below ``max``) / epi600_window (v395: EPI-600 of the
window from the EPI engine's day cache, every day computed). Read-only. Prints PASS/FAIL per check;
exit 1 when any check fails, 2 when the file is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.kpi.availability import window as plant_window      # noqa: E402
from argia.report import methodology as MC                      # noqa: E402

DEFAULT = "/opt/argia/acceptance/methodology.json"
Fetch = Callable[[str], List[List[str]]]


def _f(x) -> Optional[float]:
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def _q(s: str) -> str:
    return s.replace("'", "''")


def measure(c: dict, fetch: Fetch) -> Tuple[Optional[float], str]:
    """(measured value in %, or K for thermal; detail). Pure given ``fetch``."""
    t, pk = c["type"], _q(c["plant"])
    if t in ("inverter_availability", "plant_availability"):
        rows = [(r[0], int(_f(r[1]) or 0), int(_f(r[2]) or 0), int(_f(r[3]) or 0)) for r in fetch(
            "SELECT inverter_sn, up_steps, down_steps, unknown_steps FROM inverter_availability_daily"
            f" WHERE plant_key = '{pk}' AND prod_date BETWEEN DATE '{c['from']}' AND DATE '{c['to']}';")]
        rated = {r[0]: _f(r[1]) or 0.0 for r in fetch(f"SELECT inverter_sn, rated_kw FROM inverter WHERE plant_key = '{pk}';")}
        per, plant = MC.availability_window(rows, rated)
        if t == "inverter_availability":
            a = per.get(c["serial"], (None, None))[0]
            return (None if a is None else 100 * a), f"{len(rows)} inverter-days"
        days = [(_f(r[0]), _f(r[1])) for r in fetch(
            "SELECT availability, avail_coverage FROM daily_production"
            f" WHERE plant_key = '{pk}' AND prod_date BETWEEN DATE '{c['from']}' AND DATE '{c['to']}';")]
        own, _cov = plant_window(days, len(days))
        if plant[0] is None:
            return None, "no inverter steps"
        gap = abs(100 * plant[0] - 100 * (own or 0))
        return 100 * plant[0], f"daily plant values give {100 * (own or 0):.2f}% (gap {gap:.2f} pt)" + (
            " - MISMATCH" if own is None or gap > c.get("max_gap", 0.1) else "")
    if t in ("month_vs_contract", "closed_months_vs_contract"):
        months = [c["month"]] if t == "month_vs_contract" else c["months"]
        act = con = 0.0
        for ym in months:
            y, m = int(ym[:4]), int(ym[5:7])
            a = _f((fetch(f"SELECT sum(energy_kwh) FROM daily_production WHERE plant_key = '{pk}'"
                          f" AND extract(year FROM prod_date) = {y} AND extract(month FROM prod_date) = {m}"
                          + (f" AND prod_date <= DATE '{c['asof']}'" if t == "month_vs_contract" else "") + ";")
                    or [[""]])[0][0]) or 0.0
            k = _f((fetch(f"SELECT contract_kwh FROM contract_monthly WHERE plant_key = '{pk}' AND year = {y}"
                          f" AND month = {m};") or [[""]])[0][0]) or 0.0
            if t == "month_vs_contract":
                basis, pct, partial = MC.month_vs_contract(ym, a, k, c["asof"])
                return pct, f"actual {a:,.0f} kWh vs contract {basis:,.0f} kWh" + (" (prorated)" if partial else "")
            act += a
            con += k
        return (act / con * 100 if con else None), f"actual {act:,.0f} kWh vs contract {con:,.0f} kWh"
    if t == "epi600_window":
        import datetime as dt
        from argia.epi import config as EPC
        from argia.epi import live as EPL
        d0, d1 = dt.date.fromisoformat(c["from"]), dt.date.fromisoformat(c["to"])
        fp = EPL.fingerprint(EPC.EPI_DIR, EPC.LAYOUT_DIR, c["plant"])
        days, d = {}, d0
        while d <= d1:
            p = EPL.read_cached(EPL.CACHE_DIR, c["plant"], d, fp)
            days[d.isoformat()] = p["tallies"] if p else None
            d += dt.timedelta(days=1)
        v, n = MC._epi(days.values())
        done = sum(1 for x in days.values() if x)
        return (None if v is None else 100 * v), f"{done}/{len(days)} days computed, {n} samples >= 600 W/m2" + (
            " - MISMATCH (days missing)" if done < len(days) else "")
    if t == "thermal_dt_peer_max":
        r = fetch("SELECT max(dt_peer_peak_c) FROM thermal_daily"
                  f" WHERE plant_key = '{pk}' AND prod_date BETWEEN DATE '{c['from']}' AND DATE '{c['to']}';")
        return (_f(r[0][0]) if r else None), "worst 5-min deviation at steady load"
    raise ValueError(f"unknown check type {t}")


def judge(c: dict, value: Optional[float], detail: str) -> bool:
    if value is None or "MISMATCH" in detail or "error:" in detail:
        return False
    if c["type"] == "thermal_dt_peer_max":
        return value <= float(c["max"])
    return abs(value - float(c["expect"])) <= float(c.get("tol", 0.5))


def run(checks: List[dict], fetch: Fetch, out=print) -> int:
    failed = 0
    for c in checks:
        try:
            v, detail = measure(c, fetch)
        except Exception as e:      # noqa: BLE001 - a broken check is a failed check, never a crash
            v, detail = None, f"error: {e}"
        ok = judge(c, v, detail)
        failed += not ok
        want = (f"<= {c.get('max')}" if c.get("type") == "thermal_dt_peer_max"
                else f"{c.get('expect')} +/- {c.get('tol', 0.5)}")
        out(f"{'PASS' if ok else 'FAIL'}  {c.get('name', '?')}: {'-' if v is None else f'{v:.2f}'} (want {want}) - {detail}")
    out(f"{len(checks) - failed}/{len(checks)} checks pass")
    return failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=DEFAULT)
    a = ap.parse_args(argv)
    if not os.path.exists(a.file):
        print(f"no expectations file at {a.file}")
        return 2
    from argia.store.pgq import psql_rows
    with open(a.file, encoding="utf-8") as fh:
        checks = json.load(fh)
    return 1 if run(checks, psql_rows) else 0


if __name__ == "__main__":
    sys.exit(main())
