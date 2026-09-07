"""Deep analysis: inverter output when hot vs not (92 days, 2026-07-01 .. 09-06).
Three methods, as Tomasz asked: (1) cooler-peer counterfactual per 5-min
interval with a per-inverter cool baseline (removes DC-size bias);
(2) the same inverter against itself at the same irradiance, module
temperature and hour (isolates inverter-internal heat from module heat);
(3) the empirical ratio-vs-temperature curve per inverter and fleet.
Outputs: results.json + PNG charts for the report."""
import json, math
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100",
     "magenta": "#e87ba4", "green": "#008300", "violet": "#4a3aa7", "red": "#e34948",
     "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e6e6e3", "surface": "#fcfcfb"}
SERIES = [C["blue"], C["orange"], C["aqua"], C["yellow"], C["magenta"], C["green"]]
plt.rcParams.update({"figure.facecolor": C["surface"], "axes.facecolor": C["surface"], "axes.edgecolor": C["grid"],
                     "axes.grid": True, "grid.color": C["grid"], "grid.linewidth": 0.8, "axes.spines.top": False,
                     "axes.spines.right": False, "font.size": 10, "text.color": C["ink"], "axes.labelcolor": C["ink2"],
                     "xtick.color": C["ink2"], "ytick.color": C["ink2"], "legend.frameon": False})

T_HOT, T_HIGH, T_CRIT = 65.0, 70.0, 75.0
COOL_MAX = 60.0           # intervals below this define the unit's own baseline
PEER_DT = 5.0             # a "cooler" peer is at least this much cooler
RUN_MIN = 0.15            # peers must run >= 15 % of DC rating to be a reference
CLIP = 0.97               # AC output >= 97 % of the AC rating = clipping, excluded
DERATE = 0.03             # an interval counts as derating when actual < 97 % of expected

t = pd.read_csv("telemetry_92d.csv", parse_dates=["ts_utc"])
inv = pd.read_csv("inverters.csv").set_index("inverter_sn")
pl = pd.read_csv("plants.csv").set_index("plant_key")
t["ts"] = t["ts_utc"].dt.tz_convert("America/Mexico_City")
t["bucket"] = t["ts"].dt.floor("5min")
t["rated_dc"] = t["inverter_sn"].map(inv["rated_kw_dc"])
t["rated_ac"] = t["inverter_sn"].map(inv["rated_kw"])
t = t[t["rated_dc"].notna() & (t["power_w"] > 0)].copy()
t["sp"] = t["power_w"] / (t["rated_dc"] * 1000.0)              # W per W_dc  (0..~0.9)
t["clip"] = t["power_w"] >= CLIP * t["rated_ac"] * 1000.0
t = t.drop_duplicates(["plant_key", "inverter_sn", "bucket"]).sort_values(["plant_key", "bucket", "inverter_sn"])
t["month"] = t["ts"].dt.strftime("%Y-%m")
t["hour"] = t["ts"].dt.hour

# ---------------------------------------------------------------- method 1: cooler peers
import os
rows = []
for (pk, b), g in ([] if os.path.exists("P.pkl") else t.groupby(["plant_key", "bucket"], sort=False)):
    if len(g) < 2:
        continue
    g = g.set_index("inverter_sn")
    for sn, r in g.iterrows():
        peers = g.drop(sn)
        running = peers[peers["sp"] >= RUN_MIN]
        ref_all = running["sp"].median() if len(running) else np.nan
        cooler = running[running["temperature_c"] <= r["temperature_c"] - PEER_DT]
        ref_cool = cooler["sp"].median() if len(cooler) else np.nan
        rows.append((pk, sn, b, r["temperature_c"], r["ambient_temp_c"], r["module_temp_c"], r["irradiance_wm2"],
                     r["sp"], r["power_w"], r["clip"], ref_all, ref_cool, len(cooler), r["month"], r["hour"],
                     peers["temperature_c"].median()))
P = pd.read_pickle("P.pkl") if os.path.exists("P.pkl") else pd.DataFrame(rows, columns=["pk", "sn", "b", "temp", "amb", "mod", "irr", "sp", "w", "clip", "ref_all", "ref_cool",
                                 "n_cool", "month", "hour", "peer_temp"])
