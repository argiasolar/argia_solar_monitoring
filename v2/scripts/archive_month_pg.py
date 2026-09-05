#!/usr/bin/env python3
"""Monthly archive from PostgreSQL to Drive (v197) — replaces the
GitHub Action ``v2-archive-month`` (scripts/archive_month.py), which copied
one month of KPI_Daily / Alerts / Telemetry_Argia from the live sheet
into an archive spreadsheet and pruned the telemetry tab.

With the tabs retired the sources are the tables, and the archive is
plain CSV under the same Drive root the telemetry export already uses
(GOOGLE_ARCHIVE_FOLDER_ID):

    Monthly_Archive/<YYYY>/kpi_daily_<YYYY-MM>.csv        daily_production, every column
    Monthly_Archive/<YYYY>/alert_ledger_<YYYY-MM>.csv     alerts OPENED in the month
    (telemetry: the nightly telemetry_archive --source pg CSVs per plant/day)

Idempotent by name (an existing file is updated in place), verifies the
upload, deletes nothing. Pure helpers are unit-tested; the I/O is thin.

    PYTHONPATH=. python scripts/archive_month_pg.py                # DRY RUN, previous month
    PYTHONPATH=. python scripts/archive_month_pg.py --month 2026-08 --apply
EXIT: 0 ok   1 export failed   3 config
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import logging
import os
import sys
import tempfile
from typing import List, Tuple

from argia.core.job_log import apply_flag_write_if, instrument

LOG = logging.getLogger("argia.archive_month_pg")

ROOT_FOLDER = "Monthly_Archive"


def previous_month(today: dt.date) -> str:
    first = today.replace(day=1)
    last_prev = first - dt.timedelta(days=1)
    return last_prev.strftime("%Y-%m")


def month_bounds(ym: str) -> Tuple[str, str]:
    """('YYYY-MM-01', first day of the next month) — half-open. Pure."""
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return f"{y:04d}-{m:02d}-01", nxt.isoformat()


def kpi_sql(ym: str) -> str:
    lo, hi = month_bounds(ym)
    return (f"SELECT * FROM daily_production WHERE prod_date >= DATE '{lo}'"
            f" AND prod_date < DATE '{hi}' ORDER BY prod_date, plant_key;")


def alerts_sql(ym: str) -> str:
    lo, hi = month_bounds(ym)
    return (f"SELECT * FROM alert_ledger WHERE opened_utc >= '{lo}'"
            f" AND opened_utc < '{hi}' ORDER BY opened_utc, alert_id;")


def csv_row_count(text: str) -> int:
    return max(sum(1 for _ in csv.reader(io.StringIO(text))) - 1, 0)


def file_names(ym: str) -> List[Tuple[str, str]]:
    return [("kpi_daily_%s.csv" % ym, kpi_sql(ym)),
            ("alert_ledger_%s.csv" % ym, alerts_sql(ym))]


def _fetch_csv(sql: str) -> str:
    """v207.2: one psql wrapper (argia.store.pgq); the name stays as
    the seam tests monkeypatch."""
    from argia.store.pgq import psql_csv
    return psql_csv(sql, timeout=300)


def export(drive, base_id: str, ym: str, apply: bool) -> int:
    year_folder = None
    for name, sql in file_names(ym):
        text = _fetch_csv(sql)
        n = csv_row_count(text)
        if not apply:
            LOG.info("[dry-run] would upload %s (%d row(s))", name, n)
            continue
        if year_folder is None:
            root = drive.ensure_folder(base_id, ROOT_FOLDER)
            year_folder = drive.ensure_folder(root, ym[:4])
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(text)
            tmp = fh.name
        try:
            drive.upload_file(year_folder, name, tmp, "text/csv")
        finally:
            os.unlink(tmp)
        if drive.find_file(year_folder, name) is None:
            LOG.error("post-upload verify failed for %s", name)
            return 1
        LOG.info("archived %s (%d row(s))", name, n)
    return 0


@instrument("archive_month_pg", write_if=apply_flag_write_if)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--month", default=None, help="YYYY-MM (default: previous month)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    base = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if not base:
        LOG.error("GOOGLE_ARCHIVE_FOLDER_ID not set"); return 3
    ym = a.month or previous_month(dt.date.today())
    from argia.core.drive import DriveClient
    return export(DriveClient(), base, ym, a.apply)


if __name__ == "__main__":
    sys.exit(main())
