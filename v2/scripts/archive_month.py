#!/usr/bin/env python3
"""Argia_Mont — monthly archive to Drive (plan #8).

Copies one month's rows from the live spreadsheet into
``Argia_Mont_Archive_YYYY_MM`` inside the shared Drive folder, verifies row
counts, and only then prunes the archived TELEMETRY rows from the live
tabs. KPI_Daily keeps its own 14-day pruning; the Alerts ledger is copied
but never pruned (it is small and IS the operational history).

Order is a hard invariant: copy -> verify -> prune. Any verification
failure aborts before anything is deleted. Prune only ever removes one
CONTIGUOUS block per tab; a non-contiguous month (should be impossible in
append-ordered tabs) is skipped with a warning, never guessed at.

Idempotent: re-running reuses the existing archive file and skips tabs
already fully archived.

USAGE
    PYTHONPATH=. python scripts/archive_month.py                 # DRY RUN, prev month
    PYTHONPATH=. python scripts/archive_month.py --month 2026-07
    PYTHONPATH=. python scripts/archive_month.py --month 2026-07 --apply

EXIT CODES
    0  ran cleanly (dry-run report, or archive verified [+pruned])
    1  verification failed — NOTHING was pruned
    3  config error
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from typing import Dict, List

from argia.archive.monthly import (
    CELL_BUDGET_WARN,
    MonthBlock,
    chunk_rows,
    datetime_format_columns,
    locate_month_block,
    month_title,
    previous_month,
    projected_cells,
    verify_copy,
)
from argia.core.drive import DriveClient
from argia.core.normalize import normalize_text
from argia.core.sheets import SheetsClient
from argia.kpi.reconcile import date_key
from argia.core.time_utils import now_mx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.archive_month")

# Tabs to archive, with how to date a row and whether to prune afterwards.
#   key: column whose value dates the row
#   prune: telemetry only — KPI_Daily has its own pruning, Alerts stays live
TAB_SPECS = [
    {"tab": "KPI_Daily", "key": "date_iso", "prune": False},
    {"tab": "Alerts", "key": "opened_utc", "prune": False},
    {"tab": "Telemetry_Argia", "key": "timestamp_mx", "prune": True},
]
DEEP_TAB_KEY = "timestamp_mx"


def _key_fn(header: List[str], key_col: str):
    idx = [normalize_text(h) for h in header].index(key_col)

    def key_of(row: List) -> str:
        if idx >= len(row) or row[idx] in (None, ""):
            return ""
        return date_key(row[idx])
    return key_of


def _load_block(sheets: SheetsClient, tab: str, key_col: str,
                month: str) -> MonthBlock:
    data = sheets.read_range(tab, "A1:ZZ")
    if not data:
        return MonthBlock(tab=tab, header=[], rows=[], start_row=0,
                          end_row=0, contiguous=False, total_data_rows=0)
    return locate_month_block(tab, data, month,
                              _key_fn(data[0], key_col))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--month", default=None,
                        help="YYYY-MM to archive (default: previous month)")
    parser.add_argument("--apply", action="store_true",
                        help="actually create/copy/prune (default: dry run)")
    args = parser.parse_args(argv)
    month = args.month or previous_month(now_mx().date())
    if len(month) != 7 or month[4] != "-":
        log.error("--month must be YYYY-MM (got %r)", args.month)
        return 3

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    folder_id = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if not sheet_id or not folder_id:
        log.error("GOOGLE_SHEET_ID_V2 and GOOGLE_ARCHIVE_FOLDER_ID must be set "
                  "(run scripts/archive_preflight.py first)")
        return 3
    try:
        live = SheetsClient(sheet_id=sheet_id)
        from argia.core.config import load_portfolio
        portfolio = load_portfolio(live)
    except Exception as e:  # noqa: BLE001
        log.error("bootstrap failed: %s", e)
        return 3

    specs = list(TAB_SPECS) + [
        {"tab": f"Telemetry_{p.plant_key}", "key": DEEP_TAB_KEY, "prune": True}
        for p in portfolio.active_plants()
    ]

    # ---- locate every month block (read-only) ----
    blocks: List[Dict] = []
    for spec in specs:
        try:
            b = _load_block(live, spec["tab"], spec["key"], month)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: could not read (%s) — skipped", spec["tab"], e)
            continue
        blocks.append({"spec": spec, "block": b})
        log.info("%-18s month rows=%-6d rows_total=%-6d block=%s%s",
                 b.tab, b.count, b.total_data_rows,
                 f"{b.start_row}..{b.end_row}" if b.count else "-",
                 "" if (b.contiguous or not b.count) else "  NON-CONTIGUOUS")

    total = projected_cells([x["block"] for x in blocks])
    log.info("Projected archive size: ~%d cells%s", total,
             "  (WARNING: near the 10M/sheet limit!)"
             if total > CELL_BUDGET_WARN else "")
    if all(x["block"].count == 0 for x in blocks):
        log.info("No rows for %s in any tab — nothing to archive.", month)
        return 0

    if not args.apply:
        log.info("[DRY RUN] no archive created, nothing copied or pruned. "
                 "Re-run with --apply.")
        return 0

    # ---- create/reuse archive file ----
    title = month_title(month)
    drive = DriveClient()
    archive_id = drive.find_spreadsheet(folder_id, title)
    if archive_id:
        log.info("Reusing existing archive '%s' (%s)", title, archive_id)
    else:
        archive_id = drive.create_spreadsheet(folder_id, title)
    archive = SheetsClient(sheet_id=archive_id)

    # ---- copy + verify (all tabs) ----
    failures = 0
    verified: List[Dict] = []
    for x in blocks:
        b: MonthBlock = x["block"]
        if b.count == 0:
            continue
        archive.ensure_tab(b.tab)
        existing = archive.read_range(b.tab, "A1:ZZ")
        already = max(0, len(existing) - 1) if existing else 0
        if already == b.count:
            log.info("%s: already fully archived (%d rows) — skipping copy",
                     b.tab, already)
        else:
            if already:
                log.warning("%s: archive holds %d rows vs %d expected — "
                            "recopying from scratch is NOT automatic; "
                            "clear the tab in the archive and re-run",
                            b.tab, already, b.count)
                failures += 1
                continue
            archive.write_header_row(b.tab, b.header)
            for chunk in chunk_rows(b.rows):
                archive.append_rows(b.tab, chunk, value_input_option="RAW")
            log.info("%s: copied %d rows", b.tab, b.count)
        after = archive.read_range(b.tab, "A1:ZZ")
        ok, msg = verify_copy(b, max(0, len(after) - 1))
        log.info(msg)
        if ok:
            verified.append(x)
            # presentation: header + datetime display (idempotent, so a
            # re-run also repairs an earlier archive created without it)
            try:
                archive.freeze_and_bold_header(b.tab)
                for col, pattern in datetime_format_columns(b.header):
                    archive.format_datetime_column(b.tab, col, pattern)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: formatting failed (data unaffected): %s",
                            b.tab, e)
        else:
            failures += 1

    # drop the default empty tab left by spreadsheet creation
    if archive.delete_tab_if_exists("Sheet1"):
        log.info("Removed default 'Sheet1' from the archive")

    if failures:
        log.error("%d tab(s) failed verification — NOTHING pruned (exit 1)",
                  failures)
        return 1

    # ---- prune telemetry blocks (verified tabs only) ----
    for x in verified:
        spec, b = x["spec"], x["block"]
        if not spec["prune"]:
            continue
        if not b.contiguous:
            log.warning("%s: month rows not contiguous — prune SKIPPED "
                        "(archive copy is complete and verified)", b.tab)
            continue
        live.delete_row_range(b.tab, b.start_row, b.end_row)
        log.info("%s: pruned rows %d..%d (%d rows) from live sheet",
                 b.tab, b.start_row, b.end_row, b.count)

    log.info("Archive %s complete: %s", month, title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
