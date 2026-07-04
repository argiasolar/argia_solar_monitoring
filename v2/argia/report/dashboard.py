"""
Argia_Mont_v2 dashboard builder.

Produces two LONG-format fact tables that feed the Looker Studio report:

  Dashboard_Inverter  grain: plant x inverter x time-bucket
  Dashboard_Plant     grain: plant x time-bucket

Long format means inverter is a *dimension*, not a column, so a plant growing
from 4 to 6 inverters needs no schema change.

Design notes
------------
* Theoretical energy uses the SAME formula as KPI_Daily.expected_kwh, applied
  per bucket instead of per day:

      theoretical_kwh = kwp_dc * irradiance_kwh_m2 * expected_factor

  All three inputs come from the Plants config. Because the formula is linear
  in irradiance, the buckets sum back to the KPI_Daily daily expected_kwh.

* Bucket energy uses cumulative-counter differencing on etoday_kwh (the same
  MAX(within) - MAX(before) pattern the v1 dashboard used), which is robust to
  sparse / bursty polling and extends unchanged to 5-min buckets.

* Inverter status is a priority state machine derived from real telemetry, not
  the vendor's decorative flag. This fixes the "ONLINE while producing 0 kWh"
  bug (a dark inverter must not read ONLINE).

The core is pure functions over lists of dicts so it is trivially testable and
client-agnostic. The Google-Sheets read/write lives behind a thin adapter.
"""

from __future__ import annotations

import datetime as dt
import statistics

from argia.kpi.energy import find_carryover_cut
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# --- configuration ---------------------------------------------------------

BUCKET_MINUTES = 60          # 60 now; drop to 30/15/5 when telemetry densifies
DAY_START_H = 6              # first bucket of the daylight window (local)
DAY_END_H = 20               # exclusive
DAYLIGHT_WM2 = 50.0          # avg irradiance above this => sun is up

# Status classification is DELEGATED to the shared module — one state
# machine for every consumer (see argia/analytics/status.py). Vocabulary
# constants are re-exported here so existing imports keep working.
from argia.analytics.status import (  # noqa: E402
    DERATED,
    FAULT,
    IDLE_NIGHT,
    NO_DATA,
    OFFLINE,
    ONLINE,
    UNDERPERFORMING,
    ZERO_KWH,
    InverterBucket,
    classify_plant_bucket,
)


# --- small helpers ---------------------------------------------------------

def _num(v) -> float | None:
    """Coerce a cell to float, treating blanks/garbage as None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def bucket_start(ts: dt.datetime, minutes: int = BUCKET_MINUTES) -> dt.datetime:
    """Floor a local timestamp to the start of its bucket."""
    total = ts.hour * 60 + ts.minute
    floored = (total // minutes) * minutes
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def daylight_buckets(day: dt.date, minutes: int = BUCKET_MINUTES) -> list[dt.datetime]:
    """The fixed list of bucket-start timestamps for one day's daylight window."""
    out = []
    t = dt.datetime.combine(day, dt.time(DAY_START_H, 0))
    end = dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(hours=DAY_END_H)
    step = dt.timedelta(minutes=minutes)
    while t < end:
        out.append(t)
        t += step
    return out


# --- input records ---------------------------------------------------------

@dataclass
class Plant:
    plant_key: str
    customer: str
    kwp_dc: float
    expected_factor: float

    def theoretical(self, irr_kwh_m2: float) -> float:
        """KPI_Daily expected-energy formula, applied to any irradiance window."""
        return self.kwp_dc * (irr_kwh_m2 or 0.0) * self.expected_factor


@dataclass
class Sample:
    """One telemetry poll for one inverter (from Telemetry_Argia)."""
    ts: dt.datetime
    plant_key: str
    inverter_sn: str
    inverter_label: str
    status: float | None
    fault_code: str | None      # normalized summary, e.g. "0" or "FT=302"
    power_w: float | None
    etoday_kwh: float | None
    temperature_c: float | None
    irradiance_wm2: float | None
    irradiance_kwh_m2_5m: float | None
    cloud_cover_pct: float | None
    ambient_temp_c: float | None
    module_temp_c: float | None
    derating_mode: float | None = None   # absent in the unified feed


# --- bucketing -------------------------------------------------------------

