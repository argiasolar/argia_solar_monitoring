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

v276 (2026-09-30, Tomasz: "numbers too big ... usually we are losing
connections not the production"), checked against September's nightly
counters on pio06 - PPA losses fell from $36,362 to $11,262 MXN:

* **catch-up**: a vendor day counter that froze during a data gap is
  corrected by the next night's LIFETIME counter step (``catch_up`` /
  ``allocate_catch_up``): the energy it proves is added to the day before
  judging it. SAG 19, 25, 28 Sep: 5,520 kWh that had been booked as lost;
* **tolerance**: the ESTIMATED part of a gap (underperformance + silent
  slots) counts only from 10% of expected (2 x the 4.8% day-to-day scatter
  of 143 normal plant-days); 0 W and heat are measured and always count;
* every row stores how it was made (counter, catch-up, ratios, tolerance)
  and ``explain_day`` turns it into the text the pages show on hover / tap.

Pure: no I/O, no database, no clock.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from statistics import median
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

PEER_MAX_KM = 60.0
HEALTHY_FRAC = 0.85          # a peer counts only if it made >= 85% of its own weather expectation
MIN_CALIB_DAYS = 5           # fewer shared healthy days -> no peer figure for this plant
CALIB_BOUNDS = (0.7, 1.3)    # a ratio outside this is a data problem, not a roof
DARK_FRAC = 0.05             # output below 5% of expected = the plant was down all day
# v276 (Tomasz, 2026-09-30: "numbers too big ... usually we are losing connections
# not the production"). Two corrections, both from the September data:
UNDERPERF_TOL = 0.10         # an ESTIMATED shortfall (underperformance, or silent slots
                             # judged by the counter) below 10% of expected is the scatter
                             # of the estimate itself, not a loss. Measured on pio06: 143
                             # normal PPA plant-days of Sep 2026 scatter around their
                             # expectation with a 4.8% standard deviation (p10 0.97,
                             # p90 1.07); 10% = 2 sigma. Counting every low day but never
                             # the high ones had booked 1-2% of every plant's month as
                             # 'lost'. Inverters at 0 W and heat derating are MEASURED
                             # and always count.
CATCHUP_MIN_KWH = 20.0       # next-night lifetime step beyond the day counter: smaller
                             # differences are counter rounding, not a data gap
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


def slot_causes_detail(expected: Optional[float], actual: Optional[float], own: Dict[str, Slot],
                       peer_irradiance: Dict[str, float], n_inverters: int,
                       minutes: float = SLOT_MIN) -> Tuple[float, float, float]:
    """(0 W kWh - measured, silent-slot kWh - estimated from the counter,
    excused kWh). The two parts of slot_causes' unavailability, kept apart
    so only the estimated one is subject to UNDERPERF_TOL (v276)."""
    total, excused = slot_causes(expected, actual, own, peer_irradiance, n_inverters, minutes)
    if total <= 0:
        return 0.0, 0.0, excused
    zero_only = min(_zero_w_part(expected, own, peer_irradiance, n_inverters), total)
    return zero_only, total - zero_only, excused


def _zero_w_part(expected, own, peer_irradiance, n_inverters) -> float:
    if not expected or expected <= 0:
        return 0.0
    daylight: Dict[str, float] = {}
    for ts in set(own) | set(peer_irradiance):
        o = own.get(ts)
        irr = o.irradiance if (o and o.irradiance is not None) else peer_irradiance.get(ts)
        if irr is not None and irr >= MIN_IRRADIANCE_WM2:
            daylight[ts] = float(irr)
    total_irr = sum(daylight.values())
    if total_irr <= 0:
        return 0.0
    zero = 0.0
    for ts, irr in daylight.items():
        o = own.get(ts)
        if o and not o.excused and o.reporting > 0 and n_inverters and o.down:
            zero += float(expected) * irr / total_irr * min(o.down, n_inverters) / n_inverters
    return zero


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


def tolerate(split: Optional[Split], expected: Optional[float], silent: float = 0.0) -> Tuple[Optional[Split], float]:
    """(split, kWh ignored as normal variation). The ESTIMATED part of a day's
    gap - underperformance plus the unavailability of silent slots (``silent``,
    judged by the counter) - counts only when together it reaches UNDERPERF_TOL
    of the day's expectation; below that it is the scatter of the estimate.
    0 W slots and heat derating are measured and always kept."""
    if split is None or not expected:
        return split, 0.0
    sil = min(max(float(silent or 0.0), 0.0), split.unavailability)
    est = split.underperformance + sil
    if est <= 0 or est >= UNDERPERF_TOL * float(expected):
        return split, 0.0
    return Split(split.lost - est, split.unavailability - sil, split.overheating, 0.0, split.excused), est


