"""Deemed energy ("energía compensada") — contract-anchored.

When a customer forces a PPA plant down, the plant is billed what it
WOULD have produced. Per Tomasz's call (2026-07) and the existing
``Contract_Monthly.contract_kwh_daily`` property, the basis is the
contract, not measured irradiance:

    deemed_day = max(0, contract_daily × daylight_fraction
                        − measured_in_window)

* ``contract_daily``      = Contract_Monthly.contract_kwh ÷ days-in-month
                            for THAT day's month (escalations included).
* ``daylight_fraction``   = the window's overlap with civil daylight
                            (06:00–20:00 MX = 14 h) on that day. Night
                            hours produce nothing, so a shutdown at
                            02:00 deems zero.
* ``measured_in_window``  = what the plant actually made during the
                            window (from Dashboard_Plant buckets —
                            measured data, not an estimate). Subtracted
                            so a plant that limped rather than died is
                            not double-billed. A full outage → 0 → deemed
                            collapses cleanly to ``contract_daily``.

Everything is computed PER CALENDAR DAY and summed, so a window that
spans midnight or a month boundary (where ``contract_daily`` changes) is
handled correctly.

No irradiance dependency: this works even when the sun sensor was also
down, and every number traces to the signed contract — an auditor can
reconstruct it from Contract_Monthly alone. Honest consequence: on a
very sunny day deemed under-compensates vs reality, on a cloudy day
over — inherent to contract-anchoring, and presumably what the PPA text
says.

These are pure functions. The caller supplies ``contract_daily`` and
``measured_in_window`` (kpi_eod wires them to the live sheets); the
engine never touches I/O.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, Dict, List, Optional, Tuple

from argia.core.normalize import safe_float
from argia.core.time_utils import MX_TZ
from argia.maintenance.events import BILLABLE_CATEGORIES, MaintenanceEvent

LOG = logging.getLogger("argia.maintenance.deemed")

# Civil daylight convention for the deemed basis. Documented so the
# invoice annex can cite it. 06:00–20:00 MX = 14 h.
DAYLIGHT_START_H = 6
DAYLIGHT_END_H = 20
DAYLIGHT_HOURS = DAYLIGHT_END_H - DAYLIGHT_START_H


def daylight_fraction(day: dt.date, start: dt.datetime, end: dt.datetime,
                      *, start_h: int = DAYLIGHT_START_H,
                      end_h: int = DAYLIGHT_END_H) -> float:
    """Fraction of ``day``'s daylight window covered by [start, end].

    All datetimes are compared in MX local time. Returns a value in
    [0, 1]; 0 when the overlap is entirely at night or off-day.
    """
    day_start = dt.datetime(day.year, day.month, day.day, start_h, 0, 0,
                            tzinfo=MX_TZ)
    day_end = dt.datetime(day.year, day.month, day.day, end_h, 0, 0,
                          tzinfo=MX_TZ)
    s = start.astimezone(MX_TZ)
    e = end.astimezone(MX_TZ)
    lo = max(s, day_start)
    hi = min(e, day_end)
    if hi <= lo:
        return 0.0
    covered_h = (hi - lo).total_seconds() / 3600.0
    return max(0.0, min(1.0, covered_h / (end_h - start_h)))


def deemed_kwh_for_day(day: dt.date, start: dt.datetime, end: dt.datetime,
                       contract_daily: Optional[float],
                       measured_in_window: Optional[float],
                       *, start_h: int = DAYLIGHT_START_H,
                       end_h: int = DAYLIGHT_END_H) -> float:
    """Deemed kWh for a single day of a window. Returns 0.0 when there is
    no contract basis (``contract_daily`` None — e.g. a CAPEX/lighting key
    with a blank contract_kwh) or no daylight overlap."""
    if contract_daily is None or contract_daily <= 0:
        return 0.0
    frac = daylight_fraction(day, start, end, start_h=start_h, end_h=end_h)
    if frac <= 0.0:
        return 0.0
    expected = contract_daily * frac
    measured = max(0.0, measured_in_window or 0.0)
    return max(0.0, expected - measured)


def event_day_spans(event: MaintenanceEvent,
                    now: Optional[dt.datetime] = None
                    ) -> List[Tuple[dt.date, dt.datetime, dt.datetime]]:
    """Split an event window into per-calendar-day (MX) spans:
    ``[(day, day_window_start, day_window_end), ...]``.

    An ongoing event (blank end_ts) is bounded at ``now`` so it is never
    infinite. Each returned span is clipped to that calendar day, so the
    caller can look up the right month's ``contract_daily`` and the right
    day's measured energy independently.
    """
    start = event.start_ts.astimezone(MX_TZ)
    end = event.effective_end(now).astimezone(MX_TZ)
    if end <= start:
        return []
    spans: List[Tuple[dt.date, dt.datetime, dt.datetime]] = []
    day = start.date()
    last = end.date()
    while day <= last:
        day_start = dt.datetime(day.year, day.month, day.day, 0, 0, 0,
                                tzinfo=MX_TZ)
        next_day = day_start + dt.timedelta(days=1)
        seg_start = max(start, day_start)
        seg_end = min(end, next_day)
        if seg_end > seg_start:
            spans.append((day, seg_start, seg_end))
        day = next_day.date()
    return spans


# contract_daily_for(plant_key, year, month) -> Optional[float]
ContractDailyFn = Callable[[str, int, int], Optional[float]]
# measured_for(plant_key, date_iso, day_start, day_end) -> Optional[float]
MeasuredFn = Callable[[str, str, dt.datetime, dt.datetime], Optional[float]]


def deemed_by_plant_day(events: List[MaintenanceEvent],
                        contract_daily_for: ContractDailyFn,
                        measured_for: MeasuredFn,
                        *, now: Optional[dt.datetime] = None,
                        categories=BILLABLE_CATEGORIES
                        ) -> Dict[Tuple[str, str], float]:
    """{(plant_key, 'YYYY-MM-DD'): deemed_kwh} across all APPROVED events
    of a billable ``category``.

    Category gating (customer only, by default) AND approval gating are
    enforced here — argia / force_majeure / draft events contribute zero
    deemed no matter what. If multiple events touch the same plant-day
    (overlapping shutdowns), their deemed contributions add; measured is
    scoped to each event's own window so there is no double subtraction.
    """
    out: Dict[Tuple[str, str], float] = {}
    for e in events:
        if not e.approved or e.category not in categories:
            continue
        for (day, seg_start, seg_end) in event_day_spans(e, now=now):
            cd = contract_daily_for(e.plant_key, day.year, day.month)
            if cd is None or cd <= 0:
                continue
            measured = measured_for(
                e.plant_key, day.isoformat(), seg_start, seg_end)
            kwh = deemed_kwh_for_day(day, seg_start, seg_end, cd, measured)
            if kwh > 0:
                key = (e.plant_key, day.isoformat())
                out[key] = out.get(key, 0.0) + kwh
    return out


def deemed_for_date(events: List[MaintenanceEvent], date_iso: str,
                    contract_daily_for: ContractDailyFn,
                    measured_for: MeasuredFn,
                    *, now: Optional[dt.datetime] = None,
                    categories=BILLABLE_CATEGORIES) -> Dict[str, float]:
    """{plant_key: deemed_kwh} for a single day — the kpi_eod entry point
    (it stamps one day, yesterday). Thin filter over
    :func:`deemed_by_plant_day`."""
    full = deemed_by_plant_day(events, contract_daily_for, measured_for,
                               now=now, categories=categories)
    return {pk: kwh for (pk, d), kwh in full.items() if d == date_iso}


def measured_in_window_from_buckets(
        bucket_rows, plant_key: str, date_iso: str,
        seg_start: dt.datetime, seg_end: dt.datetime,
        energy_kwh_full_day: Optional[float],
        daylight_frac: float) -> float:
    """Measured energy inside a window, for the kpi_eod ``measured_for``.

    * Full-day window (frac ≈ 1): return the day's total ``energy_kwh``
      — exact, no bucket read needed.
    * Partial-day window: sum ``Dashboard_Plant.total_kwh`` for buckets of
      this plant+date whose hour falls inside the window.
    * Partial-day but buckets have aged out of the rolling Dashboard_Plant
      buffer: fall back to ``energy_kwh × daylight_fraction`` — a
      documented approximation (logged), never a silent zero.

    ``bucket_rows`` is the raw Dashboard_Plant table (list of dicts). The
    hour match uses ``hour_label`` (e.g. "13:00"): a bucket for hour H is
    counted when H is inside [window_start_hour, window_end_hour).
    """
    if daylight_frac >= 0.999:
        return energy_kwh_full_day or 0.0

    pk = str(plant_key).upper()
    lo_h = seg_start.astimezone(MX_TZ).hour
    hi_dt = seg_end.astimezone(MX_TZ)
    # a window ending exactly on the hour excludes that hour's bucket
    hi_h = hi_dt.hour if (hi_dt.minute or hi_dt.second) else hi_dt.hour
    total = 0.0
    matched = 0
    for r in bucket_rows or []:
        if str(r.get("plant_key") or "").strip().upper() != pk:
            continue
        d = str(r.get("date_mx") or "")
        # date_mx may be a serial/formatted date; compare on the iso prefix
        if not _date_matches(d, date_iso):
            continue
        hour_label = str(r.get("hour_label") or "").strip()
        if len(hour_label) < 2 or not hour_label[:2].isdigit():
            continue
        bh = int(hour_label[:2])
        if lo_h <= bh < max(hi_h, lo_h + 1):
            total += safe_float(r.get("total_kwh")) or 0.0
            matched += 1
    if matched == 0:
        approx = (energy_kwh_full_day or 0.0) * daylight_frac
        LOG.info("deemed measured_in_window[%s %s]: no live buckets in "
                 "window — approximating %.1f kWh from energy×daylight",
                 pk, date_iso, approx)
        return approx
    return total


def _date_matches(cell, date_iso: str) -> bool:
    """True when a Dashboard_Plant date cell refers to ``date_iso``.
    Tolerates iso strings and 'YYYY-MM-DD...' prefixes; a serial float is
    not resolved here (the full-day fast path covers the common case)."""
    s = str(cell).strip()
    return s[:10] == date_iso
