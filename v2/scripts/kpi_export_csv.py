"""Export the KPI_Daily hot window to CSV — feed for the pio06 reporting DB.

Runs in GitHub Actions (v2-kpi-export workflow) with the same secrets as
v2-daily-run. Reads the live KPI_Daily tab (14-day hot window), normalizes
date_iso (Sheets reads values back FORMATTED — v95 lesson), and writes the
last N days as CSV with the sheet's own header row. The pio06 server pulls
this file from the ``data`` branch and upserts it into PostgreSQL.

Pure logic (normalize_date_iso, window_rows) has no Google imports so unit
tests run without google-api libs.

Usage:
    PYTHONPATH=. python scripts/kpi_export_csv.py --days 20 --out kpi_window.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from typing import Any, List, Optional, Tuple


def normalize_date_iso(value: Any) -> Optional[str]:
    """Sheet date cell -> 'YYYY-MM-DD', or None if unparseable.

    Sheets returns FORMATTED values: a date written as 2026-08-24 can read
    back as '8/24/2026' (or stay ISO if the cell is text). Accept both.
    """
    s = str(value or "").strip()
    if not s:
        return None
    # Google serial number (days since 1899-12-30) — what read_range actually
    # returned live 2026-08-25: date cells came back as '46204' (= 2026-07-01)
    try:
        serial = float(s)
        if 30000 <= serial <= 80000:      # ~1982..2119, anything else is not a date
            return (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))).isoformat()
        return None
    except ValueError:
        pass
    # drop a time component ("2026-08-24 0:00:00", "2026-08-24T05:00:00Z")
    s = s.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def window_rows(
    values: List[List[Any]], days: int, today: Optional[dt.date] = None,
) -> Tuple[List[Any], List[List[Any]]]:
    """(header, rows of the last ``days`` days, date_iso normalized to ISO).

    ``values`` is the raw read_range result: header row + data rows.
    Rows with an unparseable date or missing plant_key are dropped —
    the DB upsert must never see garbage keys.
    """
    if not values:
        return [], []
    header, raw = list(values[0]), values[1:]
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    out: List[List[Any]] = []
    for r in raw:
        if not r or len(r) < 2:
            continue
        d = normalize_date_iso(r[0])
        plant = str(r[1] or "").strip()
        if not d or not plant or d < cutoff:
            continue
        row = list(r)
        row[0] = d
        out.append(row)
    return header, out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--out", default="kpi_window.csv")
    args = ap.parse_args(argv)

    from argia.core.sheets import SheetsClient  # deferred: needs google libs

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "")
    if not sheet_id:
        print("GOOGLE_SHEET_ID_V2 is not set", file=sys.stderr)
        return 2
    client = SheetsClient(sheet_id)
    values = client.read_range("KPI_Daily", "A1:Z")
    header, rows = window_rows(values, args.days)
    if not rows:
        print("No rows in window — refusing to write an empty export", file=sys.stderr)
        print(f"diagnostics: read {len(values)} raw rows from KPI_Daily", file=sys.stderr)
        if values:
            print(f"header row: {values[0]}", file=sys.stderr)
            cells = [repr(r[0]) for r in values[1:6] if r]
            print(f"first date cells: {cells}", file=sys.stderr)
        return 3
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    dates = sorted({r[0] for r in rows})
    print(f"wrote {args.out}: {len(rows)} rows, {dates[0]} .. {dates[-1]}, "
          f"{len({r[1] for r in rows})} plants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
