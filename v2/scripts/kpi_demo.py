#!/usr/bin/env python3
"""Argia_Mont — KPI demo (read-only).

For a given date, computes and prints:
- Per-plant energy, irradiance, PR, capacity factor
- Per-inverter peer ranking within each plant

This is NOT a production cron entry. It exists to let you eyeball the
KPI math against real data BEFORE Stage 7.4 starts firing alerts based
on these numbers.

USAGE
    PYTHONPATH=. python scripts/kpi_demo.py
    PYTHONPATH=. python scripts/kpi_demo.py --date 2026-05-13
    PYTHONPATH=. python scripts/kpi_demo.py --plant-key QRO1 --log-level DEBUG

EXIT CODE
    0  ran cleanly
    1  partial — some plants had no data
    3  config error (sheet unreachable, etc.)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.kpi import (
    compute_inverter_peer_ranking,
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
    """Default date: yesterday in MX local time. Today's data is incomplete
    until midnight passes."""
    import datetime as dt
    return (now_mx().date() - dt.timedelta(days=1)).isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--date", default=None,
        help="Local plant date YYYY-MM-DD (default: yesterday MX)",
    )
    parser.add_argument(
        "--plant-key", default=None,
        help="Limit to one plant",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.kpi_demo")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 is not set")
        return 3

    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:
        log.error("SheetsClient failed: %s", e)
        return 3

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        log.error("load_portfolio failed: %s", e)
        return 3

    date_iso = args.date or _yesterday_mx_iso()
    log.info("Computing KPIs for date %s", date_iso)

    bundle = read_day_bundle(sheets, date_iso)
    log.info("DayBundle has %d rows across %d plants",
             len(bundle.rows), len(bundle.plant_keys()))

    plants_to_process = portfolio.active_plants()
    if args.plant_key:
        plants_to_process = [
            p for p in plants_to_process if p.plant_key == args.plant_key
        ]
        if not plants_to_process:
            log.error("Plant key '%s' not found or not active", args.plant_key)
            return 1

    # Header
    print()
    print(f"=== Argia_Mont KPI demo for {date_iso} ===")
    print()
    print(
        f"{'Plant':10s} {'Energy':>10s} {'H':>8s} {'PR':>7s} {'PR-conf':>8s} "
        f"{'CF':>6s} {'CF-conf':>8s} {'src':>14s} Notes"
    )
    print("-" * 108)

    plants_with_data = 0
    plants_without = 0

    for plant in plants_to_process:
        rows = bundle.rows_for_plant(plant.plant_key)
        if not rows:
            print(f"{plant.plant_key:10s} {'--':>10s} -- no telemetry for this date")
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

        energy_str = f"{perf.energy_kwh:.1f}" if perf.energy_kwh is not None else "--"
        irr_str = f"{perf.irradiance_kwh_m2:.2f}" if perf.irradiance_kwh_m2 is not None else "--"
        pr_str = f"{perf.pr:.3f}" if perf.pr is not None else "--"
        cf_str = f"{perf.capacity_factor:.3f}" if perf.capacity_factor is not None else "--"

        print(
            f"{plant.plant_key:10s} "
            f"{energy_str:>10s} "
            f"{irr_str:>8s} "
            f"{pr_str:>7s} "
            f"{perf.pr_confidence.value:>8s} "
            f"{cf_str:>6s} "
            f"{perf.capacity_factor_confidence.value:>8s} "
            f"{perf.irradiance_source.value:>14s} "
            f"{perf.notes}"
        )

        # Per-inverter peer ranking
        inv_meta = {
            inv.inverter_sn: {
                "rated_kw": inv.rated_kw,
                "inverter_label": inv.inverter_label,
            }
            for inv in portfolio.inverters_for(plant.plant_key)
        }
        ranks = compute_inverter_peer_ranking(
            plant.plant_key, energy_by_inv, inv_meta,
        )
        for r in ranks:
            sy_str = (
                f"{r.specific_yield_kwh_per_kwp:5.2f}"
                if r.specific_yield_kwh_per_kwp is not None else "  --"
            )
            rel_str = (
                f"{r.relative_to_peer * 100:5.1f}%"
                if r.relative_to_peer is not None else "  -- "
            )
            marker = ""
            if r.relative_to_peer is not None and r.relative_to_peer < 0.85:
                marker = " ⚠"
            energy_inv = f"{r.energy_kwh:7.1f}" if r.energy_kwh is not None else "    --"
            print(
                f"   {r.inverter_label[:20]:20s} "
                f"E={energy_inv} kWh  "
                f"yield={sy_str} kWh/kWp  ({rel_str} of peers){marker}"
            )
        print()

    print(f"Summary: {plants_with_data} plant(s) with data, {plants_without} without")

    if plants_without and not plants_with_data:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
