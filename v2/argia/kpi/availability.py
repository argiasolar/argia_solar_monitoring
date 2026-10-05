"""Plant-day availability from evidence (v310).

Tomasz, 2026-10-04, on the 30-day availability column: "double check it if it
was really the case ... we have to deliver solid results". It was not. The old
measure (archive.kpi_daily.compute_availability) cut the day into "slots" at
every polling gap longer than 5 minutes. With the server polling every 5
minutes a whole day collapsed into 1 to 4 slots, so one short connection
hiccup cost an inverter a third of its day, and the stored values did not even
reproduce from the same data (Plastic Omnium, 2 Oct: stored 55 %, recomputed
100 %, while the plant made 123 % of its expected energy). Every "down" day of
the PPA plants in September was a plant producing 97-123 % of expected.

The rule now (IEC 61724-3 idea: time without information is not downtime):

* the day is cut into fixed 15-minute steps; only steps with the sun at least
  ``SUN_MIN_ELEV`` degrees above the plant's horizon are judged (dawn, dusk and
  a small inverter's later wake-up are not downtime);
* per inverter and step:
    - UP      a reading with power > 0 (producing - whatever the status code
              says: the Hirschmann inverter reports a fault code while it
              produces; that is underperformance, shown by PR and losses);
    - DOWN    readings, all at 0 W (tripped, faulted, grid lost) - unless the
              inverter's day counter grew through the step (a stale 0 W
              value on the vendor feed, not a stop);
    - UNKNOWN no reading with a power value (connection lost);
* an UNKNOWN step becomes UP when the inverter's own day counter proves it
  produced through the gap: its day energy per rated kW reaches
  ``COUNTER_PROOF`` of its peers' median, and - so a plant-wide silence (a
  grid loss also kills the datalogger) is never waved through by equally
  silent peers - the plant's counted energy reaches ``COUNTER_PROOF`` of the
  expected energy when that is known;
* availability = UP / (UP + DOWN), weighted by rated kW; UNKNOWN steps that
  nothing proves are left out and reported as ``coverage`` = known share of
  the judged time. A dark plant is "no data", never 0 % and never 100 %.

Pure: no I/O. Same inputs, same result.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MX_OFFSET = dt.timedelta(hours=-6)        # Mexico City: UTC-6 all year since 2022
STEP_MIN = 15
SUN_MIN_ELEV = 15.0
COUNTER_PROOF = 0.90
BRACKET_MIN = 15                          # how far a counter reading may sit from the step it judges
COUNTER_STEP_KWH = 0.05                   # counter growth that proves energy flowed (above rounding)
FALLBACK_HOURS = (8, 17)                  # judged window when a plant has no coordinates

ENSURE_SQL = "ALTER TABLE daily_production ADD COLUMN IF NOT EXISTS avail_coverage numeric(6,4);"

UP, DOWN, UNKNOWN = "UP", "DOWN", "UNKNOWN"

# (ts_utc, inverter_sn, status, power_w, etoday_kwh)
Sample = Tuple[dt.datetime, str, Optional[int], Optional[float], Optional[float]]


@dataclass(frozen=True)
class AvailabilityDay:
    availability: Optional[float]        # 0..1, None when no step is known
    coverage: float                      # 0..1, share of judged inverter-time with a known state
    steps: int                           # judged 15-minute steps
    down_steps: Dict[str, int] = field(default_factory=dict)
    unknown_steps: Dict[str, int] = field(default_factory=dict)   # left unknown (not proven)
    proven: Tuple[str, ...] = ()         # inverters whose gaps their counter proved


def sun_elevation(lat: float, lon: float, t_local: dt.datetime) -> float:
    """Solar elevation in degrees (NOAA simplified); ``t_local`` is naive MX time."""
    utc = t_local - MX_OFFSET
    doy = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cosz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, cosz))))


def judged_steps(day: dt.date, lat: Optional[float], lon: Optional[float]) -> List[int]:
    """Indexes (0..95) of the day's 15-minute steps that are judged."""
    out = []
    for i in range(24 * 60 // STEP_MIN):
        start = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=i * STEP_MIN)
        if lat is None or lon is None:
            if FALLBACK_HOURS[0] <= start.hour < FALLBACK_HOURS[1]:
                out.append(i)
        elif sun_elevation(lat, lon, start + dt.timedelta(minutes=STEP_MIN / 2)) >= SUN_MIN_ELEV:
            out.append(i)
    return out


def _local(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return ts + MX_OFFSET


def _counter_grew(pts: List[Tuple[dt.datetime, float]], day: dt.date, i: int) -> bool:
    """Did the inverter's day counter grow across step ``i``? Uses the last
    reading at or before the step start and the first at or after its end,
    each at most ``BRACKET_MIN`` minutes away (so a gap of hours cannot lend
    the step energy produced at another time)."""
    t0 = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=i * STEP_MIN)
    t1 = t0 + dt.timedelta(minutes=STEP_MIN)
    near = dt.timedelta(minutes=BRACKET_MIN)
    before = [e for t, e in pts if t0 - near <= t <= t0]
    after = [e for t, e in pts if t1 <= t <= t1 + near]
    inside = [e for t, e in pts if t0 <= t < t1]
    if not before and inside:
        before = [inside[0]]
    if not after and inside:
        after = [inside[-1]]
    return bool(before and after) and after[0] - before[-1] > COUNTER_STEP_KWH


