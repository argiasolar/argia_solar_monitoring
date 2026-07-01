#!/usr/bin/env python3
"""Seed a per-plant ``gamma_pmax`` column on the Plants tab.

Adds the ``gamma_pmax`` header (if missing) and fills a default of -0.0035
(-0.35%/degC, standard crystalline silicon) into every plant row that doesn't
already have a value. Later you can overwrite any cell with that site's real
module-datasheet coefficient — the pipeline reads the per-plant value and only
falls back to the default when the cell is blank.

The Plants tab is parsed by header NAME (not column position), so adding this
column anywhere in A:AB is safe. Blank cells are filled; existing values are
never overwritten (idempotent).

The decision is a pure function (``plan_gamma_fill``); the only side effects
are ``write_values`` calls, and only when ``--apply`` is passed.

USAGE
    python scripts/migrate_add_gamma_pmax_col.py            # dry-run (default)
    python scripts/migrate_add_gamma_pmax_col.py --apply    # actually write
    python scripts/migrate_add_gamma_pmax_col.py --default -0.0040 --apply

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

from argia.core.sheets import SheetsClient, _col_to_a1

LOG = logging.getLogger("argia.migrate.gamma_pmax")

PLANTS_TAB = "Plants"
GAMMA_COL = "gamma_pmax"
DEFAULT_GAMMA_PMAX = -0.0035
PLANTS_READ_RANGE = "A1:AB"


def _trim(cells: List) -> List[str]:
    out = [str(c).strip() for c in cells]
    while out and not out[-1]:
        out.pop()
    return out


def plan_gamma_fill(
    header: List,
    data_rows: List[List],
    default: float = DEFAULT_GAMMA_PMAX,
) -> Tuple[bool, int, List[Tuple[int, float]]]:
    """Decide the migration for the Plants tab.

    Returns ``(needs_header, col_index_1based, fills)`` where ``fills`` is a
    list of ``(sheet_row_number, value)`` for blank gamma cells on real plant
    rows (a plant row is one with a non-empty first column / plant_key).

    Pure: no I/O. Idempotent — a row that already has a value is not refilled.
    """
    trimmed = _trim(header)
    if GAMMA_COL in trimmed:
        col_index = trimmed.index(GAMMA_COL) + 1
        needs_header = False
    else:
        col_index = len(trimmed) + 1
        needs_header = True

    fills: List[Tuple[int, float]] = []
    for i, row in enumerate(data_rows):
        if not row or not str(row[0]).strip():
            continue  # not a plant row
        current = row[col_index - 1] if len(row) >= col_index else ""
        if str(current).strip() == "":
            fills.append((i + 2, default))  # A2 == sheet row 2
    return needs_header, col_index, fills


def run_migration(
    sheets: SheetsClient,
    apply: bool,
    default: float = DEFAULT_GAMMA_PMAX,
    log: logging.Logger = LOG,
) -> dict:
    """Read Plants, plan the gamma fill, and (if apply) write header + cells."""
    raw = sheets.read_range(PLANTS_TAB, PLANTS_READ_RANGE)
    header = raw[0] if raw else []
    data_rows = raw[1:] if len(raw) > 1 else []

    needs_header, col_index, fills = plan_gamma_fill(header, data_rows, default)
    col = _col_to_a1(col_index)
    mode = "APPLY" if apply else "DRY RUN"

    if needs_header:
        log.info("[%s] add '%s' header at column %s", mode, GAMMA_COL, col)
        if apply:
            sheets.write_values(PLANTS_TAB, f"{col}1", [[GAMMA_COL]])
    else:
        log.info("'%s' header already present at column %s", GAMMA_COL, col)

    if fills:
        log.info("[%s] fill %d plant row(s) with default %.4f",
                 mode, len(fills), default)
        if apply:
            for row_num, val in fills:
                sheets.write_values(PLANTS_TAB, f"{col}{row_num}", [[val]])
    else:
        log.info("No blank gamma cells to fill")

    return {
        "header_added": 1 if needs_header else 0,
        "cells_filled": len(fills) if apply else 0,
        "cells_to_fill": len(fills),
    }


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a per-plant gamma_pmax column on the Plants tab."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Default is a dry run.")
    parser.add_argument("--default", type=float, default=DEFAULT_GAMMA_PMAX,
                        help=f"Default gamma value (default {DEFAULT_GAMMA_PMAX}).")
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
    LOG.info("=== gamma_pmax Plants-column migration [%s] ===", mode)
    summary = run_migration(sheets, apply=args.apply, default=args.default)
    LOG.info("Summary: header_added=%d cells_to_fill=%d cells_filled=%d",
             summary["header_added"], summary["cells_to_fill"],
             summary["cells_filled"])
    if not args.apply and (summary["header_added"] or summary["cells_to_fill"]):
        LOG.info("Dry run only — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
