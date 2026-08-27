"""Argia_Mont — direct KPI sheet -> PostgreSQL mirror (P1-9).

Runs on pio06 at 06:15 MX (argia-kpimirror.timer), right after argia-kpi
stamps yesterday into KPI_Daily. Upserts the hot window into
daily_production with the protected semantics in argia.store.kpi_mirror
— minutes of lag instead of the GitHub round-trip's hours. The GitHub
export + argia-sync (14:10 UTC) stay on as the verifying canary until
this has soaked; then they retire.

USAGE
    PYTHONPATH=. python scripts/kpi_pg_mirror.py [--days 25] [--dry-run]

EXIT CODES
    0 mirrored (or clean dry-run)   1 nothing to mirror   2 config error
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ
from argia.kpi.reconcile import date_key
from argia.store import pg_mirror
from argia.store.kpi_mirror import build_upsert_sql, normalize_rows
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.kpi_pg_mirror")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KPI sheet -> PG mirror")
    parser.add_argument("--days", type=int, default=25,
                        help="window size (guards against rewriting history)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.error("ARGIA_PG_MIRROR not enabled")
        return 2
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 2

    records = SheetsClient(sheet_id=sheet_id).read_table("KPI_Daily", "A1:AZ")
    min_date = (dt.datetime.now(MX_TZ).date()
                - dt.timedelta(days=args.days)).isoformat()
    rows = normalize_rows(records, date_key, min_date=min_date)
    if not rows:
        LOG.warning("no KPI rows in the %d-day window", args.days)
        return 1
    sql = build_upsert_sql(rows)
    if args.dry_run:
        LOG.info("dry-run: %d rows, %d bytes of SQL — not applied",
                 len(rows), len(sql))
        return 0
    psql_exec(sql)
    r = psql_rows("SELECT count(*), max(prod_date) FROM daily_production;")
    LOG.info("mirrored %d rows (window >= %s); daily_production now %s",
             len(rows), min_date, r[0] if r else "?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