def bucket_energy(samples: Sequence[Sample], b_start: dt.datetime,
                  b_end: dt.datetime, day_start: dt.datetime) -> tuple[float, bool]:
    """Energy produced within [b_start, b_end) via cumulative-counter diff.

    Returns (energy_kwh, reported_in_bucket).

    energy = MAX(etoday within bucket) - MAX(etoday earlier same day), clamped
    at >= 0. A bucket with no sample yields 0 and its energy naturally rolls
    into the next bucket that advances the counter.
    """
    within = [s.etoday_kwh for s in samples
              if b_start <= s.ts < b_end and s.etoday_kwh is not None]
    if not within:
        return 0.0, False
    before = [s.etoday_kwh for s in samples
              if day_start <= s.ts < b_start and s.etoday_kwh is not None]
    e_within = max(within)
    e_before = max(before) if before else 0.0
    return max(0.0, e_within - e_before), True


def last_in_bucket(samples: Sequence[Sample], b_start: dt.datetime,
                   b_end: dt.datetime) -> Sample | None:
    inb = [s for s in samples if b_start <= s.ts < b_end]
    return max(inb, key=lambda s: s.ts) if inb else None


# --- build -----------------------------------------------------------------

@dataclass
class BuildResult:
    inverter_rows: list[dict] = field(default_factory=list)
    plant_rows: list[dict] = field(default_factory=list)


