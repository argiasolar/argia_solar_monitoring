"""EPI-600 / EPI-150 / Availability-150 for one plant over a period (spec v0.7 steps 1-7).

Pure: the caller hands in 5-minute buckets already read from the database (or an
export), this module does the arithmetic and returns tallies that can be audited
sample by sample. Rules, each traceable to the spec or the audit doc:

- a bucket is 5 minutes; every sample weighs 5/60 h (spec 3.1);
- station data valid = GHI, panel and ambient temperature present, and the panel
  sensor not more than 1 C below ambient when GHI > 600 (spec 3.2);
- a string sample needs its current and its voltage (Growatt: string current x
  the voltage of its MPPT, strings 2i-1 and 2i share MPPT i - proven on October
  2026 data, 1.8-3.1 % residual; Huawei MG0: input current x input voltage;
  MPPT-only inverters: MPPT current x voltage);
- strings before their COD do not exist (excluded everywhere);
- an inverter is producing when the DC of its strings is >= 5 % of their expected
  DC (spec 3.2); not producing -> excluded from EPI, DOWN for availability;
- no inverter data in a bucket -> MISSING: neither UP nor DOWN; reported as
  coverage (the v310 counter proof is applied later, in the availability job);
- EPI = sum(real) / sum(expected) over exactly the same valid samples, at every
  level (string, inverter, plant), never an average of ratios (spec step 5, 7);
- station data quality (IEA PVPS T13-28 section 3.3, after IEC 61724-1/-3), before
  anything else: range -6..1500 W/m2; GHI > 20 W/m2 with the sun below the horizon
  is invalid; a frozen value (the same GHI, within 0.5 W/m2, in 4 or more
  consecutive buckets with GHI > 50) is invalid from its 2nd bucket on - October
  2026: the SAG station (shared by Vitalmex) froze 47 times, 1,335 daylight
  samples (37 %), the other stations 0.1-0.3 %;
- optional stability filter (audit 2, Vitalmex): station GHI at t-5, t, t+5 min
  within +/-5 % (ASTM E2848-style); off unless asked for.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from argia.epi import model as M
from argia.epi import sky

STEP_H = 5.0 / 60.0
STABLE_BAND = 0.05


@dataclass(frozen=True)
class Channel:
    kind: str     # "str" (Growatt string), "pv" (Huawei input), "mppt"
    index: int    # 1-based

    def power_w(self, str_a: Sequence[Optional[float]], mppt_a: Sequence[Optional[float]],
                mppt_v: Sequence[Optional[float]]) -> Optional[float]:
        def at(a, i):
            return a[i - 1] if a is not None and 0 < i <= len(a) else None
        if self.kind == "str":
            i, v = at(str_a, self.index), at(mppt_v, (self.index + 1) // 2)
        elif self.kind == "pv":
            i, v = at(str_a, self.index), at(mppt_v, self.index)
        elif self.kind == "mppt":
            i, v = at(mppt_a, self.index), at(mppt_v, self.index)
        else:
            raise ValueError(self.kind)
        if i is None or v is None:
            return None
        return max(0.0, i) * max(0.0, v)


@dataclass(frozen=True)
class Station:
    ts: dt.datetime           # mean time of the plant's samples in the bucket (aware)
    ghi: Optional[float]
    t_panel: Optional[float]
    t_amb: Optional[float]


@dataclass(frozen=True)
class InvSample:
    str_a: Sequence[Optional[float]]
    mppt_a: Sequence[Optional[float]]
    mppt_v: Sequence[Optional[float]]


@dataclass
class Avail:
    up: int = 0
    down: int = 0
    missing: int = 0
    up_by_counter: int = 0      # part of `up`: silent, but the day counter proves production (gaps.py)
    down_by_counter: int = 0    # part of `down`: silent, and the counter proves no production
    by_ac_power: int = 0        # judged on AC power because string data was absent

    @property
    def value(self) -> Optional[float]:
        k = self.up + self.down
        return None if k == 0 else self.up / k

    @property
    def coverage(self) -> Optional[float]:
        k = self.up + self.down + self.missing
        return None if k == 0 else (self.up + self.down) / k


@dataclass
class Result:
    epi600: Dict[str, M.Tally] = field(default_factory=dict)    # key: "plant", "INV1", "INV1:PV3"
    epi150: Dict[str, M.Tally] = field(default_factory=dict)
    avail: Dict[str, Avail] = field(default_factory=dict)       # key: inverter label
    daily600: Dict[dt.date, M.Tally] = field(default_factory=dict)
    reasons: Dict[str, int] = field(default_factory=dict)       # why buckets / samples were left out

    def _t(self, d, k):
        return d.setdefault(k, M.Tally())

    def count(self, why: str, n: int = 1):
        self.reasons[why] = self.reasons.get(why, 0) + n


def station_valid(s: Station) -> Tuple[bool, str]:
    if s.ghi is None or s.t_panel is None or s.t_amb is None:
        return False, "station_missing"
    if s.ghi > 600 and s.t_panel < s.t_amb - 1.0:
        return False, "panel_sensor_below_ambient"
    return True, ""


GHI_MIN, GHI_MAX = -6.0, 1500.0
FROZEN_RUN = 4          # 4 buckets = 15 minutes unchanged after the first; a clear solar noon moves >2 W/m2 in that time
FROZEN_DELTA = 0.5
FROZEN_MIN_GHI = 50.0
NIGHT_MAX_GHI = 20.0


def station_qc(station: Mapping[int, Station], lat: float, lon: float) -> Dict[int, str]:
    """Bucket -> reason for every station sample that fails a quality check."""
    bad: Dict[int, str] = {}
    bs = sorted(b for b in station if station[b].ghi is not None)
    i = 0
    while i < len(bs):                       # frozen runs
        j = i
        while (j + 1 < len(bs) and bs[j + 1] == bs[j] + 1
               and abs(station[bs[j + 1]].ghi - station[bs[i]].ghi) < FROZEN_DELTA):
            j += 1
        if j - i + 1 >= FROZEN_RUN and station[bs[i]].ghi > FROZEN_MIN_GHI:
            for k in bs[i + 1:j + 1]:
                bad[k] = "ghi_frozen"
        i = j + 1
    for b in bs:
        g = station[b].ghi
        if g < GHI_MIN or g > GHI_MAX:
            bad[b] = "ghi_out_of_range"
        elif g > NIGHT_MAX_GHI and sky.sun_position(station[b].ts, lat, lon).zenith > 90:
            bad[b] = "ghi_at_night"
    return bad


def stable(prev: Optional[Station], cur: Station, nxt: Optional[Station]) -> bool:
    vals = [x.ghi for x in (prev, cur, nxt) if x is not None and x.ghi is not None]
    if len(vals) < 3 or min(vals) <= 0:
        return False
    return max(vals) / min(vals) <= 1 + STABLE_BAND


def expected_inverter_w(pm: M.PlantModel, strs: Sequence[M.StringDef], st: Station,
                        sun: Optional[sky.SunPos] = None) -> Tuple[float, List[Tuple[M.StringDef, float, float]]]:
    """Capped expected DC of one inverter for one station sample, and the per-string
    (string, G_poa, expected W after the limit). Strings before their COD are left out."""
    sun = sun or sky.sun_position(st.ts, pm.lat, pm.lon)
    local = st.ts.astimezone(dt.timezone(dt.timedelta(hours=-6)))
    live = []
    cache: Dict[Tuple[float, float], float] = {}
    for s in strs:
        d = M.degradation_factor(s.module, s.cod, local.date())
        if d is None:
            continue
        key = (s.tilt, s.azimuth)
        if key not in cache:
            cache[key] = sky.poa(st.ghi, st.ts, pm.lat, pm.lon, s.tilt, s.azimuth, sun=sun).poa_global
        g = cache[key]
        e = M.expected_string_w(s.kwp, d, g, M.pr0(pm.b, M.t_cell(st.t_panel, g), g),
                                pm.soiling_pct[local.month - 1], pm.bias[local.month - 1])
        live.append((s, g, e))
    if not live:
        return 0.0, []
    lim = pm.ac_limit_kw.get(strs[0].inverter)
    scale = M.cap_scale([e for _, _, e in live], lim) if lim else 1.0
    out = [(s, g, e * scale) for s, g, e in live]
    return sum(e for _, _, e in out), out


def run_plant(pm: M.PlantModel,
              strings: Sequence[M.StringDef],
              channel_of: Mapping[str, Channel],                 # uid -> channel
              sn_of: Mapping[str, str],                          # inverter label -> serial
              station: Mapping[int, Station],                    # bucket (epoch // 300) -> station
              samples: Mapping[Tuple[str, int], InvSample],      # (serial, bucket) -> sample
              tz: dt.tzinfo,
              stability_filter: bool = False,
              ac_power_w: Optional[Mapping[Tuple[str, int], float]] = None,
              gap_verdict: Optional[Mapping[Tuple[str, int], str]] = None) -> Result:
    """ac_power_w: (serial, bucket) -> inverter AC power, used for availability when the
    string sample is missing (EPI still needs strings). gap_verdict: (serial, bucket) ->
    "up"/"down" from the day counter over a silent gap (gaps.reconstruct_day)."""
    res = Result()
    by_inv: Dict[str, List[M.StringDef]] = {}
    for s in strings:
        by_inv.setdefault(s.inverter, []).append(s)
    planes = sorted({(s.tilt, s.azimuth) for s in strings})
    qc = station_qc(station, pm.lat, pm.lon)
    for b in sorted(station):
        st = station[b]
        if b in qc:
            res.count(qc[b])
            continue
        ok, why = station_valid(st)
        if not ok:
            res.count(why)
            continue
        if stability_filter and not stable(station.get(b - 1), st, station.get(b + 1)):
            res.count("unstable_irradiance")
            continue
        day = st.ts.astimezone(tz).date()
        month = st.ts.astimezone(tz).month
        sun = sky.sun_position(st.ts, pm.lat, pm.lon)
        gp = {p: sky.poa(st.ghi, st.ts, pm.lat, pm.lon, p[0], p[1], sun=sun).poa_global for p in planes}
        for inv, strs in by_inv.items():
            live = []
            for s in strs:
                d = M.degradation_factor(s.module, s.cod, day)
                if d is None:
                    continue
                g = gp[(s.tilt, s.azimuth)]
                tc = M.t_cell(st.t_panel, g)
                e = M.expected_string_w(s.kwp, d, g, M.pr0(pm.b, tc, g),
                                        pm.soiling_pct[month - 1], pm.bias[month - 1])
                live.append((s, g, e))
            if not live:
                continue
            lim = pm.ac_limit_kw.get(inv)
            scale = M.cap_scale([e for _, _, e in live], lim) if lim else 1.0
            judged = st.ghi > M.EPI_150                          # Availability-150 uses station irradiance
            sn = sn_of[inv]
            smp = samples.get((sn, b))
            if smp is None:
                if judged:
                    a = res.avail.setdefault(inv, Avail())
                    pac = (ac_power_w or {}).get((sn, b))
                    verdict = (gap_verdict or {}).get((sn, b))
                    if pac is not None:
                        exp_ac = sum(e * scale for _, _, e in live) * M.INVERTER_EFFICIENCY
                        a.by_ac_power += 1
                        if exp_ac > 0 and pac >= M.PRODUCING_SHARE * exp_ac:
                            a.up += 1
                        else:
                            a.down += 1
                    elif verdict == "up":
                        a.up += 1
                        a.up_by_counter += 1
                    elif verdict == "down":
                        a.down += 1
                        a.down_by_counter += 1
                    else:
                        a.missing += 1
                res.count("string_data_missing")
                continue
            reals = [(s, g, e * scale, channel_of[s.uid].power_w(smp.str_a, smp.mppt_a, smp.mppt_v))
                     for s, g, e in live]
            exp_tot = sum(e for _, _, e, _ in reals)
            real_tot = sum(p for _, _, _, p in reals if p is not None)
            producing = exp_tot > 0 and real_tot >= M.PRODUCING_SHARE * exp_tot
            if judged:
                a = res.avail.setdefault(inv, Avail())
                if producing:
                    a.up += 1
                else:
                    a.down += 1
            if not producing:
                res.count("inverter_not_producing")
                continue
            for s, g, e, p in reals:
                if p is None:
                    res.count("string_value_missing")
                    continue
                if g >= M.EPI_150:
                    for k in ("plant", inv, s.uid):
                        res._t(res.epi150, k).add(p, e, STEP_H)
                if g >= M.EPI_600:
                    for k in ("plant", inv, s.uid):
                        res._t(res.epi600, k).add(p, e, STEP_H)
                    res._t(res.daily600, day).add(p, e, STEP_H)
    return res


def plant_availability(res: Result, kwp_by_inv: Mapping[str, float]) -> Optional[float]:
    """kWp-weighted mean of inverter availabilities (spec step 6)."""
    num = den = 0.0
    for inv, a in res.avail.items():
        if a.value is not None:
            num += a.value * kwp_by_inv[inv]
            den += kwp_by_inv[inv]
    return None if den == 0 else num / den
