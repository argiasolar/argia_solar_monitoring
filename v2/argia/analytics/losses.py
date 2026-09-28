"""v264 - what under-production costs, per plant and day, split by cause.

Tomasz, 2026-09-28, about SAG: "where can I see the financial impact of
under-production ... expected production, actual production, loss 1000 MXN
due to unavailability or due to overheating, or due to underperformance.
The expected production should be from weather and peers production and
100% availability."

Expected, two independent ways, both assuming a fully available plant:

* **weather**: ``daily_production.expected_kwh`` - nameplate x the day's
  measured irradiance x the plant's expected factor, stamped nightly;
* **peers**: what the plant's neighbours actually made that day, per kWp
  (median of the healthy peers within ``PEER_MAX_KM``), times this plant's
  kWp, times this plant's OWN usual ratio to those peers (the median over
  earlier days when both were healthy - a roof that always makes 3% more
  than its neighbour keeps that 3%). A peer counts on a day only if it made
  at least ``HEALTHY_FRAC`` of its own weather expectation: a peer that
  was itself down cannot set the bar.

The peers figure is used when it exists (it sees the same clouds, soiling
season and heat). Otherwise the weather model is scaled by the plant's own
usual actual/model ratio on healthy days ("weather"), or used raw when that
ratio is unknown or implausible ("weather-model"). Both inputs are stored.

Lost = expected - actual, never negative, then split in this order:

1. **unavailability** - the day's expected energy is spread over its
   daylight 5-minute slots by irradiance; slots where inverters reported
   0 W lose their share, and slots where the plant sent nothing are judged
   by the vendor's daily counter (see ``slot_causes``); a day with (almost)
   no output at all is unavailability as a whole;
2. **overheating** - ``thermal_daily.lost_kwh``, the derating the thermal
   job measured against cooler peers;
3. **underperformance** - the rest: the plant ran but made less than it
   should (soiling, strings, clipping, faults not visible as 0 W, and
   hours without telemetry).

Each cause is capped at what is left, so the three always add up to the
loss. Approved customer maintenance windows are ``excused`` - that energy
is billed as deemed energy, so it is not a loss to ARGIA.

Pesos: the PPA tariff for the month (``contract_monthly.tariff_mxn``,
else the plant's standing tariff). CAPEX plants have no per-kWh tariff:
their loss is shown in kWh, never as an invented peso figure.

Pure: no I/O, no database, no clock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PEER_MAX_KM = 60.0
HEALTHY_FRAC = 0.85          # a peer counts only if it made >= 85% of its own weather expectation
MIN_CALIB_DAYS = 5           # fewer shared healthy days -> no peer figure for this plant
CALIB_BOUNDS = (0.7, 1.3)    # a ratio outside this is a data problem, not a roof
DARK_FRAC = 0.05             # output below 5% of expected = the plant was down all day
MIN_IRRADIANCE_WM2 = 20.0
SLOT_MIN = 5.0


# ------------------------------------------------------------ geography
def km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def peer_groups(coords: Dict[str, Tuple[Optional[float], Optional[float]]],
                max_km: float = PEER_MAX_KM) -> Dict[str, List[str]]:
    """{plant: [peers within max_km]}; a plant without coordinates has none."""
    ok = {k: v for k, v in coords.items() if v[0] is not None and v[1] is not None}
    return {k: sorted(o for o in ok if o != k and km(ok[k], ok[o]) <= max_km) if k in ok else []
            for k in coords}


# ------------------------------------------------------------ expected
def healthy(actual: Optional[float], weather_expected: Optional[float]) -> bool:
    return bool(actual and weather_expected and weather_expected > 0
                and actual >= HEALTHY_FRAC * weather_expected)


def calibration(pairs: Iterable[Tuple[Optional[float], Optional[float]]]) -> Optional[float]:
    """Median of own/peer specific yield over days when both were healthy.
    ``pairs`` holds only such days. None when too few or implausible."""
    ratios = [o / p for o, p in pairs if o and p and o > 0 and p > 0]
    if len(ratios) < MIN_CALIB_DAYS:
        return None
    r = median(ratios)
    return r if CALIB_BOUNDS[0] <= r <= CALIB_BOUNDS[1] else None


def peer_expected(kwp: Optional[float], peer_yields: Sequence[float],
                  ratio: Optional[float]) -> Optional[float]:
    """kWh this plant should have made, judged by its healthy neighbours."""
    ys = [y for y in peer_yields if y and y > 0]
    if not kwp or not ys or ratio is None:
        return None
    return float(kwp) * median(ys) * ratio


def choose_expected(weather: Optional[float], peers: Optional[float],
                    weather_ratio: Optional[float] = None) -> Tuple[Optional[float], str]:
    """Peers first; else the weather model scaled by this plant's own usual
    actual/model ratio ('weather'); else the raw model ('weather-model',
    a plant whose ratio is implausible or unknown)."""
    if peers is not None:
        return peers, "peers"
    if weather is not None and weather > 0:
        if weather_ratio is not None:
            return float(weather) * weather_ratio, "weather"
        return float(weather), "weather-model"
    return None, "none"


# ------------------------------------------------------------ causes
@dataclass
class Slot:
    """One 5-minute slot of one plant, from telemetry."""
    irradiance: Optional[float]     # W/m2 measured at the plant (None: not reported)
    down: int                       # inverters that reported 0 W
    reporting: int                  # inverters that reported at all
    power_kw: float                 # sum of the reported inverter power
    excused: bool = False           # inside an approved customer maintenance window


def slot_causes(expected: Optional[float], actual: Optional[float], own: Dict[str, Slot],
                peer_irradiance: Dict[str, float], n_inverters: int,
                minutes: float = SLOT_MIN) -> Tuple[float, float]:
    """(unavailability kWh, excused kWh) for one plant-day.

    The day's expected energy is spread over the daylight slots in
    proportion to irradiance (the plant's own sensor, else the peers'
    median), so the pieces always add up to the daily expectation.

    * a slot where inverters reported 0 W loses its share x the share of
      inverters down;
    * slots where the plant sent NOTHING are judged by the vendor's daily
      counter: whatever the counter made beyond the reported slots was made
      in the silent ones; if that is less than the silent slots should have
      made, the shortfall is unavailability. A datalogger that loses its
      connection while the inverters keep producing therefore costs nothing,
      and a plant that is really off during the gap is counted as off.

    Daylight = any slot where the plant or a peer measured >= 20 W/m2."""
    if not expected or expected <= 0 or actual is None:
        return 0.0, 0.0
    daylight: Dict[str, float] = {}
    for ts in set(own) | set(peer_irradiance):
        o = own.get(ts)
        irr = o.irradiance if (o and o.irradiance is not None) else peer_irradiance.get(ts)
        if irr is not None and irr >= MIN_IRRADIANCE_WM2:
            daylight[ts] = float(irr)
    total_irr = sum(daylight.values())
    if total_irr <= 0:
        return 0.0, 0.0
    share = {ts: float(expected) * irr / total_irr for ts, irr in daylight.items()}
    zero = silent_expected = reported_kwh = excused = 0.0
    for ts, e in share.items():
        o = own.get(ts)
        if o and o.excused:
            excused += e
            continue
        if o and o.reporting > 0:
            reported_kwh += max(o.power_kw, 0.0) * minutes / 60.0
            if n_inverters and o.down:
                zero += e * min(o.down, n_inverters) / n_inverters
        else:
            silent_expected += e
    silent_made = max(0.0, float(actual) - reported_kwh)
    return zero + max(0.0, silent_expected - silent_made), excused


@dataclass
class Split:
    lost: float
    unavailability: float
    overheating: float
    underperformance: float
    excused: float


def split_loss(expected: Optional[float], actual: Optional[float],
               unavailability: Optional[float] = 0.0, overheating: Optional[float] = 0.0,
               excused: Optional[float] = 0.0) -> Optional[Split]:
    """Lost kWh and its causes; the causes always add up to the loss."""
    if expected is None or actual is None:
        return None
    gap = max(0.0, float(expected) - float(actual))
    exc = min(max(float(excused or 0.0), 0.0), gap)
    lost = gap - exc
    if lost <= 0:
        return Split(0.0, 0.0, 0.0, 0.0, exc)
    if float(actual) < DARK_FRAC * float(expected):
        return Split(lost, lost, 0.0, 0.0, exc)          # down all day
    u = min(max(float(unavailability or 0.0), 0.0), lost)
    th = min(max(float(overheating or 0.0), 0.0), lost - u)
    return Split(lost, u, th, lost - u - th, exc)


def mxn(kwh: Optional[float], tariff: Optional[float]) -> Optional[float]:
    if kwh is None or not tariff:
        return None
    return round(float(kwh) * float(tariff), 2)


# ------------------------------------------------------------ one plant-day
@dataclass
class LossDay:
    plant_key: str
    prod_date: str
    kwp_dc: float
    expected_weather_kwh: Optional[float]
    expected_peers_kwh: Optional[float]
    expected_kwh: Optional[float]
    expected_basis: str
    peers: str
    actual_kwh: Optional[float]
    lost_kwh: Optional[float]
    unavailability_kwh: Optional[float]
    overheating_kwh: Optional[float]
    underperformance_kwh: Optional[float]
    excused_kwh: Optional[float]
    tariff_mxn: Optional[float]
    lost_mxn: Optional[float]


def compute_day(plant_key: str, day: str, kwp: float, actual: Optional[float],
                weather_expected: Optional[float], peer_yields: Dict[str, float],
                peer_ratio: Optional[float], weather_ratio: Optional[float],
                own_slots: Dict[str, Slot], peer_irradiance: Dict[str, float], n_inverters: int,
                overheating: float, tariff: Optional[float]) -> LossDay:
    """``peer_yields``: {peer: kWh/kWp that day} for the HEALTHY peers only."""
    pe = peer_expected(kwp, list(peer_yields.values()), peer_ratio)
    exp, basis = choose_expected(weather_expected, pe, weather_ratio)
    unav, excused = slot_causes(exp, actual, own_slots, peer_irradiance, n_inverters)
    s = split_loss(exp, actual, unav, overheating, excused)
    r1 = (lambda v: None if v is None else round(v, 1))
    return LossDay(
        plant_key, day, kwp, r1(weather_expected), r1(pe), r1(exp), basis,
        ",".join(sorted(peer_yields)) if basis == "peers" else "",
        r1(actual),
        r1(s.lost) if s else None, r1(s.unavailability) if s else None,
        r1(s.overheating) if s else None, r1(s.underperformance) if s else None,
        r1(s.excused) if s else None,
        tariff, mxn(s.lost, tariff) if s else None)


def totals(days: Iterable[LossDay]) -> Dict[str, float]:
    """Sums over days; pesos only over days that have a tariff."""
    out = {k: 0.0 for k in ("expected", "actual", "lost", "unavailability", "overheating",
                            "underperformance", "excused", "lost_mxn", "unavailability_mxn",
                            "overheating_mxn", "underperformance_mxn")}
    priced = False
    for d in days:
        out["expected"] += d.expected_kwh or 0.0
        out["actual"] += d.actual_kwh or 0.0
        out["lost"] += d.lost_kwh or 0.0
        out["unavailability"] += d.unavailability_kwh or 0.0
        out["overheating"] += d.overheating_kwh or 0.0
        out["underperformance"] += d.underperformance_kwh or 0.0
        out["excused"] += d.excused_kwh or 0.0
        if d.tariff_mxn:
            priced = True
            out["lost_mxn"] += mxn(d.lost_kwh or 0.0, d.tariff_mxn) or 0.0
            out["unavailability_mxn"] += mxn(d.unavailability_kwh or 0.0, d.tariff_mxn) or 0.0
            out["overheating_mxn"] += mxn(d.overheating_kwh or 0.0, d.tariff_mxn) or 0.0
            out["underperformance_mxn"] += mxn(d.underperformance_kwh or 0.0, d.tariff_mxn) or 0.0
    out["priced"] = 1.0 if priced else 0.0
    return out
