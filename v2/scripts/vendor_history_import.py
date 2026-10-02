"""Import a plant's daily energy from the vendor's own history (pio06).

v285 (Tomasz, 2026-10-01): "do the SMS so it matches the total we have
currently in Growatt". The monitoring knows SMS only from 10 Jul 2026; Growatt
has it from 5 Apr 2025. This writes the vendor's days into daily_production so
every month equals the vendor's month total (method: argia.recon.history_import).

Growatt (the web panel's month and year charts) and, since v288, SolarEdge
(site energy per DAY in one-year requests and per MONTH; key from the plant's
secret_api_name).

  dry run (default - prints the plan, writes nothing):
    scripts/vendor_history_import.py --plant-key MEX3 --from 2025-04-05 --to 2026-09-30
  from a saved capture instead of asking Growatt:
    ... --capture /root/sms_probe/growatt_sms_months.json
  write it:
    ... --apply

Days already stored at the target (to 0.001 kWh) are left alone; NEW days are inserted
(source v2), others updated with the energy-proportional columns rescaled.
Every written row carries the vendor-counter note, so the morning KPI run
never overwrites it. Re-running is a no-op.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from typing import Dict, Optional

from argia.recon import history_import as H
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.vendor_history_import")
DELAY_SEC = 0.5


def months_between(d0: dt.date, d1: dt.date):
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def fetch_growatt_capture(site_id: str, d0: dt.date, d1: dt.date) -> dict:
    """{'YYYY-MM': [day kWh...], 'year_YYYY': [12 month kWh], 'captured_utc'}."""
    from argia.vendors.growatt_web import GrowattWebClient
    from argia.vendors.growatt_web_parser import parse_energy_series
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError("GROWATT_USERNAME / GROWATT_PASSWORD not set")
    c = GrowattWebClient(username=user, password=pwd)
    c.login()
    cap: dict = {}
    for ym in months_between(d0, d1):
        s = parse_energy_series(c.get_max_month_chart(site_id, ym))
        if s is None:
            raise RuntimeError(f"Growatt month chart {ym}: no data")
        cap[ym] = s
        time.sleep(DELAY_SEC)
    for y in range(d0.year, d1.year + 1):
        s = parse_energy_series(c.get_max_year_chart(site_id, y))
        if s is None:
            raise RuntimeError(f"Growatt year chart {y}: no data")
        cap[f"year_{y}"] = s
        time.sleep(DELAY_SEC)
    cap["captured_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return cap


SE_DAY_CHUNK = 365    # SolarEdge: timeUnit=DAY is limited to one year per request


def solaredge_capture(day_series: dict, month_series: dict, d0: dt.date, d1: dt.date) -> dict:
    """The Growatt-shaped capture from SolarEdge series (PURE, v288).
    ``day_series`` {'YYYY-MM-DD': kWh|None}, ``month_series`` {'YYYY-MM-01': kWh|None}.
    A day the vendor does not know (None / absent) stays None (no row is
    written for it); the month total is the vendor's month value."""
    import calendar
    cap: dict = {}
    for ym in months_between(d0, d1):
        y, m = int(ym[:4]), int(ym[5:7])
        n = calendar.monthrange(y, m)[1]
        vals = [day_series.get(f"{ym}-{i:02d}") for i in range(1, n + 1)]
        cap[ym] = [None if v is None else float(v) for v in vals]      # unknown stays unknown
    for y in range(d0.year, d1.year + 1):
        cap[f"year_{y}"] = [float(month_series.get(f"{y}-{m:02d}-01") or 0.0) for m in range(1, 13)]
    return cap


