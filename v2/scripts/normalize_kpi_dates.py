#!/usr/bin/env python3
"""Argia_Mont — one-time repair: make KPI_Daily.date_iso uniformly a real date.

Old RAW inserts stored ``date_iso`` as text; updates stored it as a real date.
The mixed column breaks the downstream QUERY(IMPORTRANGE) in ARGIA_Solar (QUERY
infers one type per column and nulls the minority), so text-date rows drop out of
DailyData_v2 / Reconcile until reformatted by hand. This converts the text cells
to real dates. Touches only the date_iso column — nothing else.

The write path is already fixed (inserts use USER_ENTERED), so this is a one-off
to clean up rows written before that fix.

USAGE
    PYTHONPATH=. python scripts/normalize_kpi_dates.py            # DRY RUN (default)
    PYTHONPATH=. python scripts/normalize_kpi_dates.py --apply    # actually write

EXIT CODES
    0  ran cleanly (dry-run or applied)
    3  config error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.archive.kpi_daily import normalize_kpi_date_iso
from argia.core.sheets import SheetsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.normalize_kpi_dates")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write the conversions (default is a dry run)",
    )
    args = parser.parse_args(argv)
    dry_run = not args.apply

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:  # noqa: BLE001
        log.error("SheetsClient failed: %s", e)
        return 3

    if dry_run:
        log.info("DRY RUN — no cells will be written. Re-run with --apply to write.")

    result = normalize_kpi_date_iso(sheets, dry_run=dry_run)
    log.info("Result: scanned=%d text_dates=%d %s=%d",
             result["scanned"], result["text_dates"],
             "would_fix" if dry_run else "fixed", result["fixed"])
    if dry_run and result["fixed"]:
        log.info("Re-run with --apply to convert these %d text dates to real dates.",
                 result["fixed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
