#!/usr/bin/env python3
"""v378: run the EPI-600 engine for a period from export files and print an auditable report.

Inputs (private, never in the repository):
  --export-dir  telemetry.csv, strings.csv, detail.csv, layouts/<KEY>.json (fin_verify export format)
  --annex-a / --annex-c / --annex-b   the spec annexes (strings, soiling, lat/lon/b0..b3)
  --bias        JSON written by epi600_design_check.py --out (computed B_m)
  --limits      JSON {plant: {INVn: kW AC}}
  --plants      JSON {plant name: {"key": KEY, "vendor": GROWATT|HUAWEI,
                "channel_overrides": {"INV2:PV4": 16}}} (register corrections proven from data)
Options: --stability (irradiance stability filter), --plant NAME (repeatable).

Checks printed with every result:
  * string sum vs inverter DC: sum of string powers / inverter DC power (Growatt ppv_w,
    Huawei ppv_w) over the samples where both exist - proves the string power
    reconstruction (current x voltage, wiring) before any EPI is trusted;
  * per inverter and plant: EPI-600, EPI-150, valid samples, band, availability, coverage;
  * daily EPI-600 spread (std of daily values) - the stability-filter test;
  * every exclusion reason with its count.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.epi import engine as E   # noqa: E402
from argia.epi import gaps as G     # noqa: E402
from argia.epi import sky           # noqa: E402
from argia.epi import model as M    # noqa: E402

MX = dt.timezone(dt.timedelta(hours=-6))   # America/Mexico_City has no DST since Oct 2022


def ts_parse(s: str) -> dt.datetime:
    s = s.strip()
    if s.endswith("+00"):
        s += ":00"
    return dt.datetime.fromisoformat(s)


def arr(s: str):
    s = (s or "").strip()
    if not s or s == "NULL":
        return None
    out = []
    for x in s.strip("{}").split(","):
        x = x.strip()
        out.append(None if x in ("", "NULL") else float(x))
    return out


def fmt(x, d=4):
    return "n/a" if x is None else f"{x:.{d}f}"


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def channel_for(string_id: str, plant_vendor: str) -> E.Channel:
    n = int(re.sub(r"\D", "", string_id))
    if string_id.upper().startswith("MPPT"):
        return E.Channel("mppt", n)
    return E.Channel("pv" if plant_vendor == "HUAWEI" else "str", n)


def load_export(d, key):
    station_rows = defaultdict(list)
    with open(os.path.join(d, "telemetry.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["plant_key"] != key:
                continue
            t = ts_parse(r["ts_utc"])
            station_rows[int(t.timestamp()) // 300].append(
                (t, fnum(r["irradiance_wm2"]), fnum(r["module_temp_c"]), fnum(r["ambient_temp_c"])))
    station = {}
    for b, rows in station_rows.items():
        def avg(i):
            v = [x[i] for x in rows if x[i] is not None]
            return sum(v) / len(v) if v else None
        mean_ts = dt.datetime.fromtimestamp(sum(x[0].timestamp() for x in rows) / len(rows), dt.timezone.utc)
        station[b] = E.Station(mean_ts, avg(1), avg(2), avg(3))
    samples = {}
    with open(os.path.join(d, "strings.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["plant_key"] != key:
                continue
            b = int(ts_parse(r["ts_utc"]).timestamp()) // 300
            samples[(r["inverter_sn"], b)] = E.InvSample(arr(r["str_a"]) or [], arr(r["mppt_a"]) or [], arr(r["mppt_v"]) or [])
    ac, ctr = {}, defaultdict(dict)
    rows = defaultdict(list)
    with open(os.path.join(d, "telemetry.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["plant_key"] == key:
                rows[r["inverter_sn"]].append((int(ts_parse(r["ts_utc"]).timestamp()) // 300,
                                               fnum(r["power_w"]), fnum(r["etoday_kwh"])))
    stale = {}
    for sn, rs in rows.items():
        st_b = G.stale_buckets(rs)
        stale[sn] = len(st_b)
        for bk, p, c in rs:
            if bk in st_b:
                continue                     # a frozen API record is missing data, not zero production
            if p is not None:
                ac[(sn, bk)] = p
            if c is not None:
                ctr[sn][bk] = c
    load_export.stale = stale
    ppv = {}
    with open(os.path.join(d, "detail.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["plant_key"] == key and fnum(r["ppv_w"]) is not None:
                ppv[(r["inverter_sn"], int(ts_parse(r["ts_utc"]).timestamp()) // 300)] = float(r["ppv_w"])
    return station, samples, ppv, ac, ctr


def energy_and_gaps(pm, strs_by_inv, sn_of, station, ctr, daily_total=None):
    """Per inverter-day: counter-conserving 5-minute energy (gaps.py) and the gap verdicts.
    daily_total: {date: plant kWh} (daily_production): closes UNKNOWN end-of-day slots
    (rule 3) - the plant residual is spread over every inverter's unknown slots by the
    same weights; a negative residual is reported and left unknown."""
    qc = E.station_qc(station, pm.lat, pm.lon)
    verdict, summary = {}, defaultdict(lambda: defaultdict(float))
    open_slots = defaultdict(list)          # date -> [(inv, weight)]
    known_day = defaultdict(float)
    for inv, sn in sn_of.items():
        strs = strs_by_inv[inv]
        kwp = sum(s.kwp for s in strs)
        days = defaultdict(list)
        for day in (daily_total or {}):
            days[day]                                  # a day without any counter sample is all UNKNOWN
        for bk in ctr.get(sn, {}):
            days[dt.datetime.fromtimestamp(bk * 300, dt.timezone.utc).astimezone(MX).date()].append(bk)
        for day, bks in days.items():
            d0 = int(dt.datetime(day.year, day.month, day.day, tzinfo=MX).timestamp()) // 300
            slots, w = [], {}
            for k in range(d0, d0 + 288):
                tmid = dt.datetime.fromtimestamp(k * 300 + 150, dt.timezone.utc)
                z = sky.sun_position(tmid, pm.lat, pm.lon).zenith
                if z >= 90:
                    continue
                slots.append(k)
                s = station.get(k)
                if s is not None and k not in qc and E.station_valid(s)[0]:
                    w[k] = E.expected_inverter_w(pm, strs, s)[0]
                else:
                    w[k] = sky.clear_sky_ghi_haurwitz(z) * kwp            # shape only
            rec, gaps = G.reconstruct_day([(k, ctr[sn][k]) for k in bks], slots, w)
            sm = summary[inv]
            for s in rec:
                sm[s.flag + "_slots"] += 1
                if s.energy_kwh is not None:
                    sm[s.flag + "_kwh"] += s.energy_kwh
                    known_day[day] += s.energy_kwh
                else:
                    open_slots[day].append((inv, w.get(s.bucket, 0.0)))
            for g in gaps:
                sm["gaps"] += 1
                sm["gap_" + g.availability] += 1
                if g.availability in ("up", "down"):
                    for k in range(g.first, g.last + 1):
                        verdict[(sn, k)] = g.availability
    for day, opens in open_slots.items():
        tot = (daily_total or {}).get(day)
        if tot is None:
            continue
        resid = tot - known_day[day]
        wsum = sum(x for _, x in opens)
        if resid < -0.5 or wsum <= 0:
            for inv, _ in opens:
                summary[inv]["daily_total_conflict_slots"] += 1
            continue
        for inv, x in opens:
            e = max(resid, 0.0) * x / wsum
            sm = summary[inv]
            sm["unknown_slots"] -= 1
            sm["closed_by_daily_slots"] += 1
            sm["closed_by_daily_kwh"] += e
    return verdict, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    for a in ("--export-dir", "--annex-a", "--annex-b", "--annex-c", "--bias", "--limits", "--plants"):
        ap.add_argument(a, required=True)
    ap.add_argument("--stability", action="store_true")
    ap.add_argument("--plant", action="append")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    plants = json.load(open(a.plants, encoding="utf-8"))
    bias = json.load(open(a.bias, encoding="utf-8"))["bias"]
    limits = json.load(open(a.limits, encoding="utf-8"))
    with open(a.annex_c, encoding="utf-8") as f:
        soiling = tuple(float(r["design_soiling_pct"]) for r in csv.DictReader(f))
    annex_b = {r["plant"]: r for r in csv.DictReader(open(a.annex_b, encoding="utf-8"))}
    strings = defaultdict(list)
    sid = {}
    for r in csv.DictReader(open(a.annex_a, encoding="utf-8")):
        s = M.StringDef(r["uid"], r["inverter"], float(r["kwp"]), float(r["tilt"]), float(r["azimuth"]),
                        r["module"], dt.date.fromisoformat(r["cod_date"]))
        strings[r["plant"]].append(s)
        sid[(r["plant"], s.uid)] = r["string_id"]
    report = {}
    for name in a.plant or sorted(plants):
        key, vendor = plants[name]["key"], plants[name]["vendor"]
        b = annex_b[name]
        pm = M.PlantModel(name, float(b["latitude"]), float(b["longitude"]),
                          tuple(float(b[k]) for k in ("b0", "b1", "b2", "b3")), soiling,
                          tuple(bias[name]), limits[name])
        lay = json.load(open(os.path.join(a.export_dir, "layouts", f"{key}.json"), encoding="utf-8"))
        sn_of = {i["inv"]: i["sn"] for i in lay["inverters"]}
        strs = strings[name]
        chan = {s.uid: channel_for(sid[(name, s.uid)], vendor) for s in strs}
        # register corrections proven from data (which input actually carries the string)
        for uid, idx in (plants[name].get("channel_overrides") or {}).items():
            chan[uid] = E.Channel(chan[uid].kind, int(idx))
        station, samples, ppv, ac, ctr = load_export(a.export_dir, key)
        strs_by_inv = defaultdict(list)
        for s in strs:
            strs_by_inv[s.inverter].append(s)
        daily_total = {}
        dpath = os.path.join(a.export_dir, "daily.csv")
        if os.path.exists(dpath):
            for r in csv.DictReader(open(dpath, encoding="utf-8")):
                if r["plant_key"] == key and fnum(r["energy_kwh"]) is not None:
                    daily_total[dt.date.fromisoformat(r["prod_date"])] = float(r["energy_kwh"])
        verdict, esum = energy_and_gaps(pm, strs_by_inv, sn_of, station, ctr, daily_total)
        # integrity check: sum of string powers vs inverter DC
        chk = {}
        for inv, sn in sn_of.items():
            num = den = 0.0
            n = 0
            for (s_sn, bk), smp in samples.items():
                if s_sn != sn or (sn, bk) not in ppv or ppv[(sn, bk)] < 5000:
                    continue
                tot = sum(p for p in (chan[s.uid].power_w(smp.str_a, smp.mppt_a, smp.mppt_v) for s in strs if s.inverter == inv) if p)
                num += tot
                den += ppv[(sn, bk)]
                n += 1
            chk[inv] = (num / den if den else None, n)
        res = E.run_plant(pm, strs, chan, sn_of, station, samples, MX, stability_filter=a.stability,
                          ac_power_w=ac, gap_verdict=verdict)
        kwp_inv = defaultdict(float)
        for s in strs:
            kwp_inv[s.inverter] += s.kwp
        daily = [t.epi for d, t in sorted(res.daily600.items()) if t.n >= 12 and t.epi is not None]
        sd = statistics.pstdev(daily) if len(daily) > 1 else None
        p600, p150 = res.epi600.get("plant", M.Tally()), res.epi150.get("plant", M.Tally())
        av = E.plant_availability(res, kwp_inv)
        print(f"\n=== {name} ({key}) {'stability filter ON' if a.stability else ''}")
        print(f"  stale inverter records dropped: {getattr(load_export, 'stale', {})}")
        print(f"  EPI-600 {fmt(p600.epi)}  n={p600.n}  band={M.band(p600.epi, p600.n)} | EPI-150 {fmt(p150.epi)} n={p150.n}"
              f" | Availability-150 {fmt(av)} | daily EPI-600 sd {fmt(sd)} over {len(daily)} days")
        for inv in sorted(sn_of):
            t6, t1, av_i = res.epi600.get(inv, M.Tally()), res.epi150.get(inv, M.Tally()), res.avail.get(inv, E.Avail())
            r, n = chk[inv]
            print(f"  {inv} {sn_of[inv]}: EPI-600 {t6.epi if t6.epi is not None else float('nan'):.4f} (n={t6.n})"
                  f"  EPI-150 {t1.epi if t1.epi is not None else float('nan'):.4f}  avail {av_i.value if av_i.value is not None else float('nan'):.4f}"
                  f" cov {av_i.coverage if av_i.coverage is not None else float('nan'):.3f} (up {av_i.up} down {av_i.down} missing {av_i.missing};"
                  f" by AC {av_i.by_ac_power}, counter up {av_i.up_by_counter} down {av_i.down_by_counter})"
                  f"  | strings/inverter DC {r if r else float('nan'):.4f} (n={n})")
        for inv in sorted(sn_of):
            sm = esum[inv]
            print(f"  {inv} energy: measured {sm['measured_kwh']:.0f} kWh ({sm['measured_slots']:.0f} slots), reconstructed "
                  f"{sm['reconstructed_kwh']:.0f} kWh ({sm['reconstructed_slots']:.0f} slots, {sm['gaps']:.0f} gaps: up {sm['gap_up']:.0f}"
                  f" down {sm['gap_down']:.0f} unknown {sm['gap_unknown']:.0f}), closed by the daily total {sm['closed_by_daily_kwh']:.0f} kWh"
                  f" ({sm['closed_by_daily_slots']:.0f} slots), still unknown {sm['unknown_slots']:.0f} slots")
        e_tot = sum(sm["measured_kwh"] + sm["reconstructed_kwh"] + sm["closed_by_daily_kwh"] for sm in esum.values())
        d_tot = sum(daily_total.values())
        if d_tot:
            print(f"  energy check: engine {e_tot:.1f} kWh vs daily_production {d_tot:.1f} kWh ({len(daily_total)} days): ratio {e_tot / d_tot:.4f}")
        low = sorted(((t.epi, k) for k, t in res.epi600.items() if ":" in k and t.epi is not None and t.n >= 50))
        print("  lowest strings EPI-600:", ", ".join(f"{k} {e:.3f}" for e, k in low[:6]))
        print("  excluded:", ", ".join(f"{k}={v}" for k, v in sorted(res.reasons.items())))
        report[name] = {"epi600": p600.epi, "n600": p600.n, "epi150": p150.epi, "avail": av, "daily_sd": sd,
                        "inverters": {inv: {"epi600": res.epi600.get(inv, M.Tally()).epi, "strings_vs_dc": chk[inv][0]} for inv in sn_of},
                        "strings": {k: {"epi600": t.epi, "n": t.n} for k, t in res.epi600.items() if ":" in k}}
    if a.json_out:
        json.dump(report, open(a.json_out, "w", encoding="utf-8"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
