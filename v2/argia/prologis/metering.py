"""Metering for the Prologis platform (v292).

Until Prologis grants ARGIA administrator access to its SolarEdge / Hark
accounts there is no measured data, so every number on the platform comes
from ``SampleSource``: a deterministic, physically plausible simulation
(sun position for the real coordinates, Haurwitz clear-sky irradiance,
seasonal cloudiness, a PR near 0.80, inverter clipping). Every page that
shows it carries the SAMPLE label. When a site gets a solaredge_site_id
and its telemetry is collected, ``source_for`` switches that site to
measured data with no change to the pages.

Pure: no I/O. Same inputs, same numbers (tests rely on it).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

MX_OFFSET = dt.timedelta(hours=-6)          # Mexico City: UTC-6 all year since 2022
STEP_MIN = 5
PR_TARGET = 0.80
AC_RATIO = 0.83                              # kWac / kWp (SolarEdge Synergy designs)
CO2_KG_PER_KWH = 0.444                       # CRE/SEMARNAT grid factor used in ARGIA reports
SAMPLE = "sample"


def _rand(*parts) -> float:
    """Deterministic uniform [0, 1) from any parts."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def sun_elevation(lat: float, lon: float, t_local: dt.datetime) -> float:
    """Solar elevation in degrees (NOAA simplified). ``t_local`` is MX time."""
    utc = t_local - MX_OFFSET
    doy = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cosz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, cosz))))


def clear_sky_ghi(elev_deg: float) -> float:
    """Haurwitz clear-sky GHI, W/m2."""
    if elev_deg <= 0:
        return 0.0
    cz = math.sin(math.radians(elev_deg))
    return 1098.0 * cz * math.exp(-0.057 / cz)


def day_clearness(code: str, day: dt.date) -> float:
    """0.35..1.0: dry season (Nov-Apr) mostly clear, rainy season cloudier."""
    rainy = day.month in (6, 7, 8, 9)
    base = 0.62 if rainy else 0.86
    spread = 0.30 if rainy else 0.14
    # one weather draw per area and day (neighbouring roofs share the sky)
    v = base + spread * (_rand("sky", day.isoformat()) - 0.5) * 2
    v += 0.04 * (_rand("site", code, day.isoformat()) - 0.5)
    return max(0.35, min(1.0, v))


@dataclass(frozen=True)
class Event:
    """A simulated fault, so the sample shows what the platform catches."""
    kind: str            # 'inverter_off' | 'comm_loss'
    share: float         # share of the plant affected (inverter_off)


def event_for(code: str, day: dt.date, n_sites: int, index: int) -> Optional[Event]:
    """Deterministic, rare: about one plant in the portfolio per week has a
    partial inverter outage, one has a communication loss."""
    r = _rand("event", code, day.isoformat())
    if r < 0.025:
        return Event("inverter_off", 0.33)
    if r < 0.035:
        return Event("comm_loss", 0.0)
    return None


@lru_cache(maxsize=4096)
def _profile(code: str, lat: float, lon: float, kwp: float, day: dt.date) -> Tuple[Tuple[str, float, float], ...]:
    """((HH:MM, kW, GHI W/m2), ...) every 5 minutes, 05:30-20:00 MX, full day."""
    k = day_clearness(code, day)
    ph1, ph2 = _rand("ph1", code, day) * 6.28, _rand("ph2", day) * 6.28
    out = []
    t = dt.datetime.combine(day, dt.time(5, 30))
    end = dt.datetime.combine(day, dt.time(20, 0))
    kac = kwp * AC_RATIO
    while t <= end:
        el = sun_elevation(lat, lon, t)
        ghi_cs = clear_sky_ghi(el)
        m = t.hour + t.minute / 60
        wob = 1 - (1 - k) * (0.6 + 0.4 * math.sin(m * 1.7 + ph1) * math.sin(m * 0.9 + ph2))
        ghi = max(0.0, ghi_cs * min(1.0, wob))
        kw = min(kac, kwp * PR_TARGET * ghi / 1000.0 * (1 + 0.04 * (_rand("pr", code) - 0.5)))
        out.append((t.strftime("%H:%M"), round(max(0.0, kw), 2), round(ghi, 1)))
        t += dt.timedelta(minutes=STEP_MIN)
    return tuple(out)


@dataclass
class DayResult:
    day: dt.date
    kwh: float
    irr_kwh_m2: float
    expected_kwh: float
    availability: float
    pr: Optional[float]
    event: Optional[Event]
    series: List[Tuple[str, Optional[float]]]     # (HH:MM, kW or None = no data)


