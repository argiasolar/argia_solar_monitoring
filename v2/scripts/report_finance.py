"""Generate the portfolio financial report (investor/shareholder), on demand.

HTML + PDF over an arbitrary date range; uploads to the Reports Drive
folder and queues on Report_Outbox with channel=shareholders (v46
notifier mails it to the shareholders recipient list).

Usage:
    # current month to date (MX time), dry-run: files only, no upload
    PYTHONPATH=. python scripts/report_finance.py --dry-run

    # explicit range, full delivery
    PYTHONPATH=. python scripts/report_finance.py \\
        --start 2026-07-01 --end 2026-07-31

Exit codes: 0 ok, 2 nothing to report, 3 bootstrap/env failure,
4 PDF failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.core.config import load_portfolio          # noqa: E402
from argia.core.job_log import instrument             # noqa: E402
from argia.core.sheets import SheetsClient            # noqa: E402
from argia.finance.income import Period               # noqa: E402
from argia.finance.report import (                    # noqa: E402
    build_finance_report_data, render_html,
)
from argia.report.output import (                     # noqa: E402
    append_outbox, html_to_pdf,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.report_finance")

MX = ZoneInfo("America/Mexico_City")


def default_period(today: dt.date | None = None) -> Period:
    d = today or dt.datetime.now(MX).date()
    return Period(dt.date(d.year, d.month, 1), d)


@instrument("report_finance")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--start", default=None,
                        help="period start YYYY-MM-DD (default: 1st of "
                             "the current MX month)")
    parser.add_argument("--end", default=None,
                        help="period end YYYY-MM-DD inclusive (default: "
                             "today, MX)")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--html-only", action="store_true",
                        help="skip PDF rendering")
    parser.add_argument("--dry-run", action="store_true",
                        help="write files locally; no Drive upload, no "
                             "outbox row")
    args = parser.parse_args(argv)

    if args.start or args.end:
        if not (args.start and args.end):
            log.error("--start and --end must be given together")
            return 3
        period = Period.from_iso(args.start, args.end)
    else:
        period = default_period()

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

    data = build_finance_report_data(sheets, portfolio, period)
    if not data.assets:
        log.warning("no assets resolved — is Contract_Monthly populated?")
        return 2
    log.info("Finance report %s..%s: %d assets, expected %.0f MXN, "
             "actual %.0f MXN, service %.0f MXN",
             period.start, period.end, len(data.assets),
             data.expected_total, data.actual_total, data.service_total)

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_finance_")
    os.makedirs(out_dir, exist_ok=True)
    base = "ARGIA_Finance_%s_%s" % (period.start.isoformat(),
                                    period.end.isoformat())
    html_path = os.path.join(out_dir, base + ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(data))
    log.info("HTML written: %s (%d bytes)", html_path,
             os.path.getsize(html_path))

    pdf_path = None
    if not args.html_only:
        pdf_path = os.path.join(out_dir, base + ".pdf")
        try:
            html_to_pdf(html_path, pdf_path)
            log.info("PDF written: %s (%d bytes)", pdf_path,
                     os.path.getsize(pdf_path))
        except Exception as e:  # noqa: BLE001
            log.error("PDF conversion failed: %s", e)
            return 4

    if args.dry_run:
        log.info("[dry-run] no upload, no outbox row")
        return 0

    from argia.core.drive import DriveClient  # local import as in daily
    drive = DriveClient()
    reports_id = drive.ensure_folder(folder_id, "Reports")
    pdf_id = None
    if pdf_path:
        pdf_id = drive.upload_file(reports_id, base + ".pdf", pdf_path,
                                   "application/pdf")
    html_id = drive.upload_file(reports_id, base + ".html", html_path,
                                "text/html")
    try:
        append_outbox(sheets, date_iso=period.end.isoformat(),
                      kind="finance_period", pdf_file_id=pdf_id,
                      html_file_id=html_id,
                      now_utc_iso=dt.datetime.now(dt.timezone.utc)
                      .isoformat(timespec="seconds"),
                      channel="shareholders")
        log.info("Report_Outbox row appended (finance_period, "
                 "shareholders)")
    except Exception as e:  # noqa: BLE001
        log.error("Report_Outbox append failed (report IS uploaded): %s",
                  e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
