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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--date", default=None,
                        help="ISO date (default: today, MX local — this is "
                             "an EVENING job reporting the current day)")
    parser.add_argument("--dry-run", action="store_true",
                        help="render locally, upload nothing")
    parser.add_argument("--html-only", action="store_true",
                        help="skip PDF conversion (debug aid)")
    parser.add_argument("--out-dir", default=None,
                        help="where to write the local files "
                             "(default: temp dir)")
    args = parser.parse_args(argv)
    date_iso = args.date or now_mx().date().isoformat()

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
    if pdf_path:
        drive.upload_file(reports_id, base + ".pdf", pdf_path,
                          "application/pdf")
    drive.upload_file(reports_id, base + ".html", html_path, "text/html")
    log.info("Report %s uploaded to Drive folder '%s'",
             date_iso, REPORTS_FOLDER_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
