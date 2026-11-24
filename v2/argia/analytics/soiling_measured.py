"""Soiling measured from the plant's own data (v397, Arturo's methodology R11).

"Soiling loss is estimated as the change in temperature corrected production per irradiance
between clean events (before and after cleaning, same method), not only from a modeled
profile." The index is the day's temperature-normalized PR (pr_stc: energy / (kWp x
irradiance), corrected to 25 C cell), on VALID days only: measured irradiance (not the
cloud model), at least MIN_IRR kWh/m2 (an overcast day scatters the ratio), plant
availability >= MIN_AVAIL and the weather station check not failed.

* A **clean event** is a logged cleaning (maintenance_event, category 'cleaning') or a day
  with at least RAIN_MM of rain (Open-Meteo, weather_daily).
* **Gain** of an event = median index of the first N valid days after it / median of the last
  N valid days before it, minus 1 - "after each cleaning the portal reports the measured gain".
* **Soiling now** = 1 - (median of the last N valid days / the reference right after the
  last clean event) - a lower bound (the event may not have cleaned everything) - with the
  rate per day since the event.

Pure.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

RAIN_MM = 5.0
MIN_IRR = 3.0
MIN_AVAIL = 0.98
N = 3


@dataclass(frozen=True)
class Event:
    day: str
    kind: str                  # "rain" | "cleaning"
    gain: Optional[float]      # measured, None when not enough valid days on one side


@dataclass(frozen=True)
class Soiling:
    events: Tuple[Event, ...]
    last_event: Optional[Event]
    reference: Optional[float]     # index right after the last event
    current: Optional[float]       # index of the last valid days
    loss: Optional[float]          # 1 - current / reference (>= 0)
    rate_per_day: Optional[float]
    days_since: Optional[int]


def valid_days(rows: Mapping[str, Tuple[Optional[float], Optional[float], Optional[float], Optional[str], bool]]
               ) -> Dict[str, float]:
    """{day: index} from {day: (pr_stc, irradiance kWh/m2, availability, irradiance source,
    station check failed)}."""
    out = {}
    for d, (pr, irr, av, src, st_fail) in rows.items():
        if pr is None or irr is None or irr < MIN_IRR or st_fail:
            continue
        if src and str(src).startswith('cloud'):
            continue
        if av is not None and av < MIN_AVAIL:
            continue
        out[d] = pr
    return out


def clean_events(rain: Mapping[str, float], cleanings: Mapping[str, str]) -> Dict[str, str]:
    """{day: kind} - logged cleanings win over rain on the same day."""
    out = {d: "rain" for d, mm in rain.items() if mm is not None and mm >= RAIN_MM}
    out.update({d: "cleaning" for d in cleanings})
    return out


def _med(vals: List[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def measure(index: Mapping[str, float], events: Mapping[str, str], asof: str) -> Soiling:
    days = sorted(d for d in index if d <= asof)
    evs = []
    ev_days = sorted(d for d in events if d <= asof)
    for i, e in enumerate(ev_days):
        nxt = ev_days[i + 1] if i + 1 < len(ev_days) else None
        before = [index[d] for d in days if d < e][-N:]
        after = [index[d] for d in days if d > e and (nxt is None or d < nxt)][:N]
        gain = (statistics.median(after) / statistics.median(before) - 1) if len(before) == N and len(after) == N else None
        evs.append(Event(e, events[e], None if gain is None else round(gain, 4)))
    # v398: the last event is the latest one, even before any valid day after it (then the
    # level is still being measured) - an earlier event must not stand in for a fresh rain
    last = evs[-1] if evs else None
    ref = cur = loss = rate = since = None
    if last:
        since = (dt.date.fromisoformat(asof) - dt.date.fromisoformat(last.day)).days
        after = [d for d in days if d > last.day]
        ref = _med([index[d] for d in after[:N]])
        tail = after[N:][-N:]
        if len(tail) == N and ref:
            cur = _med([index[d] for d in tail])
            loss = round(max(0.0, 1 - cur / ref), 4)
            span = (dt.date.fromisoformat(tail[-1]) - dt.date.fromisoformat(after[N // 2])).days
            rate = round(loss / span, 5) if span > 0 else None
    return Soiling(tuple(evs), last, ref, cur, loss, rate, since)
