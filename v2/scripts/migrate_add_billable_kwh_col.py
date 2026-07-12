#!/usr/bin/env python3
"""Add a ``billable_kwh`` column to KPI_Daily and back-fill history.

Why the back-fill matters: the finance layer prefers ``billable_kwh``
over ``energy_kwh``. If the column exists but historical rows are blank,
those days would read as blank. v91's income loader also falls back to
``energy_kwh`` per row (belt), but back-filling ``billable_kwh =
energy_kwh`` on every existing row (there were no maintenance events
before v91, so billable == energy for all history) makes the column
uniformly populated (suspenders).

Idempotent: a row that already has a ``billable_kwh`` value is never
overwritten. Rows with no ``energy_kwh`` are left blank (a no-data day
must not be back-filled to 0). Going forward ``kpi_eod`` stamps
``billable_kwh`` for every processed plant-day.

KPI_Daily is parsed by header NAME, so adding the column at the end is
safe. The decision is a pure function (``plan_billable_backfill``); the
only side effects are ``write_values`` calls, and only under ``--apply``.

USAGE
    python scripts/migrate_add_billable_kwh_col.py            # dry-run
    python scripts/migrate_add_billable_kwh_col.py --apply    # write

EXIT CODES
    0  ran cleanly (dry-run or apply)
    3  config error (missing env vars / client build failed)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Tuple

from argia.core.normalize import safe_float
from argia.core.sheets import SheetsClient, _col_to_a1

LOG = logging.getLogger("argia.migrate.billable_kwh")

KPI_TAB = "KPI_Daily"
ENERGY_COL = "energy_kwh"
BILLABLE_COL = "billable_kwh"
KPI_READ_RANGE = "A1:ZZ"


def _trim(cells: List) -> List[str]:
    out = [str(c).strip() for c in cells]
    while out and not out[-1]:
        out.pop()
    return out


def plan_billable_backfill(
    header: List,
    data_rows: List[List],
) -> Tuple[bool, int, List[Tuple[int, float]]]:
    """Decide the migration.

    Returns ``(needs_header, billable_col_1based, fills)`` where ``fills``
    is ``[(sheet_row_number, energy_value)]`` for rows that have an
    ``energy_kwh`` value but a blank ``billable_kwh``.

    Pure: no I/O. Idempotent — a row with an existing billable value is
    skipped. A row with a blank/zero energy value is skipped (a no-data
    day stays blank, never back-filled to 0). Raises if there is no
    ``energy_kwh`` column to source from.
    """
    trimmed = _trim(header)
    if ENERGY_COL not in trimmed:
        raise ValueError("KPI_Daily has no energy_kwh column to back-fill "
                         "from")
    energy_idx = trimmed.index(ENERGY_COL)

    if BILLABLE_COL in trimmed:
        billable_col = trimmed.index(BILLABLE_COL) + 1
        needs_header = False
    else:
        billable_col = len(trimmed) + 1
        needs_header = True

    fills: List[Tuple[int, float]] = []
    for i, row in enumerate(data_rows):
        if not row or not str(row[0]).strip():
            continue  # not a data row
        existing = (row[billable_col - 1]
                    if len(row) >= billable_col else "")
        if str(existing).strip() != "":
            continue  # already has a billable value
        energy = safe_float(row[energy_idx]) if len(row) > energy_idx else None
        if energy is None:
            continue  # no-data day → leave blank
        fills.append((i + 2, energy))  # A2 == sheet row 2
    return needs_header, billable_col, fills


def run_migration(sheets: SheetsClient, apply: bool,
                  log: logging.Logger = LOG) -> dict:
    raw = sheets.read_range(KPI_TAB, KPI_READ_RANGE)
    header = raw[0] if raw else []
    data_rows = raw[1:] if len(raw) > 1 else []

    needs_header, col_index, fills = plan_billable_backfill(header, data_rows)
    col = _col_to_a1(col_index)
    mode = "APPLY" if apply else "DRY RUN"

    if needs_header:
        log.info("[%s] add '%s' header at column %s", mode, BILLABLE_COL, col)
        if apply:
            sheets.write_values(KPI_TAB, f"{col}1", [[BILLABLE_COL]])
    else:
        log.info("'%s' header already present at column %s",
                 BILLABLE_COL, col)

    if fills:
        log.info("[%s] back-fill %d row(s) billable_kwh = energy_kwh",
                 mode, len(fills))
        if apply:
            # one batched write, not a per-cell loop (60/min quota).
            sheets.batch_write_cells(
                KPI_TAB, [(r, col_index, v) for r, v in fills])
    else:
        log.info("No rows to back-fill")

    return {
        "header_added": 1 if needs_header else 0,
        "rows_backfilled": len(fills) if apply else 0,
        "rows_to_backfill": len(fills),
    }


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Add + back-fill the KPI_Daily billable_kwh column.")
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
    LOG.info("=== billable_kwh KPI_Daily-column migration [%s] ===", mode)
    summary = run_migration(sheets, apply=args.apply)
    LOG.info("Summary: header_added=%d rows_to_backfill=%d rows_backfilled=%d",
             summary["header_added"], summary["rows_to_backfill"],
             summary["rows_backfilled"])
    if not args.apply and (summary["header_added"]
                           or summary["rows_to_backfill"]):
        LOG.info("Dry run only — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
