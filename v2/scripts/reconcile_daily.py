#!/usr/bin/env python3
"""Stage 1 — Daily energy reconciliation: v2 KPI_Daily vs v1 DailyData.

READ-ONLY. This script never writes to either spreadsheet. It reads both,
joins per (plant, day), and prints a table + summary so you can see whether
v2's collection matches v1's within tolerance. Energy is the gate; PR is shown
alongside purely for diagnosis (config/irradiance divergence is expected once
v2's corrected plant sizes kick in — it does NOT fail the gate).

Run it during the parallel-collector window (v1 on the Pi, v2 on GitHub). Once
v2 moves to the Pi and dual-writes from one poll, this comparison stops being a
real test of collection quality.

USAGE
    PYTHONPATH=. python scripts/reconcile_daily.py
    PYTHONPATH=. python scripts/reconcile_daily.py --days 21
    PYTHONPATH=. python scripts/reconcile_daily.py --start 2026-06-30 --end 2026-07-10
    PYTHONPATH=. python scripts/reconcile_daily.py --tolerance 1.0
    PYTHONPATH=. python scripts/reconcile_daily.py --include-today
    PYTHONPATH=. python scripts/reconcile_daily.py --csv reconcile.csv

ENV
    GOOGLE_SHEET_ID_V2   Argia_Mont_v2 spreadsheet id (KPI_Daily lives here)
    GOOGLE_SHEET_ID_V1   ARGIA_Solar spreadsheet id (DailyData lives here).
                         Falls back to the known ARGIA_Solar id if unset.
    GOOGLE_CREDENTIALS   service-account JSON (must have read on BOTH sheets)

EXIT CODES
    0  every overlapping full day is within energy tolerance (PR may diverge)
    1  at least one ENERGY-MISMATCH on an overlapping day (collection gap)
    2  no overlapping days to compare (nothing proven yet)
    3  config / credentials error
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import sys
from typing import List, Optional, Set

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.kpi.reconcile import (
    BUCKET_ENERGY,
    BUCKET_MISSING_V1,
    BUCKET_MISSING_V2,
    BUCKET_OK,
    BUCKET_PR,
    ReconcileRow,
    build_reconcile,
    summarize,
)

# ARGIA_Solar (v1 financial workbook) — stable, known id. Overridable via env
# GOOGLE_SHEET_ID_V1 or --v1-sheet-id.
DEFAULT_V1_SHEET_ID = "16rzpz5gvzSh4WdBQ2qv7pD_EY0V7r0IrvfKVj1Fl0wk"

V1_TAB = "DailyData"
V2_TAB = "KPI_Daily"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _fmt(v: Optional[float], nd: int = 1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def _print_table(rows: List[ReconcileRow]) -> None:
    if not rows:
        print("(no rows in range)")
        return
    hdr = (f"{'date':<11}{'plant':<7}{'v1_kWh':>10}{'v2_kWh':>10}"
           f"{'dE%':>8}{'v1_PR':>8}{'v2_PR':>8}{'dPR%':>8}  bucket")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r.date_iso:<11}{r.plant_key:<7}"
            f"{_fmt(r.v1_energy_kwh):>10}{_fmt(r.v2_energy_kwh):>10}"
            f"{_fmt(r.energy_delta_pct):>8}"
            f"{_fmt(r.v1_pr, 3):>8}{_fmt(r.v2_pr, 3):>8}"
            f"{_fmt(r.pr_delta_pct):>8}  {r.bucket}"
        )


def _write_csv(path: str, rows: List[ReconcileRow]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "date_iso", "plant_key", "v1_energy_kwh", "v2_energy_kwh",
            "energy_delta_pct", "v1_irr", "v2_irr", "v1_kwp",
            "v1_pr", "v2_pr", "pr_delta_pct", "bucket", "within_tolerance", "note",
        ])
        for r in rows:
            w.writerow([
                r.date_iso, r.plant_key, r.v1_energy_kwh, r.v2_energy_kwh,
                r.energy_delta_pct, r.v1_irr, r.v2_irr, r.v1_kwp,
                r.v1_pr, r.v2_pr, r.pr_delta_pct, r.bucket,
                r.within_tolerance, r.note,
            ])


def _date_range_set(start: str, end: str) -> Set[str]:
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    out, cur = set(), d0
    while cur <= d1:
        out.add(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=int, default=14,
                        help="Look back this many days from yesterday (default 14). "
                             "Ignored if --start/--end given.")
    parser.add_argument("--start", default=None, help="ISO start date (inclusive)")
    parser.add_argument("--end", default=None, help="ISO end date (inclusive)")
    parser.add_argument("--tolerance", type=float, default=2.0,
                        help="Energy gate threshold, percent (default 2.0)")
    parser.add_argument("--include-today", action="store_true",
                        help="Include today's (likely partial) day. Off by default.")
    parser.add_argument("--v1-sheet-id", default=None,
                        help="Override ARGIA_Solar sheet id (else env/known default)")
    parser.add_argument("--csv", default=None, help="Also write results to this CSV path")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.reconcile_daily")

    v2_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not v2_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    v1_id = (args.v1_sheet_id
             or os.environ.get("GOOGLE_SHEET_ID_V1", "").strip()
             or DEFAULT_V1_SHEET_ID)

    try:
        v2_sheets = SheetsClient(sheet_id=v2_id)
        v1_sheets = SheetsClient(sheet_id=v1_id)
    except Exception as e:  # noqa: BLE001 - surface any auth/config failure
        log.error("SheetsClient init failed: %s", e)
        return 3

    try:
        portfolio = load_portfolio(v2_sheets)
    except Exception as e:  # noqa: BLE001
        log.error("load_portfolio failed: %s", e)
        return 3
    active_plants = {p.plant_key for p in portfolio.active_plants()}
    log.info("Active plants: %s", ", ".join(sorted(active_plants)))

    # Date window.
    include_dates: Optional[Set[str]] = None
    if args.start and args.end:
        include_dates = _date_range_set(args.start, args.end)
    elif args.start or args.end:
        log.error("Pass BOTH --start and --end, or neither")
        return 3
    else:
        yesterday = now_mx().date() - dt.timedelta(days=1)
        first = yesterday - dt.timedelta(days=max(0, args.days - 1))
        include_dates = _date_range_set(first.isoformat(), yesterday.isoformat())

    today_iso = now_mx().date().isoformat()
    exclude_dates = set() if args.include_today else {today_iso}

    try:
        v2_rows = v2_sheets.read_table(V2_TAB, "A1:Z")
        v1_rows = v1_sheets.read_table(V1_TAB, "A1:Z")
    except Exception as e:  # noqa: BLE001
        log.error("read failed: %s", e)
        return 3
    log.info("Read %d KPI_Daily rows, %d DailyData rows", len(v2_rows), len(v1_rows))

    rows = build_reconcile(
        v1_rows=v1_rows,
        v2_rows=v2_rows,
        active_plants=active_plants,
        tolerance_pct=args.tolerance,
        include_dates=include_dates,
        exclude_dates=exclude_dates,
    )

    _print_table(rows)
    counts = summarize(rows)
    print()
    print(f"Summary (tolerance {args.tolerance:g}% on daily energy):")
    print(f"  OK              {counts[BUCKET_OK]}")
    print(f"  PR-DIVERGENCE   {counts[BUCKET_PR]}   (energy matched; config/irradiance)")
    print(f"  ENERGY-MISMATCH {counts[BUCKET_ENERGY]}   (collection gap — investigate)")
    print(f"  MISSING-V1      {counts[BUCKET_MISSING_V1]}")
    print(f"  MISSING-V2      {counts[BUCKET_MISSING_V2]}")

    if args.csv:
        _write_csv(args.csv, rows)
        log.info("Wrote %d rows to %s", len(rows), args.csv)

    overlap = sum(1 for r in rows if r.v1_energy_kwh is not None and r.v2_energy_kwh is not None)
    if overlap == 0:
        log.warning("No overlapping full days — nothing proven yet. "
                    "v1 and v2 need days where both have a complete row.")
        return 2
    if counts[BUCKET_ENERGY] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
