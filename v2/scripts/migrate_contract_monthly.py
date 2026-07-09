"""Create and populate the Contract_Monthly tab; add the O&M column.

Seeds the commercial-expectations table from v1 (ARGIA_Solar export of
2026-07-09) plus the live Design_Monthly values:

  * PPA plants: contract_kwh + tariff_mxn per month over the full
    contract horizon (v1 ContractData — escalations already priced in),
    design_kwh where known (2026, from Design_Monthly)
  * LaaS projects: fixed_income_ccy per month (USD-indexed fees:
    LOAX1 26,750.00 / LGTO1 15,233.00), revenue-bearing months only
  * 1,235 rows, 2024-01 .. 2043-12

Also appends the ``om_cost_monthly_mxn`` header to the Plants tab if
absent (values stay blank — manual entry, average monthly O&M per
plant).

The old Design_Monthly tab is NOT deleted; the design loader now reads
Contract_Monthly first and falls back to it. Delete it manually once a
KPI run has confirmed "Design baseline loaded ... from Contract_Monthly"
in the log.

Usage:
    PYTHONPATH=. python scripts/migrate_contract_monthly.py          # dry-run
    PYTHONPATH=. python scripts/migrate_contract_monthly.py --apply  # write
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.core.sheets import SheetsClient          # noqa: E402
from argia.finance.contract import (                # noqa: E402
    CONTRACT_HEADER, CONTRACT_TAB,
)

LOG = logging.getLogger("migrate_contract")

SEED = (Path(__file__).resolve().parents[1] / "data" / "finance"
        / "contract_monthly_seed.csv")
PLANTS_TAB = "Plants"
OM_COLUMN = "om_cost_monthly_mxn"


def read_seed() -> list:
    with open(SEED, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in CONTRACT_HEADER
                   if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit("%s: missing columns %s" % (SEED.name, missing))
        return [[row.get(col, "") for col in CONTRACT_HEADER]
                for row in reader]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true",
                        help="write to the live sheet (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = read_seed()
    plants = sorted({r[0] for r in rows})
    years = sorted({r[1] for r in rows})
    laas = sorted({r[0] for r in rows if r[6] not in ("", None)})
    LOG.info("seed: %d rows, plants=%s, years %s..%s, LaaS fee rows for %s",
             len(rows), ",".join(plants), years[0], years[-1],
             ",".join(laas))

    if not args.apply:
        LOG.info("[dry-run] would create/populate %s (%d rows) and append "
                 "'%s' header to %s if absent", CONTRACT_TAB, len(rows),
                 OM_COLUMN, PLANTS_TAB)
        LOG.info("re-run with --apply to write")
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    sheets = SheetsClient(sheet_id=sheet_id)

    # 1) Contract_Monthly
    sheets.ensure_tab(CONTRACT_TAB)
    sheets.write_header_row(CONTRACT_TAB, CONTRACT_HEADER)
    sheets.freeze_and_bold_header(CONTRACT_TAB)
    stats = sheets.upsert_rows(CONTRACT_TAB, rows,
                               natural_key_columns=[0, 1, 2])
    LOG.info("%s: %s", CONTRACT_TAB, stats)
    got = len(sheets.read_table(CONTRACT_TAB))
    if got < len(rows):
        LOG.error("%s: verification failed — %d rows read back, expected "
                  ">= %d", CONTRACT_TAB, got, len(rows))
        return 1
    LOG.info("%s: verified %d rows", CONTRACT_TAB, got)

    # 2) Plants: append om_cost_monthly_mxn header if absent
    header = [str(c or "").strip()
              for c in sheets.read_range(PLANTS_TAB, "A1:ZZ1")[0]]
    if OM_COLUMN in header:
        LOG.info("%s: '%s' column already present", PLANTS_TAB, OM_COLUMN)
    else:
        new_header = [c for c in header if c] + [OM_COLUMN]
        sheets.write_header_row(PLANTS_TAB, new_header)
        LOG.info("%s: appended '%s' header (values blank — fill manually "
                 "with average monthly O&M per plant, MXN)", PLANTS_TAB,
                 OM_COLUMN)

    LOG.info("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
