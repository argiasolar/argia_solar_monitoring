#!/usr/bin/env python3
"""Apply the inverter-counter rule (v206) to days already stored.

Tomasz 2026-09-05: "make a rule that we always prefer the inverter
counter — for now this is the only way we can prove it to the customer".
From v206 on, recon_snapshot writes every new day that way. This one-off
walks back over a window and raises ``daily_production.energy_kwh`` to
the day's reference — Σ of the inverters' own eToday counters as we
sampled them, the vendor plant daily only where it is higher — wherever
the stored value is below it by more than 1 %. Then the two existing
resyncs run (billable lifted to the corrected energy; PR re-derived).

Never lowers a value, never touches a CLOSED month (the fix SQL's WHERE
and the monthly freeze both guard that), never edits by hand.

    PYTHONPATH=. python scripts/inverter_counter_fix.py --days 30           # report only
    PYTHONPATH=. python scripts/inverter_counter_fix.py --days 30 --apply
EXIT: 0
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from typing import Dict, List, Optional, Tuple

from argia.recon import backfill as B
from argia.recon import engine as E

LOG = logging.getLogger("argia.inverter_counter_fix")

MX_DATE_SQL = "(ts_utc AT TIME ZONE 'America/Mexico_City')::date"


def _f(s) -> Optional[float]:
    try:
        return float(s) if s not in ("", None) else None
    except ValueError:
        return None


def plan(rows: List[Tuple[str, str, Optional[float], Optional[float], Optional[float], bool]]
         ) -> List[Dict]:
    """rows: (plant, date_iso, inverter_counter_kwh, vendor_daily_kwh,
    stored_energy_kwh, month_closed) -> the corrections to make. Pure."""
    out = []
    for pk, d, inv, vend, stored, closed in rows:
        ref, basis = E.reference_kwh(inv, vend)
        if ref is None:
            continue
        cls, delta = B.classify_day(stored, ref)
        if cls not in (B.CLASS_MISSING, B.CLASS_UNDER):
            continue
        out.append({"plant_key": pk, "date": d, "stored": stored, "reference": ref,
                    "basis": basis, "inverter_kwh": inv, "vendor_kwh": vend,
                    "closed": closed, "delta_pct": delta})
    return out


def load_rows(days: int) -> List[Tuple]:
    from argia.store.pgq import psql_rows
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    inv = {(r[0], r[1]): _f(r[2]) for r in psql_rows(
        "SELECT t.plant_key, t.d::text, sum(t.m) FROM ("
        f" SELECT plant_key, inverter_sn, {MX_DATE_SQL} AS d, max(etoday_kwh) AS m"
        f" FROM telemetry WHERE {MX_DATE_SQL} >= DATE '{since}'"
        " GROUP BY 1, 2, 3) t GROUP BY 1, 2;") if len(r) >= 3}
    vend = {(r[0], r[1]): _f(r[2]) for r in psql_rows(
        "SELECT plant_key, snap_date::text, daily_kwh FROM vendor_counter_snapshot"
        f" WHERE snap_date >= DATE '{since}';") if len(r) >= 3}
    stored = {(r[0], r[1]): _f(r[2]) for r in psql_rows(
        "SELECT plant_key, prod_date::text, energy_kwh FROM daily_production"
        f" WHERE prod_date >= DATE '{since}';") if len(r) >= 3}
    closed = {(r[0], r[1][:7]) for r in psql_rows(
        "SELECT plant_key, ref_month::text FROM reconciliation_monthly"
        " WHERE closed_at IS NOT NULL;") if len(r) >= 2}
    # today (MX) is still running — its counters are partial and its
    # daily_production row does not exist yet; the nightly recon owns it
    from argia.core.time_utils import MX_TZ
    today_mx = dt.datetime.now(MX_TZ).date().isoformat()
    keys = sorted(k for k in set(inv) | set(vend) | set(stored) if k[1] < today_mx)
    return [(pk, d, inv.get((pk, d)), vend.get((pk, d)), stored.get((pk, d)),
             (pk, d[:7]) in closed) for pk, d in keys]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fixes = plan(load_rows(a.days))
    open_fixes = [f for f in fixes if not f["closed"]]
    print(f"inverter-counter rule over the last {a.days} day(s): {len(fixes)} day(s) below the "
          f"reference, {len(open_fixes)} in open months")
    for f in fixes:
        print(f"  {f['date']} {f['plant_key']:5s} stored={f['stored'] if f['stored'] is not None else '-':>9} "
              f"-> {f['reference']:9.1f} ({f['basis']}; inverters={f['inverter_kwh']} vendor={f['vendor_kwh']})"
              + ("  CLOSED month — untouched" if f["closed"] else ""))
    if not a.apply:
        print("(report only)")
        return 0
    from argia.store.pgq import psql_exec
    # the two resyncs always run on --apply: a KPI re-stamp after a heal
    # can leave billable below energy (MEX1 2026-09-01) even with no new
    # correction to make
    for f in open_fixes:
        detail = (f"{f['basis']}; vendor plant daily {f['vendor_kwh']:.1f}"
                  if f["vendor_kwh"] is not None and f["basis"] == E.BASIS_INV else f["basis"])
        psql_exec(B.build_fix_sql(f["plant_key"], f["date"], f["reference"], f["stored"],
                                  basis=f["basis"], detail=detail))
        LOG.info("%s %s: %s -> %.1f (%s)", f["date"], f["plant_key"], f["stored"],
                 f["reference"], f["basis"])
    psql_exec(B.build_billable_resync_sql())
    psql_exec(B.build_pr_resync_sql())
    print(f"applied {len(open_fixes)} correction(s); billable and PR resynced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
