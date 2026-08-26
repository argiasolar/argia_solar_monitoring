"""Backfill vendor daily energy for a date range and repair
daily_production (pio06 only).

The vendors keep per-day history, so we CAN go back in time:
  Growatt    getMAXHistory per inverter per day (max eacToday)
  Huawei     getKpiStationDay (one call per station-month)
  SolarEdge  /site/energy timeUnit=DAY over the whole range

Dry-run prints the comparison table; --apply then (a) stores the vendor
values in vendor_counter_snapshot (ON CONFLICT DO NOTHING — real nightly
snapshots are never overwritten), (b) fills MISSING / raises UNDER days
in daily_production with provenance in status_note (never lowers — OVER
days are flagged for review), (c) recomputes reconciliation_daily.

Usage: recon_backfill.py --from-date 2026-08-01 --to-date 2026-08-26 [--apply]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from typing import Dict, Optional, Tuple

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.recon import backfill as B
from argia.recon import counters as C
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows
from argia.vendors.growatt_web_parser import (
    compute_day_total_kwh_from_history,
    parse_max_history,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("argia.recon_backfill")
GROWATT_DELAY_SEC = 0.25


def daterange(d0: dt.date, d1: dt.date):
    d = d0
    while d <= d1:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def fetch_growatt(plants, portfolio, dates) -> Dict[Tuple[str, str], Optional[float]]:
    out: Dict[Tuple[str, str], Optional[float]] = {}
    if not plants:
        return out
    from argia.vendors.growatt_web import GrowattWebClient
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not user or not pwd:
        LOG.warning("growatt: no credentials")
        return out
    client = GrowattWebClient(username=user, password=pwd)
    client.login()
    for p in plants:
        invs = [i.inverter_sn for i in portfolio.inverters_for(p.plant_key)]
        for d in dates:
            vals = []
            for sn in invs:
                try:
                    rows = parse_max_history(client.get_max_history(sn, d))
                    v = compute_day_total_kwh_from_history(rows)
                    if v is not None:
                        vals.append(v)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("growatt %s %s %s: %s", p.plant_key, sn, d,
                                str(e)[:120])
                time.sleep(GROWATT_DELAY_SEC)
            out[(p.plant_key, d)] = round(sum(vals), 3) if vals else None
            LOG.info("growatt %s %s: %s (%d/%d inverters)", p.plant_key, d,
                     out[(p.plant_key, d)], len(vals), len(invs))
    return out


def fetch_huawei(plants, d0: dt.date, d1: dt.date
                 ) -> Dict[Tuple[str, str], Optional[float]]:
    out: Dict[Tuple[str, str], Optional[float]] = {}
    if not plants:
        return out
    from argia.vendors.huawei import HuaweiClient
    user = os.environ.get("HUAWEI_USERNAME", "").strip()
    pwd = os.environ.get("HUAWEI_PASSWORD", "").strip()
    if not user or not pwd:
        LOG.warning("huawei: no credentials")
        return out
    client = HuaweiClient(username=user, password=pwd)
    client.login()
    codes = ",".join(p.site_id for p in plants if p.site_id)
    pk_by_code = {str(p.site_id): p.plant_key for p in plants}
    months = sorted({(d.year, d.month) for d in (d0, d1)}
                    | {((d0 + dt.timedelta(days=n)).year,
                        (d0 + dt.timedelta(days=n)).month)
                       for n in range(0, (d1 - d0).days + 1, 27)})
    for (yy, mm) in months:
        ct = int(dt.datetime(yy, mm, 15, 12, 0,
                             tzinfo=dt.timezone.utc).timestamp() * 1000)
        result = client._post_json("/getKpiStationDay",
                                   {"stationCodes": codes,
                                    "collectTime": ct})
        series = C.huawei_daily_series(result)
        for (code, d), v in series.items():
            pk = pk_by_code.get(code)
            if pk and d0.isoformat() <= d <= d1.isoformat():
                out[(pk, d)] = v
        LOG.info("huawei %04d-%02d: %d day-rows", yy, mm, len(series))
        time.sleep(1.0)
    return out


def fetch_solaredge(plants, d0: dt.date, d1: dt.date
                    ) -> Dict[Tuple[str, str], Optional[float]]:
    out: Dict[Tuple[str, str], Optional[float]] = {}
    from argia.vendors.solaredge import SolarEdgeClient
    for p in plants:
        key = os.environ.get(p.secret_api_name or "", "").strip()
        if not key or not p.site_id:
            continue
        client = SolarEdgeClient(api_key=key)
        try:
            resp = client._get_json(
                f"/site/{p.site_id}/energy",
                {"timeUnit": "DAY", "startDate": d0.isoformat(),
                 "endDate": d1.isoformat()})
            for d, v in C.solaredge_daily_series(resp).items():
                out[(p.plant_key, d)] = v
            LOG.info("solaredge %s: %d day-rows", p.plant_key,
                     len(C.solaredge_daily_series(resp)))
        except Exception as e:  # noqa: BLE001
            LOG.error("solaredge %s: %s", p.plant_key, str(e)[:200])
    return out


def stored_kpi(d0: dt.date, d1: dt.date) -> Dict[Tuple[str, str],
                                                 Optional[float]]:
    out: Dict[Tuple[str, str], Optional[float]] = {}
    for r in psql_rows(
            "SELECT plant_key, prod_date::text, energy_kwh"
            " FROM daily_production"
            f" WHERE prod_date BETWEEN DATE '{d0}' AND DATE '{d1}';"):
        if len(r) >= 3:
            try:
                out[(r[0], r[1])] = float(r[2]) if r[2] != "" else None
            except ValueError:
                out[(r[0], r[1])] = None
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="vendor-history backfill")
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument("--skip-brand", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    d0 = dt.date.fromisoformat(args.from_date)
    d1 = dt.date.fromisoformat(args.to_date)
    dates = list(daterange(d0, d1))

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    portfolio = load_portfolio(SheetsClient(sheet_id=sheet_id))
    active = [p for p in portfolio.active_plants()
              if (not args.plant_key or p.plant_key == args.plant_key)
              and (not args.skip_brand
                   or p.brand.upper() != args.skip_brand.upper())]

    vendor: Dict[Tuple[str, str], Optional[float]] = {}
    vendor.update(fetch_huawei(
        [p for p in active if p.brand.upper() == "HUAWEI"], d0, d1))
    vendor.update(fetch_solaredge(
        [p for p in active if p.brand.upper() == "SOLAREDGE"], d0, d1))
    vendor.update(fetch_growatt(
        [p for p in active if p.brand.upper() == "GROWATT"],
        portfolio, dates))

    kpi = stored_kpi(d0, d1)
    fixes = 0
    counts: Dict[str, int] = {}
    snap_rows = []
    LOG.info("%-6s %-11s %10s %10s %8s  %s", "PLANT", "DATE", "KPI",
             "VENDOR", "DELTA%", "CLASS")
    for p in active:
        for d in dates:
            v = vendor.get((p.plant_key, d))
            k = kpi.get((p.plant_key, d))
            cls, delta = B.classify_day(k, v)
            counts[cls] = counts.get(cls, 0) + 1
            if cls != B.CLASS_OK:
                LOG.info("%-6s %-11s %10s %10s %8s  %s", p.plant_key, d,
                         "—" if k is None else f"{k:,.1f}",
                         "—" if v is None else f"{v:,.1f}",
                         "—" if delta is None else f"{delta:+.2f}", cls)
            if v is not None:
                snap_rows.append(
                    f"('{p.plant_key}','{p.brand.upper()}',DATE '{d}',"
                    f"{v:.3f},'history-backfill')")
            if args.apply and cls in (B.CLASS_MISSING, B.CLASS_UNDER):
                psql_exec(B.build_fix_sql(p.plant_key, d, v, k))
                fixes += 1
    if args.apply and snap_rows:
        psql_exec(
            "INSERT INTO vendor_counter_snapshot (plant_key, vendor,"
            " snap_date, daily_kwh, note) VALUES\n"
            + ",\n".join(snap_rows)
            + "\nON CONFLICT (plant_key, snap_date) DO NOTHING;")
    LOG.info("SUMMARY: %s | corrections applied=%d apply=%s",
             counts, fixes, args.apply)

    if args.apply:
        import recon_snapshot as RS
        brand_by_plant = {p.plant_key: p.brand.upper() for p in active}
        total = 0
        for d in dates:
            total += RS.reconcile_day(d, brand_by_plant, dry_run=False)
        LOG.info("reconciliation_daily recomputed for %d dates "
                 "(%d rows)", len(dates), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
