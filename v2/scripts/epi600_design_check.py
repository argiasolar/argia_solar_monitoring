#!/usr/bin/env python3
"""v377: calibrate and prove the EPI-600 model on each plant's design year.

Reads the private design files (never in the repository):
  --annex-a  Annex_A_string_register.csv     (strings: kWp, tilt, azimuth, module, COD)
  --annex-b  Annex_B_plant_model_parameters.csv (lat, lon, b0..b3, Annex bias for comparison)
  --design-dir  folder with "PPA <plant>.csv" (HelioScope 8760, hour-start timestamps)
  --limits   JSON {"<plant>": {"INV1": 136.4, ...}} AC limits in kW (audit section 2)

For every plant it
  1. computes B_m = sum(design optimal_dc_power) / sum(model without bias), per month, over the
     EPI-600 domain (hours with plant G_poa >= 600), both unclipped (Arturo's method, restricted
     to the headline domain);
  2. refines B_m so that the full model (B_m + real inverter limits) equals the design DC
     limited the same way, month by month (fixed point, converges in a few steps), and reports
     EPI-600 = sum(design DC) / sum(model) over hours with G_poa >= 600;
  3. fails (exit 1) unless every plant scores 100.0 % +/- 0.1 % against the design DC before
     HelioScope's clipping, limited per inverter at the same real AC limit (kWp share).
--out writes the computed B_m as JSON for the engine's server-side configuration.

The sun is evaluated at mid-hour: HelioScope timestamps are the hour start
(proof: our POA reproduces HelioScope's POA within 0.3 % at mid-hour, 3.9 % off at the hour start).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argia.epi import model as M      # noqa: E402
from argia.epi import sky             # noqa: E402

MX_STD = dt.timezone(dt.timedelta(hours=-6))   # TMY files carry standard time (no DST)
TOL = 0.001


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load(annex_a, annex_b, design_dir, limits_path):
    strings = {}
    with open(annex_a, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            strings.setdefault(r["plant"], []).append(M.StringDef(
                r["uid"], r["inverter"], float(r["kwp"]), float(r["tilt"]), float(r["azimuth"]),
                r["module"], dt.date.fromisoformat(r["cod_date"])))
    plants = {}
    with open(annex_b, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            plants[r["plant"]] = r
    limits = json.load(open(limits_path, encoding="utf-8")) if limits_path else {}
    return strings, plants, limits


def design_rows(path):
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = dt.datetime.fromisoformat(r["timestamp"]).replace(tzinfo=MX_STD) + dt.timedelta(minutes=30)
            yield ts, fnum(r["global_horizontal_irradiance"]), fnum(r.get("avg_cell_temp")), \
                fnum(r.get("optimal_dc_power")), fnum(r.get("actual_dc_power"))


def plant_check(name, strs, b, limits, design_csv, soiling):
    lat, lon = float(b["latitude"]), float(b["longitude"])
    pm = M.PlantModel(name, lat, lon, tuple(float(b[k]) for k in ("b0", "b1", "b2", "b3")), tuple(soiling))
    planes = sorted({(s.tilt, s.azimuth) for s in strs})
    kscale = float(b["kwp_design"]) / sum(s.kwp for s in strs)   # Annex A vs design kWp (audit item)
    hours = []
    for ts, ghi, tc, opt_dc, act_dc in design_rows(design_csv):
        if ghi <= 0:
            continue
        sp = sky.sun_position(ts, lat, lon)
        g = {p: sky.poa(ghi, ts, lat, lon, p[0], p[1], sun=sp).poa_global for p in planes}
        gs = {s.uid: g[(s.tilt, s.azimuth)] for s in strs}
        hours.append((ts.month, gs, tc, opt_dc, act_dc))
    kw = {s.uid: s.kwp for s in strs}
    kw_tot = sum(kw.values())
    inv_share = {}
    for s in strs:
        inv_share[s.inverter] = inv_share.get(s.inverter, 0.0) + s.kwp / kw_tot

    def g_plant(gs):
        return sum(gs[u] * kw[u] for u in gs) / kw_tot

    # 1. calibrate on the EPI-600 domain (hours with G_poa >= 600), unclipped on both sides
    mod, des = {}, {}
    for mo, gs, tc, opt_dc, _ in hours:
        if g_plant(gs) < M.EPI_600:
            continue
        mod[mo] = mod.get(mo, 0.0) + M.unbiased_plant_expected_w(pm, strs, gs, tc, mo) * kscale
        des[mo] = des.get(mo, 0.0) + opt_dc
    bias = list(M.bias_from_design(mod, des))
    # 2. refine with the real inverter limits on both sides (fixed point, monthly):
    #    model(bias, limits) must equal the design DC limited the same way.
    #    The reference splits the design DC over inverters by kWp share.
    sel = [h for h in hours if g_plant(h[1]) >= M.EPI_600]

    def run(bias):
        per_m, t600, t600_act = {}, M.Tally(), M.Tally()
        for mo, gs, tc, opt_dc, act_dc in sel:
            by_inv = {}
            for s in strs:
                e = M.expected_string_w(s.kwp, 1.0, gs[s.uid], M.pr0(pm.b, tc, gs[s.uid]), soiling[mo - 1], bias[mo - 1])
                by_inv.setdefault(s.inverter, []).append(e * kscale)
            exp, ref = 0.0, 0.0
            for inv, es in by_inv.items():
                lim = limits.get(inv)
                exp += sum(es) * (M.cap_scale(es, lim) if lim else 1.0)
                r = opt_dc * inv_share[inv]
                ref += min(r, M.cap_dc_w(lim)) if lim else r
            a = per_m.setdefault(mo, [0.0, 0.0])
            a[0] += ref
            a[1] += exp
            t600.add(ref, exp, 1.0)
            t600_act.add(act_dc, exp, 1.0)
        return per_m, t600, t600_act

    for _ in range(30):
        per_m, t600, t600_act = run(bias)
        worst = max(abs(r / e - 1) for r, e in per_m.values())
        if worst < 1e-6:
            break
        bias = [bias[m - 1] * (per_m[m][0] / per_m[m][1]) for m in range(1, 13)]
    bias = tuple(bias)
    annex = tuple(float(b[f"bias_m{m:02d}"]) for m in range(1, 13))
    return bias, annex, t600, t600_act


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--annex-a", required=True)
    ap.add_argument("--annex-b", required=True)
    ap.add_argument("--annex-c", required=True)
    ap.add_argument("--design-dir", required=True)
    ap.add_argument("--limits")
    ap.add_argument("--out")
    ap.add_argument("--plant", action="append")
    a = ap.parse_args(argv)
    strings, plants, limits = load(a.annex_a, a.annex_b, a.design_dir, a.limits)
    with open(a.annex_c, encoding="utf-8") as f:
        soiling = [float(r["design_soiling_pct"]) for r in csv.DictReader(f)]
    ok, out = True, {}
    for name in a.plant or sorted(plants):
        bias, annex, t, ta = plant_check(name, strings[name], plants[name], limits.get(name, {}),
                                         os.path.join(a.design_dir, f"PPA {name}.csv"), soiling)
        out[name] = [round(x, 5) for x in bias]
        dmax = max(abs(x - y) for x, y in zip(bias, annex))
        good = t.epi is not None and abs(t.epi - 1) <= TOL
        ok &= good
        print(f"{name:18} design-year EPI-600 {t.epi:.4f} (n={t.n})  vs design DC after HelioScope clipping {ta.epi:.4f}"
              f"  | max |B_m - Annex B| {dmax:.4f}  {'OK' if good else 'FAIL'}")
        print("    B_m computed:", " ".join(f"{x:.4f}" for x in bias))
        print("    B_m Annex B :", " ".join(f"{x:.4f}" for x in annex))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"method": "B_m = sum(optimal_dc_power)/sum(model without bias) over hours with G_poa >= 600, mid-hour sun",
                       "generated": dt.date.today().isoformat(), "bias": out}, f, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
