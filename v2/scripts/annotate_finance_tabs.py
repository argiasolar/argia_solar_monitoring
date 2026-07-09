"""Write data-provenance annotations into the live sheet.

Two actions, both idempotent:

  1. Header-cell notes (hover comments) on Loans, Loan_Schedule,
     Contract_Monthly and Plants.om_cost_monthly_mxn — each states
     where the value comes from (contract / bank table / manual input /
     derived) per argia/finance/provenance.py.
  2. A "Finance layer — data provenance" section appended to the NOTES
     tab. Guarded by a marker line: re-running never duplicates it.

Usage:
    PYTHONPATH=. python scripts/annotate_finance_tabs.py           # dry-run
    PYTHONPATH=. python scripts/annotate_finance_tabs.py --apply   # write
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.core.sheets import SheetsClient          # noqa: E402
from argia.finance.provenance import (              # noqa: E402
    COLUMN_NOTES, NOTES_MARKER, NOTES_SECTION,
)

LOG = logging.getLogger("annotate_finance")
NOTES_TAB = "NOTES"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true",
                        help="write to the live sheet (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    total_notes = sum(len(v) for v in COLUMN_NOTES.values())
    if not args.apply:
        LOG.info("[dry-run] would set %d header note(s) across %s and "
                 "append %d line(s) to %s (marker-guarded: %r)",
                 total_notes, ", ".join(COLUMN_NOTES), len(NOTES_SECTION),
                 NOTES_TAB, NOTES_MARKER)
        return 0

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 1
    sheets = SheetsClient(sheet_id=sheet_id)

    # 1) header notes
    for tab, notes in COLUMN_NOTES.items():
        set_n = sheets.set_header_notes(tab, notes)
        LOG.info("%s: %d/%d header notes set", tab, set_n, len(notes))

    # 2) NOTES section (append once)
    existing = sheets.read_range(NOTES_TAB, "A1:A")
    flat = {str(r[0]).strip() for r in existing if r}
    if NOTES_MARKER in flat:
        LOG.info("%s: provenance section already present — skipped",
                 NOTES_TAB)
    else:
        sheets.append_rows(NOTES_TAB, [[line] for line in NOTES_SECTION])
        LOG.info("%s: appended %d provenance line(s)", NOTES_TAB,
                 len(NOTES_SECTION))
    LOG.info("annotation complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
