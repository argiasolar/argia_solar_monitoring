"""Monthly reconciliation close (pio06 only). Default: previous month.

Runs on the 1st at 06:10 MX. For every active plant it runs the four
checks (see argia.recon.engine) over the month's nightly counter
snapshots + interval telemetry and writes ``reconciliation_monthly``.

A PASS month auto-closes (closed_by='auto'). REVIEW/FAIL stay OPEN for
a manual close in the portal — and the invoice annex is gated on a
closed month, so nothing mis-reconciled can be invoiced.

A month that was already closed MANUALLY is never overwritten (the
manual decision is the stronger authority); rerunning after new data
refreshes open months only.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import logging
import sys
from typing import Dict, Optional

from argia.core.config import load_portfolio
from argia.core.sheets import open_sheets
from argia.core.time_utils import MX_TZ
from argia.recon import engine as E
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.recon_close")


def _f(s: str) -> Optional[float]:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _num(v: Optional[float]) -> str:
    return "NULL" if v is None else f"{v:.3f}"


def month_inputs(first: dt.date, last: dt.date) -> Dict[str, dict]:
    """Collect per-plant inputs for the close from PG."""
    out: Dict[str, dict] = {}

    def slot(pk: str) -> dict:
        return out.setdefault(pk, {
            "interval_sum": None, "daily_sum": None, "daily_days": 0,
            "monthly": None, "life_start": None, "life_end": None,
            "completeness": None})

    for r in psql_rows(
            "SELECT plant_key, sum(interval_kwh), avg(completeness_pct)"
            " FROM reconciliation_daily"
            f" WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}'"
            " GROUP BY 1;"):
        s = slot(r[0])
        s["interval_sum"] = _f(r[1])
        s["completeness"] = _f(r[2])

    for r in psql_rows(
            "SELECT plant_key, sum(daily_kwh),"
            " count(*) FILTER (WHERE daily_kwh IS NOT NULL)"
            " FROM vendor_counter_snapshot"
            f" WHERE snap_date BETWEEN DATE '{first}' AND DATE '{last}'"
            " GROUP BY 1;"):
        s = slot(r[0])
        s["daily_sum"] = _f(r[1])
        s["daily_days"] = int(_f(r[2]) or 0)

    # Month counter + lifetime endpoint = the LATEST snapshot inside the
    # month; lifetime start = the latest snapshot of the PREVIOUS month.
    for r in psql_rows(
            "SELECT DISTINCT ON (plant_key) plant_key, monthly_kwh,"
            " lifetime_kwh FROM vendor_counter_snapshot"
            f" WHERE snap_date BETWEEN DATE '{first}' AND DATE '{last}'"
            " ORDER BY plant_key, snap_date DESC;"):
        s = slot(r[0])
        s["monthly"] = _f(r[1])
        s["life_end"] = _f(r[2])

    prev_first = (first - dt.timedelta(days=1)).replace(day=1)
    for r in psql_rows(
            "SELECT DISTINCT ON (plant_key) plant_key, lifetime_kwh"
            " FROM vendor_counter_snapshot"
            f" WHERE snap_date BETWEEN DATE '{prev_first}'"
            f" AND DATE '{first - dt.timedelta(days=1)}'"
            " ORDER BY plant_key, snap_date DESC;"):
        slot(r[0])["life_start"] = _f(r[1])
    return out


def close_month(ref_month: str, dry_run: bool) -> int:
    first = dt.date.fromisoformat(ref_month + "-01")
    days = calendar.monthrange(first.year, first.month)[1]
    last = first.replace(day=days)

    try:
        portfolio = load_portfolio(open_sheets())   # v199
    except Exception as e:  # noqa: BLE001
        LOG.error("bootstrap failed: %s", e)
        return 0
    inputs = month_inputs(first, last)

    manually_closed = {r[0] for r in psql_rows(
        "SELECT plant_key FROM reconciliation_monthly"
        f" WHERE ref_month = DATE '{first}' AND closed_at IS NOT NULL"
        " AND closed_by <> 'auto';")}

    n = 0
    for p in portfolio.active_plants():
        pk = p.plant_key
        if pk in manually_closed:
            LOG.info("%s %s: manually closed — untouched", ref_month, pk)
            continue
        s = inputs.get(pk, {})
        mc = E.monthly_close(
            s.get("interval_sum"), s.get("daily_sum"), s.get("monthly"),
            s.get("life_start"), s.get("life_end"), s.get("completeness"),
            s.get("daily_days", 0), days)
        auto_close = mc.status == E.STATUS_PASS
        LOG.info("=== %s %s: %s basis=%s billing=%s\n    %s", ref_month, pk,
                 mc.status, mc.billing_basis, mc.billing_kwh, mc.note)
        if dry_run:
            continue
        closed = "now(), 'auto'" if auto_close else "NULL, NULL"
        psql_exec(
            "INSERT INTO reconciliation_monthly (plant_key, ref_month,"
            " interval_sum_kwh, vendor_daily_sum_kwh, vendor_monthly_kwh,"
            " lifetime_delta_kwh, completeness_pct, check1_pct, check2_pct,"
            " check4_pct, billing_kwh, billing_basis, status, note,"
            " closed_at, closed_by) VALUES"
            f" ({_txt(pk)}, DATE '{first}', {_num(mc.interval_sum_kwh)},"
            f" {_num(mc.vendor_daily_sum_kwh)},"
            f" {_num(mc.vendor_monthly_kwh)}, {_num(mc.lifetime_delta_kwh)},"
            f" {_num(mc.completeness_pct)}, {_num(mc.check1_pct)},"
            f" {_num(mc.check2_pct)}, {_num(mc.check4_pct)},"
            f" {_num(mc.billing_kwh)}, {_txt(mc.billing_basis)},"
            f" {_txt(mc.status)}, {_txt(mc.note)}, {closed})"
            " ON CONFLICT (plant_key, ref_month) DO UPDATE SET"
            " interval_sum_kwh=EXCLUDED.interval_sum_kwh,"
            " vendor_daily_sum_kwh=EXCLUDED.vendor_daily_sum_kwh,"
            " vendor_monthly_kwh=EXCLUDED.vendor_monthly_kwh,"
            " lifetime_delta_kwh=EXCLUDED.lifetime_delta_kwh,"
            " completeness_pct=EXCLUDED.completeness_pct,"
            " check1_pct=EXCLUDED.check1_pct,"
            " check2_pct=EXCLUDED.check2_pct,"
            " check4_pct=EXCLUDED.check4_pct,"
            " billing_kwh=EXCLUDED.billing_kwh,"
            " billing_basis=EXCLUDED.billing_basis,"
            " status=EXCLUDED.status, note=EXCLUDED.note,"
            " closed_at=EXCLUDED.closed_at, closed_by=EXCLUDED.closed_by,"
            " checked_at=now();")
        n += 1
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="monthly reconciliation "
                                     "close")
    parser.add_argument("--month", default=None,
                        help="YYYY-MM (default: previous month, MX)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    if args.month:
        ref = args.month
    else:
        today = dt.datetime.now(MX_TZ).date()
        prev = today.replace(day=1) - dt.timedelta(days=1)
        ref = prev.strftime("%Y-%m")
    n = close_month(ref, args.dry_run)
    LOG.info("DONE: %s months-rows upserted=%d dry_run=%s", ref, n,
             args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
