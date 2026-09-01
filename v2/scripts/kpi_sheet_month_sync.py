"""Mirror a CLOSED month's energy_kwh + billable_kwh from PostgreSQL
into the KPI_Daily sheet (pio06 only).

Found at the 2026-09-01 August close: the nightly self-heal raised
``energy_kwh`` in PG from vendor counters but ``billable_kwh`` — the
column the invoice annex actually bills — kept its pre-correction
value in both PG and the sheet, quietly shrinking every PPA invoice
(~17.8 MWh / ~39,600 MXN for August). PG was repaired at the close;
this script makes the sheet say the same thing, so the annex and the
old ARGIA_Solar invoicing query cannot disagree with the reconciled
close.

Authority model: this writes BOTH directions (raise and lower), which
``kpi_sheet_fix`` deliberately refuses without --allow-lower. It may do
so because it refuses to run at all for a month that is not CLOSED for
every plant it would touch — a closed month is a human-approved
reconciliation, and the close is the single authority the sheet must
mirror. Fail-closed: no close row, no write.

Usage: kpi_sheet_month_sync.py --month 2026-08 [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.core.normalize import normalize_text, safe_float
from argia.kpi.reconcile import date_key, plant_key_norm

# SheetsClient (google-api deps) and the PG helpers are imported inside
# the functions that need them, so the pure planner stays importable in
# test environments that have neither Google libs nor PostgreSQL.

LOG = logging.getLogger("argia.kpi_sheet_month_sync")
TAB = "KPI_Daily"
TOL_KWH = 0.05


def month_rows(ym: str):
    """{(PLANT, 'YYYY-MM-DD'): (energy, billable)} for the month from PG."""
    from argia.store.pgq import psql_rows
    out = {}
    for r in psql_rows(
            "SELECT plant_key, prod_date::text, energy_kwh, billable_kwh"
            " FROM daily_production"
            f" WHERE to_char(prod_date,'YYYY-MM') = '{ym}'"
            " AND energy_kwh IS NOT NULL;"):
        if len(r) >= 4:
            try:
                e = float(r[2])
                b = float(r[3]) if r[3] not in (None, "") else None
            except ValueError:
                continue
            out[(r[0], r[1])] = (e, b)
    return out


def closed_plants(ym: str):
    from argia.store.pgq import psql_rows
    return {r[0] for r in psql_rows(
        "SELECT plant_key FROM reconciliation_monthly"
        f" WHERE ref_month = DATE '{ym}-01' AND closed_at IS NOT NULL;")
        if r and r[0]}


def plan_cells(header, sheet_rows, pg, closed, note_suffix,
               tol=TOL_KWH):
    """Pure: (cells, skipped_open, changed, unchanged).

    ``cells`` are 1-indexed (row, col, value) writes; a plant whose
    month is not closed contributes to ``skipped_open`` and is never
    written — the fail-closed rule lives here so it is testable.
    """
    header = [normalize_text(h) for h in header]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return -1

    c_plant = col("plant_key")
    c_date = col("date_iso", "date")
    c_energy = col("energy_kwh")
    c_bill = col("billable_kwh")
    c_note = col("status_note")
    if min(c_plant, c_date, c_energy, c_bill) < 0:
        raise ValueError("KPI_Daily header misses plant/date/energy/"
                         "billable: %r" % header[:14])

    cells, skipped_open, changed, unchanged = [], set(), 0, 0
    by_key = {}
    for idx, row in enumerate(sheet_rows, start=2):
        pk = plant_key_norm(row[c_plant] if len(row) > c_plant else "")
        d = date_key(row[c_date] if len(row) > c_date else None)
        if pk and d:
            by_key[(pk, d)] = (idx, row)

    for (pk, d), (energy, billable) in sorted(pg.items()):
        if pk not in closed:
            skipped_open.add(pk)
            continue
        hit = by_key.get((pk, d))
        if hit is None:
            continue                    # missing rows are kpi_sheet_fix's job
        idx, row = hit
        row_changed = False
        for cidx, val in ((c_energy, energy), (c_bill, billable)):
            if val is None:
                continue
            old = safe_float(row[cidx] if len(row) > cidx else None)
            if old is None or abs(old - val) > tol:
                cells.append((idx, cidx + 1, round(val, 3)))
                row_changed = True
        if row_changed:
            changed += 1
            if c_note >= 0:
                old_note = str(row[c_note]) if len(row) > c_note else ""
                if note_suffix not in old_note:
                    new_note = (old_note + " | " if old_note.strip()
                                else "") + note_suffix
                    cells.append((idx, c_note + 1, new_note))
        else:
            unchanged += 1
    return cells, skipped_open, changed, unchanged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--month", required=True, help="'YYYY-MM', closed")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    from argia.core.sheets import SheetsClient
    from argia.store import pg_mirror
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    pg = month_rows(args.month)
    closed = closed_plants(args.month)
    LOG.info("PG: %d plant-days for %s; closed plants: %s",
             len(pg), args.month, sorted(closed) or "NONE")
    if not pg:
        return 2

    sheets = SheetsClient(
        sheet_id=os.environ.get("GOOGLE_SHEET_ID_V2", "").strip())
    grid = sheets.read_range(TAB, "A1:ZZ")
    if not grid:
        LOG.error("%s tab is empty", TAB)
        return 1

    note = ("synced from PG at the %s close (energy+billable ="
            " reconciled basis)" % args.month)
    cells, skipped, changed, unchanged = plan_cells(
        grid[0], grid[1:], pg, closed, note)
    for pk in sorted(skipped):
        LOG.warning("SKIP %s: month %s NOT closed — fail-closed, no "
                    "sheet write", pk, args.month)
    LOG.info("SUMMARY: rows-changed=%d unchanged=%d cells=%d apply=%s",
             changed, unchanged, len(cells), args.apply)
    if not args.apply or not cells:
        return 0
    n = sheets.batch_write_cells(TAB, cells)
    LOG.info("batch-updated %d cells", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
