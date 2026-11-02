"""Compensated energy onto the daily table, whenever the events change (v342).

Tomasz, 2 Nov 2026: "I added such event to Maintenance Events, but it didn't
trigger the change of invoice for October". Until v342 the compensated
(deemed) energy was stamped ONCE, by kpi_eod, for yesterday only: an event
logged or approved later - the normal case, customers tell us afterwards -
never reached billable_kwh, so it never reached the invoice.

Now the daily table follows the events, for any day:

    billable_kwh = energy_kwh + compensated kWh of the approved customer
                   events on that day        (argia.maintenance.deemed)

* runs nightly for the open months (recon_snapshot, after the billable
  resync, so a later energy heal can never wipe the compensation);
* a month that is already INVOICED is never rewritten silently: the
  nightly run reports it and ``scripts/invoice_reissue.py`` re-issues it
  on purpose (register + annex + PDF + Drive);
* an event deleted or withdrawn takes its compensation off again.

``plan`` is pure; ``sync_month`` does the I/O.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.maintenance.deemed import (contract_daily_rule, daylight_fraction, deemed_by_plant_day,
                                      measured_in_window_from_buckets)

LOG = logging.getLogger("argia.maintenance.deemed_sync")
EPS = 0.005


@dataclass(frozen=True)
class Change:
    plant_key: str
    date_iso: str
    energy_kwh: Optional[float]
    old_billable: Optional[float]
    new_billable: float
    deemed_kwh: float


def plan(rows: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]],
         deemed: Dict[Tuple[str, str], float]) -> Tuple[List[Change], List[Tuple[str, str, float]]]:
    """(changes, orphans). ``rows``: {(plant, day): (energy, billable)} of the
    daily table in scope; ``deemed``: {(plant, day): kWh} from the approved
    events. A compensated day without a daily row is an orphan (reported,
    never invented). Pure."""
    changes: List[Change] = []
    orphans: List[Tuple[str, str, float]] = []
    for key, d in sorted(deemed.items()):
        if d > 0 and key not in rows:
            orphans.append((key[0], key[1], round(d, 1)))
    for (pk, day), (energy, billable) in sorted(rows.items()):
        d = round(deemed.get((pk, day), 0.0), 1)
        if d > 0:
            target = round((energy or 0.0) + d, 2)
        elif billable is not None and energy is not None and billable > energy + EPS:
            target = round(energy, 2)          # an event withdrawn: its compensation goes
        else:
            continue
        if billable is not None and abs(billable - target) <= EPS:
            continue
        changes.append(Change(pk, day, energy, billable, target, d))
    return changes, orphans


def month_deemed(rows, deemed) -> Dict[str, float]:
    """{plant: Σ compensated kWh of the month's days that have a daily row}. Pure."""
    out: Dict[str, float] = {}
    for (pk, day) in rows:
        d = round(deemed.get((pk, day), 0.0), 1)
        if d > 0:
            out[pk] = round(out.get(pk, 0.0) + d, 1)
    return out


def update_sql(c: Change, event_note: str = "") -> str:
    what = (f"compensated {c.deemed_kwh:.1f} kWh from customer maintenance" if c.deemed_kwh > 0
            else "compensation removed (no approved customer event)")
    note = f" | {dt.date.today().isoformat()} billable {c.new_billable:.2f}: {what}{event_note}"
    return ("UPDATE daily_production SET billable_kwh = %.3f, status_note = trim(coalesce(status_note,'') || %s)"
            " WHERE plant_key = %s AND prod_date = DATE '%s';"
            % (c.new_billable, _q(note), _q(c.plant_key), c.date_iso))


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _f(s) -> Optional[float]:
    try:
        return float(s) if str(s).strip() != "" else None
    except ValueError:
        return None


def recent_months(base: dt.date) -> List[str]:
    """The month of ``base`` and the one before it (late customer reports). Pure."""
    prev = base.replace(day=1) - dt.timedelta(days=1)
    return [prev.strftime("%Y-%m"), base.strftime("%Y-%m")]


def month_bounds(ym: str) -> Tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-01", "%s-%02d" % (ym, calendar.monthrange(y, m)[1])


def compute(ym: str, plants: List[str], events=None):
    """(rows, deemed) for one month from PostgreSQL + the events."""
    from argia.maintenance.events import load_maintenance_events_pg
    from argia.store.pgq import psql_rows
    first, last = month_bounds(ym)
    pl = ",".join(_q(p.upper()) for p in plants) or "''"
    rows = {(r[0], r[1]): (_f(r[2]), _f(r[3])) for r in psql_rows(
        "SELECT plant_key, prod_date::text, energy_kwh, billable_kwh FROM daily_production"
        f" WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}' AND plant_key IN ({pl});") if len(r) >= 4}
    contract = {(r[0], int(r[1]), int(r[2])): _f(r[3]) for r in psql_rows(
        "SELECT plant_key, year, month, contract_kwh FROM contract_monthly WHERE contract_kwh IS NOT NULL;")
        if len(r) >= 4}
    energy = {k: v[0] for k, v in rows.items()}

    def contract_daily_for(pk, y, m):
        return contract_daily_rule(contract, pk, y, m)      # v344: annual / 12 / days

    def measured_for(pk, d_iso, s, e):
        day = dt.date.fromisoformat(d_iso)
        return measured_in_window_from_buckets(None, pk, d_iso, s, e, energy.get((pk, d_iso)),
                                               daylight_fraction(day, s, e))
    evs = events if events is not None else load_maintenance_events_pg()
    evs = [e for e in evs if e.plant_key in {p.upper() for p in plants}]
    full = deemed_by_plant_day(evs, contract_daily_for, measured_for)
    deemed = {k: v for k, v in full.items() if first <= k[1] <= last}
    return rows, deemed


def invoiced_plants(ym: str) -> Dict[str, float]:
    """{plant: penalty_kwh on the register} for an invoiced month."""
    from argia.store.pgq import psql_rows
    return {r[0]: (_f(r[1]) or 0.0) for r in psql_rows(
        f"SELECT plant_key, coalesce(penalty_kwh, 0) FROM invoicing WHERE ref_month = DATE '{ym}-01';")
        if len(r) >= 2}


def sync_month(ym: str, plants: List[str], apply: bool, include_invoiced: bool = False) -> dict:
    """Bring one month's daily table in line with the events. Invoiced
    plant-months are left alone unless ``include_invoiced`` (the re-issue);
    their pending difference is returned so the caller can say so."""
    from argia.store.pgq import psql_exec
    rows, deemed = compute(ym, plants)
    changes, orphans = plan(rows, deemed)
    invoiced = invoiced_plants(ym)
    per_plant = month_deemed(rows, deemed)
    pending = {pk: (invoiced[pk], per_plant.get(pk, 0.0)) for pk in invoiced
               if pk in {p.upper() for p in plants} and abs(invoiced[pk] - per_plant.get(pk, 0.0)) > 0.05}
    todo = [c for c in changes if include_invoiced or c.plant_key not in invoiced]
    for c in changes:
        LOG.info("deemed %s %s: billable %s -> %.2f (energy %s, compensated %.1f)%s", c.plant_key, c.date_iso,
                 c.old_billable, c.new_billable, c.energy_kwh, c.deemed_kwh,
                 "" if c in todo else " - month INVOICED, not touched (invoice_reissue)")
    for pk, day, d in orphans:
        LOG.error("deemed %s %s: %.1f kWh compensated by an approved event but the day has no daily row", pk, day, d)
    for pk, (inv, now) in sorted(pending.items()):
        LOG.warning("deemed %s %s: invoiced with %.1f kWh compensated, the events now say %.1f - "
                    "run invoice_reissue.py --month %s", pk, ym, inv, now, ym)
    if apply and todo:
        psql_exec("BEGIN;\n" + "\n".join(update_sql(c) for c in todo) + "\nCOMMIT;")
    return {"changes": changes, "applied": todo if apply else [], "orphans": orphans,
            "pending_invoiced": pending, "month_deemed": per_plant}
