"""Daily weather-station check (v396, Arturo's methodology R10, and the R2 station gate).

Performance is only as good as the sun and module temperature it is judged against. Every
day, per plant station (from the stored 5-minute data), four checks:

* **coverage** - share of the 08:00-16:59 MX five-minute steps with an irradiance, a module
  temperature and an ambient temperature reading (each channel on its own; the day's
  coverage is the lowest). Below COV_MIN the day's PR is not used (R2: "daily PR and EPI
  are not calculated when the station daylight coverage is under 90 %").
* **coupling** - the module sensor must sit on the modules: string (MPPT) voltage falls about
  0.25-0.35 %/K as the modules heat up, so the slope of the normalized MPPT voltage against
  the module temperature (samples with >= 600 W/m2) must be clearly negative. One day is
  noisy (clouds move voltage and temperature together), so the check is the median of the
  daily slopes over the last COUPLE_WINDOW days (days with >= COUPLE_MIN_N samples over a
  module-temperature span >= COUPLE_SPAN_K), needing COUPLE_MIN_DAYS such days.
* **satellite** - the nightly satellite cross-check verdict (satellite_check) is carried as
  information, not a failure: it is a rolling verdict that stays on for weeks and flags
  most of the fleet in season (production, Nov 2026).
* **module minus ambient** - at 700-950 W/m2 around noon, against the station's own first
  REF_DAYS days on record AND against the other stations over the same periods: a sensor
  that drifts warmer while the fleet does not (SAG: about 19.6 K in July, 27-28 K in
  October / November, the other stations flat or falling) fails when the excess is above
  DRIFT_MAX_K. Wind is not stored, so it is not part of the test.

Stations shared by several plants (identical daily irradiance, SAG and Vitalmex) are named.
Humidity is not stored, so the dew-point test of R10 cannot run. Pure.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

COV_MIN = 0.90
COUPLE_MAX_SLOPE = -0.10         # %/K - a coupled sensor shows about -0.25 to -0.35
COUPLE_MIN_N = 20
COUPLE_SPAN_K = 8.0
COUPLE_WINDOW = 7
COUPLE_MIN_DAYS = 4
DRIFT_MAX_K = 5.0
REF_DAYS = 30
RECENT_DAYS = 14
MIN_DT_SAMPLES = 10
EXPECTED_STEPS = 108             # 08:00-16:59 MX in 5-minute steps
SHARED_MIN_SHARE = 0.8


@dataclass(frozen=True)
class DayCheck:
    plant_key: str
    day: str
    coverage: Optional[float]
    coupling_slope: Optional[float]
    sat_status: str
    dt_mod_amb: Optional[float]
    dt_drift: Optional[float]
    shared_with: Tuple[str, ...]
    status: str                  # "ok" | "fail" | "no_data"
    reasons: Tuple[str, ...]


def drift_series(dt_daily: Mapping[str, Mapping[str, float]],
                 shared: Optional[Mapping[str, Sequence[str]]] = None) -> Dict[Tuple[str, str], float]:
    """{(plant, day): excess K} - the station's module-minus-ambient change since its own first
    REF_DAYS days on record, minus the median change of the OTHER stations over the same days
    (plants sharing its station are not "other"). ``dt_daily``: {plant: {day: daily mean
    module-minus-ambient at 700-950 W/m2}}."""
    shared = shared or {}
    ref = {}
    for pk, s in dt_daily.items():
        days = sorted(s)[:REF_DAYS]
        if len(days) >= 5:
            ref[pk] = statistics.median(s[d] for d in days)
    # one sensor, one reference: a plant sharing a station uses the reference of the group
    # member with the longest history (SMS joined in September, after the SAG sensor drifted)
    for pk in list(ref):
        group = [pk] + [o for o in shared.get(pk, ()) if o in ref]
        first = min(group, key=lambda g: min(dt_daily[g]))
        ref[pk] = ref[first]
    def recent(pk, day):
        days = [d for d in sorted(dt_daily[pk]) if d <= day][-RECENT_DAYS:]
        return statistics.median(dt_daily[pk][d] for d in days) if len(days) >= 5 else None
    out = {}
    all_days = sorted({d for s in dt_daily.values() for d in s})
    for day in all_days:
        change = {}
        for pk in ref:
            r = recent(pk, day)
            if r is not None:
                change[pk] = r - ref[pk]
        for pk, c in change.items():
            others = [v for o, v in change.items() if o != pk and o not in shared.get(pk, ())]
            if others and day in dt_daily[pk]:
                out[(pk, day)] = round(c - statistics.median(others), 2)
    return out


def shared_groups(irr_daily: Mapping[str, Mapping[str, float]]) -> Dict[str, Tuple[str, ...]]:
    """{plant: other plants with the same station} - identical daily irradiance on at least
    SHARED_MIN_SHARE of the common days (>= 5)."""
    out: Dict[str, List[str]] = {}
    pks = sorted(irr_daily)
    for i, a in enumerate(pks):
        for b in pks[i + 1:]:
            common = set(irr_daily[a]) & set(irr_daily[b])
            if len(common) < 5:
                continue
            same = sum(1 for d in common if abs(irr_daily[a][d] - irr_daily[b][d]) < 1e-6)
            if same >= SHARED_MIN_SHARE * len(common):
                out.setdefault(a, []).append(b)
                out.setdefault(b, []).append(a)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def coupling_window(daily: Mapping[str, Tuple[Optional[float], int, Optional[float]]], day: str
                    ) -> Tuple[Optional[float], int]:
    """(median slope, judged days) over the COUPLE_WINDOW days up to ``day``. ``daily``:
    {day: (slope %/K, samples, module-temperature span K)} of one station."""
    import datetime as _dt
    d1 = _dt.date.fromisoformat(day)
    keep = [v[0] for d, v in daily.items()
            if v[0] is not None and v[1] >= COUPLE_MIN_N and (v[2] or 0) >= COUPLE_SPAN_K
            and 0 <= (d1 - _dt.date.fromisoformat(d)).days < COUPLE_WINDOW]
    return (statistics.median(keep) if keep else None), len(keep)


def judge(plant_key: str, day: str, coverage: Optional[float], slope: Optional[float], slope_days: int,
          sat_status: Optional[str], dt_mod_amb: Optional[float],
          dt_drift: Optional[float], shared: Sequence[str] = ()) -> DayCheck:
    """One station-day. ``slope``: the median coupling slope of the window, over ``slope_days``."""
    reasons = []
    if coverage is None or coverage <= 0:
        return DayCheck(plant_key, day, coverage, slope, sat_status or "", dt_mod_amb, dt_drift,
                        tuple(shared), "no_data", ("no station data",))
    if coverage < COV_MIN:
        reasons.append(f"coverage {coverage * 100:.1f}% < {COV_MIN * 100:.0f}%")
    if slope is not None and slope_days >= COUPLE_MIN_DAYS and slope > COUPLE_MAX_SLOPE:
        reasons.append(f"module sensor not coupled (voltage {slope:+.2f} %/K, {slope_days}-day median)")
    if dt_drift is not None and dt_drift > DRIFT_MAX_K:
        reasons.append(f"module-ambient +{dt_drift:.1f} K vs its own start and the fleet")
    return DayCheck(plant_key, day, coverage, slope, sat_status or "", dt_mod_amb, dt_drift, tuple(shared),
                    "fail" if reasons else "ok", tuple(reasons))


TABLE_SQL = ("CREATE TABLE IF NOT EXISTS station_check_daily (plant_key text NOT NULL, prod_date date NOT NULL,"
             " coverage real, coupling_slope real, sat_status text, dt_mod_amb real, dt_drift real,"
             " shared_with text, status text NOT NULL, reasons text, PRIMARY KEY (plant_key, prod_date));")


def _n(v, fmt="{:.4f}"):
    return "NULL" if v is None else fmt.format(v)


def upsert_sql(checks: Iterable[DayCheck]) -> str:
    rows = []
    for c in checks:
        txt = lambda s: "'" + str(s).replace("'", "''") + "'"
        rows.append(f"({txt(c.plant_key)}, DATE '{c.day}', {_n(c.coverage)}, {_n(c.coupling_slope)},"
                    f" {txt(c.sat_status)}, {_n(c.dt_mod_amb, '{:.2f}')}, {_n(c.dt_drift, '{:.2f}')},"
                    f" {txt(','.join(c.shared_with))}, {txt(c.status)}, {txt('; '.join(c.reasons))})")
    if not rows:
        return ""
    return (TABLE_SQL + "\nINSERT INTO station_check_daily (plant_key, prod_date, coverage, coupling_slope,"
            " sat_status, dt_mod_amb, dt_drift, shared_with, status, reasons) VALUES\n" + ",\n".join(rows)
            + "\nON CONFLICT (plant_key, prod_date) DO UPDATE SET coverage = EXCLUDED.coverage,"
            " coupling_slope = EXCLUDED.coupling_slope, sat_status = EXCLUDED.sat_status,"
            " dt_mod_amb = EXCLUDED.dt_mod_amb, dt_drift = EXCLUDED.dt_drift, shared_with = EXCLUDED.shared_with,"
            " status = EXCLUDED.status, reasons = EXCLUDED.reasons;")