P.to_pickle("P.pkl")
P["ratio_all"] = P["sp"] / P["ref_all"]
# per-inverter cool baseline: its usual ratio to ALL running peers when it is cool and running,
# PER HOUR OF DAY (v2 of the analysis: a unit shaded in the morning has a low all-day baseline
# that hides its midday derating — Plastic Omnium inverter 4), global median as the fallback
COOL_BASE = 65.0
cool = P[(P["temp"] < COOL_BASE) & (P["sp"] >= RUN_MIN) & P["ratio_all"].notna() & ~P["clip"]]
base = cool.groupby(["pk", "sn"])["ratio_all"].median().clip(0.5, 1.3)
base_h = cool.groupby(["pk", "sn", "hour"])["ratio_all"].agg(["median", "count"])
base_h = base_h[base_h["count"] >= 20]["median"].clip(0.5, 1.3)
P["baseline"] = pd.Series(P.set_index(["pk", "sn", "hour"]).index.map(base_h), index=P.index).astype(float)
P["baseline"] = P["baseline"].fillna(pd.Series(P.set_index(["pk", "sn"]).index.map(base), index=P.index).astype(float))
P["expected_sp"] = P["ref_cool"] * P["baseline"]
P["rel"] = P["sp"] / P["expected_sp"]                       # actual / counterfactual (cooler peers)
valid = P["expected_sp"].notna() & (P["expected_sp"] >= RUN_MIN) & ~P["clip"]
P["hot"] = P["temp"] >= T_HOT
P["loss_kwh"] = 0.0
m = valid & P["hot"]
P.loc[m, "loss_kwh"] = ((P.loc[m, "expected_sp"] - P.loc[m, "sp"]).clip(lower=0) * P.loc[m, "sn"].map(inv["rated_kw_dc"]) * (5 / 60))
P["derating"] = m & (P["rel"] < 1 - DERATE)

def hours(mask):
    return float(mask.sum()) * 5 / 60

summary = []
for (pk, sn), g in P.groupby(["pk", "sn"]):
    hot = g[g["hot"]]
    hv = g[m.loc[g.index]]
    rel_hot = hv["rel"].dropna()
    rel_cool = g[valid.loc[g.index] & (g["temp"] < COOL_MAX)]["rel"].dropna()
    # confidence: enough hot intervals with a reference and a clear separation
    n = len(rel_hot)
    mean_loss = float((1 - rel_hot).mean() * 100) if n else None
    se = float(rel_hot.std(ddof=1) / math.sqrt(n) * 100) if n > 2 else None
    loss = float(g["loss_kwh"].sum())
    net = float(((hv["expected_sp"] - hv["sp"]) * hv["sn"].map(inv["rated_kw_dc"]) * (5 / 60)).sum()) if len(hv) else 0.0
    energy = float((g["w"] * 5 / 60 / 1000).sum())
    summary.append({
        "plant": pk, "sn": sn, "label": inv.loc[sn, "inverter_label"], "rated_dc": float(inv.loc[sn, "rated_kw_dc"]),
        "baseline": None if pd.isna(base.get((pk, sn))) else round(float(base[(pk, sn)]), 3),
        "peak_c": float(g["temp"].max()),
        "h65": hours(g["temp"] >= T_HOT), "h70": hours(g["temp"] >= T_HIGH), "h75": hours(g["temp"] >= T_CRIT),
        "h_hot_measured": hours(m.loc[g.index]), "h_derating": hours(g["derating"]),
        "dt_peer_p95": float((hot["temp"] - hot["peer_temp"]).quantile(0.95)) if len(hot) else None,
        "mean_loss_pct": None if mean_loss is None else round(mean_loss, 1),
        "se_pct": None if se is None else round(se, 2),
        "p95_loss_pct": None if n < 10 else round(float((1 - rel_hot).quantile(0.95) * 100), 1),
        "cool_rel_pct": None if len(rel_cool) < 10 else round(float((1 - rel_cool).mean() * 100), 1),
        "lost_kwh": round(loss, 1), "net_kwh": round(net, 1), "energy_kwh": round(energy, 0),
        "lost_pct_of_energy": round(loss / energy * 100, 2) if energy else None,
        "n_hot_ref": n,
        "by_month": {mo: round(float(x), 1) for mo, x in g.groupby("month")["loss_kwh"].sum().items()},
    })
    mid = g[(g["hour"] >= 11) & (g["hour"] <= 15) & g["ratio_all"].notna() & (g["ref_all"] >= RUN_MIN) & ~g["clip"]]
    summary[-1]["midday"] = {
        "cool_lt65": (round(float(mid[mid["temp"] < 65]["ratio_all"].median()), 3) if (mid["temp"] < 65).sum() >= 20 else None),
        "n_cool": int((mid["temp"] < 65).sum()),
        "band_65_75": (round(float(mid[(mid["temp"] >= 65) & (mid["temp"] < 75)]["ratio_all"].median()), 3) if ((mid["temp"] >= 65) & (mid["temp"] < 75)).sum() >= 20 else None),
        "hot_ge75": (round(float(mid[mid["temp"] >= 75]["ratio_all"].median()), 3) if (mid["temp"] >= 75).sum() >= 20 else None),
        "n_hot": int((mid["temp"] >= 75).sum()),
    }