def build(day: dt.date, plants: dict[str, Plant], samples: Iterable[Sample],
          inverter_labels: dict[tuple[str, str], str] | None = None,
          active_inverters: dict[str, set[str]] | None = None,
          daily_expected: dict[str, float] | None = None,
          inverter_ratings: dict[tuple[str, str], float] | None = None,
          minutes: int = BUCKET_MINUTES) -> BuildResult:
    """Build both fact tables for a single day.

    active_inverters: plant_key -> set of inverter_sn expected to be live today
    (from the Inverters config). Inverters seen in telemetry are unioned in, so
    a newly-added inverter appears even before the config is updated.

    inverter_ratings: (plant_key, inverter_sn) -> rated_kw. When every
    inverter of a plant has a rating, peer comparison is per-kW so a smaller
    unit is not falsely flagged (GTO1 MWKNE9500D: 60 kW among 124 kW peers).

    daily_expected: plant_key -> KPI_Daily.expected_kwh for `day`. When given,
    the per-bucket theoretical is that daily value distributed across buckets by
    irradiance share, so the buckets sum EXACTLY to KPI_Daily (single source of
    truth) and intraday irradiance only sets the curve shape. When absent (e.g.
    the live current day, before EOD), it falls back to the kwp*irr*ef formula.
    """
    inverter_labels = inverter_labels or {}
    active_inverters = active_inverters or {}
    daily_expected = daily_expected or {}
    inverter_ratings = inverter_ratings or {}
    samples = [s for s in samples if s.ts.date() == day]

    # index samples by (plant, inverter_sn)
    by_inv: dict[tuple[str, str], list[Sample]] = {}
    seen_inv: dict[str, set[str]] = {}
    for s in samples:
        by_inv.setdefault((s.plant_key, s.inverter_sn), []).append(s)
        seen_inv.setdefault(s.plant_key, set()).add(s.inverter_sn)
        inverter_labels.setdefault((s.plant_key, s.inverter_sn), s.inverter_label)
    for key in list(by_inv):
        lst = sorted(by_inv[key], key=lambda s: s.ts)
        # Strip leading midnight-carryover rows (same rule as kpi_eod, via the
        # shared helper): a stale etoday from YESTERDAY would otherwise become
        # the cumulative-diff baseline and zero this inverter's whole day.
        cut = find_carryover_cut([s.etoday_kwh for s in lst],
                                 [s.ts.hour for s in lst])
        by_inv[key] = lst[cut:]

    res = BuildResult()
    buckets = daylight_buckets(day, minutes)
    step = dt.timedelta(minutes=minutes)
    day_start = dt.datetime.combine(day, dt.time(0, 0))

    for pk, plant in plants.items():
        inv_sns = set(active_inverters.get(pk, set())) | seen_inv.get(pk, set())
        if not inv_sns:
            continue
        plant_samples = [s for s in samples if s.plant_key == pk]

        # per-bucket irradiance, and the day total used to distribute the
        # KPI-anchored expected energy by shape.
        irr_by_bucket = {}
        for b in buckets:
            irr_by_bucket[b] = sum(
                (s.irradiance_kwh_m2_5m or 0.0)
                for s in plant_samples if b <= s.ts < b + step)
        irr_day_total = sum(irr_by_bucket.values())
        anchor = daily_expected.get(pk)

        for b_start in buckets:
            b_end = b_start + step
            hour_label = b_start.strftime("%H:%M")

            # plant-level irradiance / weather for this bucket
            in_bucket = [s for s in plant_samples if b_start <= s.ts < b_end]
            irr_bucket = irr_by_bucket[b_start]
            irr_wm2 = _avg([s.irradiance_wm2 for s in in_bucket])
            cloud = _avg([s.cloud_cover_pct for s in in_bucket])
            mod_temp = _max([s.module_temp_c for s in in_bucket])
            amb_temp = _avg([s.ambient_temp_c for s in in_bucket])
            sun_up = (irr_wm2 or 0.0) > DAYLIGHT_WM2

            # first pass: per-inverter energy for this bucket
            energies: dict[str, tuple[float, bool, Sample | None]] = {}
            for sn in inv_sns:
                sl = by_inv.get((pk, sn), [])
                e, reported = bucket_energy(sl, b_start, b_end, day_start)
                energies[sn] = (e, reported, last_in_bucket(sl, b_start, b_end))
            producing = [e for (e, rep, _) in energies.values() if rep and e > ZERO_KWH]
            peer_median = statistics.median(producing) if producing else None

            # shared status classification — the ONLY status authority
            buckets_in = [
                InverterBucket(
                    inverter_sn=sn,
                    energy_kwh=energies[sn][0],
                    reported=energies[sn][1],
                    status_flag=(energies[sn][2].status if energies[sn][2] else None),
                    fault_code=(energies[sn][2].fault_code if energies[sn][2] else None),
                    derating_mode=(energies[sn][2].derating_mode if energies[sn][2] else None),
                    rated_kw=inverter_ratings.get((pk, sn)),
                )
                for sn in sorted(inv_sns)
            ]
            statuses = classify_plant_bucket(buckets_in, plant_key=pk,
                                             sun_up=sun_up)

            plant_total = 0.0
            reporting = 0
            faulted = 0
            n_active = len(inv_sns)
            if anchor is not None and irr_day_total > 0:
                # distribute KPI daily expected_kwh by irradiance share
                theoretical = anchor * (irr_bucket / irr_day_total)
            else:
                # live current day (no KPI row yet): fall back to the formula
                theoretical = plant.theoretical(irr_bucket)
            expected_share = theoretical / n_active if n_active else 0.0

            for sn in sorted(inv_sns):
                e, reported, last = energies[sn]
                plant_total += e
                reporting += 1 if reported else 0
                temp = last.temperature_c if last else None
                power = last.power_w if last else None
                status, reason = statuses[sn]
                if status == FAULT:
                    faulted += 1
                res.inverter_rows.append({
                    "date_mx": day.isoformat(),
                    "bucket_ts": b_start,
                    "hour_label": hour_label,
                    "plant_key": pk,
                    "customer": plant.customer,
                    "inverter_sn": sn,
                    "inverter_label": inverter_labels.get((pk, sn), sn),
                    "energy_kwh": round(e, 3),
                    "power_w": round(power, 1) if power is not None else None,
                    "temperature_c": round(temp, 1) if temp is not None else None,
                    "status": status,
                    "status_reason": reason,
                    "peer_median_kwh": round(peer_median, 3) if peer_median else None,
                    "expected_share_kwh": round(expected_share, 3),
                    "production_pct": round(100 * e / expected_share, 1) if expected_share > 0 else None,
                })

            res.plant_rows.append({
                "date_mx": day.isoformat(),
                "bucket_ts": b_start,
                "hour_label": hour_label,
                "plant_key": pk,
                "customer": plant.customer,
                "kwp_dc": plant.kwp_dc,
                "total_kwh": round(plant_total, 3),
                "theoretical_kwh": round(theoretical, 3),
                "irradiance_kwh_m2": round(irr_bucket, 5),
                "irradiance_wm2": round(irr_wm2, 1) if irr_wm2 is not None else None,
                "cloud_cover_pct": round(cloud, 1) if cloud is not None else None,
                "module_temp_c": round(mod_temp, 1) if mod_temp is not None else None,
                "ambient_temp_c": round(amb_temp, 1) if amb_temp is not None else None,
                "inverters_total": n_active,
                "inverters_reporting": reporting,
                "inverters_faulted": faulted,
                "production_pct": round(100 * plant_total / theoretical, 1) if theoretical > 0 else None,
            })

    return res


