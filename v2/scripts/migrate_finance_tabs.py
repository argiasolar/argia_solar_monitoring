"""Create and populate the Loans + Loan_Schedule tabs from the seed CSVs.

One-time migration of v1's financial records (ARGIA_Solar LoanPayments,
export of 2026-07-08) into Argia_Mont_v2. The seed lives in the repo at
v2/data/finance/ so the migration is deterministic and reviewable.

What the seed encodes (and v1 did not make explicit):
  * loan identity: (plant, principal) — SLP1 has two loans, LOAX1 has
    one (its month-1 "1/83" denominator was a typo; installments run
    1..82 continuously)
  * USD loans: payment_ccy = sum of the facility's three USD components;
    payment_mxn = payment_ccy * xr (verified exact on all 154 USD rows)
  * future USD months carry v1's projected rate (last known, 17.98) —
    projections, not commitments

Usage:
    PYTHONPATH=. python scripts/migrate_finance_tabs.py            # dry-run
    PYTHONPATH=. python scripts/migrate_finance_tabs.py --apply    # write

Exit codes: 0 ok (dry-run printed or write verified), 1 failure.
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
from argia.finance.loans import (                   # noqa: E402
    LOANS_HEADER, LOANS_TAB, SCHEDULE_HEADER, SCHEDULE_TAB,
)

LOG = logging.getLogger("migrate_finance")

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "finance"
LOANS_CSV = DATA_DIR / "loans_seed.csv"
SCHEDULE_CSV = DATA_DIR / "loan_schedule_seed.csv"


def read_seed(path: Path, header: list) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in header if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit("%s: missing columns %s" % (path.name, missing))
        return [[row.get(col, "") for col in header] for row in reader]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true",
                        help="write to the live sheet (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    loans_rows = read_seed(LOANS_CSV, LOANS_HEADER)
    sched_rows = read_seed(SCHEDULE_CSV, SCHEDULE_HEADER)

    # sanity gates — refuse to migrate a seed that doesn't add up
    n_loans = len(loans_rows)
    n_sched = len(sched_rows)
    lids = {r[0] for r in loans_rows}
    orphans = sorted({r[0] for r in sched_rows} - lids)
    if orphans:
        LOG.error("schedule references unknown loan_ids: %s", orphans)
        return 1
    LOG.info("seed: %d loans, %d schedule rows, loan_ids consistent",
             n_loans, n_sched)

    if not args.apply:
        LOG.info("[dry-run] would create/populate:")
        LOG.info("  %s (%d rows): %s", LOANS_TAB, n_loans,
                 ", ".join(sorted(lids)))
        months = sorted({r[2] for r in sched_rows})
        LOG.info("  %s (%d rows): %s .. %s", SCHEDULE_TAB, n_sched,
                 months[0], months[-1])
        LOG.info("re-run with --apply to write")
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    sheets = SheetsClient(sheet_id=sheet_id)

    for tab, header, rows in (
        (LOANS_TAB, LOANS_HEADER, loans_rows),
        (SCHEDULE_TAB, SCHEDULE_HEADER, sched_rows),
    ):
        sheets.ensure_tab(tab)
        sheets.write_header_row(tab, header)
        sheets.freeze_and_bold_header(tab)
        # idempotent: key on identity columns, so re-running updates
        # in place instead of duplicating
        key_cols = [0, 2] if tab == SCHEDULE_TAB else [0]
        stats = sheets.upsert_rows(tab, rows, natural_key_columns=key_cols)
        LOG.info("%s: %s", tab, stats)

    # verify: read back and count
    for tab, expect in ((LOANS_TAB, n_loans), (SCHEDULE_TAB, n_sched)):
        got = len(sheets.read_table(tab))
        if got < expect:
            LOG.error("%s: verification failed — %d rows read back, "
                      "expected >= %d", tab, got, expect)
            return 1
        LOG.info("%s: verified %d rows", tab, got)
    LOG.info("migration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
