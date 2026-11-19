#!/usr/bin/env python3
"""v379: acceptance test of the gap reconstruction on real complete days.

For every inverter-day of the export that is complete (a counter sample in every
daylight 5-minute slot, counter never backwards), masks a window of 1, 2 or 3 hours
starting at 09, 11, 13 or 15 h local, reconstructs it with argia.epi.gaps and
compares with the truth (the real counter increments):

  * day energy error  - must be exactly 0 (counter conservation);
  * window shape error - sum |E_rec,h - E_true,h| over the window's hours /
    true window energy, for three shapes:
      model   : the engine's expected power (station irradiance kept),
      clearsky: Haurwitz clear-sky (whole site dark, station lost too),
      flat    : uniform spread (the naive method, for comparison).

Prints per plant the median and 90th percentile of the shape error per method.
Inputs as scripts/epi600_month.py. No plant data in the repository.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.epi import engine as E   # noqa: E402
from argia.epi import gaps as G     # noqa: E402
from argia.epi import model as M    # noqa: E402
from argia.epi import sky           # noqa: E402

MX = dt.timezone(dt.timedelta(hours=-6))
WINDOWS = [(h, n) for h in (9, 11, 13, 15) for n in (1, 2, 3)]


def ts_parse(s):
    s = s.strip()
    return dt.datetime.fromisoformat(s + ":00" if s.endswith("+00") else s)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else float("nan")


def main(argv=None):
    ap = argparse.ArgumentParser()
    for a in ("--export-dir", "--annex-a", "--annex-b", "--annex-c", "--bias", "--limits", "--plants"):
        ap.add_argument(a, required=True)
    ap.add_argument("--plant", action="append")
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
    rc = 0
    for name in a.plant or sorted(plants):
        key = plants[name]["key"]
        b = annex_b[name]
        pm = M.PlantModel(name, float(b["latitude"]), float(b["longitude"]), tuple(float(b[k]) for k in ("b0", "b1", "b2", "b3")),
                          soiling, tuple(bias[name]), limits[name])
        sn_of = {i["inv"]: i["sn"] for i in json.load(open(os.path.join(a.export_dir, "layouts", f"{key}.json"), encoding="utf-8"))["inverters"]}
        inv_of = {v: k for k, v in sn_of.items()}
        st_rows, ctr, raw = defaultdict(list), defaultdict(dict), defaultdict(list)
        for r in csv.DictReader(open(os.path.join(a.export_dir, "telemetry.csv"), encoding="utf-8")):
            if r["plant_key"] != key:
                continue
            t = ts_parse(r["ts_utc"])
            bk = int(t.timestamp()) // 300
            f = lambda x: float(x) if x not in ("", None) else None
            st_rows[bk].append((t, f(r["irradiance_wm2"]), f(r["module_temp_c"]), f(r["ambient_temp_c"])))
            if r["inverter_sn"] in inv_of:
                raw[r["inverter_sn"]].append((bk, f(r["power_w"]), f(r["etoday_kwh"])))
        for sn, rs in raw.items():                     # stale API records are not truth (gaps.stale_buckets)
            bad = G.stale_buckets(rs)
            for bk, _, c in rs:
                if c is not None and bk not in bad:
                    ctr[sn][bk] = c
        station = {}
        for bk, rows in st_rows.items():
            vals = lambda i: [x[i] for x in rows if x[i] is not None]
            m = lambda v: sum(v) / len(v) if v else None
            station[bk] = E.Station(rows[0][0], m(vals(1)), m(vals(2)), m(vals(3)))
        qc = E.station_qc(station, pm.lat, pm.lon)
        errs = defaultdict(list)
        day_err = []
        n_days = 0
        for sn, cser in ctr.items():
            inv = inv_of[sn]
            strs = [s for s in strings[name] if s.inverter == inv]
            by_day = defaultdict(list)
            for bk in cser:
                by_day[dt.datetime.fromtimestamp(bk * 300, dt.timezone.utc).astimezone(MX).date()].append(bk)
            for day, bks in by_day.items():
                d0 = int(dt.datetime(day.year, day.month, day.day, tzinfo=MX).timestamp()) // 300
                slots = [k for k in range(d0, d0 + 288)
                         if sky.sun_position(dt.datetime.fromtimestamp(k * 300 + 150, dt.timezone.utc), pm.lat, pm.lon).zenith < 90]
                if not slots or any(k not in cser for k in [slots[0] - 1] + slots):
                    continue                               # only complete days are truth
                true = {k: cser[k] - cser[k - 1] for k in slots}
                if min(true.values()) < -G.COUNTER_TOL_KWH:
                    continue
                w_model, w_cs = {}, {}
                for k in slots:
                    t = dt.datetime.fromtimestamp(k * 300 + 150, dt.timezone.utc)
                    w_cs[k] = sky.clear_sky_ghi_haurwitz(sky.sun_position(t, pm.lat, pm.lon).zenith)
                    s = station.get(k)
                    if s is not None and k not in qc and E.station_valid(s)[0]:
                        w_model[k] = E.expected_inverter_w(pm, strs, s)[0]
                    else:
                        w_model[k] = w_cs[k] * sum(x.kwp for x in strs)       # fallback shape inside the model weights
                n_days += 1
                for h0, n in WINDOWS:
                    t0 = int(dt.datetime(day.year, day.month, day.day, h0, tzinfo=MX).timestamp()) // 300
                    masked = set(range(t0, t0 + 12 * n))
                    kept = [(k, cser[k]) for k in [slots[0] - 1] + slots if k not in masked]
                    true_win = sum(true[k] for k in masked if k in true)
                    if true_win < 1.0:
                        continue
                    for meth, w in (("model", w_model), ("clearsky", w_cs), ("flat", {k: 1.0 for k in slots})):
                        rec, _ = G.reconstruct_day(kept, slots, w)
                        known, unknown = G.day_energy_kwh(rec)
                        if meth == "model" and unknown == 0:
                            day_err.append(abs(known - (cser[slots[-1]] - cser[slots[0] - 1])))
                        recd = {s.bucket: s.energy_kwh for s in rec}
                        if any(recd.get(k, 0.0) is None for k in masked):
                            errs[meth + "_open"].append(1)      # no closing sample: stays UNKNOWN (rule 3)
                            continue
                        err = 0.0
                        for hh in range(n):
                            hk = range(t0 + 12 * hh, t0 + 12 * (hh + 1))
                            err += abs(sum(recd[k] for k in hk if k in recd) - sum(true[k] for k in hk if k in true))
                        errs[meth].append(err / true_win)
        print(f"== {name} ({key}): complete inverter-days {n_days}, masked windows {len(errs['model'])}, "
              f"max day-energy error {max(day_err) if day_err else float('nan'):.6f} kWh")
        for meth in ("model", "clearsky", "flat"):
            e = errs[meth]
            if e:
                print(f"   {meth:9} hourly shape error inside the gap: median {statistics.median(e):.1%}  p90 {pct(e, 0.9):.1%}"
                      f"  (open-ended windows left UNKNOWN: {len(errs[meth + '_open'])})")
        if day_err and max(day_err) > 1e-6:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
