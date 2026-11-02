"""Synthetic plant-month for the black-swan tests (v341).

The TRUE energy of every inverter on every day is known. Every observable
the platform sees is derived from it the way the vendors and our own
collectors produce it, then the month runs through the REAL engine
(argia.recon.engine): daily reconciliation, the nightly fill-or-raise of
the daily table, the month close. A scenario then states what must come
out: an auto-closed month that is right to the kWh tolerance, or a month
that is held for a person (never a wrong invoice).

Vendor behaviour modelled (from what the fleet has shown):
* Growatt inverter eToday comes in 0.1 kWh steps and is TRUNCATED (Oct
  2026: their sum ran 0.1-0.5 kWh/day below the portal); the portal plant
  daily is the true day rounded to 0.1; no month counter.
* Huawei / SolarEdge: plant daily at 0.01, a month counter, a lifetime
  register.
* "interval" in the close is the Σ of the inverters' own eToday registers
  as we read them (recon_snapshot.interval_by_plant); the daily table
  starts from our 5-min integration and is raised to the day's reference.
* Our nightly snapshot (23:50 MX) can catch a vendor day before late
  uploads arrive (SAG Oct 2026); the portal later shows the full day.
"""
from __future__ import annotations

import calendar
import inspect
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from argia.recon import engine as E

STEP = {"GROWATT": 0.1, "HUAWEI": 0.01, "SOLAREDGE": 0.01}


def floor_step(v: float, step: float) -> float:
    return round(math.floor(v / step + 1e-9) * step, 3)


def round_step(v: float, step: float) -> float:
    return round(round(v / step) * step, 3)


@dataclass
class Scenario:
    name: str
    brand: str = "GROWATT"
    year: int = 2026
    month: int = 10
    n_inv: int = 4
    kwp: float = 600.0
    seed: int = 1
    # truth modifiers
    outage_days: Set[int] = field(default_factory=set)          # plant really produced 0
    inverter_dead: Dict[int, Set[int]] = field(default_factory=dict)  # day -> inverters really at 0
    # our collection
    comms_down: Dict[int, Set[int]] = field(default_factory=dict)     # day -> inverters we did not read
    interval_completeness: float = 0.99                              # our 5-min sum share of the truth
    # vendor side
    late_upload: Dict[int, float] = field(default_factory=dict)       # day -> share missing in our nightly snapshot
    vendor_lost: Dict[int, float] = field(default_factory=dict)       # day -> share the vendor portal never got
    snapshot_missing: Set[int] = field(default_factory=set)           # day -> no nightly snapshot at all (server down)
    month_counter_short: float = 0.0                                  # kWh missing from the month counter at close
    spike_day: Optional[Tuple[int, float]] = None                     # (day, factor) vendor daily glitch
    negative_day: Optional[int] = None                                # vendor daily reported negative
    lifetime: bool = True
    lifetime_reset_day: Optional[int] = None                          # inverter swapped: register restarts
    lifetime_start_missing: bool = False                              # no snapshot at the end of the previous month
    # what must come out
    expect: str = "PASS"                                              # PASS (auto-close, right) or HOLD (person decides)
    why: str = ""

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def step(self) -> float:
        return STEP[self.brand]

    @property
    def month_counter(self) -> bool:
        return self.brand in ("HUAWEI", "SOLAREDGE")


@dataclass
class Outcome:
    close: E.MonthlyClose
    truth_kwh: float
    portal_kwh: float               # what the vendor portal shows for the month at close time (final)
    daily_table: Dict[int, float]   # what daily_production holds after the nightly fill-or-raise
    references: Dict[int, Optional[float]]

    @property
    def daily_table_sum(self) -> float:
        return round(sum(self.daily_table.values()), 3)