def compute(samples: Iterable[Sample], inverters: Sequence[Tuple[str, Optional[float]]],
            day: dt.date, lat: Optional[float], lon: Optional[float],
            expected_kwh: Optional[float] = None) -> AvailabilityDay:
    """The plant-day availability. ``inverters``: the CONFIGURED inverters as
    (serial, rated kW) - an inverter that never reports stays in the judged
    time (as unknown), it cannot silently drop out."""
    steps = judged_steps(day, lat, lon)
    judged = set(steps)
    sns = [str(sn).strip() for sn, _kw in inverters if str(sn).strip()]
    rated = {str(sn).strip(): (float(kw) if kw and kw > 0 else 1.0) for sn, kw in inverters}
    if not sns or not steps:
        return AvailabilityDay(None, 0.0, len(steps))

    seen: Dict[Tuple[str, int], List[float]] = {}
    counter: Dict[str, float] = {}
    series: Dict[str, List[Tuple[dt.datetime, float]]] = {}
    for ts, sn, _status, power, etoday in samples:
        if ts is None:
            continue
        sn = str(sn).strip()
        if sn not in rated:
            continue
        loc = _local(ts)
        if loc.date() != day:
            continue
        if etoday is not None and etoday >= 0:
            counter[sn] = max(counter.get(sn, 0.0), float(etoday))
            series.setdefault(sn, []).append((loc, float(etoday)))
        idx = (loc.hour * 60 + loc.minute) // STEP_MIN
        if idx in judged and power is not None:
            seen.setdefault((sn, idx), []).append(float(power))

    state: Dict[Tuple[str, int], str] = {}
    for sn in sns:
        for i in steps:
            p = seen.get((sn, i))
            state[(sn, i)] = UNKNOWN if p is None else (UP if max(p) > 0 else DOWN)

    # a 0 W reading whose day counter kept growing through the step is a
    # stale power value, not a stop (MEX1 16-17 Sep: all three inverters
    # "0 W" for hours on the vendor feed)
    for sn in sns:
        pts = sorted(series.get(sn, []))
        for i in steps:
            if state[(sn, i)] == DOWN and _counter_grew(pts, day, i):
                state[(sn, i)] = UP

    # counter proof for the unknown steps
    plant_ok = (expected_kwh is None or expected_kwh <= 0
                or sum(counter.values()) >= COUNTER_PROOF * expected_kwh)
    proven = []
    for sn in sns:
        if sn not in counter or not any(state[(sn, i)] == UNKNOWN for i in steps):
            continue
        peers = [counter[o] / rated[o] for o in sns if o != sn and o in counter]
        if not peers or not plant_ok:
            continue
        ref = statistics.median(peers)
        if ref > 0 and counter[sn] / rated[sn] >= COUNTER_PROOF * ref:
            proven.append(sn)
            for i in steps:
                if state[(sn, i)] == UNKNOWN:
                    state[(sn, i)] = UP

    up = sum(rated[sn] for (sn, _i), s in state.items() if s == UP)
    down = sum(rated[sn] for (sn, _i), s in state.items() if s == DOWN)
    total = sum(rated[sn] for sn in sns) * len(steps)
    known = up + down
    return AvailabilityDay(
        availability=round(up / known, 4) if known > 0 else None,
        coverage=round(known / total, 4) if total > 0 else 0.0,
        steps=len(steps),
        down_steps={sn: n for sn in sns if (n := sum(1 for i in steps if state[(sn, i)] == DOWN))},
        unknown_steps={sn: n for sn in sns if (n := sum(1 for i in steps if state[(sn, i)] == UNKNOWN))},
        proven=tuple(proven),
    )


def window(days: Iterable[Tuple[Optional[float], Optional[float]]], owed_days: int
           ) -> Tuple[Optional[float], Optional[float]]:
    """30-day (or any range) availability and coverage from the daily
    (availability, coverage) pairs. Days with no row at all, or dark days,
    add owed time without known time. A legacy row (availability without
    coverage, before v310) counts as fully covered. Returns (availability,
    coverage); availability is None when nothing in the range is known."""
    known = w_av = 0.0
    for av, cov in days:
        if av is None:
            continue
        c = 1.0 if cov is None else max(0.0, min(1.0, float(cov)))
        known += c
        w_av += float(av) * c
    av = (w_av / known) if known > 0 else None
    cov = (known / owed_days) if owed_days > 0 else None
    return (round(av, 4) if av is not None else None,
            round(min(1.0, cov), 4) if cov is not None else None)
