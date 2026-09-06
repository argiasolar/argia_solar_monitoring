"""Inverter thermal health (v216) — from "65 °C = critical" to a measured
diagnosis: how hot, hotter than whom, and what it cost.

Tomasz 2026-09-06: the monitoring flags Growatt inverters above 65 °C
as critical, "but there are no data supporting this". The manufacturer
documentation supports thermal management (Growatt MAX: power derating
above 45 °C ambient, Warning 407 / Error 408 over-temperature, warranty
exclusions for insufficient ventilation) but never says "65 °C internal
= X % lost". So ARGIA measures it on its own fleet, every 5 minutes:

* **Bands** — ARGIA operational thresholds on the inverter's internal
  temperature (not manufacturer warranty limits): normal < 50, watch
  50–60, warning 60–65, high 65–70, critical ≥ 70 (immediate ≥ 75).
* **Thermal stress index** ΔT_ambient = internal − ambient (the weather
  feed rides on every telemetry row): the same 66 °C means a healthy
  inverter at 43 °C ambient and a suspicious one at 32 °C.
* **Peer deviation** ΔT_peer = internal − median of the plant's other
  inverters: the strongest single signal of a cooling problem (dirty
  heat sink, blocked fan, bad clearance) because ambient, irradiance
  and loading are shared.
* **Suspected thermal derating** per 5-minute interval: the inverter is
  ≥ T_HOT, hotter than its cooler peers by ≥ DT_PEER_MIN, and produces
  below the cooler peers' specific power (kW per rated kW, corrected by
  its own cool-weather baseline so a smaller DC field is not "loss")
  by more than LOSS_MIN_PCT. Loss = expected − actual over the interval;
  summed per day into kWh, valued at the PPA tariff elsewhere.
* **Derating curve** — every interval with a peer reference feeds a
  temperature-binned ratio actual/expected; the knee is the first bin
  from which the median ratio stays below 1 − LOSS_MIN_PCT. Measured,
  per inverter, from ARGIA's own data.

Pure functions over sample tuples; the nightly job (scripts/
thermal_daily.py) feeds them and stores thermal_daily / thermal_bins.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ARGIA operational bands (internal inverter temperature, °C)
BANDS: List[Tuple[float, str]] = [(50.0, "normal"), (60.0, "watch"), (65.0, "warning"),
                                  (70.0, "high"), (75.0, "critical")]
T_HOT = 65.0           # derating is only suspected from here
T_CRIT = 70.0          # critical band starts
T_IMMEDIATE = 75.0     # immediate inspection
DT_PEER_MIN = 5.0      # hotter than the cooler peers by at least this
DT_PEER_POOR = 10.0    # cooling health POOR from this peer deviation
LOSS_MIN_PCT = 3.0     # a ratio below 1 - 3 % counts as derating
REF_MIN_SP = 0.20      # peers must run >= 20 % of rated (no dawn/dusk noise)
INTERVAL_MIN = 5       # telemetry cadence
BIN_C = 2.5
BIN_MIN, BIN_MAX = 40.0, 90.0
KNEE_MIN_N = 20        # samples a bin needs before it can be the knee

# (ts_utc, inverter_sn, power_w, temperature_c, ambient_c)
Sample = Tuple[dt.datetime, str, Optional[float], Optional[float], Optional[float]]


def band(temp_c: Optional[float]) -> str:
    if temp_c is None:
        return "unknown"
    for limit, name in BANDS:
        if temp_c < limit:
            return name
    return "critical"


def _bucket(ts: dt.datetime) -> dt.datetime:
    return ts.replace(minute=(ts.minute // INTERVAL_MIN) * INTERVAL_MIN, second=0, microsecond=0)


def _bin(temp_c: float) -> float:
    t = min(max(temp_c, BIN_MIN), BIN_MAX - BIN_C)
    return BIN_MIN + int((t - BIN_MIN) / BIN_C) * BIN_C


@dataclass
class IntervalEval:
    ts: dt.datetime
    sn: str
    temp_c: float
    power_kw: float
    sp: float                       # specific power actual (kW/kW rated)
    expected_kw: Optional[float]    # from cooler peers, None without a reference
    ratio: Optional[float]          # actual / expected
    dt_peer_c: Optional[float]      # temp - median(peers)
    dt_ambient_c: Optional[float]
    derating: bool = False
    lost_kw: float = 0.0


@dataclass
class InverterDay:
    sn: str
    samples: int = 0
    peak_c: Optional[float] = None
    mean_c: Optional[float] = None
    minutes_over_65: int = 0
    minutes_over_70: int = 0
    events: int = 0                    # contiguous runs >= T_HOT
    dt_peer_peak_c: Optional[float] = None
    dt_ambient_peak_c: Optional[float] = None
    derating_minutes: int = 0
    lost_kwh: float = 0.0
    energy_kwh: float = 0.0            # actual energy over evaluated intervals
    cool_ratio: Optional[float] = None  # median ratio while < T_HOT-5 (the DC-size baseline)
    band: str = "unknown"
    cooling_health: str = "n/a"        # GOOD / WATCH / POOR / n/a (no peers)
    bins: Dict[float, Tuple[int, float, float]] = field(default_factory=dict)  # bin -> (n, ratio_sum, ratio_min)


def _median(vals: Sequence[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def evaluate_intervals(samples: Iterable[Sample], rated_kw: Dict[str, float],
                       baseline: Optional[Dict[str, float]] = None) -> List[IntervalEval]:
    """One IntervalEval per (5-min bucket, inverter) with a temperature
    and power. The reference for inverter i is the median specific
    power of the OTHER inverters that are cooler by >= DT_PEER_MIN and
    running >= REF_MIN_SP; ``baseline`` (sn -> its usual ratio to the
    peers when cool, default 1) corrects for unequal DC fields."""
    baseline = baseline or {}
    by_ts: Dict[dt.datetime, Dict[str, Tuple[float, float, Optional[float]]]] = {}
    for ts, sn, p_w, t_c, amb in samples:
        if ts is None or t_c is None or p_w is None:
            continue
        sn = str(sn).strip()
        if rated_kw.get(sn, 0) <= 0:
            continue
        b = _bucket(ts)
        cur = by_ts.setdefault(b, {})
        if sn not in cur or ts > cur[sn][3]:          # newest sample in the bucket
            cur[sn] = (float(p_w) / 1000.0, float(t_c), amb, ts)  # type: ignore[assignment]
    out: List[IntervalEval] = []
    for b in sorted(by_ts):
        rows = by_ts[b]
        for sn, (kw, t_c, amb, _ts) in rows.items():
            sp = kw / rated_kw[sn]
            peers = [(o, v) for o, v in rows.items() if o != sn]
            peer_t = _median([v[1] for _, v in peers])
            dt_peer = (t_c - peer_t) if peer_t is not None else None
            cooler = [v[0] / rated_kw[o] for o, v in peers
                      if v[1] <= t_c - DT_PEER_MIN and v[0] / rated_kw[o] >= REF_MIN_SP]
            ref_sp = _median(cooler)
            expected = ratio = None
            if ref_sp is not None:
                expected = ref_sp * rated_kw[sn] * baseline.get(sn, 1.0)
                ratio = kw / expected if expected > 0 else None
            ev = IntervalEval(b, sn, t_c, kw, sp, expected, ratio, dt_peer,
                              (t_c - float(amb)) if amb is not None else None)
            if (ratio is not None and t_c >= T_HOT and dt_peer is not None
                    and dt_peer >= DT_PEER_MIN and ratio < 1 - LOSS_MIN_PCT / 100.0):
                ev.derating = True
                ev.lost_kw = max(0.0, expected - kw)     # type: ignore[operator]
            out.append(ev)
    return out


def summarise_day(evals: Sequence[IntervalEval], rated_kw: Dict[str, float]) -> Dict[str, InverterDay]:
    """Per-inverter day summary from the interval evaluations."""
    days: Dict[str, InverterDay] = {sn: InverterDay(sn=sn) for sn in rated_kw}
    by_sn: Dict[str, List[IntervalEval]] = {}
    for e in evals:
        by_sn.setdefault(e.sn, []).append(e)
    h = INTERVAL_MIN / 60.0
    for sn, lst in by_sn.items():
        d = days.setdefault(sn, InverterDay(sn=sn))
        lst.sort(key=lambda e: e.ts)
        temps = [e.temp_c for e in lst]
        d.samples = len(lst)
        d.peak_c = round(max(temps), 1)
        d.mean_c = round(sum(temps) / len(temps), 1)
        d.minutes_over_65 = sum(INTERVAL_MIN for e in lst if e.temp_c >= T_HOT)
        d.minutes_over_70 = sum(INTERVAL_MIN for e in lst if e.temp_c >= T_CRIT)
        d.energy_kwh = round(sum(e.power_kw * h for e in lst), 1)
        dps = [e.dt_peer_c for e in lst if e.dt_peer_c is not None]
        d.dt_peer_peak_c = round(max(dps), 1) if dps else None
        das = [e.dt_ambient_c for e in lst if e.dt_ambient_c is not None]
        d.dt_ambient_peak_c = round(max(das), 1) if das else None
        d.derating_minutes = sum(INTERVAL_MIN for e in lst if e.derating)
        d.lost_kwh = round(sum(e.lost_kw * h for e in lst if e.derating), 2)
        # the unit's own standing against cooler peers while NOT hot (< 65 C):
        # a unit with more DC than its peers sits above 1 here, and the
        # nightly baseline (median of these over 30 days) divides it out
        cool = [e.ratio for e in lst if e.ratio is not None and e.temp_c < T_HOT]
        d.cool_ratio = round(_median(cool), 3) if cool else None
        # events: contiguous runs at/above T_HOT (one missing tick tolerated)
        events, prev_hot_ts = 0, None
        for e in lst:
            if e.temp_c >= T_HOT:
                if prev_hot_ts is None or (e.ts - prev_hot_ts) > dt.timedelta(minutes=2 * INTERVAL_MIN):
                    events += 1
                prev_hot_ts = e.ts
        d.events = events
        d.band = band(d.peak_c)
        if d.dt_peer_peak_c is None:
            d.cooling_health = "n/a"
        elif d.dt_peer_peak_c >= DT_PEER_POOR or (d.derating_minutes >= 30 and d.lost_kwh > 0):
            d.cooling_health = "POOR"
        elif d.dt_peer_peak_c >= DT_PEER_MIN:
            d.cooling_health = "WATCH"
        else:
            d.cooling_health = "GOOD"
        for e in lst:
            if e.ratio is None:
                continue
            k = _bin(e.temp_c)
            n, s, mn = d.bins.get(k, (0, 0.0, 9.9))
            d.bins[k] = (n + 1, s + e.ratio, min(mn, e.ratio))
    return days


def evaluate_day(samples: Iterable[Sample], rated_kw: Dict[str, float],
                 baseline: Optional[Dict[str, float]] = None) -> Dict[str, InverterDay]:
    return summarise_day(evaluate_intervals(samples, rated_kw, baseline), rated_kw)


# ----------------------------------------------------------------- curve
def derating_curve(bins: Dict[float, Tuple[int, float]]) -> Dict:
    """``bins`` = {bin_c: (n, ratio_sum)} merged over a period. Returns
    the curve (mean ratio per bin) and the knee: the first bin with
    >= KNEE_MIN_N samples whose mean ratio is below 1 - LOSS_MIN_PCT and
    whose following populated bins stay below it too."""
    pts = [{"bin_c": b, "n": n, "ratio": round(s / n, 3)} for b, (n, s) in sorted(bins.items()) if n > 0]
    # the curve's own cool level (bins below T_HOT with enough samples):
    # a knee is a drop from THAT, not from 1.0 — a unit carrying more DC
    # than its peers runs above 1 when cool and still derates
    cool = [p for p in pts if p["bin_c"] < T_HOT and p["n"] >= KNEE_MIN_N // 2]
    n_cool = sum(p["n"] for p in cool)
    cool_level = round(sum(p["ratio"] * p["n"] for p in cool) / n_cool, 3) if n_cool else 1.0
    limit = cool_level * (1 - LOSS_MIN_PCT / 100.0)
    knee = None
    for i, p in enumerate(pts):
        if p["n"] < KNEE_MIN_N or p["ratio"] >= limit:
            continue
        later = [q for q in pts[i + 1:] if q["n"] >= KNEE_MIN_N // 2]
        if all(q["ratio"] < limit for q in later):
            knee = p["bin_c"]
            break
    hot = [p for p in pts if p["bin_c"] >= T_HOT and p["n"] >= KNEE_MIN_N // 2]
    n_hot = sum(p["n"] for p in hot)
    ratio_hot = round(sum(p["ratio"] * p["n"] for p in hot) / n_hot, 3) if n_hot else None
    return {"points": pts, "cool_level": cool_level, "knee_c": knee, "ratio_above_65": ratio_hot,
            "loss_above_65_pct": round((1 - ratio_hot / cool_level) * 100, 1) if ratio_hot is not None else None}


def merge_bins(rows: Iterable[Tuple[float, int, float]]) -> Dict[float, Tuple[int, float]]:
    out: Dict[float, Tuple[int, float]] = {}
    for b, n, s in rows:
        pn, ps = out.get(float(b), (0, 0.0))
        out[float(b)] = (pn + int(n), ps + float(s))
    return out


def baseline_from_history(rows: Iterable[Tuple[str, Optional[float]]]) -> Dict[str, float]:
    """sn -> median of the stored daily cool ratios (the inverter's usual
    standing against its peers when nobody is hot), clamped to a sane
    range so one odd day cannot hide a real loss."""
    acc: Dict[str, List[float]] = {}
    for sn, r in rows:
        if r is not None:
            acc.setdefault(str(sn), []).append(float(r))
    return {sn: min(1.3, max(0.5, statistics.median(v))) for sn, v in acc.items() if v}


# ------------------------------------------------------------------ SQL
def _txt(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


ENSURE_SQL = """CREATE TABLE IF NOT EXISTS thermal_daily (
    plant_key        text NOT NULL,
    inverter_sn      text NOT NULL,
    prod_date        date NOT NULL,
    samples          int,
    peak_c           numeric(5,1),
    mean_c           numeric(5,1),
    minutes_over_65  int,
    minutes_over_70  int,
    events           int,
    dt_peer_peak_c   numeric(5,1),
    dt_ambient_peak_c numeric(5,1),
    derating_minutes int,
    lost_kwh         numeric(10,2),
    energy_kwh       numeric(10,1),
    cool_ratio       numeric(6,3),
    band             text,
    cooling_health   text,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plant_key, inverter_sn, prod_date)
);
CREATE TABLE IF NOT EXISTS thermal_bins (
    plant_key   text NOT NULL,
    inverter_sn text NOT NULL,
    prod_date   date NOT NULL,
    bin_c       numeric(5,1) NOT NULL,
    n           int NOT NULL,
    ratio_sum   numeric(12,4) NOT NULL,
    ratio_min   numeric(6,3),
    PRIMARY KEY (plant_key, inverter_sn, prod_date, bin_c)
);"""


def _num(v) -> str:
    return "NULL" if v is None else repr(round(float(v), 3))


def build_upsert_sql(plant_key: str, date_iso: str, days: Dict[str, InverterDay]) -> List[str]:
    """thermal_daily + thermal_bins for one plant-day (the day's bins are
    replaced so a re-run never double counts)."""
    out: List[str] = []
    vals = []
    bins = []
    for sn, d in days.items():
        if d.samples == 0:
            continue
        vals.append(f"({_txt(plant_key)},{_txt(sn)},DATE '{date_iso}',{d.samples},{_num(d.peak_c)},{_num(d.mean_c)},"
                    f"{d.minutes_over_65},{d.minutes_over_70},{d.events},{_num(d.dt_peer_peak_c)},"
                    f"{_num(d.dt_ambient_peak_c)},{d.derating_minutes},{_num(d.lost_kwh)},{_num(d.energy_kwh)},"
                    f"{_num(d.cool_ratio)},{_txt(d.band)},{_txt(d.cooling_health)})")
        for b, (n, s, mn) in d.bins.items():
            bins.append(f"({_txt(plant_key)},{_txt(sn)},DATE '{date_iso}',{b},{n},{_num(s)},{_num(mn)})")
    if not vals:
        return out
    out.append("INSERT INTO thermal_daily (plant_key, inverter_sn, prod_date, samples, peak_c, mean_c,"
               " minutes_over_65, minutes_over_70, events, dt_peer_peak_c, dt_ambient_peak_c,"
               " derating_minutes, lost_kwh, energy_kwh, cool_ratio, band, cooling_health) VALUES\n"
               + ",\n".join(vals) +
               "\nON CONFLICT (plant_key, inverter_sn, prod_date) DO UPDATE SET samples=EXCLUDED.samples,"
               " peak_c=EXCLUDED.peak_c, mean_c=EXCLUDED.mean_c, minutes_over_65=EXCLUDED.minutes_over_65,"
               " minutes_over_70=EXCLUDED.minutes_over_70, events=EXCLUDED.events,"
               " dt_peer_peak_c=EXCLUDED.dt_peer_peak_c, dt_ambient_peak_c=EXCLUDED.dt_ambient_peak_c,"
               " derating_minutes=EXCLUDED.derating_minutes, lost_kwh=EXCLUDED.lost_kwh,"
               " energy_kwh=EXCLUDED.energy_kwh, cool_ratio=EXCLUDED.cool_ratio, band=EXCLUDED.band,"
               " cooling_health=EXCLUDED.cooling_health, computed_at=now();")
    out.append(f"DELETE FROM thermal_bins WHERE plant_key={_txt(plant_key)} AND prod_date=DATE '{date_iso}';")
    if bins:
        out.append("INSERT INTO thermal_bins (plant_key, inverter_sn, prod_date, bin_c, n, ratio_sum, ratio_min) VALUES\n"
                   + ",\n".join(bins) + ";")
    return out


def telemetry_sql(plant_key: str, date_iso: str) -> str:
    """The day's samples for one plant (MX date), newest telemetry columns."""
    return ("SELECT ts_utc::text, inverter_sn, power_w, temperature_c, ambient_temp_c FROM telemetry"
            f" WHERE plant_key = {_txt(plant_key)}"
            f" AND (ts_utc AT TIME ZONE 'America/Mexico_City')::date = DATE '{date_iso}'"
            " AND temperature_c IS NOT NULL AND power_w IS NOT NULL ORDER BY ts_utc /*tag:thermal_samples*/;")


def parse_ts(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s.replace(" ", "T").replace("+00", "+00:00"))
    except ValueError:
        return None
