"""Populate Dashboard_Inverter / Dashboard_Plant from live sheet data.

Reads Plants, Inverters, KPI_Daily, Telemetry_Argia; rebuilds a rolling
window of days with argia.report.dashboard.build(); rewrites both tabs in
full. Full rewrite = idempotent by construction (re-runs can never duplicate
rows). Tabs are never deleted (delete changes the gid and breaks the Looker
Studio data-source binding); we overwrite in place and trim leftover rows.

Theoretical energy: anchored to KPI_Daily.expected_kwh per day (single source
of truth). The live current day (no KPI row yet) falls back to
kwp_dc * irradiance * expected_factor until kpi_eod stamps it.

TECH DEBT (tracked): dashboard.py computes inverter status with its own state
machine, overlapping analytics/vendor_flags.py + inverter_health.py.
Consolidate into one shared status module before extending either side.

Usage (from v2/, like every other script):
  PYTHONPATH=. python scripts/dashboard_update.py               # dry-run
  PYTHONPATH=. python scripts/dashboard_update.py --apply       # write
  PYTHONPATH=. python scripts/dashboard_update.py --window 3 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

from argia.core.sheets import SheetsClient
from argia.report import dashboard as D

MX_TZ = ZoneInfo("America/Mexico_City")
INVERTER_TAB = "Dashboard_Inverter"
PLANT_TAB = "Dashboard_Plant"
# Cell coercion is DELEGATED to the shared module (argia/core/cells.py) —
# the one place that knows the live Sheets API returns datetimes as serial
# floats (watchdog false-alarm lesson, 2026-07-05).
from argia.core.cells import GOOGLE_EPOCH, coerce_date, coerce_ts  # noqa: E402

# re-exported for tests and downstream tooling
__all__ = ["GOOGLE_EPOCH", "coerce_date", "coerce_ts"]
from argia.core.job_log import apply_flag_write_if, instrument


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if v is None:
        return ""
    return v


# --- pure helpers (testable without a client) --------------------------------

def kpi_expected_map(kpi_rows: list[dict],
                     today: dt.date | None = None) -> dict[dt.date, dict[str, float]]:
    """Anchors for the dashboard theoretical.

    Two exclusions, both from the 2026-07-05 incident (NL1 read 32% while
    healthy): a KPI row stamped intraday carries a PARTIAL-day expected, and
    anchoring the live day crams that whole value into the elapsed buckets,
    inflating morning expected ~4x. So:
      * rows with data_class == 'partial' never anchor, and
      * the current day (MX) never anchors even if a row exists.
    The live day always uses the trapezoid fallback; the anchor snaps in
    when the completed-day KPI lands.
    """
    today = today or dt.datetime.now(MX_TZ).date()
    out: dict[dt.date, dict[str, float]] = {}
    for r in kpi_rows:
        d = coerce_date(r.get("date_iso"))
        pk = r.get("plant_key")
        e = D._num(r.get("expected_kwh"))
        if not (d and pk and e is not None):
            continue
        if str(r.get("data_class") or "").strip().lower() == "partial":
            continue
        if d >= today:
            continue
        out.setdefault(d, {})[pk] = e
    return out


def window_days(today: dt.date, window: int) -> list[dt.date]:
    return [today - dt.timedelta(days=i) for i in range(window - 1, -1, -1)]


def build_window(days, plants, samples, active, kpi_by_day, ratings=None):
    inv_rows: list[dict] = []
    plant_rows: list[dict] = []
    for day in days:
        res = D.build(day, plants, samples, active_inverters=active,
                      daily_expected=kpi_by_day.get(day, {}),
                      inverter_ratings=ratings)
        inv_rows.extend(res.inverter_rows)
        plant_rows.extend(res.plant_rows)
    return inv_rows, plant_rows


def to_matrix(columns: list[str], rows: list[dict]) -> list[list]:
    return [list(columns)] + [[_cell(r.get(c)) for c in columns] for r in rows]


# --- sheet IO -----------------------------------------------------------------

def rewrite_tab(client: SheetsClient, tab: str, matrix: list[list],
                apply: bool) -> None:
    """Overwrite a tab in place; trim leftover old rows. Never deletes the tab."""
    n_rows, n_cols = len(matrix), len(matrix[0])
    if not apply:
        print(f"[dry-run] {tab}: would write {n_rows - 1} data rows "
              f"({n_cols} cols); no changes made")
        return
    client.ensure_tab(tab)
    old = client.read_range(tab, "A1:A200000")
    old_rows = len(old)
    client.write_values(tab, f"A1:{_col_letter(n_cols)}{n_rows}", matrix)
    if old_rows > n_rows:
        client.delete_row_range(tab, n_rows + 1, old_rows)
    client.freeze_and_bold_header(tab)
    print(f"[apply] {tab}: wrote {n_rows - 1} rows"
          + (f", trimmed {old_rows - n_rows} old rows" if old_rows > n_rows else ""))


def run(client: SheetsClient, *, window: int, apply: bool,
        today: dt.date | None = None) -> int:
    today = today or dt.datetime.now(MX_TZ).date()
    days = window_days(today, window)
    print(f"Dashboard window: {days[0]} .. {days[-1]}  "
          f"({'APPLY' if apply else 'dry-run'})")

    # A1:ZZ, deliberately: three separate incidents (pr_baseline past
    # AB, show_dashboard past AJ, fault_events past P) came from
    # appended columns falling outside a hardcoded read range. Reads
    # are cheap; silent truncation is not.
    plants = D.parse_plants(client.read_table("Plants", "A1:ZZ"))
    inverter_rows = client.read_table("Inverters", "A1:Z")
    active = D.parse_active_inverters(inverter_rows)
    ratings = D.parse_inverter_ratings(inverter_rows)
    kpi_by_day = kpi_expected_map(client.read_table("KPI_Daily", "A1:V"))

    tele = client.read_table("Telemetry_Argia", "A1:Z")
    for r in tele:
        r["timestamp_mx"] = coerce_ts(r.get("timestamp_mx"))
    samples = D.parse_samples(tele)
    print(f"Inputs: {len(plants)} plants, {len(samples)} telemetry samples, "
          f"KPI days available: {len(kpi_by_day)}")

    inv_rows, plant_rows = build_window(days, plants, samples, active,
                                        kpi_by_day, ratings)

    print(f"{'date':10s} {'plant':6s} {'kwh':>9s} {'theoretical':>11s} {'kpi_exp':>9s}")
    for day in days:
        for pk in sorted(plants):
            pr = [r for r in plant_rows
                  if r["plant_key"] == pk and r["date_mx"] == day.isoformat()]
            if not pr:
                continue
            kwh = sum(r["total_kwh"] for r in pr)
            theo = sum(r["theoretical_kwh"] for r in pr)
            if kwh == 0 and theo == 0:
                continue
            exp = kpi_by_day.get(day, {}).get(pk)
            exp_s = f"{exp:9.1f}" if exp is not None else f"{'-':>9s}"
            print(f"{day.isoformat():10s} {pk:6s} {kwh:9.1f} {theo:11.1f} {exp_s}")

    rewrite_tab(client, INVERTER_TAB, to_matrix(D.INVERTER_COLUMNS, inv_rows), apply)
    rewrite_tab(client, PLANT_TAB, to_matrix(D.PLANT_COLUMNS, plant_rows), apply)
    return 0


@instrument("dashboard_update", write_if=apply_flag_write_if)
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Populate dashboard tabs")
    ap.add_argument("--apply", action="store_true",
                    help="write to the sheet (default: dry-run)")
    ap.add_argument("--window", type=int, default=7,
                    help="rolling window of days to rebuild (default 7)")
    args = ap.parse_args(argv)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2")
    if not sheet_id:
        print("ERROR: GOOGLE_SHEET_ID_V2 not set", file=sys.stderr)
        return 2
    client = SheetsClient(sheet_id)
    return run(client, window=args.window, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
