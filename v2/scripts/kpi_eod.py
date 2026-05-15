#!/usr/bin/env python3
"""Argia_Mont — End-of-day KPI archival.

Runs ONCE PER DAY, after the day's telemetry has fully landed. Typically
scheduled in cron around 01:30 MX (when even slow vendors have flushed).

Steps:
1. Load yesterday's Telemetry_Argia rows (MX local date)
2. For each active plant: compute energy, irradiance, PR, capacity factor
3. Upsert one row per (plant, yesterday) into KPI_Daily
4. Optionally prune rows older than 14 days

USAGE
    PYTHONPATH=. python scripts/kpi_eod.py
    PYTHONPATH=. python scripts/kpi_eod.py --date 2026-05-13
    PYTHONPATH=. python scripts/kpi_eod.py --dry-run
    PYTHONPATH=. python scripts/kpi_eod.py --prune
    PYTHONPATH=. python scripts/kpi_eod.py --prune-apply   # ACTUALLY DELETE

EXIT CODES
    0  ran cleanly, KPIs upserted
    1  partial — some plants had no data
    2  nothing written (no data anywhere)
    3  config error
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from typing import List

from argia.archive.kpi_daily import (
    HOT_WINDOW_DAYS,
    create_kpi_daily_tab_if_missing,
    perf_to_row,
    prune_old_rows,
    upsert_kpi_rows,
)
from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.kpi import (
    compute_plant_energy,
    compute_plant_pr,
    read_day_bundle,
)
from argia.kpi.irradiance import daily_irradiance_for_plant


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _yesterday_mx_iso() -> str:
    return (now_mx().date() - dt.timedelta(days=1)).isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--date", default=None,
        help="Local date YYYY-MM-DD (default: yesterday MX)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and log results, do not write to KPI_Daily",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help=f"Find rows older than {HOT_WINDOW_DAYS} days but DO NOT delete (preview)",
    )
    parser.add_argument(
        "--prune-apply", action="store_true",
        help=f"Actually delete rows older than {HOT_WINDOW_DAYS} days. DESTRUCTIVE.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.kpi_eod")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3

    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:
        log.error("SheetsClient failed: %s", e)
        return 3

    # Bootstrap KPI_Daily if needed
    try:
        created = create_kpi_daily_tab_if_missing(sheets)
        if created:
            log.info("Created KPI_Daily tab")
    except Exception as e:
        log.warning("Could not bootstrap KPI_Daily: %s", e)

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        log.error("load_portfolio failed: %s", e)
        return 3

    date_iso = args.date or _yesterday_mx_iso()
    log.info("Computing EOD KPIs for date %s", date_iso)
    bundle = read_day_bundle(sheets, date_iso)

    new_rows: List = []
    plants_with_data = 0
    plants_without = 0
    for plant in portfolio.active_plants():
        rows = bundle.rows_for_plant(plant.plant_key)
        if not rows:
            log.info("[%s] no telemetry for %s — skipping",
                     plant.plant_key, date_iso)
            plants_without += 1
            continue
        plants_with_data += 1

        energy_by_inv = compute_plant_energy(rows)
        irr = daily_irradiance_for_plant(rows, lat=plant.lat, date_iso=date_iso)
        perf = compute_plant_pr(
            plant_key=plant.plant_key, date_iso=date_iso,
            kwp_dc=plant.kwp_dc, kwp_ac=plant.kwp_ac,
            energy_per_inverter=energy_by_inv,
            irradiance=irr,
            inverter_count_expected=len(portfolio.inverters_for(plant.plant_key)),
        )
        new_rows.append(perf_to_row(perf))
        log.info(
            "[%s] energy=%s kWh  PR=%s (%s)  CF=%s (%s)",
            plant.plant_key,
            f"{perf.energy_kwh:.1f}" if perf.energy_kwh else "--",
            f"{perf.pr:.3f}" if perf.pr else "--",
            perf.pr_confidence.value,
            f"{perf.capacity_factor:.3f}" if perf.capacity_factor else "--",
            perf.capacity_factor_confidence.value,
        )

    # Upsert
    if new_rows:
        stats = upsert_kpi_rows(sheets, new_rows, dry_run=args.dry_run)
        log.info("KPI_Daily upsert: %s", stats)
    else:
        log.warning("No KPI rows to write")

    # Prune (optional)
    if args.prune or args.prune_apply:
        today_iso = now_mx().date().isoformat()
        result = prune_old_rows(
            sheets, today_iso=today_iso,
            window_days=HOT_WINDOW_DAYS,
            apply=args.prune_apply,
        )
        log.info("Prune: %s", result)

    if plants_with_data == 0:
        return 2
    if plants_without > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
