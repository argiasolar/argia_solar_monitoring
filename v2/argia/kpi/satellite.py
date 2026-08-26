"""Satellite irradiance cross-check (Open-Meteo) — sensor-drift detection.

WHY: PR, PR_STC and expected-energy all stand on the site's own irradiance
sensor (ShineMaster / vendor pyranometer). A drifting or dirty sensor skews
ALL of them in the same direction, and nothing vendor-side ever disagrees —
only an independent reference can notice. (AGS doctrine: flag what you
cannot verify; never let a silent input rot.)

HOW: Open-Meteo daily shortwave_radiation_sum (GHI, model analysis) at the
plant's coordinates. The site sensor measures plane-of-array; satellite
gives horizontal — the two are NOT equal and are never compared directly.
Instead the measured/satellite RATIO per day is tracked: for one site and
season that ratio is roughly stable, so a step or trend in it means a
sensor problem (or persistent sensor shading/soiling). We compare the
median ratio of the recent window against the baseline window before it
and flag a relative change beyond threshold.

Never used for billing. Billing stands on vendor counters (recon engine).

Pure functions only — the HTTP fetch lives in scripts/satellite_check.py.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Dict, List, Optional
from urllib.parse import urlencode


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
PAST_DAYS = 40
"""Window fetched: covers the 7-day recent + ~28-day baseline comparison
with slack for days lost to gaps or overcast (< MIN_DAYLIGHT_KWH_M2)."""

MIN_DAYLIGHT_KWH_M2 = 0.5
"""Days below this (either side) are skipped — the ratio of two small
noisy numbers is meaningless."""

RECENT_DAYS = 7
MIN_RECENT = 4
MIN_BASELINE = 10
DRIFT_REVIEW_PCT = 10.0
"""|recent/baseline - 1| beyond this → REVIEW. Chosen loose on purpose:
POA/GHI ratio moves a few % with season and weather mix; 10% in a week
is not weather."""


def build_url(lat: float, lon: float, past_days: int = PAST_DAYS) -> str:
    """Open-Meteo forecast-API URL (past_days gives recent history with no
    ERA5 archive delay — model analysis is fine for drift detection)."""
    return OPEN_METEO_URL + "?" + urlencode({
        "latitude": f"{float(lat):.6f}",
        "longitude": f"{float(lon):.6f}",
        "daily": "shortwave_radiation_sum",
        "past_days": int(past_days),
        "forecast_days": 1,
        "timezone": "America/Mexico_City",
    })


def parse_daily_ghi(payload: dict) -> Dict[str, float]:
    """{date_iso: GHI kWh/m2} from an Open-Meteo daily response.

    Unit-aware (daily_units): MJ/m2 (the API default) → /3.6, Wh/m2 →
    /1000, kWh/m2 → as-is. An UNKNOWN unit returns {} — a number we can't
    verify the unit of is worse than no number. Null days are skipped.
    """
    try:
        unit = str(payload["daily_units"]["shortwave_radiation_sum"])
        times = payload["daily"]["time"]
        vals = payload["daily"]["shortwave_radiation_sum"]
    except (KeyError, TypeError):
        return {}
    if "MJ" in unit:
        factor = 1.0 / 3.6
    elif "kWh" in unit:
        factor = 1.0
    elif "Wh" in unit:
        factor = 1.0 / 1000.0
    else:
        return {}
    out: Dict[str, float] = {}
    for t, v in zip(times, vals):
        if v is None:
            continue
        try:
            out[str(t)] = round(float(v) * factor, 3)
        except (TypeError, ValueError):
            continue
    return out


def ratio_series(measured: Dict[str, Optional[float]],
                 satellite: Dict[str, float],
                 min_kwh_m2: float = MIN_DAYLIGHT_KWH_M2,
                 ) -> Dict[str, float]:
    """{date: measured/satellite} for days where BOTH sides are present
    and above the daylight floor. Missing either side just drops the day
    — completeness is the drift check's problem, not this function's."""
    out: Dict[str, float] = {}
    for day, m in measured.items():
        s = satellite.get(day)
        if m is None or s is None:
            continue
        try:
            m_f, s_f = float(m), float(s)
        except (TypeError, ValueError):
            continue
        if m_f < min_kwh_m2 or s_f < min_kwh_m2:
            continue
        out[day] = round(m_f / s_f, 4)
    return out


def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@dataclasses.dataclass(frozen=True)
class DriftCheck:
    status: str                        # OK | REVIEW | NO_DATA
    drift_pct: Optional[float]         # recent vs baseline, signed
    recent_median: Optional[float]
    baseline_median: Optional[float]
    n_recent: int
    n_baseline: int
    note: str


def drift_check(ratios: Dict[str, float],
                recent_days: int = RECENT_DAYS,
                min_recent: int = MIN_RECENT,
                min_baseline: int = MIN_BASELINE,
                threshold_pct: float = DRIFT_REVIEW_PCT) -> DriftCheck:
    """Split the ratio series into the last ``recent_days`` CALENDAR days
    (relative to the newest day present) vs everything before, compare
    medians. Calendar-based on purpose: with sparse data, "last N
    entries" would silently pull old days into the recent window and
    dilute a real drift. Median (not mean) on purpose: one broken day
    must not fake a drift. Too little data on either side → NO_DATA,
    never a guessed verdict."""
    parsed = {}
    for day in ratios:
        try:
            parsed[day] = _dt.date.fromisoformat(str(day))
        except ValueError:
            continue                   # unparsable date: excluded, honest
    days = sorted(parsed)
    if not days:
        return DriftCheck("NO_DATA", None, None, None, 0, 0,
                          "not enough overlapping days"
                          f" (recent 0/{min_recent},"
                          f" baseline 0/{min_baseline})")
    cutoff = parsed[days[-1]] - _dt.timedelta(days=recent_days - 1)
    recent = [d for d in days if parsed[d] >= cutoff]
    baseline = [d for d in days if parsed[d] < cutoff]
    n_r, n_b = len(recent), len(baseline)
    if n_r < min_recent or n_b < min_baseline:
        return DriftCheck(
            "NO_DATA", None, None, None, n_r, n_b,
            f"not enough overlapping days (recent {n_r}/{min_recent},"
            f" baseline {n_b}/{min_baseline})")
    r_med = _median([ratios[d] for d in recent])
    b_med = _median([ratios[d] for d in baseline])
    if b_med <= 0:
        return DriftCheck("NO_DATA", None, round(r_med, 4),
                          round(b_med, 4), n_r, n_b,
                          "baseline median not positive")
    drift = (r_med / b_med - 1.0) * 100.0
    if abs(drift) > threshold_pct:
        status = "REVIEW"
        note = (f"measured/satellite ratio moved {drift:+.1f}% vs the"
                " previous weeks — check the irradiance sensor"
                " (soiling, shading, failure)")
    else:
        status = "OK"
        note = "sensor ratio stable vs satellite GHI"
    return DriftCheck(status, round(drift, 1), round(r_med, 4),
                      round(b_med, 4), n_r, n_b, note)
