#!/usr/bin/env python3
"""
Argia_Mont — daily aggregate.

Writes one row per active plant to DailyProduction. Idempotent on
(date, plant_key) — re-running for the same date updates existing rows
rather than appending duplicates.

USAGE
    python scripts/argia_mont_daily.py
    python scripts/argia_mont_daily.py --date 2026-05-10
    python scripts/argia_mont_daily.py --dry-run
    python scripts/argia_mont_daily.py --plant-key SLP1

EXIT CODES
    0  all plants succeeded
    1  partial — some plants failed (others succeeded)
    2  total failure (no plants processed)
    3  config error (sheet unreachable, etc.)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.orchestrator import run_daily


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _yesterday_iso_mx() -> str:
    """Default date: yesterday in MX time (the day we usually want to aggregate)."""
    from argia.core.time_utils import now_mx
    import datetime as dt

    yesterday = now_mx().date() - dt.timedelta(days=1)
    return yesterday.isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--date",
        default=_yesterday_iso_mx(),
        help="ISO date to aggregate (default: yesterday in MX time)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and compute, but do not write to the sheet",
    )
    parser.add_argument(
        "--plant-key",
        default=None,
        help="Run only this one plant (e.g. SLP1)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.daily")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 is not set — cannot continue")
        return 3

    try:
        sheets = SheetsClient(sheet_id=sheet_id)
    except Exception as e:
        log.error("Failed to construct SheetsClient: %s", e)
        return 3

    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        log.error("Failed to load portfolio: %s", e)
        return 3

    log.info(
        "Loaded portfolio: %d plants (%d active), %d inverter rows",
        len(portfolio.plants),
        len([p for p in portfolio.plants.values() if p.active]),
        sum(len(v) for v in portfolio.inverters_by_plant.values()),
    )

    result = run_daily(
        sheets=sheets,
        portfolio=portfolio,
        date_iso=args.date,
        dry_run=args.dry_run,
        only_plant=args.plant_key,
    )

    log.info(
        "Run %s done: status=%s plants=%d skipped=%d rows=%d errors=%d",
        result.run_id, result.status,
        result.plants_processed, result.plants_skipped,
        result.rows_written, len(result.errors),
    )
    for err in result.errors:
        log.error("  %s", err)

    if result.status == "OK":
        return 0
    if result.status == "PARTIAL":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