def _avg(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _max(vals):
    v = [x for x in vals if x is not None]
    return max(v) if v else None


# --- ordered column lists (stable output for Looker) -----------------------

INVERTER_COLUMNS = [
    "date_mx", "bucket_ts", "hour_label", "plant_key", "customer",
    "inverter_sn", "inverter_label", "energy_kwh", "power_w", "temperature_c",
    "status", "status_reason", "peer_median_kwh", "expected_share_kwh",
    "production_pct",
]
PLANT_COLUMNS = [
    "date_mx", "bucket_ts", "hour_label", "plant_key", "customer", "kwp_dc",
    "total_kwh", "theoretical_kwh", "irradiance_kwh_m2", "irradiance_wm2",
    "cloud_cover_pct", "module_temp_c", "ambient_temp_c", "inverters_total",
    "inverters_reporting", "inverters_faulted", "production_pct",
]


# --- parsing seam (raw sheet rows -> typed records) ------------------------
# A SheetsClient returns each tab as a list of dicts {header: value}. These
# helpers turn those into the typed records build() consumes. The xlsx demo
# runner and the live Google-Sheets client share this seam.

def parse_plants(rows: list[dict]) -> dict[str, "Plant"]:
    out = {}
    for r in rows:
        pk = r.get("plant_key")
        if not pk:
            continue
        kwp = _num(r.get("kwp_dc_override")) or _num(r.get("kwp_dc"))
        ef = _num(r.get("expected_factor"))
        if kwp is None or ef is None:
            continue
        out[pk] = Plant(pk, r.get("customer") or pk, kwp, ef)
    return out


def parse_active_inverters(rows: list[dict]) -> dict[str, set]:
    out: dict[str, set] = {}
    for r in rows:
        pk, sn = r.get("plant_key"), r.get("inverter_sn")
        if not pk or not sn:
            continue
        active = r.get("in_service_today")
        if active in (None, ""):
            active = r.get("active")
        if str(active).strip().upper() in ("TRUE", "1", "YES") or active is True:
            out.setdefault(pk, set()).add(sn)
    return out


def parse_inverter_ratings(rows: list[dict]) -> dict[tuple, float]:
    """(plant_key, inverter_sn) -> rated_kw, for per-kW peer fairness."""
    out: dict[tuple, float] = {}
    for r in rows:
        pk, sn = r.get("plant_key"), r.get("inverter_sn")
        kw = _num(r.get("rated_kw"))
        if pk and sn and kw and kw > 0:
            out[(pk, sn)] = kw
    return out


def parse_samples(rows: list[dict]) -> list["Sample"]:
    out = []
    for r in rows:
        ts = r.get("timestamp_mx")
        if not isinstance(ts, dt.datetime) or not r.get("plant_key") or not r.get("inverter_sn"):
            continue
        out.append(Sample(
            ts=ts, plant_key=r["plant_key"], inverter_sn=r["inverter_sn"],
            inverter_label=r.get("inverter_label") or r["inverter_sn"],
            status=_num(r.get("status")), fault_code=r.get("fault_code"),
            power_w=_num(r.get("power_w")), etoday_kwh=_num(r.get("etoday_kwh")),
            temperature_c=_num(r.get("temperature_c")),
            irradiance_wm2=_num(r.get("irradiance_wm2")),
            irradiance_kwh_m2_5m=_num(r.get("irradiance_kwh_m2_5m")),
            cloud_cover_pct=_num(r.get("cloud_cover_pct")),
            ambient_temp_c=_num(r.get("ambient_temp_c")),
            module_temp_c=_num(r.get("module_temp_c")),
            derating_mode=_num(r.get("derating_mode")),
        ))
    return out


def to_matrix(columns: list[str], dicts: list[dict]) -> list[list]:
    """Header row + data rows, ready to hand to a sheet writer."""
    return [list(columns)] + [[d.get(c) for c in columns] for d in dicts]
