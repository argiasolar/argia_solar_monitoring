"""Data-staleness detector — pipeline health (plan metric ``data_stale``).

Answers "did telemetry actually arrive?" — independent of what the plants
did. A plant with zero rows all day means the collector failed for it (or
the vendor API did); a plant with a multi-hour daylight hole means samples
were dropped. Either way the day's aggregates are suspect and someone
should know WITHOUT reading logs.

This complements data_class: data_class silently protects downstream
consumers from partial days; data_stale actively raises a hand.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.analytics.inverter_health import Severity
from argia.archive.kpi_daily import (
    CLOUD_DAYLIGHT_END_HOUR,
    CLOUD_DAYLIGHT_START_HOUR,
)
from argia.core.time_utils import MX_TZ, utc_to_mx

LOG = logging.getLogger("argia.analytics.data_health")

MAX_DAYLIGHT_GAP_HOURS = 6.0
"""Largest tolerated hole in daylight coverage before a WARNING fires.

Generous on purpose: GitHub's scheduler routinely stretches the cadence to
1-2 h, which must stay silent. The real 2026-06-30 failure (last sample
13:18, then nothing) left a 6.7 h trailing hole — that must fire."""


@dataclass(frozen=True)
class StaleBreach:
    """A plant whose telemetry coverage broke for the day."""

    plant_key: str
    gap_hours: Optional[float]   # None when there were no rows at all
    severity: Severity
    message: str


def evaluate_data_stale(
    timestamps_by_plant: Dict[str, List[dt.datetime]],
    active_plants: List[str],
    date_iso: str,
    max_gap_hours: float = MAX_DAYLIGHT_GAP_HOURS,
) -> List[StaleBreach]:
    """Flag plants with no telemetry, or with a daylight hole > threshold.

    ``timestamps_by_plant`` maps plant_key -> that day's sample timestamps
    (UTC, any order). Gaps are measured over the MX daylight window
    INCLUDING the edges — a day whose last sample lands at 13:18 has a
    trailing hole to 20:00 even though no two samples are far apart.

    - zero rows all day        -> CRITICAL (collector produced nothing)
    - largest hole > threshold -> WARNING  (aggregates for the day suspect)

    Pure function — no I/O.
    """
    y, m, d = (int(x) for x in date_iso.split("-"))
    day_start = dt.datetime(y, m, d, CLOUD_DAYLIGHT_START_HOUR, tzinfo=MX_TZ)
    day_end = dt.datetime(y, m, d, CLOUD_DAYLIGHT_END_HOUR, tzinfo=MX_TZ)

    breaches: List[StaleBreach] = []
    for pk in sorted(active_plants):
        stamps = timestamps_by_plant.get(pk) or []
        if not stamps:
            breaches.append(StaleBreach(
                plant_key=pk, gap_hours=None, severity=Severity.CRITICAL,
                message=(f"{pk}: NO telemetry arrived for {date_iso} "
                         f"[CRITICAL]"),
            ))
            continue

        mx = sorted(t.astimezone(MX_TZ) for t in stamps if t is not None)
        # fenceposts: daylight start, every sample clamped in-window, daylight end
        points = [day_start] + [t for t in mx if day_start <= t <= day_end] + [day_end]
        worst = max((b - a).total_seconds() / 3600.0
                    for a, b in zip(points[:-1], points[1:]))
        if worst > max_gap_hours:
            breaches.append(StaleBreach(
                plant_key=pk, gap_hours=round(worst, 1),
                severity=Severity.WARNING,
                message=(f"{pk}: {worst:.1f} h daylight hole in telemetry on "
                         f"{date_iso} (max allowed {max_gap_hours:.0f} h) "
                         f"[WARNING]"),
            ))
    return breaches
