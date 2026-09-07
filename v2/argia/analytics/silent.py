"""Silent-inverter classification through the vendor counter (v203).

The acute tier opens ``inverter_silent`` when one inverter stops sending
while its siblings produce. This module is the DAILY owner: over a full
day it finds every daylight gap of an inverter, and classifies each one
with the one fact the acute tier cannot have yet — what the inverter's
own energy counter (etoday) did across the gap, compared with the
siblings' counters per rated kW over the same window:

    counter climbed like the siblings -> COMMS gap, no energy lost
                                        (SLP2 2026-09-04: +145 kWh across
                                        a 5.6 h gap, 91 % of siblings/kW)
    counter flat / far below siblings -> the unit was OFF, energy lost
    never came back that day          -> unconfirmed, treat as OFF

PURE: rows in, breaches out.

v223: a gap that the WHOLE fleet shares is the collector's (2026-09-06
13:55-14:35 MX: PostgreSQL on pio06 was unreachable for 35 min and every
Growatt plant went blank at once — the rule then blamed seven
inverters at two plants for a "silent" spell they never had). Such
windows come from ``collector_windows`` and are excluded before an
inverter is judged.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from argia.analytics.inverter_health import Severity
from argia.core.time_utils import utc_to_mx

GAP_MIN = 45                 # = acute.SILENT_WARN_MIN
WINDOW_START_HOUR = 9        # = acute.DARK_CHECK_START_HOUR
WINDOW_END_HOUR = 17
COMMS_RATIO = 0.70
"""Counter growth across the gap >= this share of the siblings' per-kW
growth -> the inverter produced through the gap (comms only)."""
MIN_SIBLING_SAMPLES = 3
"""Fewer producing sibling samples inside the gap = the plant as a whole
was dark (NL2 2026-09-04: both units gone 13:14-14:35, one straggler
sample) — plant-level rules own that."""
MIN_SIBLING_KWH_PER_KW = 0.05
"""Below this the siblings barely produced during the gap (dusk, heavy
cloud): nothing to compare against, the gap is reported as unclassified."""


@dataclass(frozen=True)
class SilentBreach:
    plant_key: str
    inverter_sn: str
    gap_start_utc: dt.datetime
    gap_end_utc: Optional[dt.datetime]     # None = never came back that day
    gap_min: float
    self_kwh: Optional[float]              # counter growth across the gap
    sibling_kwh_per_kw: Optional[float]    # median sibling growth per kW
    ratio: Optional[float]
    kind: str                              # comms | off | unconfirmed | unclassified
    severity: Severity
    message: str


Sample = Tuple[dt.datetime, str, Optional[float], Optional[float]]
"""(ts_utc, inverter_sn, etoday_kwh, power_w) — one plant's day."""


def _counter_at(rows: Sequence[Sample], sn: str, ts: dt.datetime,
                before: bool) -> Optional[float]:
    """Last counter at or before ``ts`` (before=True) or first at or after."""
    vals = [r[2] for r in rows if r[1] == sn and r[2] is not None
            and ((r[0] <= ts) if before else (r[0] >= ts))]
    if not vals:
        return None
    return vals[-1] if before else vals[0]


def _gaps(rows: Sequence[Sample], sn: str, day_end_utc: dt.datetime
          ) -> List[Tuple[dt.datetime, Optional[dt.datetime]]]:
    ts = sorted(r[0] for r in rows if r[1] == sn)
    out = []
    for a, b in zip(ts, ts[1:]):
        if (b - a).total_seconds() / 60.0 >= GAP_MIN:
            out.append((a, b))
    if ts and (day_end_utc - ts[-1]).total_seconds() / 60.0 >= GAP_MIN:
        out.append((ts[-1], None))
    return out


SLOT_MIN = 5
COLLECTOR_SHARE = 0.5
"""A 5-minute slot in which fewer than this share of the plants that
reported that day delivered a sample is a collector slot (our side or
the vendor cloud), not a site event."""
COLLECTOR_OVERLAP = 0.8
"""An inverter gap that lies at least this much inside collector
windows is not the inverter's silence."""