def catch_up(counters: Sequence[Tuple[str, Optional[float], Optional[float]]]) -> Dict[str, float]:
    """{night: kWh the lifetime counter gained beyond that night's day counter}.

    ``counters``: [(YYYY-MM-DD, vendor day kWh, vendor lifetime kWh)] of one
    plant, nightly snapshots. Pure.

    When the vendor stops receiving data (datalogger / internet down) the DAY
    counter freezes; the inverters keep producing and, once the link is back,
    the LIFETIME counter jumps by everything made meanwhile. So on the night
    after a gap: lifetime step > day counter, and the excess is energy that
    belongs to the earlier day(s). SAG, September: 19, 25 and 28 Sep had the
    day counter frozen at 427 / 264 / 1,203 kWh; the next nights' lifetime
    steps carried +2,442 / +1,749 / +1,329 kWh. Only consecutive nights with
    both counters are compared."""
    rows = sorted((d, dk, lt) for d, dk, lt in counters)
    out: Dict[str, float] = {}
    for (d0, _k0, l0), (d1, k1, l1) in zip(rows, rows[1:]):
        if l0 is None or l1 is None or k1 is None:
            continue
        if (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days != 1:
            continue
        excess = (float(l1) - float(l0)) - float(k1)
        if excess >= CATCHUP_MIN_KWH:
            out[d1] = excess
    return out


def allocate_catch_up(excess: Dict[str, float], shortfall: Dict[str, float], back: int = 3) -> Dict[str, float]:
    """{day: kWh credited back}. Each night's excess goes to the day before it,
    then further back (up to ``back`` days) - each day credited at most its own
    shortfall (expected - counted). Energy that fits no shortfall is left
    uncredited: it can only make a day look better, never invent a loss. Pure."""
    left = dict(shortfall)
    out: Dict[str, float] = {}
    for night, kwh in sorted(excess.items()):
        n = dt.date.fromisoformat(night)
        for k in range(1, back + 1):
            if kwh <= 0:
                break
            d = (n - dt.timedelta(days=k)).isoformat()
            room = max(0.0, left.get(d, 0.0))
            give = min(room, kwh)
            if give > 0:
                out[d] = out.get(d, 0.0) + give
                left[d] = room - give
                kwh -= give
    return out


def month_budget(counters: Sequence[Tuple[str, Optional[float], Optional[float]]],
                 counted: Dict[str, Optional[float]]) -> Dict[str, float]:
    """{YYYY-MM: kWh the vendor LIFETIME counter holds for that month beyond
    the days already counted}. v305 (Tomasz, 2026-10-04: "we always have to
    reconcile against the portal counter for the total month production").

    The month's true production is the lifetime counter's step from the
    last night of the previous month to the month's last snapshot night
    (SAG, September: 71,438 kWh = the vendor portal's month counter = the
    invoice). ``counted``: {day: kWh already in daily_production} - often
    healed already from the vendor history, in which case nothing is left
    to credit. Without the previous month's last night the span starts at
    the month's first snapshot night (only the days after it count); a
    month with fewer than two lifetime readings is absent (no credit:
    production is never invented). Pure."""
    lt = {d: float(l) for d, _k, l in counters if l is not None}
    out: Dict[str, float] = {}
    for m in sorted({d[:7] for d in counted}):
        y, mo = (int(x) for x in m.split("-"))
        prev_end = (dt.date(y, mo, 1) - dt.timedelta(days=1)).isoformat()
        last_counted = max(d for d in counted if d[:7] == m)
        nights = sorted(d for d in lt if d[:7] == m and d <= last_counted)
        if not nights:
            continue
        last = nights[-1]
        # from the previous month's last night when we have it; otherwise the
        # month's first snapshot night (only the days after it are covered)
        start = prev_end if prev_end in lt else nights[0]
        if start >= last:
            continue
        done = sum(float(v) for d, v in counted.items() if d[:7] == m and start < d <= last and v is not None)
        out[m] = (lt[last] - lt[start]) - done
    return out


def cap_to_month(credit: Dict[str, float], budget: Dict[str, float]) -> Dict[str, float]:
    """``allocate_catch_up``'s credits, never beyond what the month's lifetime
    counter leaves uncounted (``month_budget``), earliest day first. Before
    v305 a day counter healed from the vendor history AND its lifetime catch-up
    were both counted: SAG September read 73,859 kWh against the portal's
    71,438. Pure."""
    left = {m: max(0.0, v) for m, v in budget.items()}
    out: Dict[str, float] = {}
    for d, kwh in sorted(credit.items()):
        room = left.get(d[:7], 0.0)
        give = min(room, kwh)
        if give > 0:
            out[d] = give
            left[d[:7]] = room - give
    return out


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
    # v276: how the numbers were made - every figure on the pages can say where it came from
    counter_kwh: Optional[float] = None        # the vendor's day counter as read that night
    catchup_kwh: Optional[float] = None        # energy the next night's lifetime counter proved (data gap)
    peer_ratio: Optional[float] = None         # this plant's usual kWh/kWp vs its peers (90 days)
    weather_ratio: Optional[float] = None      # this plant's usual actual / weather model (90 days)
    tolerance_kwh: Optional[float] = None      # shortfall within normal variation, not counted


def compute_day(plant_key: str, day: str, kwp: float, actual: Optional[float],
                weather_expected: Optional[float], peer_yields: Dict[str, float],
                peer_ratio: Optional[float], weather_ratio: Optional[float],
                own_slots: Dict[str, Slot], peer_irradiance: Dict[str, float], n_inverters: int,
                overheating: float, tariff: Optional[float], catchup: float = 0.0) -> LossDay:
    """``peer_yields``: {peer: kWh/kWp that day} for the HEALTHY peers only.
    ``actual`` is the day counter; ``catchup`` the energy a later night's
    lifetime counter proved was made that day (v276) - added before judging."""
    counter = actual
    if actual is not None and catchup:
        actual = float(actual) + float(catchup)
    pe = peer_expected(kwp, list(peer_yields.values()), peer_ratio)
    exp, basis = choose_expected(weather_expected, pe, weather_ratio)
    zero_w, silent, excused = slot_causes_detail(exp, actual, own_slots, peer_irradiance, n_inverters)
    s, tol = tolerate(split_loss(exp, actual, zero_w + silent, overheating, excused), exp, silent)
    if basis == "weather-model":
        # v277: the raw model with no plausible ratio for this plant (design data or
        # sensor wrong) is not a basis for money or kWh 'lost' - GTO2 and NL2 booked
        # ~21,000 kWh each in Sep 2026 against it. Shown, never counted.
        s, tol = None, None
    r1 = (lambda v: None if v is None else round(v, 1))
    r3 = (lambda v: None if v is None else round(v, 3))
    return LossDay(
        plant_key, day, kwp, r1(weather_expected), r1(pe), r1(exp), basis,
        ",".join(sorted(peer_yields)) if basis == "peers" else "",
        r1(actual),
        r1(s.lost) if s else None, r1(s.unavailability) if s else None,
        r1(s.overheating) if s else None, r1(s.underperformance) if s else None,
        r1(s.excused) if s else None,
        tariff, mxn(s.lost, tariff) if s else None,
        r1(counter), r1(catchup) if catchup else 0.0,
        r3(peer_ratio) if basis == "peers" else None,
        r3(weather_ratio) if basis == "weather" else None, r1(tol))


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


# ------------------------------------------------------------ v276 explanations
# Tomasz, 2026-09-30: "if there is underperformance please specify how did you
# estimate it. Add to all numbers that are not 100% a tool tip". One wording,
# used by the portal (mouse-over) and the phone app (tap).
def _k(v) -> str:
    return "-" if v is None else f"{float(v):,.0f}"


def explain_day(r: dict, name: Callable[[str], str] = lambda k: k) -> Tuple[str, str]:
    """(EN, ES) plain-text explanation of one loss_daily row (a dict with the
    table's column names; missing v276 columns are simply left out). Pure."""
    en: List[str] = []
    es: List[str] = []
    exp, basis = r.get("expected_kwh"), r.get("expected_basis") or "none"
    kwp = r.get("kwp_dc")
    if exp is None:
        return ("No expectation for this day: no healthy neighbour and no weather data.",
                "Sin expectativa para este día: sin vecina sana ni datos de clima.")
    if basis == "peers":
        peers = ", ".join(name(p) for p in (r.get("peers") or "").split(",") if p)
        ratio = r.get("peer_ratio")
        if ratio and kwp:
            y = float(r.get("expected_peers_kwh") or exp) / (float(kwp) * float(ratio))
            en.append(f"Expected {_k(exp)} kWh = nearby healthy plants ({peers}) made {y:.2f} kWh per kWp that day "
                      f"x this plant's usual ratio to them {float(ratio):.3f} (last 90 days) x {float(kwp):,.1f} kWp.")
            es.append(f"Esperado {_k(exp)} kWh = plantas vecinas sanas ({peers}) produjeron {y:.2f} kWh por kWp ese día "
                      f"x la relación habitual de esta planta con ellas {float(ratio):.3f} (últimos 90 días) x {float(kwp):,.1f} kWp.")
        else:
            en.append(f"Expected {_k(exp)} kWh from nearby healthy plants ({peers}) and this plant's usual ratio to them.")
            es.append(f"Esperado {_k(exp)} kWh a partir de plantas vecinas sanas ({peers}) y la relación habitual con ellas.")
    elif basis == "weather":
        wr = r.get("weather_ratio")
        extra = f" x this plant's usual actual/model ratio {float(wr):.3f} (last 90 days)" if wr else ""
        extra_es = f" x la relación habitual real/modelo de esta planta {float(wr):.3f} (últimos 90 días)" if wr else ""
        en.append(f"Expected {_k(exp)} kWh = weather model {_k(r.get('expected_weather_kwh'))} kWh (measured irradiance){extra}; "
                  "no healthy neighbour that day.")
        es.append(f"Esperado {_k(exp)} kWh = modelo de clima {_k(r.get('expected_weather_kwh'))} kWh (irradiancia medida){extra_es}; "
                  "sin vecina sana ese día.")
    else:
        en.append(f"Expected {_k(exp)} kWh = weather model only: this plant has not met its model in 90 days, so the model "
                  "is not reliable for it (check the irradiance sensor or the design data). No loss is booked against it.")
        es.append(f"Esperado {_k(exp)} kWh = solo modelo de clima: la planta no ha alcanzado su modelo en 90 días, así que no es "
                  "confiable para ella (revisar el sensor de irradiancia o los datos de diseño). No se registra pérdida contra él.")
        return " ".join(en), " ".join(es)
    act, counter, catch = r.get("actual_kwh"), r.get("counter_kwh"), r.get("catchup_kwh") or 0.0
    if catch and counter is not None:
        en.append(f"Actual {_k(act)} kWh = vendor day counter {_k(counter)} + {_k(catch)} kWh proved by the next night's "
                  "lifetime counter: the data link was down, the plant kept producing - not a loss.")
        es.append(f"Real {_k(act)} kWh = contador diario del fabricante {_k(counter)} + {_k(catch)} kWh probados por el contador "
                  "total de la noche siguiente: se cayó la conexión, la planta siguió produciendo - no es pérdida.")
    else:
        en.append(f"Actual {_k(act)} kWh = vendor day counter.")
        es.append(f"Real {_k(act)} kWh = contador diario del fabricante.")
    lost = r.get("lost_kwh") or 0.0
    un, oh, up = (r.get("unavailability_kwh") or 0.0), (r.get("overheating_kwh") or 0.0), (r.get("underperformance_kwh") or 0.0)
    if lost > 0:
        en.append(f"Lost {_k(lost)} kWh: unavailability {_k(un)} (inverters at 0 W in daylight, or silent while the counter shows "
                  f"no production), overheating {_k(oh)} (derating measured against cooler peers), underperformance {_k(up)} "
                  "(the rest: the plant ran but made less than expected, not explained by 0 W or heat).")
        es.append(f"Perdido {_k(lost)} kWh: indisponibilidad {_k(un)} (inversores en 0 W con luz, o sin datos mientras el contador "
                  f"muestra que no produjeron), sobrecalentamiento {_k(oh)} (reducción medida contra pares más frescos), "
                  f"bajo desempeño {_k(up)} (el resto: operó pero produjo menos, sin explicación por 0 W o calor).")
    else:
        en.append("Nothing lost.")
        es.append("Nada perdido.")
    tol = r.get("tolerance_kwh") or 0.0
    if tol >= 1:
        en.append(f"{_k(tol)} kWh short of the estimate is within normal day-to-day variation (under 10%) and not counted.")
        es.append(f"{_k(tol)} kWh debajo de la estimación están dentro de la variación normal diaria (menos de 10%) y no se cuentan.")
    if r.get("excused_kwh"):
        en.append(f"{_k(r['excused_kwh'])} kWh excused: approved customer maintenance, billed as deemed energy.")
        es.append(f"{_k(r['excused_kwh'])} kWh justificados: mantenimiento aprobado del cliente, facturado como energía considerada.")
    if r.get("lost_mxn") is not None and r.get("tariff_mxn"):
        en.append(f"MXN = {_k(lost)} kWh x PPA tariff {float(r['tariff_mxn']):.4f}.")
        es.append(f"MXN = {_k(lost)} kWh x tarifa PPA {float(r['tariff_mxn']):.4f}.")
    return " ".join(en), " ".join(es)


def explain_period(catchup_kwh: float, tolerance_kwh: float, days: int) -> Tuple[str, str]:
    """(EN, ES) what a period total includes and what it deliberately does not."""
    return (f"Sum of the daily losses over {days} day(s): unavailability + overheating + underperformance. "
            f"Not counted: {catchup_kwh:,.0f} kWh the vendor's lifetime counter proved were produced during data gaps, "
            f"and {tolerance_kwh:,.0f} kWh of day-to-day scatter below 10% of the estimate.",
            f"Suma de las pérdidas diarias de {days} día(s): indisponibilidad + sobrecalentamiento + bajo desempeño. "
            f"No se cuentan: {catchup_kwh:,.0f} kWh que el contador total del fabricante probó producidos durante cortes de datos, "
            f"ni {tolerance_kwh:,.0f} kWh de variación diaria menor al 10% de la estimación.")
