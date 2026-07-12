#!/usr/bin/env python3
"""Create/prepare the Maintenance_Events tab.

Ensures the tab exists with the v91 header, attaches the provenance
notes (from ``argia.finance.provenance``) to each header cell, and
freezes/bolds row 1. Idempotent: never overwrites an existing header or
existing rows, so it is safe to re-run.

This is the ONLY thing that creates the tab — the loader
(``argia.maintenance.events.load_maintenance_events``) degrades to an
empty list on a missing tab, it never creates one. After running this,
enter events by hand (they are rare and contractual).

USAGE
    python scripts/maintenance_events_setup.py            # dry-run
    python scripts/maintenance_events_setup.py --apply    # write

EXIT CODES
    0  ran cleanly (dry-run or apply)
    3  config error (missing env vars / client build failed)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.core.sheets import SheetsClient
from argia.finance.provenance import COLUMN_NOTES
from argia.maintenance.events import (
    MAINTENANCE_EVENTS_HEADER, MAINTENANCE_EVENTS_TAB,
)

LOG = logging.getLogger("argia.setup.maintenance_events")


def run_setup(sheets: SheetsClient, apply: bool,
              log: logging.Logger = LOG) -> dict:
    """Ensure tab + header + notes. Returns a summary dict. In dry-run,
    reports what WOULD happen without writing."""
    mode = "APPLY" if apply else "DRY RUN"

    # Header drift guard: the sheet header must match the code's header,
    # and every column must be documented in provenance (same rule the
    # finance completeness test enforces).
    notes = COLUMN_NOTES.get(MAINTENANCE_EVENTS_TAB, {})
    undocumented = [c for c in MAINTENANCE_EVENTS_HEADER if c not in notes]
    if undocumented:
        raise ValueError("undocumented Maintenance_Events columns (add to "
                         "provenance.COLUMN_NOTES first): %s" % undocumented)

    if not apply:
        log.info("[%s] would ensure tab '%s' with %d columns and %d "
                 "provenance note(s)", mode, MAINTENANCE_EVENTS_TAB,
                 len(MAINTENANCE_EVENTS_HEADER), len(notes))
        log.info("[%s] header: %s", mode,
                 " | ".join(MAINTENANCE_EVENTS_HEADER))
        return {"applied": 0, "notes": len(notes)}

    sheets.ensure_tab(MAINTENANCE_EVENTS_TAB)
    sheets.ensure_header(MAINTENANCE_EVENTS_TAB, MAINTENANCE_EVENTS_HEADER)
    set_n = sheets.set_header_notes(MAINTENANCE_EVENTS_TAB, notes)
    sheets.freeze_and_bold_header(MAINTENANCE_EVENTS_TAB)
    log.info("[%s] tab '%s' ready (%d note(s) set)", mode,
             MAINTENANCE_EVENTS_TAB, set_n)
    return {"applied": 1, "notes": set_n}


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create/prepare the Maintenance_Events tab.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Default is a dry run.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if not os.environ.get("GOOGLE_SHEET_ID_V2", "").strip():
        LOG.error("GOOGLE_SHEET_ID_V2 is empty")
        return 3
    if not os.environ.get("GOOGLE_CREDENTIALS", "").strip():
        LOG.error("GOOGLE_CREDENTIALS is empty")
        return 3

    try:
        sheets = SheetsClient(sheet_id=os.environ["GOOGLE_SHEET_ID_V2"].strip())
    except Exception as e:  # noqa: BLE001
        LOG.error("Setup failed: %s", e)
        return 3

    mode = "APPLY" if args.apply else "DRY RUN"
    LOG.info("=== Maintenance_Events setup [%s] ===", mode)
    summary = run_setup(sheets, apply=args.apply)
    if not args.apply:
        LOG.info("Dry run only — re-run with --apply to create the tab.")
    LOG.info("Summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