def fetch_solaredge_capture(site_id: str, api_key: str, d0: dt.date, d1: dt.date) -> dict:
    from argia.recon.counters import solaredge_daily_series
    from argia.vendors.solaredge import SolarEdgeClient
    if not api_key:
        raise RuntimeError("SolarEdge API key not set for this plant")
    c = SolarEdgeClient(api_key=api_key)
    days: dict = {}
    a = d0
    while a <= d1:
        b = min(a + dt.timedelta(days=SE_DAY_CHUNK - 1), d1)
        days.update(solaredge_daily_series(c._get_json(
            f"/site/{site_id}/energy", {"timeUnit": "DAY", "startDate": a.isoformat(), "endDate": b.isoformat()})))
        a = b + dt.timedelta(days=1)
        time.sleep(DELAY_SEC)
    months = solaredge_daily_series(c._get_json(
        f"/site/{site_id}/energy", {"timeUnit": "MONTH", "startDate": d0.replace(day=1).isoformat(),
                                    "endDate": d1.isoformat()}))
    cap = solaredge_capture(days, months, d0, d1)
    cap["captured_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return cap


def split_capture(cap: dict):
    """(month_days, month_totals, day_sums) from a capture."""
    month_days = {k: v for k, v in cap.items() if len(k) == 7 and k[4] == "-"}
    month_totals: Dict[str, float] = {}
    for k, v in cap.items():
        if k.startswith("year_"):
            y = k[5:]
            for i, val in enumerate(v):
                month_totals[f"{y}-{i + 1:02d}"] = float(val or 0.0)
    day_sums = {k: round(sum(float(x or 0) for x in v), 3) for k, v in month_days.items()}
    # (None = a day the vendor does not know; kept as None for targets_for_range)
    return month_days, month_totals, day_sums


def stored_days(pk: str, d0: dt.date, d1: dt.date) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for r in psql_rows("SELECT prod_date::text, energy_kwh FROM daily_production"
                       f" WHERE plant_key = '{pk}' AND prod_date BETWEEN DATE '{d0}' AND DATE '{d1}';"):
        out[r[0]] = float(r[1]) if len(r) > 1 and r[1] != "" else None
    return out


def _f(v) -> str:
    return " - " if v is None else f"{v:,.1f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="vendor history import into daily_production")
    ap.add_argument("--plant-key", required=True)
    ap.add_argument("--from", dest="d0", required=True)
    ap.add_argument("--to", dest="d1", required=True)
    ap.add_argument("--capture", default=None, help="read the vendor history from this JSON instead of the vendor")
    ap.add_argument("--save", default=None, help="write the fetched vendor history to this JSON")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    pk = args.plant_key.strip().upper()
    d0, d1 = dt.date.fromisoformat(args.d0), dt.date.fromisoformat(args.d1)
    if d1 < d0:
        LOG.error("--to before --from")
        return 2
    rows = psql_rows(f"SELECT brand, site_id, coalesce(secret_api_name, '') FROM plant WHERE plant_key = '{pk}';")
    if not rows:
        LOG.error("unknown plant %s", pk)
        return 2
    brand, site_id = rows[0][0].upper(), rows[0][1]
    key_env = rows[0][2] if len(rows[0]) > 2 else ""
    vendor = brand.title()
    if args.capture:
        with open(args.capture, encoding="utf-8") as fh:
            cap = json.load(fh)
        src = f"capture {os.path.basename(args.capture)} {cap.get('captured_utc', '')}".strip()
    else:
        if brand == "GROWATT":
            cap = fetch_growatt_capture(site_id, d0, d1)
        elif brand == "SOLAREDGE":
            cap = fetch_solaredge_capture(site_id, os.environ.get(key_env, "").strip(), d0, d1)
        else:
            LOG.error("%s is %s - only Growatt and SolarEdge history are supported", pk, brand)
            return 2
        src = f"{vendor} live {cap['captured_utc']} UTC"
        if args.save:
            with open(args.save, "w", encoding="utf-8") as fh:
                json.dump(cap, fh)
    month_days, month_totals, day_sums = split_capture(cap)
    how = ("day value from the month chart, month reconciled to the year chart" if brand == "GROWATT"
           else "day value from the site energy per day, month reconciled to the site energy per month")
    tag = f"{vendor} history import {dt.date.today()}: {how}; {src}"

    targets = H.targets_for_range(month_days, month_totals, d0, d1)
    stored = stored_days(pk, d0, d1)
    changes = H.plan_changes(targets, stored)
    summ = H.month_summary(targets, stored, month_totals, day_sums)

    print(f"{'MONTH':8} {'VENDOR MONTH':>13} {'VENDOR DAYS':>12} {'STORED NOW':>11} {'AFTER':>11} {'NEW':>4} {'UPD':>4}")
    for r in summ:
        print(f"{r['month']:8} {_f(r['vendor_month']):>13} {_f(r['vendor_days']):>12} {_f(r['stored']):>11}"
              f" {_f(r['after']):>11} {r['new']:>4} {r['update']:>4}")
    tot_after = sum(targets.values())
    tot_vendor = sum(r["vendor_month"] or 0.0 for r in summ)
    n_new = sum(c.action == "NEW" for c in changes)
    n_upd = sum(c.action == "UPDATE" for c in changes)
    print(f"TOTAL    vendor months {tot_vendor:,.1f} | stored now {sum(float(v or 0) for v in stored.values()):,.1f}"
          f" | after {tot_after:,.1f} | new {n_new} | update {n_upd} | same {len(changes) - n_new - n_upd}")
    for c in changes:
        if c.action == "UPDATE" and abs((c.stored_kwh or 0) - c.target_kwh) > 5:
            print(f"  update {c.day}: {_f(c.stored_kwh)} -> {c.target_kwh:,.1f}")

    if not args.apply:
        print("DRY RUN: nothing written (add --apply)")
        return 0
    if n_new or n_upd:
        psql_exec(H.build_sql(pk, changes, tag), timeout=300)
    after = stored_days(pk, d0, d1)
    bad = []
    for r in summ:
        got = sum(float(after.get(d) or 0) for d in targets if d[:7] == r["month"])
        if abs(got - r["after"]) > 0.01:
            bad.append((r["month"], got, r["after"]))
    missing = [d for d in targets if d not in after]
    if bad or missing:
        print(f"VERIFY FAILED: months {bad} missing days {missing[:5]}")
        return 1
    print(f"VERIFY OK: {len(targets)} days, every month equals its target; total {sum(float(after[d] or 0) for d in targets):,.1f} kWh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
