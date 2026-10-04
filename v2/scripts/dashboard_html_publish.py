"""Render the HTML dashboard from the Dashboard tabs into a local file.

Reads Dashboard_Plant / Dashboard_Inverter and renders one self-contained
HTML file. v305 (Tomasz, 2026-10-04: "I am not using Google Cloud for
anything"): the upload to the Google Cloud Storage bucket is gone - the
portal (portal.argia.com.mx) is the dashboard; this script stays a local
render tool for checks and tests. No timer runs it.

Usage (from v2/):
  PYTHONPATH=. python scripts/dashboard_html_publish.py                # render to $ARGIA_LOG_DIR/dashboard.html
  PYTHONPATH=. python scripts/dashboard_html_publish.py --out /tmp/d.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import tempfile
import sys
from zoneinfo import ZoneInfo

from argia.core.sheets import SheetsClient, open_sheets
from argia.report import dashboard_html
from argia.core.job_log import apply_flag_write_if, instrument

MX_TZ = ZoneInfo("America/Mexico_City")
OBJECT_NAME = "dashboard.html"


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
    explicitly hidden by show_dashboard (v74 report-axis flag -
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


def run(client: SheetsClient, *, out_path: str) -> int:
    # A1:ZZ everywhere (see dashboard_update.py note): the A1:P read
    # here silently dropped the 17th Dashboard_Inverter column -
    # fault_events - killing the "fault today" UI from the day it
    # shipped (v67) until this fix.
    from argia.core.config_pg import plants_records
    plant_cfg = plants_records(client, "A1:ZZ")                   # v198 door
    from argia.report import dashboard_pg as DP           # v195 door
    prows = coerce_rows(DP.plant_records(client), NUMERIC_PLANT)
    irows = coerce_rows(DP.inverter_records(client), NUMERIC_INV)
    plants = active_plants(plant_cfg)
    # v84: the Dashboard tabs now store ALL active plants (CAPEX rows
    # feed the per-client pages); this internal page must embed ONLY
    # the show_dashboard set - filtering the rows, not just the
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

    return 0


@instrument("dashboard_publish", write_if=apply_flag_write_if)   # a local render never logs a run
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Publish HTML dashboard")
    # Default OUTSIDE the working tree (2026-07-07: writing into the
    # repo left an untracked build artifact that tripped deploy.sh's
    # dirty-tree guard on the Pi - three pushes sat undelivered).
    ap.add_argument("--out",
                    default=os.path.join(
                        os.environ.get("ARGIA_LOG_DIR", tempfile.gettempdir()),
                        "dashboard.html"),
                    help="local output path (default $ARGIA_LOG_DIR/"
                         "dashboard.html, falling back to the system tmp "
                         "dir - NEVER inside the repo)")
    args = ap.parse_args(argv)
    try:
        client = open_sheets()          # v199: NullSheets once retired
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return run(client, out_path=args.out)


if __name__ == "__main__":
    sys.exit(main())
