"""Per-client daily reports — one PERFORMANCE REPORT per client channel.

For every distinct ``client_channel`` on active plants (or one channel
via --channel), builds the same daily report the internal edition uses
— but scoped to THAT client's plants only: their plant cards, their
fleet totals, their alerts, their verdict. Uploads HTML+PDF to Drive
and queues each on Report_Outbox with channel=<client_channel>, so the
v46 notifier mails it to that client's Recipients rows.

Isolation guarantees (inherited, not reimplemented):
  * plants: Portfolio.for_client_channel() contains only the channel's
    active plants — nothing else can render;
  * alerts: build_report_data scopes the alert section and verdict to
    the same plant set (v76);
  * delivery: the notifier fails CLOSED on a channel without
    Recipients rows — a report for an unconfigured client goes
    nowhere, never to a default list.

Usage:
    PYTHONPATH=. python scripts/report_client_daily.py --dry-run
    PYTHONPATH=. python scripts/report_client_daily.py --channel acme
    PYTHONPATH=. python scripts/report_client_daily.py --date 2026-07-09

Exit codes: 0 ok (including "no client channels configured"),
3 bootstrap failure, 4 PDF failure, 5 one or more channels failed.
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
from argia.report.daily import (                      # noqa: E402
    build_report_data, render_html,
)
from argia.report.output import (                     # noqa: E402
    append_outbox, html_to_pdf,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("argia.report_client_daily")

MX = ZoneInfo("America/Mexico_City")


def default_date() -> str:
    """Yesterday, MX — client reports are KPI-final morning editions."""
    return (dt.datetime.now(MX).date() - dt.timedelta(days=1)).isoformat()


@instrument("report_client_daily")
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--date", default=None,
                        help="report date YYYY-MM-DD (default: "
                             "yesterday, MX)")
    parser.add_argument("--channel", default=None,
                        help="run one channel only (default: every "
                             "configured client channel)")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="render locally; no upload, no outbox")
    args = parser.parse_args(argv)

    date_iso = args.date or default_date()

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

    channels = ([args.channel] if args.channel
                else portfolio.client_channels())
    if not channels:
        log.info("no client channels configured — nothing to do")
        return 0

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_client_")
    os.makedirs(out_dir, exist_ok=True)
    failed = []

    for channel in channels:
        view = portfolio.for_client_channel(channel)
        if not view.plants:
            log.warning("channel %r: no active plants — skipped", channel)
            continue
        log.info("channel %r: %d plant(s) — %s", channel,
                 len(view.plants), ", ".join(sorted(view.plants)))
        try:
            data = build_report_data(sheets, view, date_iso)
        except Exception as e:  # noqa: BLE001
            log.error("channel %r: build failed: %s", channel, e)
            failed.append(channel)
            continue

        base = "ARGIA_Daily_%s_%s" % (channel, date_iso)
        html_path = os.path.join(out_dir, base + ".html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(data))
        pdf_path = os.path.join(out_dir, base + ".pdf")
        try:
            html_to_pdf(html_path, pdf_path)
        except Exception as e:  # noqa: BLE001
            log.error("channel %r: PDF failed: %s", channel, e)
            return 4
        log.info("channel %r: rendered %s (%d KiB pdf)", channel, base,
                 os.path.getsize(pdf_path) // 1024)

        if args.dry_run:
            continue
        from argia.core.drive import DriveClient
        drive = DriveClient()
        reports_id = drive.ensure_folder(folder_id, "Reports")
        pdf_id = drive.upload_file(reports_id, base + ".pdf", pdf_path,
                                   "application/pdf")
        html_id = drive.upload_file(reports_id, base + ".html",
                                    html_path, "text/html")
        try:
            append_outbox(sheets, date_iso=date_iso, kind="client_daily",
                          pdf_file_id=pdf_id, html_file_id=html_id,
                          now_utc_iso=dt.datetime.now(dt.timezone.utc)
                          .isoformat(timespec="seconds"),
                          channel=channel)
            log.info("channel %r: outbox row appended", channel)
        except Exception as e:  # noqa: BLE001
            log.error("channel %r: outbox append failed (report IS "
                      "uploaded): %s", channel, e)
            failed.append(channel)

    if args.dry_run:
        log.info("[dry-run] files in %s — no upload, no outbox", out_dir)
    return 5 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
