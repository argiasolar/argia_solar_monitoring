"""Irradiance integration — Stage 7.2.

Converts a day's W/m² readings into a single kWh/m² value.

Two inputs supported:
1. ``InverterRow.irradiance_wm2`` — from the plant's ShineMaster (or a neighbor).
   Sampled at telemetry cadence (typically every 5 min during daylight).
2. ``InverterRow.cloud_cover_pct`` + plant coordinates — fallback when no
   ShineMaster reading. Uses a simple clear-sky-times-(1-cloud) model.

Hybrid strategy: prefer #1 when we have ≥10 valid samples spread across the
day. Otherwise fall back to #2. We track which source was used so downstream
code (PR confidence rating, daily report transparency) can show it.

Why trapezoidal: irradiance is a smooth continuous curve sampled discretely.
Rectangular sum (W/m² × Δt for each sample) systematically overestimates;
trapezoidal averages each pair of adjacent samples. With 5-minute samples on
a typical sun day the trapezoidal estimate is within 1-2% of a 1-minute
ground-truth integration.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from argia.core.time_utils import MX_TZ, UTC
from argia.kpi.reader import InverterRow

LOG = logging.getLogger("argia.kpi.irradiance")


class IrradianceSource(str, Enum):
    SHINEMASTER = "shinemaster"
    CLOUD_COVER_MODEL = "cloud_cover_model"
    NONE = "none"


@dataclass(frozen=True)
class IrradianceDay:
    """End-of-day plane-of-array irradiation, kWh/m²."""

    kwh_m2: Optional[float]
    """Best-effort daily integral. None if no usable data."""

    source: IrradianceSource
    samples_used: int
    """Count of input samples (for ShineMaster) or hours of cloud data
    (for the fallback). Lets callers reason about confidence."""


# We need at least this many distinct timestamps with non-None
# irradiance_wm2 readings before we trust the ShineMaster source.
# At 5-min cadence over a ~13h day = 156 samples max; 10 is a low bar.
MIN_SHINEMASTER_SAMPLES = 10

# Realistic surface plane-of-array ceiling. Noon sun at AM1 tops out around
# 1100 W/m²; the solar constant is ~1361. Readings above this are sensor
# spikes — we CLAMP them to this value rather than drop the sample. Dropping
# would remove the time point and, given the coarse ShineMaster cadence below,
# blow a ~2h hole in the daily trapezoidal integral.
MAX_PLAUSIBLE_WM2 = 1200.0

# The ShineMaster env-history is coarse: distinct irradiance readings land
# only ~every 2 hours (each repeated a few times seconds apart), NOT every
# 5 minutes as first assumed. The daily integrator must therefore bridge ~2h
# gaps between real readings; only a gap beyond this is treated as a genuine
# data outage (not interpolated).
SHINEMASTER_MAX_GAP_SEC = 10800  # 3 hours


def _dedupe_by_timestamp(
    rows: List[InverterRow],
) -> List[Tuple[dt.datetime, float]]:
    """A day's telemetry has N rows per timestamp (one per inverter), but
    the irradiance reading is plant-wide and identical across them. Build a
    deduplicated time series.

    Filters out:
    - None readings and negatives
    - Duplicate timestamps (keeps the first one encountered after sort)
    Clamps readings above MAX_PLAUSIBLE_WM2 down to it (sensor spikes) —
    the time point is kept so the daily integral stays connected.
    """
    if not rows:
        return []
    by_ts: dict = {}
    for r in rows:
        if r.irradiance_wm2 is None:
            continue
        try:
            v = float(r.irradiance_wm2)
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        if v > MAX_PLAUSIBLE_WM2:
            v = MAX_PLAUSIBLE_WM2  # clamp sensor spike; keep the time point
        by_ts.setdefault(r.timestamp_utc, v)
    return sorted(by_ts.items())


def _trapezoidal_integrate_wm2_to_kwh_m2(
    points: List[Tuple[dt.datetime, float]],
    max_gap_sec: int = SHINEMASTER_MAX_GAP_SEC,
) -> Optional[float]:
    """Trapezoidal integration of W/m² samples → kWh/m² for the day.

    Skips intervals with gaps > max_gap_sec so a genuine multi-hour sensor
    outage isn't filled in by linear interpolation. The default (3h) bridges
    the ShineMaster's normal ~2h reading cadence while still catching real
    outages. (The earlier 1h default assumed dense 5-min sampling that this
    source does not actually provide, so every real interval was discarded
    and the daily total collapsed to ~0.)

    Math:
      for each consecutive pair (t1, w1), (t2, w2) with dt = t2 - t1 in hours:
          contribution = ((w1 + w2) / 2) × dt           [W·h/m²]
      total kWh/m² = sum / 1000
    """
    if len(points) < 2:
        return None

    total_wh_m2 = 0.0
    skipped_intervals = 0
    for (t1, w1), (t2, w2) in zip(points[:-1], points[1:]):
        gap = (t2 - t1).total_seconds()
        if gap <= 0:
            continue
        if gap > max_gap_sec:
            skipped_intervals += 1
            continue
        dt_hours = gap / 3600.0
        total_wh_m2 += ((w1 + w2) / 2.0) * dt_hours

    if skipped_intervals:
        LOG.info(
            "Skipped %d irradiance intervals with gaps > %ds",
            skipped_intervals, max_gap_sec,
        )

    return round(total_wh_m2 / 1000.0, 4)


def integrate_irradiance_kwh_m2(
    rows: List[InverterRow],
    max_gap_sec: int = SHINEMASTER_MAX_GAP_SEC,
) -> IrradianceDay:
    """ShineMaster path. Returns IrradianceDay or NONE if too few samples."""
    points = _dedupe_by_timestamp(rows)
    if len(points) < MIN_SHINEMASTER_SAMPLES:
        return IrradianceDay(
            kwh_m2=None,
            source=IrradianceSource.NONE,
            samples_used=len(points),
        )
    total = _trapezoidal_integrate_wm2_to_kwh_m2(points, max_gap_sec=max_gap_sec)
    if total is None or total <= 0:
        return IrradianceDay(
            kwh_m2=None,
            source=IrradianceSource.NONE,
            samples_used=len(points),
        )
    return IrradianceDay(
        kwh_m2=total,
        source=IrradianceSource.SHINEMASTER,
        samples_used=len(points),
    )


# ---------- cloud-cover fallback ----------


def _clear_sky_kwh_m2_simple(
    lat: float,
    date_iso: str,
    cloud_fraction: float,
    site_tz=MX_TZ,
) -> Optional[float]:
    """Very simple clear-sky model with cloud derate.

    NOT a substitute for a real radiation model. The intent is order-of-
    magnitude: tell us if we should expect ~6 kWh/m² (sunny Mexico) vs
    ~2 kWh/m² (heavy overcast). PR computed with this fallback gets a
    `LOW_CONFIDENCE` flag downstream.

    Method:
    1. Compute solar declination + sunrise/sunset for the date+latitude
    2. Approximate clear-sky day total as 9 × max_solar_elev_factor kWh/m²
       (a coarse heuristic that matches NREL clear-sky averages for
       mid-latitudes ±10%)
    3. Apply (1 - cloud_fraction) × 0.7  derate
       — the 0.7 accounts for the fact that even thick clouds let diffuse
       radiation through

    Args:
        lat: degrees, positive=north
        date_iso: 'YYYY-MM-DD'
        cloud_fraction: 0.0 (clear) to 1.0 (overcast)
        site_tz: timezone for day_of_year computation
    """
    try:
        date = dt.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return None

    if not (-90 <= lat <= 90):
        return None
    cloud_fraction = max(0.0, min(1.0, cloud_fraction))

    # Solar declination (degrees)
    n = date.timetuple().tm_yday
    declination_deg = 23.45 * math.sin(math.radians(360.0 / 365.0 * (n - 81)))

    # Solar elevation at noon
    lat_rad = math.radians(lat)
    decl_rad = math.radians(declination_deg)
    sin_noon_elev = math.sin(lat_rad) * math.sin(decl_rad) + math.cos(lat_rad) * math.cos(decl_rad)
    if sin_noon_elev <= 0:
        # Polar night
        return 0.0
    noon_elev_factor = sin_noon_elev  # in [0, 1]

    # Clear-sky day total — heuristic
    clear_sky_kwh_m2 = 9.0 * noon_elev_factor

    # Cloud derate
    cloud_derate = 1.0 - 0.7 * cloud_fraction
    return round(clear_sky_kwh_m2 * cloud_derate, 3)


def _avg_daytime_cloud(rows: List[InverterRow]) -> Optional[float]:
    """Average cloud_cover_pct across distinct daytime timestamps.

    'Daytime' = any row where irradiance_wm2 > 50 OR power_w > 0 across
    at least one inverter. Falls back to all hours when nothing daylight-
    looking is found. Result in [0, 1] as a fraction (not pct)."""
    if not rows:
        return None

    daytime_pcts: List[float] = []
    fallback_pcts: List[float] = []

    by_ts: dict = {}
    for r in rows:
        if r.cloud_cover_pct is None:
            continue
        if r.timestamp_utc in by_ts:
            continue
        by_ts[r.timestamp_utc] = (r.cloud_cover_pct, r.irradiance_wm2 or 0, r.power_w or 0)

    for ts, (pct, irr, pw) in by_ts.items():
        try:
            pct_f = float(pct)
        except (TypeError, ValueError):
            continue
        fallback_pcts.append(pct_f)
        if irr > 50 or pw > 0:
            daytime_pcts.append(pct_f)

    sample = daytime_pcts or fallback_pcts
    if not sample:
        return None

    avg_pct = sum(sample) / len(sample)
    return max(0.0, min(1.0, avg_pct / 100.0))


def estimate_irradiance_from_clouds(
    rows: List[InverterRow],
    lat: Optional[float],
    date_iso: str,
    site_tz=MX_TZ,
) -> IrradianceDay:
    """Fallback path when ShineMaster doesn't give enough samples.

    Reads cloud_cover_pct from the same rows. If neither lat nor cloud
    data exist, returns NONE."""
    if lat is None:
        return IrradianceDay(kwh_m2=None, source=IrradianceSource.NONE, samples_used=0)
    cloud_fraction = _avg_daytime_cloud(rows)
    if cloud_fraction is None:
        return IrradianceDay(kwh_m2=None, source=IrradianceSource.NONE, samples_used=0)
    kwh = _clear_sky_kwh_m2_simple(lat, date_iso, cloud_fraction, site_tz)
    if kwh is None or kwh <= 0:
        return IrradianceDay(kwh_m2=None, source=IrradianceSource.NONE, samples_used=0)
    return IrradianceDay(
        kwh_m2=kwh,
        source=IrradianceSource.CLOUD_COVER_MODEL,
        samples_used=1,
    )


# ---------- hybrid entry point ----------


def daily_irradiance_for_plant(
    rows: List[InverterRow],
    lat: Optional[float],
    date_iso: str,
    site_tz=MX_TZ,
) -> IrradianceDay:
    """Hybrid: try ShineMaster first, fall back to cloud model.

    This is the function the PR computation calls."""
    shinemaster = integrate_irradiance_kwh_m2(rows)
    if shinemaster.source == IrradianceSource.SHINEMASTER:
        return shinemaster
    LOG.info(
        "Falling back to cloud-cover model (ShineMaster samples=%d, need >=%d)",
        shinemaster.samples_used, MIN_SHINEMASTER_SAMPLES,
    )
    return estimate_irradiance_from_clouds(rows, lat, date_iso, site_tz)
