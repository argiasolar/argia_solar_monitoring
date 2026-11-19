"""v378 - EPI engine over 5-minute buckets: validity, station quality, availability,
roll-ups and the October 2026 report script. Invented plant only."""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import sys

import pytest

from argia.epi import engine as E
from argia.epi import model as M
from argia.epi import sky

V2 = pathlib.Path(__file__).resolve().parents[2]
UTC = dt.timezone.utc
MX = dt.timezone(dt.timedelta(hours=-6))
LAT, LON = 20.0, -100.0


def test_channel_power():
    str_a, mppt_a, mppt_v = [8.0, 9.0, 7.0], [17.0, 7.0], [700.0, 650.0]
    assert E.Channel("str", 2).power_w(str_a, mppt_a, mppt_v) == 9.0 * 700.0     # strings 1,2 -> MPPT 1
    assert E.Channel("str", 3).power_w(str_a, mppt_a, mppt_v) == 7.0 * 650.0     # string 3 -> MPPT 2
    assert E.Channel("pv", 2).power_w(str_a, mppt_a, [700.0, 690.0, 680.0]) == 9.0 * 690.0
    assert E.Channel("mppt", 1).power_w(str_a, mppt_a, mppt_v) == 17.0 * 700.0
    assert E.Channel("str", 9).power_w(str_a, mppt_a, mppt_v) is None             # input not reported
    assert E.Channel("pv", 1).power_w([None], [], [700.0]) is None
    assert E.Channel("str", 1).power_w([-0.2], [], [700.0]) == 0.0                 # sensor offset below 0


def st(b, ghi, t_panel=40.0, t_amb=25.0):
    return E.Station(dt.datetime.fromtimestamp(b * 300 + 150, UTC), ghi, t_panel, t_amb)


def test_station_quality_checks():
    noon = int(dt.datetime(2026, 3, 21, 18, 0, tzinfo=UTC).timestamp()) // 300
    s = {noon + i: st(noon + i, g) for i, g in enumerate([700, 700, 700, 700, 650, 651, 651, 651.3, 300, 1600])}
    bad = E.station_qc(s, LAT, LON)
    # 700 x4 is frozen from its 2nd value; 650/651/651/651.3 is not (only 3 equal after a change)
    assert [bad.get(noon + i) for i in range(10)] == [None, "ghi_frozen", "ghi_frozen", "ghi_frozen",
                                                      None, None, None, None, None, "ghi_out_of_range"]
    night = int(dt.datetime(2026, 3, 21, 6, 0, tzinfo=UTC).timestamp()) // 300   # 00:00 MX
    assert E.station_qc({night: st(night, 120.0)}, LAT, LON) == {night: "ghi_at_night"}
    ok, why = E.station_valid(st(noon, 700.0, t_panel=20.0, t_amb=25.0))
    assert not ok and why == "panel_sensor_below_ambient"
    assert E.station_valid(st(noon, None))[1] == "station_missing"


def _plant():
    b = (0.85, -0.0018, 0.31, -0.21)
    pm = M.PlantModel("Demo", LAT, LON, b, (1.0,) * 12, (1.0,) * 12, {"INV1": 50.0, "INV2": 50.0})
    strs = [M.StringDef("INV1:PV1", "INV1", 10.0, 5, 180, "JAM72S40605", dt.date(2026, 1, 1)),
            M.StringDef("INV1:PV2", "INV1", 10.0, 5, 180, "JAM72S40605", dt.date(2026, 1, 1)),
            M.StringDef("INV2:PV1", "INV2", 12.0, 10, 90, "JAM72S40605", dt.date(2026, 1, 1)),
            M.StringDef("INV2:PV2", "INV2", 12.0, 10, 90, "JAM72S40605", dt.date(2026, 12, 1))]   # not built yet
    chan = {s.uid: E.Channel("pv", int(s.uid[-1])) for s in strs}
    return pm, strs, chan, {"INV1": "SN1", "INV2": "SN2"}


