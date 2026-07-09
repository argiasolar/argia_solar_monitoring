"""Render and publish financial_report.html — the online, interactive
version of the finance report (calendar from–to picker).

Mirrors dashboard_html_publish: renders one self-contained HTML file
and uploads it to the private GCS bucket the dashboard uses (viewers =
Google accounts with Storage Object Viewer). The bucket can be
overridden with GCS_FINANCE_BUCKET to give financial data a stricter
audience than the ops dashboard.

Window: the picker can select any range inside [--window-start,
--window-end] (defaults: 2026-07-01, the v2 KPI epoch, through the end
of the current MX month + 1 — enough for MTD, previous month and
forward-looking expected).

Dry-run by default: renders locally, uploads nothing.

Usage (from v2/):
  PYTHONPATH=. python scripts/financial_report_publish.py             # render only
  PYTHONPATH=. python scripts/financial_report_publish.py --apply     # render + upload
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argia.core.config import load_portfolio            # noqa: E402
from argia.core.job_log import (                        # noqa: E402
    apply_flag_write_if, instrument,
)
from argia.core.sheets import SheetsClient              # noqa: E402
from argia.finance.income import Period                 # noqa: E402
from argia.finance.webreport import (                   # noqa: E402
    build_daily_atoms, render_financial_report_html,
)
from scripts.dashboard_html_publish import upload_to_gcs  # noqa: E402

MX_TZ = ZoneInfo("America/Mexico_City")
OBJECT_NAME = "financial_report.html"
V2_EPOCH = "2026-07-01"   # first KPI_Daily day


def default_window(today: dt.date | None = None) -> Period:
    d = today or dt.datetime.now(MX_TZ).date()
    nxt_y, nxt_m = (d.year + 1, 1) if d.month == 12 else (d.year,
                                                          d.month + 1)
    end = dt.date(nxt_y, nxt_m, calendar.monthrange(nxt_y, nxt_m)[1])
    return Period(dt.date.fromisoformat(V2_EPOCH), end)


@instrument("financial_report_publish", write_if=apply_flag_write_if)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Publish the interactive financial report")
    ap.add_argument("--apply", action="store_true",
                    help="upload to GCS (default: render locally only)")
    ap.add_argument("--window-start", default=None)
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--out",
                    default=os.path.join(
                        os.environ.get("ARGIA_LOG_DIR",
                                       tempfile.gettempdir()),
                        OBJECT_NAME),
                    help="local output path (default $ARGIA_LOG_DIR/"
                         + OBJECT_NAME + ")")
    args = ap.parse_args(argv)

    if args.window_start or args.window_end:
        if not (args.window_start and args.window_end):
            print("--window-start and --window-end must be given together")
            return 3
        window = Period.from_iso(args.window_start, args.window_end)
    else:
        window = default_window()

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        print("GOOGLE_SHEET_ID_V2 not set")
        return 3
    client = SheetsClient(sheet_id=sheet_id)
    portfolio = load_portfolio(client)

    data = build_daily_atoms(client, portfolio, window)
    now = dt.datetime.now(MX_TZ).strftime("%Y-%m-%d %H:%M")
    html = render_financial_report_html(data, generated_at=now)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("rendered %s: %d KiB, %d plants, %d days, actuals through %s"
          % (args.out, len(html) // 1024, len(data["plants"]),
             len(data["days"]), data["last_actual_day"]))

    if not args.apply:
        print("[dry-run] not uploading (pass --apply to publish)")
        return 0
    bucket = (os.environ.get("GCS_FINANCE_BUCKET", "").strip()
              or os.environ.get("GCS_DASHBOARD_BUCKET", "").strip())
    if not bucket:
        print("NOTICE: no GCS bucket configured (GCS_FINANCE_BUCKET / "
              "GCS_DASHBOARD_BUCKET) — skipping upload.")
        return 0
    upload_to_gcs(bucket, OBJECT_NAME, html)
    print("[apply] uploaded to gs://%s/%s — view at "
          "https://storage.cloud.google.com/%s/%s"
          % (bucket, OBJECT_NAME, bucket, OBJECT_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