def collector_windows(ts_by_plant: Dict[str, Sequence[dt.datetime]],
                      slot_min: int = SLOT_MIN,
                      share: float = COLLECTOR_SHARE) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Windows [start, end) in which the fleet as a whole went blank:
    consecutive slots where fewer than ``share`` of the day's reporting
    plants have any sample, between the first and last slot with data.
    Pure."""
    plants = [pk for pk, ts in ts_by_plant.items() if ts]
    if len(plants) < 2:
        return []
    step = dt.timedelta(minutes=slot_min)

    def slot(t: dt.datetime) -> dt.datetime:
        return t.replace(minute=(t.minute // slot_min) * slot_min, second=0, microsecond=0)
    present: Dict[dt.datetime, set] = {}
    for pk in plants:
        for t in ts_by_plant[pk]:
            if t is not None:
                present.setdefault(slot(t), set()).add(pk)
    if not present:
        return []
    need = share * len(plants)
    out: List[Tuple[dt.datetime, dt.datetime]] = []
    t, last = min(present), max(present)
    cur: Optional[dt.datetime] = None
    while t <= last:
        blank = len(present.get(t, ())) < need
        if blank and cur is None:
            cur = t
        elif not blank and cur is not None:
            out.append((cur, t))
            cur = None
        t += step
    if cur is not None:
        out.append((cur, last + step))
    return out


def inside_collector_windows(a: dt.datetime, b: dt.datetime,
                             windows: Sequence[Tuple[dt.datetime, dt.datetime]],
                             min_overlap: float = COLLECTOR_OVERLAP) -> bool:
    """True when at least ``min_overlap`` of [a, b] lies inside the
    collector windows (a slot of tolerance on each side). Pure."""
    total = (b - a).total_seconds()
    if total <= 0 or not windows:
        return False
    tol = dt.timedelta(minutes=SLOT_MIN)
    covered = 0.0
    for s, e in windows:
        lo, hi = max(a, s - tol), min(b, e + tol)
        if hi > lo:
            covered += (hi - lo).total_seconds()
    return covered / total >= min_overlap


def evaluate_silent_gaps(
    plant_key: str,
    rows: Sequence[Sample],
    rated_kw: Dict[str, float],
    day_end_utc: dt.datetime,
    configured: Optional[Sequence[str]] = None,
    collector: Sequence[Tuple[dt.datetime, dt.datetime]] = (),
) -> List[SilentBreach]:
    """One plant, one day. ``day_end_utc`` = end of the production window
    (a gap that runs to it is 'never came back'). Only gaps that START
    inside the 09-17 MX window and during which a sibling produced count;
    one breach per inverter (its longest gap). ``collector`` windows
    (v223, from ``collector_windows``) are the fleet-wide blanks that are
    never an inverter's fault."""
    sns = sorted(set(configured or []) | {r[1] for r in rows})
    out: List[SilentBreach] = []
    for sn in sns:
        best: Optional[SilentBreach] = None
        my_rows = [r for r in rows if r[1] == sn]
        gaps = _gaps(rows, sn, day_end_utc) if my_rows else []
        if not my_rows and configured and sn in configured:
            gaps = []            # never reported at all: inverter-silent (mailer) territory
        for a, b in gaps:
            a_mx = utc_to_mx(a)
            if not (WINDOW_START_HOUR <= a_mx.hour < WINDOW_END_HOUR):
                continue
            end = b or day_end_utc
            gap_min = (end - a).total_seconds() / 60.0
            if inside_collector_windows(a, end, collector):
                continue         # the whole fleet was blank: our collector, not this unit
            sib_in_gap = [r for r in rows if r[1] != sn and a < r[0] < end
                          and (r[3] or 0) > 0]
            if len(sib_in_gap) < MIN_SIBLING_SAMPLES:
                continue         # whole plant was dark: plant-level rules own it
            # siblings' growth per rated kW over the gap window
            per_kw: List[float] = []
            for osn in {r[1] for r in sib_in_gap}:
                kw = rated_kw.get(osn)
                c0, c1 = _counter_at(rows, osn, a, True), _counter_at(rows, osn, end, False if b else True)
                if kw and c0 is not None and c1 is not None and c1 >= c0:
                    per_kw.append((c1 - c0) / kw)
            sib = statistics.median(per_kw) if per_kw else None
            self_kwh = ratio = None
            kw_self = rated_kw.get(sn)
            if b is not None:
                c0, c1 = _counter_at(rows, sn, a, True), _counter_at(rows, sn, b, False)
                if c0 is not None and c1 is not None:
                    self_kwh = max(c1 - c0, 0.0)
                    if sib and sib >= MIN_SIBLING_KWH_PER_KW and kw_self:
                        ratio = (self_kwh / kw_self) / sib
            hhmm = f"{a_mx:%H:%M}"
            if b is None:
                kind, sev = "unconfirmed", Severity.CRITICAL
                msg = (f"{plant_key} {sn}: silent since {hhmm} MX ({gap_min:.0f} min to "
                       f"the end of the day) while siblings produced — no counter to "
                       f"confirm production; treat as OFF [CRITICAL]")
            elif ratio is None:
                kind, sev = "unclassified", Severity.WARNING
                msg = (f"{plant_key} {sn}: no data {hhmm}-{utc_to_mx(b):%H:%M} MX "
                       f"({gap_min:.0f} min) while siblings reported; counter "
                       f"+{(self_kwh or 0):.0f} kWh across the gap, siblings too low "
                       f"to compare [WARNING]")
            elif ratio >= COMMS_RATIO:
                kind, sev = "comms", Severity.WARNING
                msg = (f"{plant_key} {sn}: no data {hhmm}-{utc_to_mx(b):%H:%M} MX "
                       f"({gap_min:.0f} min) but its counter climbed +{self_kwh:.0f} kWh "
                       f"({100 * ratio:.0f}% of siblings per kW) — it kept producing: "
                       f"datalogger/RS485 link, no energy lost [WARNING]")
            else:
                lost = max(sib * (kw_self or 0) - (self_kwh or 0), 0.0)
                kind, sev = "off", Severity.CRITICAL
                msg = (f"{plant_key} {sn}: no data {hhmm}-{utc_to_mx(b):%H:%M} MX "
                       f"({gap_min:.0f} min) and its counter grew only +{(self_kwh or 0):.0f} kWh "
                       f"({100 * ratio:.0f}% of siblings per kW) — the unit was OFF, "
                       f"~{lost:.0f} kWh lost [CRITICAL]")
            br = SilentBreach(plant_key, sn, a, b, round(gap_min), self_kwh, sib, ratio,
                              kind, sev, msg)
            if best is None or gap_min > best.gap_min:
                best = br
        if best is not None:
            out.append(best)
    return out