def _day(pm, strs, perf):
    """One clear March day; every string delivers `perf` x its expected DC (V = 600 V)."""
    station, samples = {}, {}
    day = dt.date(2026, 3, 21)
    for k in range(12 * 7, 12 * 17):                       # 07:00-17:00 MX
        ts = dt.datetime(2026, 3, 21, tzinfo=MX) + dt.timedelta(minutes=5 * k + 2)
        b = int(ts.timestamp()) // 300
        z = sky.sun_position(ts, LAT, LON).zenith
        ghi = 1000 * max(0.0, __import__("math").cos(__import__("math").radians(z))) + 3 * (k % 7)   # never frozen
        station[b] = E.Station(ts.astimezone(UTC), ghi, 45.0, 25.0)
        for inv, sn in (("INV1", "SN1"), ("INV2", "SN2")):
            live = [s for s in strs if s.inverter == inv and s.cod <= day]
            es = []
            for s in live:
                g = sky.poa(ghi, ts, LAT, LON, s.tilt, s.azimuth).poa_global
                es.append(M.expected_string_w(s.kwp, M.degradation_factor(s.module, s.cod, day), g,
                                              M.pr0(pm.b, M.t_cell(45.0, g), g), 1.0, 1.0))
            sc = M.cap_scale(es, pm.ac_limit_kw[inv])
            cur = [0.0, 0.0]
            for s, e in zip(live, es):
                cur[int(s.uid[-1]) - 1] = e * sc * perf / 600.0
            samples[(sn, b)] = E.InvSample(cur, [], [600.0, 600.0])
    return station, samples


def test_known_performance_is_recovered_exactly():
    pm, strs, chan, sn_of = _plant()
    station, samples = _day(pm, strs, 0.95)
    res = E.run_plant(pm, strs, chan, sn_of, station, samples, MX)
    assert res.epi600["plant"].epi == pytest.approx(0.95, abs=1e-9)
    assert 0 < len(res.steps600) <= len(station)                # distinct 5-minute steps, for the 200-step rule
    assert res.epi150["INV2"].epi == pytest.approx(0.95, abs=1e-9)
    assert "INV2:PV2" not in res.epi600                     # before its COD: does not exist
    p = res.epi600["plant"]
    parts = [res.epi600[k] for k in ("INV1", "INV2")]
    assert p.real_wh == pytest.approx(sum(t.real_wh for t in parts))       # roll-up = sum, not mean
    assert res.avail["INV1"].value == 1.0 and res.avail["INV1"].coverage == 1.0


def test_down_missing_and_frozen_are_kept_apart():
    pm, strs, chan, sn_of = _plant()
    station, samples = _day(pm, strs, 1.0)
    bs = sorted(station)
    for b in bs[60:72]:                                      # INV2 off for an hour at midday
        samples[("SN2", b)] = E.InvSample([0.0, 0.0], [], [600.0, 600.0])
    for b in bs[30:36]:                                      # INV1 silent for 30 minutes
        del samples[("SN1", b)]
    g = station[bs[90]].ghi
    for b in bs[90:96]:                                      # station frozen for 30 minutes
        station[b] = E.Station(station[b].ts, g, 45.0, 25.0)
    res = E.run_plant(pm, strs, chan, sn_of, station, samples, MX)
    assert res.avail["INV2"].down == 12 and res.avail["INV1"].missing == 6 and res.avail["INV1"].down == 0
    assert res.reasons["ghi_frozen"] == 5 and res.reasons["inverter_not_producing"] == 12
    assert res.epi600["plant"].epi == pytest.approx(1.0, abs=1e-9)     # off and silent samples never enter EPI
    kwp = {"INV1": 20.0, "INV2": 12.0}
    assert E.plant_availability(res, kwp) == pytest.approx((20 * 1.0 + 12 * res.avail["INV2"].value) / 32)


