#!/usr/bin/env python3
"""Argia_Mont — Soiling assessment.

Reads KPI_Daily history + Plants (pr_baseline, tariff) + Cleaning_Costs,
prints a per-plant soiling assessment. Read-only — no alerts, no writes.

USAGE
    PYTHONPATH=. python scripts/soiling_check.py
    PYTHONPATH=. python scripts/soiling_check.py --as-of 2026-05-14
    PYTHONPATH=. python scripts/soiling_check.py --plant-key QRO1
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.analytics.soiling import (
    SoilingDecision,
    assess_plant_soiling,
    create_cleaning_costs_tab_if_missing,
    load_cleaning_costs,
)
from argia.archive.kpi_daily import load_kpi_daily
from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--as-of", default=None,
                        help="YYYY-MM-DD (default: today MX)")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--bootstrap-costs-tab", action="store_true",
                        help="Create Cleaning_Costs tab with one row per active plant")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.soiling_check")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    sheets = SheetsClient(sheet_id=sheet_id)

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        log.error("load_portfolio failed: %s", e)
        return 3

    if args.bootstrap_costs_tab:
        pks = sorted(p.plant_key for p in portfolio.active_plants())
        created = create_cleaning_costs_tab_if_missing(sheets, plant_keys=pks)
        log.info("Cleaning_Costs created: %s", created)
        if not args.plant_key and args.as_of is None:
            # If user only asked to bootstrap, exit
            return 0

    costs = load_cleaning_costs(sheets)
    history = load_kpi_daily(sheets)

    as_of = args.as_of or now_mx().date().isoformat()
    log.info("Soiling assessment as of %s, window=%d days, history=%d rows",
             as_of, args.window_days, len(history))

    plants = portfolio.active_plants()
    if args.plant_key:
        plants = [p for p in plants if p.plant_key == args.plant_key]
        if not plants:
            log.error("Plant %s not found", args.plant_key)
            return 1

    print()
    print(f"=== Soiling assessment as of {as_of} ===")
    print()
    print(
        f"{'Plant':10s} {'Decision':17s} {'PR(roll)':>9s} {'PR(base)':>9s} "
        f"{'Loss%':>7s} {'Loss$/mo':>10s} {'Cost$':>8s} Notes"
    )
    print("-" * 110)

    any_due = False
    for plant in plants:
        cost = costs.get(plant.plant_key)
        assessment = assess_plant_soiling(
            plant_key=plant.plant_key,
            as_of_date=as_of,
            kpi_history=history,
            pr_baseline=plant.pr_baseline,
            tariff_mxn_per_kwh=plant.tariff_mxn_per_kwh,
            cleaning_cost=cost,
            window_days=args.window_days,
        )

        if assessment.decision in (SoilingDecision.DUE, SoilingDecision.OVERDUE):
            any_due = True

        roll = f"{assessment.pr_rolling_median:.3f}" if assessment.pr_rolling_median else "--"
        base = f"{assessment.pr_baseline:.3f}" if assessment.pr_baseline else "--"
        loss_pct = (
            f"{assessment.pr_loss_pct * 100:.1f}%"
            if assessment.pr_loss_pct is not None else "--"
        )
        loss_mxn = (
            f"{assessment.projected_monthly_loss_mxn:.0f}"
            if assessment.projected_monthly_loss_mxn is not None else "--"
        )
        cost_str = (
            f"{assessment.cleaning_cost_mxn:.0f}"
            if assessment.cleaning_cost_mxn else "--"
        )

        marker = {
            SoilingDecision.DUE: " ⚠",
            SoilingDecision.OVERDUE: " 🚨",
        }.get(assessment.decision, "")

        print(
            f"{plant.plant_key:10s} "
            f"{assessment.decision.value + marker:17s} "
            f"{roll:>9s} {base:>9s} "
            f"{loss_pct:>7s} {loss_mxn:>10s} {cost_str:>8s} "
            f"{assessment.notes[:50]}"
        )

    print()
    return 0 if not any_due else 1  # 1 = action needed but ran cleanly


if __name__ == "__main__":
    sys.exit(main())
