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


def evaluate_silent_gaps(
    plant_key: str,
    rows: Sequence[Sample],
    rated_kw: Dict[str, float],
    day_end_utc: dt.datetime,
    configured: Optional[Sequence[str]] = None,
) -> List[SilentBreach]:
    """One plant, one day. ``day_end_utc`` = end of the production window
    (a gap that runs to it is 'never came back'). Only gaps that START
    inside the 09-17 MX window and during which a sibling produced count;
    one breach per inverter (its longest gap)."""
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
            sib_in_gap = [r for r in rows if r[1] != sn and a < r[0] < end
                          and (r[3] or 0) > 0]
            if not sib_in_gap:
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
