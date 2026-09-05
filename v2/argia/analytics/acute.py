"""Acute (per-snapshot) detectors — conditions trustworthy from ONE sample.

These run every telemetry collection during daylight, so a plant that dies
at 09:00 raises a hand within the next cycle instead of tomorrow 06:30.
Selection rule: only conditions where a SINGLE snapshot is evidence —

- inverter_fault      device self-diagnosed fault token in its latest sample
- inverter_temp_high  thermal mass makes one high reading real, not noise
- plant_offline       the WHOLE plant at 0 W mid-daylight; all inverters
                      simultaneously is never a transient. (A single
                      inverter at 0 IS transient — proven repeatedly — so
                      per-inverter zero stays daily-only via the relative
                      detector.)
- data_stale (acute)  the plant's newest sample is older than N minutes of
                      daylight; stateless, tolerant of one flaky poll.

The acute tier only OPENS/TOUCHES alerts (engine ``resolve_missing=False``).
The DAILY run owns resolution, arbitrating on full-day aggregates — this
one-way design makes flapping structurally impossible.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.analytics.inverter_health import Severity
from argia.analytics.vendor_flags import MIN_FAULT_SAMPLES, fault_tokens
from argia.core.time_utils import utc_to_mx

LOG = logging.getLogger("argia.analytics.acute")

# Latest-sample freshness: a snapshot older than this says nothing about NOW.
FRESH_WINDOW_MIN = 45

# Vendor-fault look-back: faults are judged over ALL samples in this
# window, not just the latest one. A self-recovering trip (JFM5D8900B
# FT=302, 2026-07-09, 13:06-13:11 MX — two samples, cleared before the
# 13:30 tick) was structurally invisible to latest-sample-only
# evaluation. 35 min covers the 30-min tick cadence plus jitter; the
# evidence bar is MIN_FAULT_SAMPLES faulted samples in the window,
# mirroring the daily tier so a single-sample vendor blip still does
# not page anyone.
FAULT_LOOKBACK_MIN = 35

# Mid-daylight window (MX) for the plant-dark check. Narrower than the
# 06-20 aggregation window on purpose: at the edges a healthy plant can
# legitimately sit at ~0 W, so acute zero-power is only evidence mid-day.
DARK_CHECK_START_HOUR = 9
DARK_CHECK_END_HOUR = 17

TEMP_WARN_C = 65.0
TEMP_CRIT_C = 75.0

ACUTE_STALE_MIN = 120
"""No sample for a plant in this many daylight minutes -> acute data gap.
Generous vs GitHub's jittery cadence (verified 1-2 h gaps are normal)."""

# v203 — one inverter silent while its siblings produce (SLP2 2026-09-04:
# Inverter 1 sent nothing 14:20-19:59 MX while Inverter 2 reported
# 105 kW; the portal said "stale", nobody was told). Per-inverter ZERO
# stays daily-only (transient) — per-inverter SILENCE next to a
# producing sibling is not transient at 45 min.
SILENT_WARN_MIN = 45
SILENT_CRIT_MIN = 180
SIBLING_MIN_W = 5000.0
"""A sibling counts as 'producing' above this (daylight, not dusk)."""

DAYLIGHT_START_HOUR = 6
DAYLIGHT_END_HOUR = 20


@dataclass(frozen=True)
class AcuteBreach:
    metric: str            # inverter_fault | inverter_temp_high | plant_offline | data_stale
    plant_key: str
    inverter_sn: str       # "" for plant-level
    severity: Severity
    value: Optional[float]
    message: str


def _latest_per_inverter(
    samples: List[Tuple[dt.datetime, str, str, Optional[float],
                        Optional[float], Optional[int], Optional[str]]],
) -> Dict[Tuple[str, str], Tuple]:
    """Newest sample per (plant, sn). Sample: (ts, plant, sn, power_w,
    temperature_c, status, fault_code)."""
    latest: Dict[Tuple[str, str], Tuple] = {}
    for s in samples:
        ts, plant, sn = s[0], str(s[1]).strip(), str(s[2]).strip()
        if ts is None:
            continue
        key = (plant, sn)
        if key not in latest or ts > latest[key][0]:
            latest[key] = s
    return latest


