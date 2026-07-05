#!/usr/bin/env python3
"""Argia_Mont — daily report: render HTML, print to PDF, upload to Drive.

Evening job (report family, part 1 — e-mail comes once all report types
exist). Reads the day's KPI_Daily / Alerts / telemetry, renders the daily
performance report, converts it to PDF with headless Chromium
(playwright), and uploads BOTH files into ``Reports/`` inside the ARGIA
archive Shared Drive. Upload is idempotent by filename — a re-run updates
the same Drive file instead of creating "(1)" duplicates.

USAGE
    PYTHONPATH=. python scripts/report_daily.py                # today (MX)
    PYTHONPATH=. python scripts/report_daily.py --date 2026-07-02
    PYTHONPATH=. python scripts/report_daily.py --dry-run      # render only
    PYTHONPATH=. python scripts/report_daily.py --html-only    # skip PDF

EXIT CODES
    0  report rendered (and uploaded, unless dry-run)
    2  no KPI data for the date (nothing to report)
    3  config error
    4  PDF conversion failed
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile

from argia.core.config import load_portfolio
from argia.core.drive import DriveClient
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.report.daily import build_report_data, render_html

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.report_daily")

REPORTS_FOLDER_NAME = "Reports"


def html_to_pdf(html_path: str, pdf_path: str) -> None:
    """Print the HTML to PDF with headless Chromium — renders the inline
    SVG charts and web fonts exactly as a browser does."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{os.path.abspath(html_path)}")
        page.wait_for_load_state("networkidle")   # let fonts arrive
        page.pdf(path=pdf_path, format="A4",
                 margin={"top": "12mm", "bottom": "12mm",
                         "left": "10mm", "right": "10mm"},
                 print_background=True)
        browser.close()


OUTBOX_TAB = "Report_Outbox"
OUTBOX_HEADER = ["date_iso", "kind", "pdf_file_id", "html_file_id",
                 "created_utc", "notified_at"]


def resolve_report_date(date_arg, when, now_mx_dt) -> str:
    """Explicit --date wins; otherwise 'today'/'yesterday' in MX time."""
    if date_arg:
        return date_arg
    d = now_mx_dt.date()
    if when == "yesterday":
        d = d - dt.timedelta(days=1)
    return d.isoformat()


def append_outbox(sheets, *, date_iso: str, kind: str,
                  pdf_file_id: str | None, html_file_id: str | None,
                  now_utc_iso: str) -> None:
    """Queue the uploaded report for e-mail delivery.

    The Apps Script notifier (docs/notifier.gs) scans this APPEND-ONLY tab
    every few minutes, mails rows whose notified_at is empty (PDF attached
    from Drive), and stamps notified_at. Append-only means the stamp can
    never be wiped by a rewrite — unlike the engine-owned Alerts tab, which
    is why alerts use a separate ledger instead.
    """
    sheets.ensure_tab(OUTBOX_TAB)
    sheets.ensure_header(OUTBOX_TAB, OUTBOX_HEADER)
    sheets.append_rows(OUTBOX_TAB, [[
        date_iso, kind, pdf_file_id or "", html_file_id or "",
        now_utc_iso, "",
    ]])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--date", default=None,
                        help="ISO date (overrides --when)")
    parser.add_argument("--when", choices=("today", "yesterday"),
                        default="today",
                        help="which MX day to report when --date is not "
                             "given: 'today' (evening ops snapshot) or "
                             "'yesterday' (morning KPI-exact performance "
                             "report, run after kpi_eod)")
    parser.add_argument("--dry-run", action="store_true",
                        help="render locally, upload nothing")
    parser.add_argument("--html-only", action="store_true",
                        help="skip PDF conversion (debug aid)")
    parser.add_argument("--out-dir", default=None,
                        help="where to write the local files "
                             "(default: temp dir)")
    args = parser.parse_args(argv)
    date_iso = resolve_report_date(args.date, args.when, now_mx())

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    folder_id = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    if not folder_id and not args.dry_run:
        log.error("GOOGLE_ARCHIVE_FOLDER_ID not set (needed for upload)")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("bootstrap failed: %s", e)
        return 3

    data = build_report_data(sheets, portfolio, date_iso)
    n_kpi = sum(1 for p in data.plants if p.energy_kwh is not None)
    if n_kpi == 0:
        log.warning("no KPI rows for %s — nothing to report "
                    "(did kpi_eod run for this date?)", date_iso)
        return 2
    log.info("Report %s: %d plants with KPI data, %d open alerts",
             date_iso, n_kpi, len(data.alerts))

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_report_")
    os.makedirs(out_dir, exist_ok=True)
    base = f"ARGIA_Daily_{date_iso}"
    html_path = os.path.join(out_dir, base + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(data))
    log.info("HTML written: %s (%d bytes)",
             html_path, os.path.getsize(html_path))

    pdf_path = None
    if not args.html_only:
        pdf_path = os.path.join(out_dir, base + ".pdf")
        try:
            html_to_pdf(html_path, pdf_path)
            log.info("PDF written: %s (%d bytes)",
                     pdf_path, os.path.getsize(pdf_path))
        except Exception as e:  # noqa: BLE001
            log.error("PDF conversion failed: %s", e)
            return 4

    if args.dry_run:
        log.info("[DRY RUN] nothing uploaded — files left in %s", out_dir)
        return 0

    drive = DriveClient()
    reports_id = drive.ensure_folder(folder_id, REPORTS_FOLDER_NAME)
    pdf_id = None
    if pdf_path:
        pdf_id = drive.upload_file(reports_id, base + ".pdf", pdf_path,
                                   "application/pdf")
    html_id = drive.upload_file(reports_id, base + ".html", html_path,
                                "text/html")
    log.info("Report %s uploaded to Drive folder '%s'",
             date_iso, REPORTS_FOLDER_NAME)
    try:
        kind = ("morning_yesterday" if args.when == "yesterday"
                and not args.date else "evening_today")
        append_outbox(sheets, date_iso=date_iso, kind=kind,
                      pdf_file_id=pdf_id, html_file_id=html_id,
                      now_utc_iso=dt.datetime.now(dt.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"))
        log.info("Report_Outbox row appended (%s)", kind)
    except Exception as e:  # noqa: BLE001
        # e-mail queueing must never fail the report itself
        log.error("Report_Outbox append failed (report IS uploaded): %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