S = pd.DataFrame(summary)

# ---------------------------------------------------------------- method 3: ratio vs own temperature (all-peer reference, baseline-normalised)
bands = [(0, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 200)]
def band(tc):
    for lo, hi in bands:
        if lo <= tc < hi:
            return f"{lo}–{hi}" if hi < 200 else f"≥{lo}"
Pn = P[P["ratio_all"].notna() & (P["ref_all"] >= RUN_MIN) & ~P["clip"] & P["baseline"].notna()].copy()
Pn["norm"] = Pn["ratio_all"] / Pn["baseline"]
Pn["band"] = Pn["temp"].apply(band)
curve_fleet = Pn.groupby("band")["norm"].agg(["mean", "count"]).reindex([f"{lo}–{hi}" if hi < 200 else f"≥{lo}" for lo, hi in bands])
curve_inv = {f"{pk}/{sn}": g.groupby("band")["norm"].agg(["mean", "count"]).reindex(curve_fleet.index).round(4).to_dict("index")
             for (pk, sn), g in Pn.groupby(["pk", "sn"]) if (g["temp"] >= T_HIGH).sum() >= 24}
# cooler-peer counterfactual curve (the loss curve): rel by band, only hot bands have refs by construction
curve_cf = P[valid].assign(band=lambda d: d["temp"].apply(band)).groupby("band")["rel"].agg(["mean", "count"]).reindex(curve_fleet.index)

