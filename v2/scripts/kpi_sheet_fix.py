"""One-off: push the vendor-history energy corrections into the
KPI_Daily SHEET (pio06 only).

PostgreSQL was corrected by recon_backfill (87 plant-days, provenance in
status_note); the Google Sheet KPI_Daily still holds the Pi-incident
undercounts and 08-25 gaps — and ARGIA_Solar invoicing QUERYs that tab.
This script mirrors the PG corrections into the sheet, surgically:
existing rows get ONLY energy_kwh + status_note cells rewritten (one
batchUpdate); missing (plant, date) rows are appended with energy +
provenance and blanks elsewhere. Nothing is ever lowered.

Usage: kpi_sheet_fix.py [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.kpi.reconcile import date_key, plant_key_norm
from argia.store import pg_mirror
from argia.store.pgq import psql_rows

LOG = logging.getLogger("argia.kpi_sheet_fix")
TAB = "KPI_Daily"
TOL_KWH = 0.5


def corrections():
    """{(PLANT, 'YYYY-MM-DD'): kwh} from PG rows carrying backfill provenance."""
    out = {}
    for r in psql_rows(
            "SELECT plant_key, prod_date::text, energy_kwh"
            " FROM daily_production"
            " WHERE status_note LIKE 'energy from vendor daily counter%'"
            " AND energy_kwh IS NOT NULL;"):
        if len(r) >= 3:
            try:
                out[(r[0], r[1])] = float(r[2])
            except ValueError:
                pass
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    fixes = corrections()
    LOG.info("PG carries %d corrected plant-days", len(fixes))
    if not fixes:
        return 0

    sheets = SheetsClient(
        sheet_id=os.environ.get("GOOGLE_SHEET_ID_V2", "").strip())
    grid = sheets.read_range(TAB, "A1:ZZ")
    if not grid:
        LOG.error("%s tab is empty", TAB)
        return 1
    header = [normalize_text(h) for h in grid[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return -1

    c_plant = col("plant_key")
    c_date = col("date_iso", "date")
    c_energy = col("energy_kwh")
    c_note = col("status_note")
    if min(c_plant, c_date, c_energy) < 0:
        LOG.error("%s header misses plant/date/energy: %s", TAB, header[:12])
        return 1

    row_by_key = {}
    for idx, row in enumerate(grid[1:], start=2):     # sheet rows, 1-indexed
        pk = plant_key_norm(row[c_plant] if len(row) > c_plant else "")
        d = date_key(row[c_date] if len(row) > c_date else None)
        if pk and d:
            row_by_key[(pk, d)] = (idx, row)

    cells = []
    appends = []
    lowered = raised = filled = same = 0
    for (pk, d), kwh in sorted(fixes.items()):
        hit = row_by_key.get((pk, d))
        if hit is None:
            new_row = [""] * len(header)
            new_row[c_plant] = pk
            new_row[c_date] = d
            new_row[c_energy] = kwh
            if c_note >= 0:
                new_row[c_note] = "vendor-history backfill (row was missing)"
            appends.append(new_row)
            filled += 1
            LOG.info("APPEND %s %s -> %.1f kWh (row missing)", pk, d, kwh)
            continue
        idx, row = hit
        old = safe_float(row[c_energy] if len(row) > c_energy else None)
        if old is not None and abs(old - kwh) <= TOL_KWH:
            same += 1
            continue
        if old is not None and old > kwh:
            lowered += 1
            LOG.warning("SKIP %s %s: sheet %.1f > vendor %.1f — never "
                        "lowering automatically", pk, d, old, kwh)
            continue
        raised += 1
        LOG.info("FIX %s %s: %s -> %.1f kWh", pk, d,
                 "blank" if old is None else f"{old:.1f}", kwh)
        cells.append((idx, c_energy + 1, kwh))
        if c_note >= 0:
            note = ("energy corrected from vendor daily counter (was "
                    + ("blank" if old is None else f"{old:.1f}") + ")")
            cells.append((idx, c_note + 1, note))

    LOG.info("SUMMARY: raise=%d append=%d unchanged=%d skipped-higher=%d "
             "apply=%s", raised, filled, same, lowered, args.apply)
    if not args.apply:
        return 0
    if cells:
        n = sheets.batch_write_cells(TAB, cells)
        LOG.info("batch-updated %d cells", n)
    if appends:
        n = sheets.append_rows(TAB, appends)
        LOG.info("appended %d rows", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
