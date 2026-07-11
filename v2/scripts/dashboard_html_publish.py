"""Render the HTML dashboard from the Dashboard tabs and publish it to GCS.

Reads Dashboard_Plant / Dashboard_Inverter (the shared truth the Looker
report and alert engine also see), renders one self-contained HTML file, and
uploads it to a private Google Cloud Storage bucket viewable at:

    https://storage.cloud.google.com/<bucket>/dashboard.html

Upload auth reuses the SAME service account as the Sheets client
(GOOGLE_CREDENTIALS) — grant it "Storage Object Admin" on the bucket once;
no new secret. Viewers are plain Google accounts granted "Storage Object
Viewer" on the bucket.

Dry-run by default: renders to a local file, uploads nothing.

Usage (from v2/):
  PYTHONPATH=. python scripts/dashboard_html_publish.py                # render only
  PYTHONPATH=. python scripts/dashboard_html_publish.py --apply       # render + upload
  PYTHONPATH=. python scripts/dashboard_html_publish.py --out /tmp/d.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
import sys
from zoneinfo import ZoneInfo

import google.auth.transport.requests
from google.oauth2.service_account import Credentials

from argia.core.sheets import SheetsClient
from argia.report import dashboard_html
from argia.core.job_log import apply_flag_write_if, instrument

MX_TZ = ZoneInfo("America/Mexico_City")
OBJECT_NAME = "dashboard.html"
GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


NUMERIC_PLANT = {"kwp_dc", "total_kwh", "theoretical_kwh", "cloud_cover_pct",
                 "inverters_total", "inverters_reporting", "inverters_faulted"}
NUMERIC_INV = {"energy_kwh", "temperature_c"}


def coerce_rows(rows: list[dict], numeric: set) -> list[dict]:
    """Sheets returns everything as strings; the renderer wants numbers."""
    out = []
    for r in rows:
        c = dict(r)
        for k in numeric:
            c[k] = _num(c.get(k))
        out.append(c)
    return out


def active_plants(plant_config_rows: list[dict]) -> list[str]:
    """plant_keys for the dashboard selector: active=TRUE and not
    explicitly hidden by show_dashboard (v74 report-axis flag —
    blank/absent means visible, mirroring parse_plants)."""
    out = []
    for r in plant_config_rows:
        pk = r.get("plant_key")
        if not pk:
            continue
        if str(r.get("active")).strip().upper() not in ("TRUE", "1",
                                                        "YES"):
            continue
        if str(r.get("show_dashboard", "")).strip().upper() in (
                "FALSE", "0", "NO"):
            continue
        out.append(pk)
    return sorted(out)


def upload_to_gcs(bucket: str, object_name: str, html: str,
                  credentials_json: str | None = None,
                  session=None) -> None:
    """Upload via the JSON API using the existing service account.

    ``session`` is injectable for tests; production builds an AuthorizedSession
    from GOOGLE_CREDENTIALS with the storage scope.
    """
    if session is None:
        raw = credentials_json or os.environ.get("GOOGLE_CREDENTIALS", "")
        if not raw:
            raise RuntimeError("GOOGLE_CREDENTIALS not set")
        creds = Credentials.from_service_account_info(
            json.loads(raw), scopes=[GCS_SCOPE])
        session = google.auth.transport.requests.AuthorizedSession(creds)
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
           f"?uploadType=media&name={object_name}")
    resp = session.post(
        url, data=html.encode("utf-8"),
        headers={"Content-Type": "text/html; charset=utf-8",
                 "Cache-Control": "no-cache"})
    if resp.status_code != 200:
        raise RuntimeError(
            f"GCS upload failed: HTTP {resp.status_code}: {resp.text[:300]}")


def run(client: SheetsClient, *, out_path: str, apply: bool,
        bucket: str | None, session=None) -> int:
    # A1:ZZ everywhere (see dashboard_update.py note): the A1:P read
    # here silently dropped the 17th Dashboard_Inverter column —
    # fault_events — killing the "fault today" UI from the day it
    # shipped (v67) until this fix.
    plant_cfg = client.read_table("Plants", "A1:ZZ")
    prows = coerce_rows(client.read_table("Dashboard_Plant", "A1:ZZ"),
                        NUMERIC_PLANT)
    irows = coerce_rows(client.read_table("Dashboard_Inverter", "A1:ZZ"),
                        NUMERIC_INV)
    plants = active_plants(plant_cfg)
    # v84: the Dashboard tabs now store ALL active plants (CAPEX rows
    # feed the per-client pages); this internal page must embed ONLY
    # the show_dashboard set — filtering the rows, not just the
    # selector, so hidden plants' data never ships in the payload.
    visible = set(plants)
    prows = [r for r in prows if str(r.get("plant_key", "")) in visible]
    irows = [r for r in irows if str(r.get("plant_key", "")) in visible]
    now = dt.datetime.now(MX_TZ).strftime("%Y-%m-%d %H:%M")

    html = dashboard_html.render(prows, irows, generated_at=now,
                                 active_plants=plants)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"rendered {out_path}: {len(html)//1024} KiB, "
          f"{len(prows)} plant rows, {len(irows)} inverter rows, "
          f"plants={plants}")

    if not apply:
        print("[dry-run] not uploading (pass --apply to publish)")
        return 0
    if not bucket:
        print("NOTICE: GCS_DASHBOARD_BUCKET not set — skipping upload. "
              "Set the secret to enable publishing.")
        return 0
    upload_to_gcs(bucket, OBJECT_NAME, html, session=session)
    print(f"[apply] uploaded to gs://{bucket}/{OBJECT_NAME} — view at "
          f"https://storage.cloud.google.com/{bucket}/{OBJECT_NAME}")
    return 0


@instrument("dashboard_publish", write_if=apply_flag_write_if)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish HTML dashboard")
    ap.add_argument("--apply", action="store_true",
                    help="upload to GCS (default: render locally only)")
    # Default OUTSIDE the working tree (2026-07-07: writing into the
    # repo left an untracked build artifact that tripped deploy.sh's
    # dirty-tree guard on the Pi — three pushes sat undelivered).
    ap.add_argument("--out",
                    default=os.path.join(
                        os.environ.get("ARGIA_LOG_DIR", tempfile.gettempdir()),
                        "dashboard.html"),
                    help="local output path (default $ARGIA_LOG_DIR/"
                         "dashboard.html, falling back to the system tmp "
                         "dir — NEVER inside the repo)")
    args = ap.parse_args(argv)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2")
    if not sheet_id:
        print("ERROR: GOOGLE_SHEET_ID_V2 not set", file=sys.stderr)
        return 2
    client = SheetsClient(sheet_id)
    return run(client, out_path=args.out, apply=args.apply,
               bucket=os.environ.get("GCS_DASHBOARD_BUCKET"))


if __name__ == "__main__":
    sys.exit(main())
