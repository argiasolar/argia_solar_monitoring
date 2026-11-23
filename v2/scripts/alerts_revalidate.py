#!/usr/bin/env python3
"""v391: close the plant energy alerts that the loss check has since proved false.

Tomasz, 23 Nov 2026: "make sure that issues that are not real are removed, as currently it looks
like the issues are still there and we are ignoring them".

An ``energy_daily_pct`` alert says "the plant produced X % of expected" for one day. The loss check
(loss_daily, recomputed every morning and after a fix such as v389) judges the same day against the
neighbouring plants. When it finds NOTHING lost on a basis it trusts (neighbours, or an estimated sun
that books no underperformance), the alert was a false alarm: it is resolved here, its message keeps
the original wording and adds why it was closed. Nothing is mailed. Every other alert is left alone.

    PYTHONPATH=. python scripts/alerts_revalidate.py            # dry run: prints what it would close
    PYTHONPATH=. python scripts/alerts_revalidate.py --apply    # writes the resolved rows (PG ledger)

Run it between two alerts_snapshot ticks (it refuses while one runs): the snapshot rewrites the ledger
it read, so a write in the middle of its run could be undone.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from argia.core import alerts_pg
from argia.core.alerts_state import AlertRecord, records_from_rows, record_to_row, resolve_alert
from argia.core.time_utils import MX_TZ
from argia.store.pgq import psql_rows

LOG = logging.getLogger("argia.alerts_revalidate")
METRIC = "energy_daily_pct"
TRUSTED = ("peers", "weather-estimated")
MAX_LOST_KWH = 1.0


def alert_day(rec: AlertRecord) -> Optional[str]:
    """The production day an energy alert speaks about: the daily engine runs the morning after, so the
    day before the MX date of its last evaluation (last_seen)."""
    try:
        seen = dt.datetime.fromisoformat(rec.last_seen_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (seen.astimezone(MX_TZ).date() - dt.timedelta(days=1)).isoformat()


def false_alarms(records: List[AlertRecord],
                 verdicts: Dict[Tuple[str, str], Tuple[str, Optional[float], Optional[float], Optional[float]]]
                 ) -> List[Tuple[AlertRecord, str]]:
    """[(open energy alert, reason)] the loss check contradicts. ``verdicts``: (plant, day) ->
    (expected_basis, expected_kwh, actual_kwh, lost_kwh) from loss_daily. Pure."""
    out = []
    for r in records:
        if not r.is_open() or r.metric != METRIC:
            continue
        day = alert_day(r)
        v = verdicts.get((r.plant_key, day)) if day else None
        if not v:
            continue
        basis, exp, act, lost = v
        if basis in TRUSTED and lost is not None and lost < MAX_LOST_KWH:
            why = (f"closed {dt.date.today().isoformat()}: false alarm - the loss check for {day} found nothing lost "
                   f"({'neighbouring plants' if basis == 'peers' else 'estimated sun, no station reading'}: "
                   f"expected {exp:,.0f} kWh, made {act:,.0f} kWh)" if exp is not None and act is not None else
                   f"closed {dt.date.today().isoformat()}: false alarm - the loss check for {day} found nothing lost")
            out.append((r, why))
    return out


def snapshot_running() -> bool:
    r = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", "argia-alerts-snap.service"],
                       capture_output=True, text=True)
    return r.stdout.strip() not in ("inactive", "failed", "")


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="close energy alerts the loss check proved false")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if alerts_pg.source() != "pg":
        LOG.error("the ledger is not in PostgreSQL (ARGIA_ALERTS_SOURCE) - nothing done")
        return 2
    if a.apply and snapshot_running():
        LOG.error("alerts_snapshot is running - try again in a minute")
        return 3
    records = list(records_from_rows(alerts_pg.read_records()).records)
    days = sorted({d for d in (alert_day(r) for r in records if r.is_open() and r.metric == METRIC) if d})
    verdicts = {}
    if days:
        verdicts = {(r[0], r[1]): (r[2], _f(r[3]), _f(r[4]), _f(r[5])) for r in psql_rows(
            "SELECT plant_key, prod_date::text, expected_basis, expected_kwh, actual_kwh, lost_kwh FROM loss_daily"
            " WHERE prod_date IN (" + ",".join(f"DATE '{d}'" for d in days) + ");")}
    found = false_alarms(records, verdicts)
    now = dt.datetime.now(dt.timezone.utc)
    changed = [resolve_alert(r, now, final_message=f"{r.message} [{why}]") for r, why in found]
    for r, why in found:
        LOG.info("%s %s %s: %s", "CLOSE" if a.apply else "would close", r.alert_id, r.plant_key, why)
    if not found:
        LOG.info("no open energy alert is contradicted by the loss check")
    if a.apply and changed:
        n = alerts_pg.write_rows([record_to_row(r) for r in changed])
        LOG.info("wrote %d resolved row(s)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
