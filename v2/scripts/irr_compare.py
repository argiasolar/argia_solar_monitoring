"""irr_compare — dense ShineMaster history vs snapshot trapezoid vs stored KPI.

The verification tool for the dense-irradiance rollout: for one date, for
every plant with a datalogger_sn, print

    plant | dense kWh/m² (samples) | snapshot kWh/m² (samples) | stored KPI

side by side. Read-only: touches no tabs, stamps nothing. Run it for a few
July days, eyeball the deltas (expectation from the 2026-07-06 analysis:
dense lands between the snapshot value and v1's model value, with hundreds
of samples instead of dozens), and only then enable --dense-irradiance on
the scheduled kpi_eod.

Usage (from v2/, needs GOOGLE_* and GROWATT_* env):
  PYTHONPATH=. python scripts/irr_compare.py --date 2026-07-05
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.cells import coerce_date
from argia.kpi.irradiance import integrate_history_points
from argia.kpi.irradiance import daily_irradiance_for_plant
from argia.kpi.reader import read_day_bundle
from argia.meteo.growatt_env import DEFAULT_ENV_ADDR, fetch_env_day
from argia.vendors.growatt_web import GrowattWebClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("irr_compare")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare irradiance methods")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (MX)")
    args = ap.parse_args(argv)
    date_iso = args.date

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not sheet_id or not user or not pwd:
        log.error("need GOOGLE_SHEET_ID_V2 + GROWATT_USERNAME/PASSWORD")
        return 3

    sheets = SheetsClient(sheet_id=sheet_id)
    portfolio = load_portfolio(sheets)
    bundle = read_day_bundle(sheets, date_iso)

    stored = {}
    for r in sheets.read_table("KPI_Daily", "A1:V"):
        if coerce_date(r.get("date_iso")) == dt.date.fromisoformat(date_iso):
            try:
                stored[r.get("plant_key")] = float(r.get("irradiance_kwh_m2"))
            except (TypeError, ValueError):
                pass

    web = GrowattWebClient(username=user, password=pwd)
    web.login()

    print(f"\n{'plant':6s} {'dense kWh/m2':>13s} {'(n)':>6s} "
          f"{'snapshot':>9s} {'(n)':>5s} {'storedKPI':>10s} {'d vs snap':>9s}")
    for plant in portfolio.plants:
        if not plant.datalogger_sn:
            continue
        rows = bundle.rows_for_plant(plant.plant_key)
        snap = daily_irradiance_for_plant(rows, lat=plant.lat,
                                          date_iso=date_iso)
        addr = int(plant.datalogger_addr or DEFAULT_ENV_ADDR)
        try:
            points = fetch_env_day(web, plant.datalogger_sn, addr, date_iso)
            dense = integrate_history_points(points)
        except Exception as e:  # noqa: BLE001
            print(f"{plant.plant_key:6s}  FETCH FAILED: {e}")
            continue
        d = dense.kwh_m2
        sv = snap.kwh_m2
        delta = (f"{100 * (d - sv) / sv:+5.1f}%" if d and sv else "    –")
        print(f"{plant.plant_key:6s} {d if d else 0:13.3f} "
              f"{dense.samples_used:6d} {sv if sv else 0:9.3f} "
              f"{snap.samples_used:5d} "
              f"{stored.get(plant.plant_key, float('nan')):10.3f} {delta}")
    print("\nread-only comparison — nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
