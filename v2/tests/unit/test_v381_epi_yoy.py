"""v381 - year-over-year report: satellite-driven expected energy, day selection, month sums."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import sys

import pytest

from argia.epi import model as M
from argia.epi import sky

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
import epi600_yoy as Y      # noqa: E402

LAT, LON = 20.0, -100.0


def _files(tmp_path, energy_factor=0.9):
    strs = [("INV1", "PV1", 10.0, 5, 180), ("INV1", "PV2", 10.0, 5, 180)]
    with open(tmp_path / "A.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "inverter", "string_id", "uid", "kwp", "tilt", "azimuth", "module", "cod_date"])
        for inv, sid, kwp, tilt, az in strs:
            w.writerow(["Demo", inv, sid, f"{inv}:{sid}", kwp, tilt, az, "JAM72S40605", "2025-01-01"])
    with open(tmp_path / "B.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "latitude", "longitude", "b0", "b1", "b2", "b3", "tcell_fallback_k_K_per_kWm2"])
        w.writerow(["Demo", LAT, LON, 0.85, -0.0018, 0.31, -0.21, 30.0])
    with open(tmp_path / "C.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "design_soiling_pct"])
        for m in range(1, 13):
            w.writerow([m, 1.0])
    with open(tmp_path / "PPA Demo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "dry_bulb_temperature"])
        for m in range(1, 13):
            for h in range(24):
                w.writerow([f"2002-{m:02d}-15 {h:02d}:00:00", 20.0 + h * 0.5])
    (tmp_path / "bias.json").write_text(json.dumps({"bias": {"Demo": [1.0] * 12}}), encoding="utf-8")
    (tmp_path / "lim.json").write_text(json.dumps({"Demo": {"INV1": 50.0}}), encoding="utf-8")
    (tmp_path / "plants.json").write_text(json.dumps({"Demo": {"key": "DEMO", "vendor": "GROWATT"}}), encoding="utf-8")
    sat = tmp_path / "sat"
    sat.mkdir()
    times, vals = [], []
    t = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
    while t < dt.datetime(2025, 6, 4, tzinfo=dt.timezone.utc):
        mid = t + dt.timedelta(minutes=30)                 # value at t+1h = mean of (t, t+1h]
        z = sky.sun_position(mid, LAT, LON).zenith
        times.append((t + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        vals.append(None if (t.day == 3 and t.hour == 18) else round(max(0.0, 900 * math.cos(math.radians(z))), 1))
        t += dt.timedelta(hours=1)
    (sat / "Demo.json").write_text(json.dumps({"hourly": {"time": times, "shortwave_radiation": vals}}), encoding="utf-8")
    return strs, times, vals


def test_month_ratio_is_measured_over_expected(tmp_path, capsys):
    strs, times, vals = _files(tmp_path)
    # expected energy per local day, computed independently with the same functions
    pm = M.PlantModel("Demo", LAT, LON, (0.85, -0.0018, 0.31, -0.21), (1.0,) * 12, (1.0,) * 12, {"INV1": 50.0})
    sdefs = [M.StringDef(f"{i}:{s}", i, k, t, a, "JAM72S40605", dt.date(2025, 1, 1)) for i, s, k, t, a in strs]
    tamb = Y.t_amb_climatology(str(tmp_path / "PPA Demo.csv"))
    exp = {}
    for ts, v in zip(times, vals):
        mid = dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone.utc) - dt.timedelta(minutes=30)
        if v is None or sky.sun_position(mid, LAT, LON).zenith >= 90:
            continue
        d = mid.astimezone(Y.MX).date()
        exp[d] = exp.get(d, 0.0) + Y.hourly_expected_ac_kwh(pm, sdefs, 30.0, tamb, mid, v)
    with open(tmp_path / "daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant_key", "prod_date", "energy_kwh", "irradiance_kwh_m2", "data_class", "source"])
        w.writerow(["DEMO", "2025-06-01", round(exp[dt.date(2025, 6, 1)] * 0.9, 6), 6.0, "", "v1"])
        w.writerow(["DEMO", "2025-06-02", round(exp[dt.date(2025, 6, 2)] * 0.9, 6), 6.0, "full", "v2"])
        w.writerow(["DEMO", "2025-06-03", 50.0, 6.0, "full", "v2"])          # a satellite hour is missing: day dropped
        w.writerow(["DEMO", "2024-12-31", 50.0, 6.0, "", "v1"])               # before COD: dropped
    rc = Y.main(["--daily", str(tmp_path / "daily.csv"), "--sat-dir", str(tmp_path / "sat"), "--design-dir", str(tmp_path),
                 "--annex-a", str(tmp_path / "A.csv"), "--annex-b", str(tmp_path / "B.csv"), "--annex-c", str(tmp_path / "C.csv"),
                 "--bias", str(tmp_path / "bias.json"), "--limits", str(tmp_path / "lim.json"),
                 "--plants", str(tmp_path / "plants.json"), "--json-out", str(tmp_path / "y.json")])
    r = json.loads((tmp_path / "y.json").read_text(encoding="utf-8"))["Demo"]
    assert rc == 0 and list(r) == ["2025-06"]
    assert r["2025-06"]["days"] == 2 and r["2025-06"]["epi_sat"] == pytest.approx(0.9, abs=1e-6)
    assert r["2025-06"]["pr_station"] == pytest.approx(r["2025-06"]["energy_kwh"] / (12.0 * 20.0))


def test_strings_before_cod_add_nothing():
    pm = M.PlantModel("Demo", LAT, LON, (0.85, -0.0018, 0.31, -0.21), (1.0,) * 12, (1.0,) * 12, {})
    late = [M.StringDef("INV1:PV1", "INV1", 10.0, 5, 180, "JAM72S40605", dt.date(2030, 1, 1))]
    mid = dt.datetime(2026, 3, 21, 18, 0, tzinfo=dt.timezone.utc)
    assert Y.hourly_expected_ac_kwh(pm, late, 30.0, {(3, 12): 25.0}, mid, 900.0) == 0.0