def evaluate_acute(
    samples: List[Tuple[dt.datetime, str, str, Optional[float],
                        Optional[float], Optional[int], Optional[str]]],
    active_plants: List[str],
    now_utc: dt.datetime,
    fresh_window_min: int = FRESH_WINDOW_MIN,
    stale_min: int = ACUTE_STALE_MIN,
    absent_gap_hours: Optional[float] = None,
    configured_inverters: Optional[Dict[str, List[str]]] = None,
) -> List[AcuteBreach]:
    """Evaluate the acute conditions against the newest samples.

    ``configured_inverters`` ({plant: [sn]} of ACTIVE, in-service units)
    enables the per-inverter silence check — an inverter absent from the
    tail is only reportable when the caller says how long the tail is
    (``absent_gap_hours``).

    ``samples`` is [(timestamp_utc, plant_key, inverter_sn, power_w,
    temperature_c, status, fault_code), ...] — the recent tail of telemetry.
    Pure function — no I/O.
    """
    now_mx = utc_to_mx(now_utc)
    if not (DAYLIGHT_START_HOUR <= now_mx.hour < DAYLIGHT_END_HOUR):
        return []  # acute conditions are only meaningful in daylight

    latest = _latest_per_inverter(samples)
    fresh_cut = now_utc - dt.timedelta(minutes=fresh_window_min)
    breaches: List[AcuteBreach] = []

    newest_by_plant: Dict[str, dt.datetime] = {}
    fresh_by_plant: Dict[str, List[Tuple]] = {}
    for (plant, sn), s in latest.items():
        ts = s[0]
        if plant not in newest_by_plant or ts > newest_by_plant[plant]:
            newest_by_plant[plant] = ts
        if ts >= fresh_cut:
            fresh_by_plant.setdefault(plant, []).append(s)

    # --- per-inverter: vendor faults (look-back window, not just the
    # latest sample — see FAULT_LOOKBACK_MIN) ---
    fault_cut = now_utc - dt.timedelta(minutes=FAULT_LOOKBACK_MIN)
    fault_hits: Dict[Tuple[str, str], List[Tuple[dt.datetime, str]]] = {}
    for ts, plant, sn, _pw, _temp, _st, fault in samples:
        if ts < fault_cut or plant not in active_plants:
            continue
        tokens = fault_tokens(fault)
        if tokens:
            fault_hits.setdefault((plant, sn), []).append(
                (ts, ",".join(tokens)))
    for (plant, sn), hits in sorted(fault_hits.items()):
        if len(hits) < MIN_FAULT_SAMPLES:
            continue   # single blip: below the daily tier's evidence bar
        hits.sort()
        codes = ",".join(sorted({c for _, c in hits}))
        first, last = hits[0][0], hits[-1][0]
        breaches.append(AcuteBreach(
            metric="inverter_fault", plant_key=plant, inverter_sn=sn,
            severity=Severity.CRITICAL, value=None,
            message=(f"{plant} {sn}: vendor fault {codes} in "
                     f"{len(hits)} samples "
                     f"({utc_to_mx(first):%H:%M}-{utc_to_mx(last):%H:%M} "
                     f"MX, last {FAULT_LOOKBACK_MIN} min) [CRITICAL]"),
        ))

    # --- per-inverter: temperature (latest fresh sample) ---
    for plant, rows in sorted(fresh_by_plant.items()):
        for ts, _p, sn, _pw, temp, _st, fault in sorted(rows, key=lambda r: r[2]):
            if temp is not None and temp >= TEMP_WARN_C:
                crit = temp >= TEMP_CRIT_C
                breaches.append(AcuteBreach(
                    metric="inverter_temp_high", plant_key=plant,
                    inverter_sn=sn,
                    severity=Severity.CRITICAL if crit else Severity.WARNING,
                    value=round(float(temp), 1),
                    message=(f"{plant} {sn}: internal temperature "
                             f"{temp:.1f} degC (>= "
                             f"{TEMP_CRIT_C if crit else TEMP_WARN_C:.0f}) "
                             f"[{'CRITICAL' if crit else 'WARNING'}]"),
                ))

    # --- plant-level: dark plant (only mid-daylight, only on fresh data) ---
    if DARK_CHECK_START_HOUR <= now_mx.hour < DARK_CHECK_END_HOUR:
        for plant, rows in sorted(fresh_by_plant.items()):
            powers = [r[3] for r in rows]
            if powers and all((p or 0) <= 0 for p in powers):
                breaches.append(AcuteBreach(
                    metric="plant_offline", plant_key=plant, inverter_sn="",
                    severity=Severity.CRITICAL, value=0.0,
                    message=(f"{plant}: ALL {len(powers)} reporting "
                             f"inverter(s) at 0 W at "
                             f"{now_mx:%H:%M} MX [CRITICAL]"),
                ))

    # --- per-inverter: silent while a sibling produces (v203) ---
    if configured_inverters and DARK_CHECK_START_HOUR <= now_mx.hour < DARK_CHECK_END_HOUR:
        for plant, sns in sorted(configured_inverters.items()):
            if plant not in active_plants:
                continue
            fresh = fresh_by_plant.get(plant, [])
            producing = [r for r in fresh if (r[3] or 0) >= SIBLING_MIN_W]
            if not producing:
                continue            # a dark plant is plant_offline / data_stale
            max_kw = max(r[3] for r in producing) / 1000.0
            for sn in sorted(sns):
                if any(r[2] == sn for r in producing):
                    continue
                s = latest.get((plant, sn))
                if s is None:
                    if absent_gap_hours is None:
                        continue    # short tail: cannot say how long
                    age_min = absent_gap_hours * 60.0
                    since = f">= {absent_gap_hours:.1f} h"
                else:
                    age_min = (now_utc - s[0]).total_seconds() / 60.0
                    since = f"since {utc_to_mx(s[0]):%H:%M} MX"
                if age_min < SILENT_WARN_MIN:
                    continue
                crit = age_min >= SILENT_CRIT_MIN
                breaches.append(AcuteBreach(
                    metric="inverter_silent", plant_key=plant, inverter_sn=sn,
                    severity=Severity.CRITICAL if crit else Severity.WARNING,
                    value=round(age_min / 60.0, 1),
                    message=(f"{plant} {sn}: no data for {age_min:.0f} min "
                             f"({since}) while {len(producing)} sibling(s) "
                             f"report up to {max_kw:.0f} kW — inverter off or "
                             f"datalogger link; the vendor counter decides "
                             f"when it reappears "
                             f"[{'CRITICAL' if crit else 'WARNING'}]"),
                ))

    # --- plant-level: acute data gap ---
    for plant in sorted(active_plants):
        newest = newest_by_plant.get(plant)
        if newest is None:
            # Absent from the tail entirely. When the caller reads only a
            # TAIL of telemetry (and tells us its span), absence means the
            # plant has been silent for AT LEAST that long -> fire. Without
            # a span (full-day callers, short tails) stay conservative and
            # leave it to the daily data_stale.
            if absent_gap_hours is not None and absent_gap_hours * 60 > stale_min:
                breaches.append(AcuteBreach(
                    metric="data_stale", plant_key=plant, inverter_sn="",
                    severity=Severity.WARNING, value=round(absent_gap_hours, 1),
                    message=(f"{plant}: no telemetry in the last "
                             f">= {absent_gap_hours:.1f} h [WARNING]"),
                ))
            continue
        age_min = (now_utc - newest).total_seconds() / 60.0
        if age_min > stale_min:
            breaches.append(AcuteBreach(
                metric="data_stale", plant_key=plant, inverter_sn="",
                severity=Severity.WARNING, value=round(age_min / 60.0, 1),
                message=(f"{plant}: no telemetry for {age_min/60.0:.1f} h "
                         f"(last {utc_to_mx(newest):%H:%M} MX) [WARNING]"),
            ))
    return breaches
