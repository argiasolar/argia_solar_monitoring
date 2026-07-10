"""Publish a daily performance report page per client channel.

For every distinct ``client_channel`` on active plants (or one via
--channel), renders the daily PERFORMANCE REPORT scoped to that
client's plants and uploads it to GCS as ``<channel>.html``:

    tetrapack.html   sms.html   budenheim.html   hirschmann.html

Nothing is mailed — no Report_Outbox row, no Drive upload; this is the
give-the-client-a-URL path. Isolation is inherited from v76/v77: the
page can only contain the channel's plants and the channel's alerts.

Date defaults to TODAY (MX): the page is the client's live view
(v71 "DAY IN PROGRESS" semantics apply until the day is stamped);
re-running replaces the object, so a cron keeps it fresh and the
morning run after kpi-eod turns yesterday's numbers final.

Bucket: GCS_CLIENT_BUCKET, falling back to GCS_DASHBOARD_BUCKET.
CAUTION before granting any client viewer rights: bucket permissions
are bucket-wide — clients sharing one bucket can open each other's
pages. Per-client buckets are the isolation lever when onboarding.

Usage (from v2/):
  PYTHONPATH=. python scripts/client_reports_publish.py            # dry-run
  PYTHONPATH=. python scripts/client_reports_publish.py --apply
  PYTHONPATH=. python scripts/client_reports_publish.py --channel sms --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
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
from argia.report.daily import (                        # noqa: E402
    build_report_data, render_html,
)
from scripts.dashboard_html_publish import upload_to_gcs  # noqa: E402

MX_TZ = ZoneInfo("America/Mexico_City")

_TOKEN = re.compile(r"^[a-z0-9_]+$")


def object_name(channel: str) -> str:
    """``<channel>.html`` — and refuse anything that isn't a clean
    channel token, because this string becomes a public-ish URL path.
    Config already normalizes channels; this is the defensive layer."""
    if not _TOKEN.match(channel or ""):
        raise ValueError("invalid client channel token: %r" % channel)
    return channel + ".html"


@instrument("client_reports_publish", write_if=apply_flag_write_if)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Publish per-client daily report pages")
    ap.add_argument("--apply", action="store_true",
                    help="upload to GCS (default: render locally only)")
    ap.add_argument("--date", default=None,
                    help="report date YYYY-MM-DD (default: today, MX)")
    ap.add_argument("--channel", default=None,
                    help="one channel only (default: all configured)")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)

    date_iso = args.date or dt.datetime.now(MX_TZ).date().isoformat()

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        print("GOOGLE_SHEET_ID_V2 not set")
        return 3
    client = SheetsClient(sheet_id=sheet_id)
    portfolio = load_portfolio(client)

    channels = ([args.channel] if args.channel
                else portfolio.client_channels())
    if not channels:
        print("no client channels configured — nothing to do")
        return 0

    bucket = (os.environ.get("GCS_CLIENT_BUCKET", "").strip()
              or os.environ.get("GCS_DASHBOARD_BUCKET", "").strip())
    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_clientpub_")
    os.makedirs(out_dir, exist_ok=True)
    failed = []

    for channel in channels:
        try:
            name = object_name(channel)
        except ValueError as e:
            print("skipping: %s" % e)
            failed.append(channel)
            continue
        view = portfolio.for_client_channel(channel)
        if not view.plants:
            print("channel %r: no active plants — skipped" % channel)
            continue
        try:
            data = build_report_data(client, view, date_iso)
            html = render_html(data)
        except Exception as e:  # noqa: BLE001
            print("channel %r: build failed: %s" % (channel, e))
            failed.append(channel)
            continue
        local = os.path.join(out_dir, name)
        with open(local, "w", encoding="utf-8") as f:
            f.write(html)
        print("rendered %s: %d KiB, %d plant(s) [%s], date %s"
              % (name, len(html) // 1024, len(view.plants),
                 ", ".join(sorted(view.plants)), date_iso))
        if not args.apply:
            continue
        if not bucket:
            print("NOTICE: no GCS bucket configured (GCS_CLIENT_BUCKET /"
                  " GCS_DASHBOARD_BUCKET) — rendered only.")
            continue
        try:
            upload_to_gcs(bucket, name, html)
            print("[apply] gs://%s/%s — view at "
                  "https://storage.cloud.google.com/%s/%s"
                  % (bucket, name, bucket, name))
        except Exception as e:  # noqa: BLE001
            print("channel %r: upload failed: %s" % (channel, e))
            failed.append(channel)

    if not args.apply:
        print("[dry-run] files in %s — nothing uploaded" % out_dir)
    return 5 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
