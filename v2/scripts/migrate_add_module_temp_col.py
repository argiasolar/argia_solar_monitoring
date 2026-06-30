#!/usr/bin/env python3
"""Additive header migration: append ``module_temp_c`` to existing telemetry tabs.

Schema v2 -> v3 appends ONE trailing column (``module_temp_c``, the env-station
Backplane Temp) to both ``ARGIA_SCHEMA`` (Telemetry_Argia) and ``PLANT_SCHEMA``
(every ``Telemetry_<KEY>`` tab).

Because the new column is at the END of the schema, this is a safe in-place
migration — NOT the "delete the tab" path. The sheets writer's header check is
an exact compare (after trimming trailing empties), so once the header row
carries ``module_temp_c`` as its last cell, it matches the v3 schema and writes
resume. Existing data rows are left untouched; they simply read back with a
blank in the new last column.

The decision is a pure function (``plan_header_migration``) so it's fully
unit-tested. The only side effect is ``SheetsClient.write_header_row`` on tabs
that need the append, and ONLY when ``--apply`` is passed.

USAGE
    python scripts/migrate_add_module_temp_col.py            # dry-run (default)
    python scripts/migrate_add_module_temp_col.py --apply    # actually write
    python scripts/migrate_add_module_temp_col.py --log-level DEBUG

EXIT CODES
    0  ran cleanly (dry-run or apply)
    1  one or more tabs had an unexpected ("mismatch") header — nothing written
       for those; investigate before applying
    3  config error (missing env vars / client build failed)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Tuple

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    ARGIA_TAB_NAME,
    PLANT_SCHEMA,
    TelemetrySchema,
    plant_tab_name,
)

LOG = logging.getLogger("argia.migrate.module_temp_col")


# ============================================================
# Pure: decide what to do with one tab's header
# ============================================================


# Actions returned by plan_header_migration.
ACTION_SKIP = "skip"          # header already matches v3 schema — nothing to do
ACTION_APPEND = "append"      # header is the v3 schema minus its last col — append it
ACTION_ABSENT = "absent"      # no header row yet — fresh tab, written on next run
ACTION_MISMATCH = "mismatch"  # header is something else — do NOT touch, investigate


def _trim_trailing_empty(cells: List[str]) -> List[str]:
    trimmed = [str(c).strip() for c in cells]
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed


def plan_header_migration(
    existing_header: List[str],
    schema: TelemetrySchema,
) -> Tuple[str, Optional[List[str]]]:
    """Decide the migration action for one tab, given its current header row.

    Returns ``(action, new_header)``:
      * ``(ACTION_SKIP, None)``     — already on the target schema.
      * ``(ACTION_APPEND, header)`` — header equals the target minus its last
        column; ``header`` is the full target header to write.
      * ``(ACTION_ABSENT, None)``   — no header at all (empty tab).
      * ``(ACTION_MISMATCH, None)`` — header is neither; leave it alone.

    Pure and schema-agnostic: works for any single-trailing-column append.
    """
    expected = list(schema.columns)
    trimmed = _trim_trailing_empty(existing_header)

    if not trimmed:
        return ACTION_ABSENT, None
    if trimmed == expected:
        return ACTION_SKIP, None
    if trimmed == expected[:-1]:
        return ACTION_APPEND, expected
    return ACTION_MISMATCH, None


# ============================================================
# Task list
# ============================================================


def build_tasks(portfolio) -> List[Tuple[str, TelemetrySchema]]:
    """All telemetry tabs to migrate: the aggregate tab + one per plant.

    Includes inactive plants — their tabs exist and must stay schema-aligned.
    """
    tasks: List[Tuple[str, TelemetrySchema]] = [(ARGIA_TAB_NAME, ARGIA_SCHEMA)]
    for plant_key in portfolio.plants:
        tasks.append((plant_tab_name(plant_key), PLANT_SCHEMA))
    return tasks


# ============================================================
# Apply (side effects isolated here)
# ============================================================


def run_migration(
    sheets: SheetsClient,
    tasks: List[Tuple[str, TelemetrySchema]],
    apply: bool,
    log: logging.Logger = LOG,
) -> dict:
    """Walk the tasks, plan each, and (if ``apply``) append the header cell.

    Never deletes or rewrites data rows — only ``write_header_row`` on tabs whose
    header is the target minus the new last column. Returns a summary dict.
    """
    summary = {
        ACTION_SKIP: 0,
        ACTION_APPEND: 0,
        ACTION_ABSENT: 0,
        ACTION_MISMATCH: 0,
        "error": 0,
    }
    mode = "APPLY" if apply else "DRY RUN"

    for tab, schema in tasks:
        try:
            rows = sheets.read_range(tab, "A1:ZZ1")
        except Exception as e:  # noqa: BLE001
            # Most commonly the tab doesn't exist yet — nothing to migrate; it
            # will be created with the full v3 header on the next write.
            log.warning("[%s] could not read header (skipping): %s", tab, e)
            summary["error"] += 1
            continue

        existing = rows[0] if rows else []
        action, new_header = plan_header_migration(existing, schema)
        summary[action] += 1

        if action == ACTION_SKIP:
            log.info("[%s] already on v3 schema (%d cols) — skip",
                     tab, schema.column_count)
        elif action == ACTION_ABSENT:
            log.info("[%s] no header yet — will be written fresh on next run",
                     tab)
        elif action == ACTION_MISMATCH:
            log.error(
                "[%s] UNEXPECTED header (not the v3 schema nor the v2 schema "
                "minus module_temp_c) — NOT touching. Investigate before apply.",
                tab,
            )
        elif action == ACTION_APPEND:
            assert new_header is not None
            log.info("[%s] [%s] append 'module_temp_c' -> %d cols",
                     tab, mode, schema.column_count)
            if apply:
                sheets.write_header_row(tab, new_header)

    return summary


# ============================================================
# CLI
# ============================================================


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Append module_temp_c to existing telemetry tab headers."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write headers. Default is a dry run (reports only).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 is empty")
        return 3
    if not os.environ.get("GOOGLE_CREDENTIALS", "").strip():
        LOG.error("GOOGLE_CREDENTIALS is empty")
        return 3

    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        LOG.error("Setup failed: %s", e)
        return 3

    tasks = build_tasks(portfolio)
    mode = "APPLY" if args.apply else "DRY RUN"
    LOG.info("=== module_temp_c header migration [%s] — %d tabs ===",
             mode, len(tasks))

    summary = run_migration(sheets, tasks, apply=args.apply)

    LOG.info(
        "Summary: append=%d skip=%d absent=%d mismatch=%d error=%d",
        summary[ACTION_APPEND], summary[ACTION_SKIP], summary[ACTION_ABSENT],
        summary[ACTION_MISMATCH], summary["error"],
    )
    if not args.apply and summary[ACTION_APPEND]:
        LOG.info("Dry run only — re-run with --apply to write the headers.")

    return 1 if summary[ACTION_MISMATCH] else 0


if __name__ == "__main__":
    sys.exit(main())