def day_result(code: str, lat: float, lon: float, kwp: float, day: dt.date,
               upto: Optional[dt.time] = None, index: int = 0, n_sites: int = 1) -> DayResult:
    prof = _profile(code, lat, lon, kwp, day)
    ev = event_for(code, day, n_sites, index)
    series: List[Tuple[str, Optional[float]]] = []
    kwh = irr = exp = 0.0
    up_slots = 0.0
    all_slots = 0
    for hhmm, kw, ghi in prof:
        if upto is not None and hhmm > upto.strftime("%H:%M"):
            break
        e_kw = kw
        if ev and ev.kind == "inverter_off" and "10:00" <= hhmm:
            e_kw = kw * (1 - ev.share)
        missing = bool(ev and ev.kind == "comm_loss" and "13:00" <= hhmm)
        series.append((hhmm, None if missing else round(e_kw, 2)))
        dt_h = STEP_MIN / 60
        irr += ghi * dt_h / 1000
        exp += kwp * PR_TARGET * ghi / 1000 * dt_h
        if ghi > 150:                        # MSA availability: hours above 150 W/m2
            all_slots += 1
            fault = bool(ev and ev.kind == "inverter_off" and "10:00" <= hhmm)
            up_slots += (1 - ev.share) if fault else 1      # weighted by the DC share down
        kwh += (kw if missing else e_kw) * dt_h      # comm loss: the meter still counts
    avail = (up_slots / all_slots) if all_slots else 1.0
    pr = (kwh / (kwp * irr)) if irr > 0.2 else None
    return DayResult(day, round(kwh, 1), round(irr, 3), round(exp, 1), round(avail, 4),
                     (round(pr, 3) if pr else None), ev, series)


@dataclass
class LiveState:
    code: str
    source: str
    status: str            # producing | night | inverter_fault | comm_loss | pre_pto
    kw_now: Optional[float]
    kwh_today: float
    expected_today: float
    pr_today: Optional[float]
    last_seen: Optional[str]


def live(site, now_local: dt.datetime, index: int = 0, n_sites: int = 1) -> LiveState:
    if not site.operating:
        return LiveState(site.code, SAMPLE, "pre_pto", None, 0.0, 0.0, None, None)
    r = day_result(site.code, site.lat, site.lon, site.kwp, now_local.date(),
                   upto=now_local.time(), index=index, n_sites=n_sites)
    known = [(t, v) for t, v in r.series if v is not None]
    kw = known[-1][1] if known else 0.0
    last = known[-1][0] if known else None
    el = sun_elevation(site.lat, site.lon, now_local)
    if r.event and r.event.kind == "comm_loss" and now_local.time() >= dt.time(13, 0):
        st = "comm_loss"
        kw = None
    elif el <= 0:
        st = "night"
    elif r.event and r.event.kind == "inverter_off" and now_local.time() >= dt.time(10, 0):
        st = "inverter_fault"
    else:
        st = "producing"
    return LiveState(site.code, SAMPLE, st, kw, r.kwh, r.expected_kwh, r.pr, last)


def history(site, end: dt.date, days: int, index: int = 0, n_sites: int = 1) -> List[DayResult]:
    """Complete days ``end-days+1 .. end`` (no day before the site's PTO quarter)."""
    out = []
    for i in range(days - 1, -1, -1):
        d = end - dt.timedelta(days=i)
        out.append(day_result(site.code, site.lat, site.lon, site.kwp, d, index=index, n_sites=n_sites))
    return out


def portfolio_kpis(sites: Sequence, now_local: dt.datetime, days: int = 30) -> Dict[str, float]:
    """Headline tiles: live MW, today MWh, 30-day MWh / PR / availability, CO2."""
    op = [s for s in sites if s.operating]
    n = len(op)
    lv = [live(s, now_local, i, n) for i, s in enumerate(op)]
    yday = now_local.date() - dt.timedelta(days=1)
    e30 = irr_w = av_w = 0.0
    kwp_op = sum(s.kwp for s in op) or 1.0
    for i, s in enumerate(op):
        h = history(s, yday, days, i, n)
        e30 += sum(d.kwh for d in h)
        irr_w += sum(d.irr_kwh_m2 for d in h) * s.kwp
        av_w += sum(d.availability for d in h) / len(h) * s.kwp
    return {
        "sites": len(sites), "operating": n,
        "kwp": round(sum(s.kwp for s in sites), 1), "kwp_operating": round(kwp_op, 1),
        "kw_now": round(sum(x.kw_now or 0 for x in lv), 1),
        "kwh_today": round(sum(x.kwh_today for x in lv), 1),
        "mwh_30d": round(e30 / 1000, 2),
        "pr_30d": round(e30 / irr_w, 3) if irr_w else 0.0,
        "availability_30d": round(av_w / kwp_op, 4),
        "co2_t_30d": round(e30 * CO2_KG_PER_KWH / 1000, 1),
        "alerts": sum(x.status in ("inverter_fault", "comm_loss") for x in lv),
    }


def source_for(site) -> str:
    """'sample' until the site's SolarEdge data is collected (v292: always)."""
    return SAMPLE
