"""Append the v74 visibility columns to the Plants tab.

Adds four headers (values left BLANK — blank means today's behavior,
so this migration is a behavioral no-op until you edit cells):

  portfolio           label: PPA / CAPEX / PROLOGIS (blank = PPA).
                      Pure grouping — controls nothing.
  show_dashboard      FALSE hides the plant from the performance
                      dashboard. Blank/TRUE = visible.
  show_daily_report   FALSE hides it from the daily PDF (both
                      editions). Blank/TRUE = visible.
  show_financial      FALSE hides it from the financial web report and
                      investor PDF. Blank/TRUE = visible.

The `active` column is untouched and remains the ONLY flag that stops
telemetry/KPI/alerts — report flags can hide a plant but can never
stop its data.

Usage:
    PYTHONPATH=. python scripts/migrate_plant_flags.py           # dry-run
    PYTHONPATH=. python scripts/migrate_plant_flags.py --apply   # write
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.core.sheets import SheetsClient          # noqa: E402

LOG = logging.getLogger("migrate_plant_flags")

PLANTS_TAB = "Plants"
NEW_COLUMNS = ["portfolio", "show_dashboard", "show_daily_report",
               "show_financial"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true",
                        help="write to the live sheet (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.apply:
        LOG.info("[dry-run] would append missing header(s) %s to %s "
                 "(values blank = current behavior)", NEW_COLUMNS,
                 PLANTS_TAB)
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    sheets = SheetsClient(sheet_id=sheet_id)
    header = [str(c or "").strip()
              for c in sheets.read_range(PLANTS_TAB, "A1:ZZ1")[0]]
    present = {h.lower() for h in header if h}
    missing = [c for c in NEW_COLUMNS if c not in present]
    if not missing:
        LOG.info("%s: all v74 columns already present", PLANTS_TAB)
        return 0
    sheets.write_header_row(PLANTS_TAB,
                            [c for c in header if c] + missing)
    LOG.info("%s: appended header(s) %s — fill per plant as needed; "
             "blank keeps today's behavior", PLANTS_TAB, missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