# ---------------------------------------------------------------- method 2: same inverter, same conditions
m2 = []
for (pk, sn), g in P.groupby(["pk", "sn"]):
    g = g[g["irr"].notna() & g["mod"].notna() & ~g["clip"] & (g["irr"] >= 300)].copy()
    if (g["temp"] >= T_HIGH).sum() < 24:
        continue
    g["ib"] = (g["irr"] // 100).astype(int)
    g["mb"] = (g["mod"] // 5).astype(int)
    cool = g[g["temp"] < T_HOT]; hot = g[g["temp"] >= T_HIGH]
    cs = cool.groupby(["ib", "mb"])["sp"].agg(["median", "count"])
    hs = hot.groupby(["ib", "mb"])["sp"].agg(["median", "count"])
    j = cs.join(hs, lsuffix="_c", rsuffix="_h", how="inner")
    j = j[(j["count_c"] >= 3) & (j["count_h"] >= 3)]
    if len(j) < 3:
        m2.append({"plant": pk, "sn": sn, "bins": int(len(j)), "loss_pct": None, "n_hot": int(j["count_h"].sum()) if len(j) else 0})
        continue
    w = j["count_h"]
    diff = float(((j["median_c"] - j["median_h"]) / j["median_c"] * w).sum() / w.sum() * 100)
    m2.append({"plant": pk, "sn": sn, "bins": int(len(j)), "loss_pct": round(diff, 1), "n_hot": int(w.sum()),
               "hot_c_mean": round(float(hot["temp"].mean()), 1), "cool_c_mean": round(float(cool["temp"].mean()), 1)})
M2 = pd.DataFrame(m2)

# ---------------------------------------------------------------- plant-wide heat vs unit-specific
plantheat = []
for pk, g in P[P["hot"]].groupby("pk"):
    dtp = g["temp"] - g["peer_temp"]
    plantheat.append({"plant": pk, "hot_intervals": int(len(g)), "share_unit_specific": round(float((dtp >= PEER_DT).mean() * 100), 1),
                      "median_dt_peer": round(float(dtp.median()), 1)})

# ---------------------------------------------------------------- money
S["tariff"] = S["plant"].map(pl["tariff_mxn_per_kwh"])
S["lost_mxn"] = (S["lost_kwh"] * S["tariff"].fillna(0)).round(0)
fleet = {"lost_kwh": round(float(S["lost_kwh"].sum()), 1), "lost_mxn": round(float(S["lost_mxn"].sum()), 0),
         "h65": round(float(S["h65"].sum()), 1), "h_derating": round(float(S["h_derating"].sum()), 1),
         "energy_kwh": round(float(S["energy_kwh"].sum()), 0),
         "days": int(P["b"].dt.date.nunique()), "intervals": int(len(P))}
by_plant = S.groupby("plant").agg(lost_kwh=("lost_kwh", "sum"), lost_mxn=("lost_mxn", "sum"), h65=("h65", "sum"),
                                  h_derating=("h_derating", "sum"), energy=("energy_kwh", "sum")).round(1)
by_plant["lost_pct"] = (by_plant["lost_kwh"] / by_plant["energy"] * 100).round(2)
monthly = P.groupby(["pk", "month"])["loss_kwh"].sum().unstack(fill_value=0).round(1)

out = {"fleet": fleet, "by_plant": by_plant.reset_index().to_dict("records"),
       "inverters": S.sort_values("lost_kwh", ascending=False).to_dict("records"),
       "curve_fleet": curve_fleet.round(4).to_dict("index"), "curve_cf": curve_cf.round(4).to_dict("index"),
       "curve_inv": curve_inv, "method2": M2.to_dict("records"), "plantheat": plantheat,
       "monthly": {pk: {mo: float(v) for mo, v in r.items()} for pk, r in monthly.iterrows()}}
json.dump(out, open("results.json", "w"), indent=1, default=str)
print(json.dumps(fleet)); print(by_plant); print(curve_fleet.round(3)); print(curve_cf.round(3)); print(M2); print(plantheat)
print(S[["plant", "label", "sn", "peak_c", "h65", "h70", "h75", "h_derating", "mean_loss_pct", "se_pct", "cool_rel_pct", "lost_kwh", "net_kwh", "lost_mxn", "n_hot_ref", "baseline"]].sort_values("lost_kwh", ascending=False).to_string())

# ---------------------------------------------------------------- charts
# 1. fleet curves
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=150)
x = np.arange(len(curve_fleet))
ax.plot(x, curve_fleet["mean"] * 100, color=C["blue"], lw=2, marker="o", ms=6, label="vs all peers, own baseline (method 3)")
ax.plot(x, curve_cf["mean"] * 100, color=C["orange"], lw=2, marker="o", ms=6, label="vs cooler peers (counterfactual, method 1)")
ax.axhline(100, color=C["ink2"], lw=1, ls=":")
ax.set_xticks(x); ax.set_xticklabels([f"{i} °C" for i in curve_fleet.index]); ax.set_ylabel("actual / expected, %")
ax.set_title("Fleet: output relative to peers by the inverter's own internal temperature", loc="left", fontsize=11, color=C["ink"])
for i, (mu, n) in enumerate(zip(curve_fleet["mean"], curve_fleet["count"])):
    ax.annotate(f"n={int(n):,}", (i, mu * 100), textcoords="offset points", xytext=(0, -14), ha="center", fontsize=8, color=C["ink2"])
ax.legend(loc="lower left", fontsize=8)
fig.tight_layout(); fig.savefig("chart_curve.png"); plt.close(fig)

# 2. NL1 hottest day: power + temperature per inverter
for pk in ("NL1", "SLP1", "MEX1"):
    g = P[P["pk"] == pk]
    if g.empty:
        continue
    day = g.groupby(g["b"].dt.date)["temp"].max().idxmax()
    d = g[g["b"].dt.date == day]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.2, 5.2), dpi=150, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    for i, (sn, s) in enumerate(d.groupby("sn")):
        lab = f"{inv.loc[sn, 'inverter_label']} ({sn})"
        a1.plot(s["b"].dt.hour + s["b"].dt.minute / 60, s["sp"] * 100, color=SERIES[i % 6], lw=1.6, label=lab)
        a2.plot(s["b"].dt.hour + s["b"].dt.minute / 60, s["temp"], color=SERIES[i % 6], lw=1.6)
    a1.set_ylabel("output, % of DC rating"); a2.set_ylabel("internal °C"); a2.set_xlabel("hour (MX)")
    a2.axhline(T_HIGH, color=C["ink2"], lw=1, ls=":"); a2.axhline(T_HOT, color=C["ink2"], lw=0.8, ls=":")
    a1.set_title(f"{pl.loc[pk, 'customer'].split('(')[0].split(' PPA')[0].title().strip()} — hottest day {day}: output and temperature per inverter", loc="left", fontsize=11)
    a1.legend(fontsize=8, loc="lower center", ncol=2)
    fig.tight_layout(); fig.savefig(f"chart_day_{pk}.png"); plt.close(fig)

# 3. loss by inverter (bar)
top = S[S["lost_kwh"] > 0].sort_values("lost_kwh", ascending=True)
if len(top):
    fig, ax = plt.subplots(figsize=(7.2, 0.35 * len(top) + 1.2), dpi=150)
    names = [f"{pl.loc[r.plant, 'customer'].split('(')[0].split(' PPA')[0].title().strip()} · {r.label}" for r in top.itertuples()]
    ax.barh(names, top["lost_kwh"], color=C["blue"], height=0.6)
    for i, v in enumerate(top["lost_kwh"]):
        ax.text(v, i, f" {v:,.0f} kWh", va="center", fontsize=8, color=C["ink2"])
    ax.set_xlabel("suspected thermal loss, kWh (92 days, cooler-peer counterfactual)")
    ax.set_title("Where the measured thermal loss is", loc="left", fontsize=11)
    fig.tight_layout(); fig.savefig("chart_loss.png"); plt.close(fig)
print("charts done")
