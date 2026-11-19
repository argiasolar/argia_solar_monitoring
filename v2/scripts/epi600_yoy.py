#!/usr/bin/env python3
"""v381: year-over-year comparison at monthly resolution, with an independent irradiance.

Why not EPI-600 for 365 days: 5-minute telemetry starts on 2026-07-01, inverter DC
on 2026-09-05 and string data on 2026-10-07 (pio06). What does exist for the whole
life of each plant is the daily energy (daily_production: vendor day counters, v1 and
v2) and the station's daily irradiance. So this report gives, per plant and month:

  EPI-sat  = sum(measured AC energy) / sum(expected AC energy), where the expected
             energy is the same spec model as EPI-600 (Erbs + Perez, PR0, degradation
             from COD, soiling, B_m, inverter limits, 0.985) driven by satellite GHI
             (Open-Meteo satellite_radiation_seamless, hourly mean of the preceding
             hour, so the sun is taken at mid-hour). Cell temperature: the spec's
             fallback T_amb + k x G_poa/1000 with k from Annex B and T_amb the design
             year's mean for that month and hour (no measured temperature exists
             before July 2026). Same reference every year -> comparable year over year.
  PR-station = sum(energy) / sum(station irradiance kWh/m2 x kWp in service).
  station/sat = sum(station daily irradiance) / sum(satellite GHI) - sensor drift.

Days count only when the plant was in service (first string COD), the satellite has
every daylight hour, and v2 marks the day complete (v1 days have no class). Strings
before their own COD are left out of the expected energy (e.g. Taigene INV5/INV6,
May 2026). Read-only inputs; no plant data in the repository.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.epi import model as M    # noqa: E402
from argia.epi import sky           # noqa: E402

MX = dt.timezone(dt.timedelta(hours=-6))


def t_amb_climatology(design_csv):
    """(month, hour local) -> mean dry-bulb temperature of the design year."""
    acc = defaultdict(lambda: [0.0, 0])
    with open(design_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = dt.datetime.fromisoformat(r["timestamp"])
            a = acc[(t.month, t.hour)]
            a[0] += float(r["dry_bulb_temperature"])
            a[1] += 1
    return {k: s / n for k, (s, n) in acc.items()}


def hourly_expected_ac_kwh(pm, strs, k_fallback, tamb, ts_mid, ghi):
    """Expected AC energy of one hour (kWh) for the strings in service at ts_mid."""
    loc = ts_mid.astimezone(MX)
    sun = sky.sun_position(ts_mid, pm.lat, pm.lon)
    by_inv = defaultdict(list)
    cache = {}
    for s in strs:
        d = M.degradation_factor(s.module, s.cod, loc.date())
        if d is None:
            continue
        key = (s.tilt, s.azimuth)
        if key not in cache:
            cache[key] = sky.poa(ghi, ts_mid, pm.lat, pm.lon, s.tilt, s.azimuth, sun=sun).poa_global
        g = cache[key]
        tc = tamb[(loc.month, loc.hour)] + k_fallback * g / 1000.0
        by_inv[s.inverter].append(M.expected_string_w(s.kwp, d, g, M.pr0(pm.b, tc, g),
                                                      pm.soiling_pct[loc.month - 1], pm.bias[loc.month - 1]))
    tot = 0.0
    for inv, es in by_inv.items():
        lim = pm.ac_limit_kw.get(inv)
        tot += sum(es) * (M.cap_scale(es, lim) if lim else 1.0)
    return tot * M.INVERTER_EFFICIENCY / 1000.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    for x in ("--daily", "--sat-dir", "--design-dir", "--annex-a", "--annex-b", "--annex-c", "--bias", "--limits", "--plants"):
        ap.add_argument(x, required=True)
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    plants = json.load(open(a.plants, encoding="utf-8"))
    bias = json.load(open(a.bias, encoding="utf-8"))["bias"]
    limits = json.load(open(a.limits, encoding="utf-8"))
    soiling = tuple(float(r["design_soiling_pct"]) for r in csv.DictReader(open(a.annex_c, encoding="utf-8")))
    annex_b = {r["plant"]: r for r in csv.DictReader(open(a.annex_b, encoding="utf-8"))}
    strings = defaultdict(list)
    for r in csv.DictReader(open(a.annex_a, encoding="utf-8")):
        strings[r["plant"]].append(M.StringDef(r["uid"], r["inverter"], float(r["kwp"]), float(r["tilt"]), float(r["azimuth"]),
                                               r["module"], dt.date.fromisoformat(r["cod_date"])))
    daily = defaultdict(dict)
    with open(a.daily, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            daily[r["plant_key"]][dt.date.fromisoformat(r["prod_date"])] = r
    out = {}
    for name, meta in plants.items():
        key = meta["key"]
        b = annex_b[name]
        pm = M.PlantModel(name, float(b["latitude"]), float(b["longitude"]), tuple(float(b[k]) for k in ("b0", "b1", "b2", "b3")),
                          soiling, tuple(bias[name]), limits[name])
        strs = strings[name]
        k_fb = float(b["tcell_fallback_k_K_per_kWm2"])
        tamb = t_amb_climatology(os.path.join(a.design_dir, f"PPA {name}.csv"))
        sat = json.load(open(os.path.join(a.sat_dir, f"{name.replace(' ', '_')}.json"), encoding="utf-8"))["hourly"]
        exp_day, sat_day, missing = defaultdict(float), defaultdict(float), set()
        for t, v in zip(sat["time"], sat["shortwave_radiation"]):
            end = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            mid = end - dt.timedelta(minutes=30)
            day = mid.astimezone(MX).date()
            if sky.sun_position(mid, pm.lat, pm.lon).zenith >= 90:
                continue
            if v is None:
                missing.add(day)
                continue
            exp_day[day] += hourly_expected_ac_kwh(pm, strs, k_fb, tamb, mid, float(v))
            sat_day[day] += float(v) / 1000.0
        first_cod = min(s.cod for s in strs)
        months = defaultdict(lambda: defaultdict(float))
        for day, r in daily[key].items():
            if day < first_cod or day not in exp_day or day in missing:
                continue
            if r["source"] == "v2" and r["data_class"] != "full":
                continue
            e = float(r["energy_kwh"] or 0)
            h = float(r["irradiance_kwh_m2"]) if r["irradiance_kwh_m2"] not in ("", None) else None
            kwp = sum(s.kwp for s in strs if s.cod <= day)
            m = months[f"{day.year}-{day.month:02d}"]
            m["days"] += 1
            m["energy"] += e
            m["expected"] += exp_day[day]
            m["sat_h"] += sat_day[day]
            if h is not None and h > 0:
                m["st_days"] += 1
                m["st_energy"] += e
                m["st_h"] += h
                m["st_h_kwp"] += h * kwp
                m["sat_h_st"] += sat_day[day]
        res = {}
        for mo, m in sorted(months.items()):
            res[mo] = {"days": int(m["days"]), "energy_kwh": m["energy"], "expected_kwh": m["expected"],
                       "epi_sat": m["energy"] / m["expected"] if m["expected"] > 0 else None,
                       "pr_station": m["st_energy"] / m["st_h_kwp"] if m["st_h_kwp"] > 0 else None,
                       "station_over_sat": m["st_h"] / m["sat_h_st"] if m["sat_h_st"] > 0 else None}
        out[name] = res
        print(f"== {name} ({key})")
        for mo, r in res.items():
            f = lambda x, d=3: "  n/a" if x is None else f"{x:.{d}f}"
            print(f"  {mo}  days {r['days']:2d}  energy {r['energy_kwh']:9.0f}  expected {r['expected_kwh']:9.0f}  EPI-sat {f(r['epi_sat'])}"
                  f"  PR-station {f(r['pr_station'])}  station/sat {f(r['station_over_sat'])}")
    if a.json_out:
        json.dump(out, open(a.json_out, "w", encoding="utf-8"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
