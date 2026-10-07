"""v267 - what a maintenance ticket costs while it stays open.

Tomasz, 2026-09-28: "add the revenue loss to the maintenance tickets ... so
we can see how much money we are losing due to not dealing with the ticket,
day by day."

Days: from the MX day the ticket was opened to the day it was resolved
(or closed), or to yesterday while it is still open - today's figure lands
tomorrow morning, when ``loss_daily`` closes the day.

Energy per day:

* a **plant-level** ticket carries the plant's whole loss that day
  (``loss_daily.lost_kwh``: expected with 100% availability minus actual);
* an **inverter** ticket carries only that inverter's shortfall against
  the plant's other inverters (its kWh versus rated kW x the median kWh/kW
  of its peers that day), never more than the plant lost. A single-inverter
  plant has no peers, so the plant figure is used and the day says so.

Pesos at the day's PPA tariff; CAPEX plants are kWh only.

Several tickets on one plant can cover the same day. Each ticket shows its
own figure; a total across tickets counts each plant-day once
(``combined``), so the money is never double-counted.

Pure: no I/O, no database, no clock.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from argia.maintenance.tickets import parse_ts

MX = ZoneInfo("America/Mexico_City")
CAUSES = ("unavailability", "overheating", "underperformance")


@dataclass
class PlantDay:
    """One loss_daily row, as the ticket needs it."""
    lost_kwh: Optional[float]
    tariff: Optional[float]
    unavailability: float = 0.0
    overheating: float = 0.0
    underperformance: float = 0.0
    lost_mxn: Optional[float] = None   # v316: loss_daily's own price of the day (the Losses page figure)


@dataclass
class DayCost:
    day: str
    lost_kwh: float
    mxn: Optional[float]
    cause: str             # unavailability / overheating / underperformance / inverter / none
    basis: str             # plant / inverter / plant-no-peers
    plant_lost_kwh: float


def mx_day(ts: str) -> Optional[dt.date]:
    d = parse_ts(ts)
    return d.astimezone(MX).date() if d else None


def window(created_at: str, resolved_at: str, closed_at: str, today: dt.date) -> List[dt.date]:
    """The MX days the ticket was open, up to yesterday."""
    start = mx_day(created_at)
    if start is None:
        return []
    end = mx_day(resolved_at) or mx_day(closed_at) or (today - dt.timedelta(days=1))
    end = min(end, today - dt.timedelta(days=1))
    out, d = [], start
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def inverter_shortfall(rated_kw: Optional[float], inverter_kwh: Optional[float],
                       peer_yields: Sequence[float]) -> Optional[float]:
    """kWh this inverter made less than its peers per rated kW would say.
    None when there is nothing to compare with."""
    ys = [y for y in peer_yields if y is not None and y > 0]
    if not rated_kw or inverter_kwh is None or not ys:
        return None
    return max(0.0, float(rated_kw) * median(ys) - float(inverter_kwh))


def main_cause(p: PlantDay) -> str:
    vals = {c: getattr(p, c) or 0.0 for c in CAUSES}
    best = max(vals, key=vals.get)
    return best if vals[best] > 0 else "none"


def ticket_days(days: Iterable[dt.date], plant: Dict[str, PlantDay],
                inverter: Optional[Dict[str, Tuple[Optional[float], Optional[float], List[float]]]] = None
                ) -> List[DayCost]:
    """``plant``: {YYYY-MM-DD: PlantDay}. ``inverter`` (inverter tickets only):
    {YYYY-MM-DD: (rated_kw, inverter_kwh, [peer kWh per kW])}."""
    out = []
    for d in days:
        ds = d.isoformat()
        p = plant.get(ds)
        if p is None or p.lost_kwh is None:
            continue
        plant_lost = max(0.0, p.lost_kwh)
        if inverter is None:
            lost, cause, basis = plant_lost, main_cause(p), "plant"
        else:
            rated, kwh, peers = inverter.get(ds, (None, None, []))
            short = inverter_shortfall(rated, kwh, peers)
            if short is None:
                lost, cause, basis = plant_lost, main_cause(p), "plant-no-peers"
            else:
                lost, cause, basis = min(short, plant_lost), "inverter", "inverter"
        # v316: a plant-basis day carries loss_daily's own lost_mxn, priced on the
        # unrounded kWh - re-pricing the stored 0.1-kWh-rounded figure differed by
        # cents and showed $1,725 here vs $1,726 on the Losses page (7 Oct 2026)
        if basis != "inverter" and p.lost_mxn is not None and p.tariff:
            price = round(max(0.0, p.lost_mxn), 2)
        else:
            price = None if not p.tariff else round(lost * p.tariff, 2)
        out.append(DayCost(ds, round(lost, 1), price,
                           cause if lost > 0 else "none", basis, round(plant_lost, 1)))
    return out


def total(days: Iterable[DayCost]) -> Tuple[float, Optional[float]]:
    days = list(days)
    kwh = round(sum(d.lost_kwh for d in days), 1)
    priced = [d.mxn for d in days if d.mxn is not None]
    return kwh, (round(sum(priced), 2) if priced else None)


def combined(tickets: Iterable[Tuple[str, List[DayCost]]], tariffs: Dict[Tuple[str, str], Optional[float]]
             ) -> Tuple[float, Optional[float]]:
    """Total across tickets, each plant-day once: a plant-level ticket takes
    the plant's loss; otherwise the inverter tickets' shortfalls add up, capped
    at what the plant lost. ``tickets``: [(plant_key, days)];
    ``tariffs``: {(plant_key, day): tariff}."""
    per: Dict[Tuple[str, str], List[DayCost]] = {}
    for pk, days in tickets:
        for d in days:
            per.setdefault((pk, d.day), []).append(d)
    kwh, mxn, priced = 0.0, 0.0, False
    for key, ds in per.items():
        plant_lost = ds[0].plant_lost_kwh
        if any(d.basis != "inverter" for d in ds):
            lost = plant_lost
        else:
            lost = min(plant_lost, sum(d.lost_kwh for d in ds))
        kwh += lost
        t = tariffs.get(key)
        if t:
            priced = True
            mxn += lost * t
    return round(kwh, 1), (round(mxn, 2) if priced else None)