def test_stability_filter_drops_ramps():
    pm, strs, chan, sn_of = _plant()
    station, samples = _day(pm, strs, 1.0)
    b = sorted(station)[60]
    station[b] = E.Station(station[b].ts, station[b].ghi * 1.3, 45.0, 25.0)   # cloud-edge spike
    on = E.run_plant(pm, strs, chan, sn_of, station, samples, MX, stability_filter=True)
    off = E.run_plant(pm, strs, chan, sn_of, station, samples, MX)
    assert on.reasons["unstable_irradiance"] >= 3          # the spike and its two neighbours
    assert on.epi600["plant"].epi == pytest.approx(1.0, abs=1e-9)
    assert off.epi600["plant"].epi < 1.0                   # the spike inflates expected without the filter


def test_month_script_on_an_export(tmp_path, capsys):
    pm, strs, chan, sn_of = _plant()
    station, samples = _day(pm, strs, 0.9)
    ex = tmp_path / "export"
    (ex / "layouts").mkdir(parents=True)
    with open(ex / "telemetry.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant_key", "inverter_sn", "ts_utc", "status", "power_w", "etoday_kwh", "irradiance_wm2", "module_temp_c", "ambient_temp_c"])
        for b, s in station.items():
            w.writerow(["DEMO", "SN1", s.ts.strftime("%Y-%m-%d %H:%M:%S+00"), 1, 0, 0, s.ghi, s.t_panel, s.t_amb])
    with open(ex / "strings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant_key", "inverter_sn", "ts_utc", "str_a", "mppt_a", "mppt_v"])
        for (sn, b), s in samples.items():
            t = dt.datetime.fromtimestamp(b * 300 + 100, UTC).strftime("%Y-%m-%d %H:%M:%S+00")
            w.writerow(["DEMO", sn, t, "{" + ",".join(str(x) for x in s.str_a) + "}", "{}", "{600,600}"])
    (ex / "detail.csv").write_text("plant_key,inverter_sn,ts_utc,ppv_w,pac_w,pf,derating_mode\n", encoding="utf-8")
    (ex / "layouts" / "DEMO.json").write_text(json.dumps({"inverters": [{"inv": "INV1", "sn": "SN1"}, {"inv": "INV2", "sn": "SN2"}]}), encoding="utf-8")
    with open(tmp_path / "A.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "inverter", "string_id", "uid", "kwp", "tilt", "azimuth", "module", "cod_date"])
        for s in strs:
            w.writerow(["Demo", s.inverter, s.uid.split(":")[1], s.uid, s.kwp, s.tilt, s.azimuth, s.module, s.cod.isoformat()])
    with open(tmp_path / "B.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "latitude", "longitude", "b0", "b1", "b2", "b3"])
        w.writerow(["Demo", LAT, LON, *pm.b])
    with open(tmp_path / "C.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "design_soiling_pct"])
        for m in range(1, 13):
            w.writerow([m, 1.0])
    (tmp_path / "bias.json").write_text(json.dumps({"bias": {"Demo": [1.0] * 12}}), encoding="utf-8")
    (tmp_path / "lim.json").write_text(json.dumps({"Demo": {"INV1": 50.0, "INV2": 50.0}}), encoding="utf-8")
    (tmp_path / "plants.json").write_text(json.dumps({"Demo": {"key": "DEMO", "vendor": "HUAWEI"}}), encoding="utf-8")
    sys.path.insert(0, str(V2 / "scripts"))
    import epi600_month as mon
    rc = mon.main(["--export-dir", str(ex), "--annex-a", str(tmp_path / "A.csv"), "--annex-b", str(tmp_path / "B.csv"),
                   "--annex-c", str(tmp_path / "C.csv"), "--bias", str(tmp_path / "bias.json"), "--limits", str(tmp_path / "lim.json"),
                   "--plants", str(tmp_path / "plants.json"), "--json-out", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    rep = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["Demo"]
    assert rc == 0 and "EPI-600 0.9000" in out
    assert rep["epi600"] == pytest.approx(0.9, abs=1e-9)
