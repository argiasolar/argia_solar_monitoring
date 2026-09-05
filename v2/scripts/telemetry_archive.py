#!/usr/bin/env python3
"""Archive old telemetry to Drive and keep a rolling window in the sheet.

Per ``Telemetry_<plant>`` tab: rows older than the retention window
(default 10 days) whose day KPI_Daily has already stamped are written to
Google Drive as one CSV per plant per day, then deleted from the live
tab. This keeps the workbook far under the 10M-cell ceiling while leaving
every report reproducible — the financial report and annex read KPI_Daily
aggregates, which are never touched.

SAFETY — the two properties that make this non-destructive:
* Archive-before-delete: a row is deleted ONLY after its day's CSV is
  confirmed present on Drive. Any archive failure aborts the delete for
  that tab; worst case the sheet keeps a few extra days.
* Stamp interlock: a day is never pruned before KPI_Daily has a FULL
  stamp for that plant+day (skipped for the shared irradiance tab, which
  is window-only).

Dry-run by default: prints exactly what it would archive and delete.

USAGE
    # see the plan, touch nothing
    PYTHONPATH=. python scripts/telemetry_archive.py
    # actually archive + prune
    PYTHONPATH=. python scripts/telemetry_archive.py --apply

ENV: GOOGLE_SHEET_ID_V2, GOOGLE_CREDENTIALS, GOOGLE_ARCHIVE_FOLDER_ID
EXIT: 0 ok   3 config error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from argia.core.config import load_portfolio
from argia.core.sheets import open_sheets
from argia.core.time_utils import now_mx
from argia.archive.kpi_daily import KPI_DAILY_TAB
from argia.telemetry.retention import (
    keep_from_date, mx_date_of, plan_prune, rows_to_csv,
    stamped_dates_from_kpi,
)

LOG = logging.getLogger("argia.telemetry.archive")

ARGIA_TAB = "Telemetry_Argia"     # shared irradiance/env — window-only
TS_COL = "timestamp_utc"


class _FolderCache:
    """Lazily ensure Telemetry_Archive/<plant>/<YYYY-MM> and cache ids."""

    def __init__(self, drive, base_id):
        self.drive = drive
        self.base_id = base_id
        self._cache = {}

    def month_folder(self, plant: str, ym: str) -> str:
        key = (plant, ym)
        if key not in self._cache:
            root = self._cache.get("_root")
            if root is None:
                root = self.drive.ensure_folder(self.base_id,
                                                "Telemetry_Archive")
                self._cache["_root"] = root
            pf = self._cache.get(("_plant", plant))
            if pf is None:
                pf = self.drive.ensure_folder(root, plant)
                self._cache[("_plant", plant)] = pf
            self._cache[key] = self.drive.ensure_folder(pf, ym)
        return self._cache[key]


def _dated_rows(data_rows, ts_idx):
    """(mx_date, row) for the parseable oldest-first prefix. Stops at the
    first row it can't date — never prune what it can't place in time."""
    out = []
    for row in data_rows:
        ts = row[ts_idx] if ts_idx < len(row) else None
        d = mx_date_of(ts)
        if d is None:
            break
        out.append((d, row))
    return out


def process_tab(sheets, drive, folders, tab, plant, stamped, keep_from,
                apply):
    """Archive+prune one tab. Returns (archived_rows, deleted_rows).
    ``stamped`` is a set of iso dates, or None for window-only tabs."""
    try:
        raw = sheets.read_range(tab, "A1:ZZ")
    except Exception:  # noqa: BLE001
        LOG.info("[%s] tab not found — skipping", tab)
        return 0, 0
    if not raw or len(raw) < 2:
        return 0, 0
    header = [str(h).strip() for h in raw[0]]
    if TS_COL not in header:
        LOG.warning("[%s] no %s column — skipping", tab, TS_COL)
        return 0, 0
    ts_idx = header.index(TS_COL)

    dated = _dated_rows(raw[1:], ts_idx)
    plan = plan_prune(dated, keep_from, stamped)
    if plan.n_prune == 0:
        LOG.info("[%s] nothing to prune (stop=%s, keep>=%s)",
                 tab, plan.stop_reason, keep_from.isoformat())
        return 0, 0

    LOG.info("[%s] %d row(s) across %d day(s) older than %s%s",
             tab, plan.n_prune, len(plan.rows_by_day), keep_from.isoformat(),
             " [window-only]" if stamped is None else "")

    # --- archive first (each day → one CSV), verify, THEN delete ---
    archived_ok = True
    for day in sorted(plan.rows_by_day):
        day_rows = plan.rows_by_day[day]
        name = "telemetry_%s_%s.csv" % (plant.lower(), day)
        if not apply:
            LOG.info("[%s]   would archive %d row(s) -> %s",
                     tab, len(day_rows), name)
            continue
        try:
            ym = day[:7]
            folder = folders.month_folder(plant, ym)
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".csv", delete=False, encoding="utf-8") as fh:
                fh.write(rows_to_csv(raw[0], day_rows))
                tmp = fh.name
            drive.upload_file(folder, name, tmp, "text/csv")
            os.unlink(tmp)
            if drive.find_file(folder, name) is None:      # verify present
                raise RuntimeError("post-upload verify failed")
            LOG.info("[%s]   archived %d row(s) -> %s", tab, len(day_rows),
                     name)
        except Exception as e:  # noqa: BLE001
            LOG.error("[%s]   ARCHIVE FAILED for %s: %s — NOT deleting",
                      tab, name, e)
            archived_ok = False
            break

    if not apply:
        LOG.info("[%s] DRY RUN — would delete rows 2..%d after archive",
                 tab, 1 + plan.n_prune)
        return plan.n_prune, 0
    if not archived_ok:
        return 0, 0

    sheets.delete_row_range(tab, 2, 1 + plan.n_prune)   # inclusive, 1-based
    LOG.info("[%s] deleted %d archived row(s) from the live tab",
             tab, plan.n_prune)
    return plan.n_prune, plan.n_prune


# ---------------------------------------------------------------- v189
# PG -> Drive export. With ARGIA_TELEMETRY_SOURCE=pg the sheet is no
# longer the thing being archived: the `telemetry` table is the record and
# Drive keeps the human-readable daily CSVs it always did — same folders
# (Telemetry_Archive/<plant>/<YYYY-MM>), same names
# (telemetry_<plant>_<date>.csv), 16 ARGIA_SCHEMA columns. Idempotent: a
# day already on Drive is skipped. Nothing is ever deleted.

def export_days_pg(drive, folders, plants, dates, apply, log=LOG):
    """Export each (plant, MX date) from PostgreSQL to Drive.
    Returns (files_written, files_skipped_existing)."""
    from argia.telemetry import pg_source
    written = skipped = 0
    for day in dates:
        grid = pg_source.read_grid(date_iso=day)
        header, rows = grid[0], grid[1:]
        by_plant = {}
        for r in rows:
            by_plant.setdefault(str(r[3]), []).append(r)      # plant_key col
        for pk in plants:
            day_rows = by_plant.get(pk, [])
            if not day_rows:
                continue
            name = "telemetry_%s_%s.csv" % (pk.lower(), day)
            if not apply:
                log.info("[%s] would export %d row(s) -> %s",
                         pk, len(day_rows), name)
                continue
            folder = folders.month_folder(pk, day[:7])
            if drive.find_file(folder, name) is not None:
                skipped += 1
                continue
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".csv", delete=False, encoding="utf-8") as fh:
                fh.write(rows_to_csv(header, day_rows))
                tmp = fh.name
            drive.upload_file(folder, name, tmp, "text/csv")
            os.unlink(tmp)
            if drive.find_file(folder, name) is None:
                raise RuntimeError("post-upload verify failed for %s" % name)
            log.info("[%s] exported %d row(s) -> %s", pk, len(day_rows), name)
            written += 1
    return written, skipped


def export_dates(today_mx, window_days: int, explicit: str | None) -> list:
    """Which MX dates to export: one explicit date, or every complete day
    inside the window (yesterday back to today-window). Pure."""
    import datetime as _dt
    if explicit:
        _dt.date.fromisoformat(explicit)
        return [explicit]
    return [(today_mx - _dt.timedelta(days=i)).isoformat()
            for i in range(1, window_days + 1)]


def _setup_logging(level="INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--window-days", type=int, default=10)
    p.add_argument("--plant", default=None, help="limit to one plant")
    p.add_argument("--include-argia", action="store_true",
                   help="also window-prune the shared %s tab" % ARGIA_TAB)
    p.add_argument("--apply", action="store_true",
                   help="archive + delete. Default is a dry run.")
    p.add_argument("--ignore-stamps", action="store_true",
                   help="EMERGENCY: prune by window alone, skipping the KPI "
                        "full-stamp interlock. Rows are STILL archived to Drive "
                        "before deletion. Exists for the cell-limit deadlock: a "
                        "full workbook makes days stamp partial, and partial "
                        "days block pruning (live incident 2026-08-18..26).")
    p.add_argument("--source", choices=["sheet", "pg"], default=None,
                   help="v189: 'pg' exports from PostgreSQL to Drive and "
                        "touches no sheet; default from "
                        "ARGIA_TELEMETRY_SOURCE")
    p.add_argument("--date", default=None,
                   help="pg mode: export this one MX date only")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    _setup_logging(args.log_level)

    from argia.telemetry import pg_source
    if (args.source or pg_source.source()) == "pg":
        return main_pg(args)

    base_folder = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if args.apply and not base_folder:
        LOG.error("GOOGLE_ARCHIVE_FOLDER_ID not set (needed to archive)")
        return 3

    try:
        sheets = open_sheets()          # v199 (sheet mode: the id is required)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        LOG.error("bootstrap failed: %s", e)
        return 3

    drive = folders = None
    if args.apply:
        from argia.core.drive import DriveClient
        drive = DriveClient()
        folders = _FolderCache(drive, base_folder)

    keep_from = keep_from_date(now_mx().date(), args.window_days)
    stamped = stamped_dates_from_kpi(sheets.read_range(KPI_DAILY_TAB, "A1:ZZ"))

    plants = [args.plant.upper()] if args.plant else \
        sorted(pk for pk in portfolio.plants)
    LOG.info("=== telemetry archive [%s] keep>=%s (%d-day window) ===",
             "APPLY" if args.apply else "DRY RUN", keep_from.isoformat(),
             args.window_days)

    if args.ignore_stamps:
        LOG.warning("--ignore-stamps: pruning by window alone; the KPI "
                    "full-stamp interlock is OFF for this run")

    tot_arch = tot_del = 0
    for pk in plants:
        interlock = None if args.ignore_stamps else stamped.get(pk, set())
        a, d = process_tab(sheets, drive, folders, "Telemetry_%s" % pk, pk,
                           interlock, keep_from, args.apply)
        tot_arch += a
        tot_del += d

    if args.include_argia and not args.plant:
        a, d = process_tab(sheets, drive, folders, ARGIA_TAB, "Argia",
                           None, keep_from, args.apply)   # window-only
        tot_arch += a
        tot_del += d

    LOG.info("Summary: %d row(s) %s, %d deleted from live tabs",
             tot_arch, "archived" if args.apply else "to archive", tot_del)
    if not args.apply:
        LOG.info("Dry run — re-run with --apply to archive and prune.")
    return 0


def main_pg(args) -> int:
    """v189 path: PG -> Drive daily CSVs. Plants come from the portfolio
    (still the sheet until Phase 5 — read-only here)."""
    base_folder = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if args.apply and not base_folder:
        LOG.error("GOOGLE_ARCHIVE_FOLDER_ID not set (needed to export)")
        return 3
    try:
        portfolio = load_portfolio(open_sheets())       # v199
        plants = [args.plant.upper()] if args.plant else \
            sorted(pk for pk in portfolio.plants)
    except Exception as e:  # noqa: BLE001
        LOG.error("bootstrap failed: %s", e)
        return 3
    drive = folders = None
    if args.apply:
        from argia.core.drive import DriveClient
        drive = DriveClient()
        folders = _FolderCache(drive, base_folder)
    dates = export_dates(now_mx().date(), args.window_days, args.date)
    LOG.info("=== telemetry export PG->Drive [%s] %d day(s) %s..%s, "
             "%d plant(s) ===", "APPLY" if args.apply else "DRY RUN",
             len(dates), dates[-1], dates[0], len(plants))
    w, sk = export_days_pg(drive, folders, plants, dates, args.apply)
    LOG.info("Summary: %d file(s) written, %d already on Drive, "
             "0 deleted (export never deletes)", w, sk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