def truth(s: Scenario) -> List[List[float]]:
    """kWh per day per inverter: a clear-sky-ish profile with weather noise."""
    rnd = random.Random(s.seed)
    per_inv_kwp = s.kwp / s.n_inv
    out = []
    for d in range(1, s.days + 1):
        if d in s.outage_days:
            out.append([0.0] * s.n_inv)
            continue
        sky = 0.55 + 0.45 * rnd.random()               # 0.55..1.0 of a 5.6 kWh/kWp day
        row = []
        for i in range(s.n_inv):
            if i in s.inverter_dead.get(d, set()):
                row.append(0.0)
            else:
                row.append(round(per_inv_kwp * 5.6 * sky * (0.97 + 0.06 * rnd.random()), 4))
        out.append(row)
    return out


def run(s: Scenario, truth_rows: Optional[List[List[float]]] = None) -> Outcome:
    """The month through the real engine, the way production runs it."""
    T = truth_rows or truth(s)
    step = s.step
    interval_sum = 0.0
    snap_sum, snap_days = 0.0, 0
    ref_sum, ref_days = 0.0, 0
    portal_month = 0.0
    daily_table: Dict[int, float] = {}
    refs: Dict[int, Optional[float]] = {}
    compl_days: List[float] = []
    for d in range(1, s.days + 1):
        row = T[d - 1]
        day_true = sum(row)
        down = s.comms_down.get(d, set())
        reading = [i for i in range(s.n_inv) if i not in down]
        inv_sum = (round(sum(floor_step(row[i], step if s.brand == "GROWATT" else 0.01) for i in reading), 3)
                   if reading else None)
        completeness = 100.0 * s.interval_completeness * len(reading) / s.n_inv
        compl_days.append(completeness)
        # daily_production before the nightly heal: our 5-min integration
        kpi = round(sum(row[i] for i in reading) * s.interval_completeness, 3)
        # reconciliation_daily.interval_kwh = Σ of the inverters' eToday registers we read
        interval_sum += inv_sum or 0.0
        portal_day = round_step(day_true * (1.0 - s.vendor_lost.get(d, 0.0)), step)
        if s.spike_day and s.spike_day[0] == d:
            portal_day = round_step(portal_day * s.spike_day[1], step)
        if s.negative_day == d:
            portal_day = -abs(portal_day) or -1.0
        portal_month += portal_day
        if d in s.snapshot_missing:
            snap = None
        else:
            snap = round_step(portal_day * (1.0 - s.late_upload.get(d, 0.0)), step)
            snap_sum += snap
            snap_days += 1
        r = E.daily_recon(inv_sum, snap, kpi, completeness)
        refs[d] = r.reference_kwh
        if r.reference_kwh is not None:
            ref_sum += r.reference_kwh
            ref_days += 1
        # nightly fill-or-raise (never lower) - recon_snapshot.reconcile_day
        daily_table[d] = max(kpi, r.reference_kwh) if r.reference_kwh is not None else kpi
    truth_total = round(sum(sum(r) for r in T), 3)
    portal_total = round(portal_month, 3)
    monthly = round(portal_total - s.month_counter_short, 2) if s.month_counter else None
    life_start = life_end = None
    if s.lifetime:
        L0 = 1_234_567.8
        life_start = None if s.lifetime_start_missing else L0
        life_end = round_step(L0 + truth_total, 0.1)
        if s.lifetime_reset_day is not None:
            after = sum(sum(r) for r in T[s.lifetime_reset_day - 1:])
            life_end = round_step(after, 0.1)                 # the new inverter's register
    extra = {}
    if "daily_table_sum_kwh" in inspect.signature(E.monthly_close).parameters:
        extra = {"daily_table_sum_kwh": round(sum(daily_table.values()), 3),
                 "daily_table_days": len(daily_table)}
    mc = E.monthly_close(round(interval_sum, 3), round(snap_sum, 3) if snap_days else None, monthly,
                         life_start, life_end, round(sum(compl_days) / len(compl_days), 2),
                         snap_days, s.days, round(ref_sum, 3) if ref_days else None, ref_days, **extra)
    return Outcome(mc, truth_total, portal_total, daily_table, refs)
