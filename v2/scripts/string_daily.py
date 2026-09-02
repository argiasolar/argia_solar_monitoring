"""Collect per-MPPT / per-string daily stats for Growatt plants (pio06).

Nightly (argia-strings.timer, after MX sunset): one getMAXHistory pass
per Growatt inverter for the current MX day, reduced by
argia.kpi.strings.channel_day_stats and upserted into ``string_daily``.
This is the raw material for the solar director's string-level
analysis (relative index, shared-MPPT deficit, StrUnmatch evidence) —
collected from the same endpoint the pipeline already calls, so the
single-holder Growatt session rule is untouched.

Pages through history (80 rows/page) until haveNext is false — one
page only covers ~7 hours and silently truncating the morning was the
bug this comment exists to prevent.

Writes by default (it is a collector, like telemetry); --dry-run
computes and logs without touching PG.
Usage: string_daily.py [--date YYYY-MM-DD] [--plant-key K] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ
from argia.kpi.strings import ENSURE_TABLE_SQL, channel_day_stats, upsert_sqls
from argia.store import pg_mirror
from argia.vendors.growatt_web_parser import parse_max_history

LOG = logging.getLogger("argia.string_daily")
DELAY_SEC = 0.25
MAX_PAGES = 12


def fetch_all_pages(client, sn: str, date_iso: str):
    """Every history row for one inverter-day, across pages."""
    rows = []
    start = 0
    for _ in range(MAX_PAGES):
        resp = client.get_max_history(sn, date_iso, start=start)
        page = parse_max_history(resp)
        rows.extend(page)
        obj = resp.get("obj") if isinstance(resp, dict) else None
        have_next = bool(obj.get("haveNext")) if isinstance(obj, dict) else False
        if not have_next or not page:
            break
        start += 1
        time.sleep(DELAY_SEC)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="per-string daily collector")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (MX);"
                        " default: current MX date")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    from argia.store.pgq import psql_exec

    date_iso = args.date or dt.datetime.now(MX_TZ).date().isoformat()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    portfolio = load_portfolio(SheetsClient(sheet_id=sheet_id))
    plants = [p for p in portfolio.plants.values()
              if p.brand.upper() == "GROWATT" and p.active
              and (not args.plant_key or p.plant_key == args.plant_key)]
    if not plants:
        LOG.info("no Growatt plants selected")
        return 0

    from argia.vendors.growatt_web import GrowattWebClient
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not user or not pwd:
        LOG.error("no Growatt credentials")
        return 1
    client = GrowattWebClient(username=user, password=pwd)
    client.login()

    if not args.dry_run:
        psql_exec(ENSURE_TABLE_SQL)

    total_rows = 0
    failures = 0
    for p in plants:
        for inv in portfolio.inverters_for(p.plant_key):
            sn = inv.inverter_sn
            try:
                rows = fetch_all_pages(client, sn, date_iso)
            except Exception as e:  # noqa: BLE001
                LOG.warning("%s/%s: fetch failed: %s", p.plant_key, sn,
                            str(e)[:120])
                failures += 1
                continue
            stats = channel_day_stats(rows)
            n_m, n_s = len(stats["mppt"]), len(stats["string"])
            e_sum = sum(m.get("energy_kwh") or 0
                        for m in stats["mppt"].values())
            LOG.info("%s/%s %s: %d samples, %d MPPT (%.1f kWh), %d strings"
                     "%s", p.plant_key, sn, date_iso, stats["samples"],
                     n_m, e_sum, n_s,
                     " [StrUnmatch]" if stats["flags"]["str_unmatch"]
                     else "")
            if not args.dry_run and (n_m or n_s):
                for sql in upsert_sqls(p.plant_key, sn, date_iso, stats):
                    psql_exec(sql)
                total_rows += n_m + n_s
            time.sleep(DELAY_SEC)
    LOG.info("DONE %s: %d channel rows %s, %d inverter(s) failed",
             date_iso, total_rows,
             "written" if not args.dry_run else "computed (dry-run)",
             failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
