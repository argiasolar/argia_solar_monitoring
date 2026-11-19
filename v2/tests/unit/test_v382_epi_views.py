"""v382 - troubleshooting views (spec v0.7 section 8): rules on an invented plant,
the card HTML, the server configuration loader, the row feed and the per-day cache.
Invented plant and serials only (the repository is public)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import re

import pytest

from argia.epi import config as C
from argia.epi import engine as E
from argia.epi import feed as F
from argia.epi import live as L
from argia.epi import model as M
from argia.epi import views as V
from argia.report import epi_views_html as H

V2 = pathlib.Path(__file__).resolve().parents[2]
UTC = dt.timezone.utc
MX = dt.timezone(dt.timedelta(hours=-6))
LAT, LON = 20.0, -100.0
DAY = dt.date(2026, 3, 21)
MOD = "JAM72S40605"
N_STR = 6
INVS = (1, 2, 3, 4)
EM = chr(0x2014)                     # the em dash, never written literally in this repo


def plant():
    pm = M.PlantModel("Demo", LAT, LON, (0.85, -0.0018, 0.31, -0.21), (1.0,) * 12, (1.0,) * 12,
                      {f"INV{i}": 80.0 for i in INVS})
    strs = [M.StringDef(f"INV{i}:S{k}", f"INV{i}", 10.0, 5, 180, MOD, dt.date(2026, 1, 1))
            for i in INVS for k in range(1, N_STR + 1)]
    chan = {s.uid: E.Channel("str", int(s.uid.split(":S")[1])) for s in strs}
    return pm, strs, chan, {f"INV{i}": f"SN{i}" for i in INVS}


def local(hh, mm):
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=MX)


def synth(perf, day=DAY):
    """A clear day 07:00-18:00 MX; string s delivers perf(uid, local time) x its design DC at 600 V.
    Also the AC power and day counter of each inverter (AC = DC x 0.985)."""
    pm, strs, chan, sn_of = plant()
    station, samples, ac, tel = {}, {}, {}, []
    t0 = dt.datetime(day.year, day.month, day.day, 7, 0, tzinfo=MX)
    ctr = {sn: 0.0 for sn in sn_of.values()}
    for k in range(12 * 11):
        ts = t0 + dt.timedelta(minutes=5 * k, seconds=150)
        b = int(ts.timestamp()) // 300
        from argia.epi import sky
        z = sky.sun_position(ts, LAT, LON).zenith
        ghi = max(0.0, 1000 * math.cos(math.radians(z))) + 3 * (k % 7)          # never frozen
        st = E.Station(ts.astimezone(UTC), ghi, 45.0, 25.0)
        station[b] = st
        for inv, sn in sn_of.items():
            mine = [s for s in strs if s.inverter == inv]
            _tot, per = E.expected_inverter_w(pm, mine, st)
            cur = [0.0] * N_STR
            for s, _g, e in per:
                cur[chan[s.uid].index - 1] = e * perf(s.uid, ts.astimezone(MX)) / 600.0
            samples[(sn, b)] = E.InvSample(cur, [], [600.0] * (N_STR // 2))
            p_ac = sum(cur) * 600.0 * 0.985
            ac[(sn, b)] = p_ac
            ctr[sn] += p_ac / 12000.0
            tel.append((sn, ts.timestamp(), p_ac, ctr[sn], ghi, 45.0, 25.0, cur))
    return (pm, strs, chan, sn_of), station, samples, ac, tel


def faults(uid, t):
    """INV1: one string at 0.70. INV2: strings 3, 4, 6 dead, string 5 at 1.25 (a wrong kWp in
    the register, say). INV3: whole inverter at 0.50 from 11:00 to 11:30. INV4: string 1 at
    0.50 before 08:30 (row shading at low sun, G_poa below 600: not a string fault).
    13:00-13:30: everything at 0.50 (cloud over the plant, not over the station).
    Inverter ratios actual/design: INV1 5.7/6 = 0.95, INV2 3.25/6 = 0.542, INV3 1.0, INV4 1.0."""
    k = 0.5 if local(13, 0) <= t < local(13, 30) else 1.0
    if uid == "INV1:S2":
        return 0.70 * k
    if uid in ("INV2:S3", "INV2:S4", "INV2:S6"):
        return 0.0
    if uid == "INV2:S5":
        return 1.25 * k
    if uid.startswith("INV3") and local(11, 0) <= t < local(11, 30):
        return 0.50 * k
    if uid == "INV4:S1" and t < local(8, 30):
        return 0.50
    return k


@pytest.fixture(scope="module")
def payload():
    (pm, strs, chan, sn_of), station, samples, ac, _tel = synth(faults)
    return V.build(pm, strs, chan, sn_of, station, samples, ac, DAY, MX,
                   workorder={"INV1:S2": "INV1 SN1, MPPT 1, input 2 (DEMO_INV1_MPPT1_C2)"})


def test_inverter_gaps_against_peers(payload):
    inv = {i["inv"]: i for i in payload["inverters"]}
    assert inv["INV1"]["gaps"] == [] and inv["INV4"]["gaps"] == []   # 0.95 of peers is inside 0.85
    g = inv["INV3"]["gaps"]
    assert len(g) == 1, g                          # the plant-wide 13:00 dip is not flagged
    assert (g[0]["from"], g[0]["to"]) == ("11:00", "11:25")
    # peers 11:00: ratios 0.95, 0.542, 0.5, 1.0 -> median 0.746; 0.5 / 0.746 - 1 = -33 %
    assert g[0]["pct_peers"] == -33 and g[0]["pct_design"] == -50
    g2 = inv["INV2"]["gaps"]                       # half the inverter dead: one run all day
    assert len(g2) == 1 and g2[0]["pct_peers"] in (-45, -44, -43)
    n_design = sum(1 for d in inv["INV2"]["design"] if d is not None and d >= V.GAP_MIN_WKWP)
    assert g2[0]["i1"] - g2[0]["i0"] + 1 == n_design


def test_strings_low_high_and_dead(payload):
    s1 = payload["strings"]["INV1"]
    assert s1["zero"] == [] and [(o["uid"], o["pct"]) for o in s1["out"]] == [("INV1:S2", -30)]
    assert s1["wo"][1] == "INV1 SN1, MPPT 1, input 2 (DEMO_INV1_MPPT1_C2)"
    s2 = payload["strings"]["INV2"]
    assert s2["zero"] == ["INV2:S3", "INV2:S4", "INV2:S6"]
    # the dead strings are out of the median: with them it would be 0.5 and S1, S2 at 1.0
    # would show as +100 %; without them only S5 is outside
    assert [(o["uid"], o["pct"]) for o in s2["out"]] == [("INV2:S5", 25)]
    assert payload["strings"]["INV3"]["out"] == []   # 30 min at 0.5 hits all its strings alike
    assert payload["strings"]["INV4"]["out"] == [] and payload["strings"]["INV4"]["zero"] == []   # shading not flagged
    assert [i["producing"] for i in payload["inverters"]] == [6, 3, 6, 6]


def test_band_is_drawn_from_raw_values(payload):
    s = payload["strings"]["INV4"]
    k = payload["t"].index("12:00")
    raw = sorted(w[k] for w in s["w"])
    assert s["median"][k] == pytest.approx((raw[2] + raw[3]) / 2, abs=1)    # values are whole W/kWp
    assert s["lo"][k] == pytest.approx(0.9 * s["median"][k], abs=1)
    assert s["hi"][k] == pytest.approx(1.1 * s["median"][k], abs=1)


def test_design_curve_and_day_epi(payload):
    inv1 = next(i for i in payload["inverters"] if i["inv"] == "INV1")
    k = payload["t"].index("12:00")
    # INV1 strings at 1.0 except S2 at 0.70 -> actual/design = 5.7 / 6
    assert inv1["w"][k] / inv1["design"][k] == pytest.approx(5.7 / 6, rel=2e-3)
    assert payload["plant"]["epi600_day"] is not None


def test_apply_mtd_adds_earlier_days(payload):
    p = json.loads(json.dumps(payload))
    one = V.apply_mtd(json.loads(json.dumps(p)), None, 1)
    two = V.apply_mtd(json.loads(json.dumps(p)), p["tallies"], 2)
    assert one["plant"]["epi600_mtd"] == two["plant"]["epi600_mtd"] == p["plant"]["epi600_day"]
    assert two["plant"]["steps600_mtd"] == 2 * one["plant"]["steps600_mtd"]
    assert two["plant"]["mtd_days"] == 2
    for a, b in zip(one["inverters"], two["inverters"]):
        assert a["epi600_mtd"] == b["epi600_mtd"] and b["steps600_mtd"] == 2 * a["steps600_mtd"]


def test_card_html(payload):
    p = V.apply_mtd(json.loads(json.dumps(payload)), None, 1)
    p["plant"]["mtd_days_expected"] = 3
    h = H.card("DEMO", p, live=True)
    assert EM not in h                        # no em dash anywhere
    assert "1 of 3 days so far" in h and "1 de 3 días por ahora" in h
    assert h.count("data-en=") == h.count("data-es=")
    blob = re.search(r'<script type="application/json" class="evdata">(.*?)</script>', h, re.S).group(1)
    got = json.loads(blob.replace("<\\/", "</"))
    assert "tallies" not in got and got["inverters"][2]["gaps"][0]["from"] == "11:00"
    assert EM not in H.pending_card()


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def config_dirs(tmp_path, overrides=None):
    epi, lay = tmp_path / "epi", tmp_path / "layouts"
    epi.mkdir(parents=True)
    lay.mkdir(parents=True)
    _, strs, _, sn_of = plant()
    _write(epi / "Annex_A_string_register.csv",
           [{"plant": "Demo", "uid": s.uid, "inverter": s.inverter, "string_id": "Istr" + s.uid.split(":S")[1],
             "kwp": s.kwp, "tilt": s.tilt, "azimuth": s.azimuth, "module": s.module, "cod_date": s.cod.isoformat()}
            for s in strs])
    _write(epi / "Annex_B_plant_model_parameters.csv",
           [{"plant": "Demo", "latitude": LAT, "longitude": LON, "b0": 0.85, "b1": -0.0018, "b2": 0.31, "b3": -0.21}])
    _write(epi / "Annex_C_design_soiling_profile.csv", [{"month": m, "design_soiling_pct": 1.0} for m in range(1, 13)])
    (epi / "bias_v1.json").write_text(json.dumps({"bias": {"Demo": [1.0] * 12}}))
    (epi / "limits_v1.json").write_text(json.dumps({"Demo": {f"INV{i}": 80.0 for i in INVS}}))
    (epi / "plants_v1.json").write_text(json.dumps(
        {"Demo": {"key": "DEMO", "vendor": "GROWATT", "channel_overrides": overrides or {}}}))
    (lay / "DEMO.json").write_text(json.dumps(
        {"plant": "DEMO", "inverters": [{"inv": i, "sn": sn} for i, sn in sn_of.items()],
         "strings": [{"sn": "SN1", "ch": "s2", "mppt": 1, "name": "DEMO_INV1_MPPT1_C2"}]}))
    return str(epi), str(lay)


def test_config_loader(tmp_path):
    epi, lay = config_dirs(tmp_path, overrides={"INV1:S6": 5})
    cfg = C.load(epi, lay)["DEMO"]
    assert cfg.name == "Demo" and cfg.vendor == "GROWATT" and cfg.sn_of["INV3"] == "SN3"
    assert cfg.channel_of["INV1:S2"] == E.Channel("str", 2)
    assert cfg.channel_of["INV1:S6"] == E.Channel("str", 5)          # register correction applied
    assert cfg.workorder["INV1:S2"] == "INV1 SN1, MPPT 1, input 2 (DEMO_INV1_MPPT1_C2)"
    assert cfg.workorder["INV2:S3"] == "INV2 SN2, MPPT 2, input 3"    # MPPT from the Growatt wiring 2i-1, 2i
    assert cfg.pm.ac_limit_kw["INV1"] == 80.0 and len(cfg.strings) == len(INVS) * N_STR
    assert C.channel_for("PV7", "HUAWEI") == E.Channel("pv", 7)
    assert C.channel_for("MPPT2", "GROWATT") == E.Channel("mppt", 2)


def test_config_incomplete_folder_means_no_card(tmp_path):
    epi, lay = config_dirs(tmp_path)
    pathlib.Path(epi, "bias_v1.json").unlink()
    assert C.load(epi, lay) == {}
    epi2, lay2 = config_dirs(tmp_path / "b")
    pathlib.Path(lay2, "DEMO.json").unlink()
    assert C.load(epi2, lay2) == {}                                  # no layout -> plant not configured


def test_feed():
    st = F.station_from_rows([(1000.0, 500.0, 40.0, 25.0), (1100.0, 520.0, None, 26.0)])
    (b, s), = st.items()
    assert b == 3 and s.ghi == 510.0 and s.t_panel == 40.0 and s.t_amb == 25.5
    sm = F.samples_from_rows([("A", 1190.0, [2.0], [], [600.0]), ("A", 1150.0, [1.0], [], [600.0])])
    assert sm[("A", 3)].str_a == [2.0]                                 # the last in time wins
    rows = [("A", 300.0 * k, 5000.0, 10.0) for k in range(10, 16)]      # same power and counter: stale
    ac, ctr, stale = F.ac_and_counters(rows + [("B", 3000.0, 100.0, 1.0)])
    assert stale["A"] > 0 and stale["B"] == 0 and ac[("B", 10)] == 100.0


class FakeDB:
    """Answers load_day's two SELECTs from synthetic rows, counting calls."""

    def __init__(self, days):
        self.rows = {}
        for d in days:
            *_x, tel = synth(lambda u, t: 1.0, d)
            self.rows[d] = tel
        self.calls = 0

    def __call__(self, sql):
        assert sql.lstrip().upper().startswith("SET STATEMENT_TIMEOUT") and "SELECT" in sql
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", sql.upper())
        self.calls += 1
        t0 = dt.datetime.fromisoformat(re.search(r"ts_utc >= '([^']+)'", sql).group(1).replace("+00", "+00:00"))
        day = t0.astimezone(MX).date()
        out = []
        for sn, ts, p, c, g, tp, ta, cur in self.rows.get(day, []):
            if "FROM telemetry" in sql:
                out.append([sn, str(ts), str(p), str(c), str(g), str(tp), str(ta)])
            else:
                out.append([sn, str(ts), ",".join(map(str, cur)), "", ",".join(["600"] * (N_STR // 2))])
        return out


def test_live_cache_budget_and_mtd(tmp_path):
    epi, lay = config_dirs(tmp_path)
    cfgs = C.load(epi, lay)
    fp = {"DEMO": L.fingerprint(epi, lay, "DEMO")}
    days = [dt.date(2026, 3, d) for d in (1, 2, 3, 4)]
    db = FakeDB(days)
    now = dt.datetime(2026, 3, 4, 12, 0, tzinfo=MX)
    cache = str(tmp_path / "cache")
    b = L.Builder(db, cfgs, MX, now, fp, cache_dir=cache, fill_per_run=2)
    p = b.card_payload("DEMO", days[3])                               # today: always computed
    assert p["plant"]["mtd_days"] == 3 and p["plant"]["mtd_days_expected"] == 4   # budget 2: Mar 1, 2
    assert sorted(x.name for x in (tmp_path / "cache" / "DEMO").iterdir()) == ["2026-03-01.json", "2026-03-02.json"]
    calls = db.calls
    b2 = L.Builder(db, cfgs, MX, now, fp, cache_dir=cache, fill_per_run=2)
    p2 = b2.card_payload("DEMO", days[3])
    assert p2["plant"]["mtd_days"] == 4                               # 2 from the cache, Mar 3 computed now
    assert db.calls - calls == 2 * 2                                  # today + Mar 3, two SELECTs each
    # a full plant at 1.0 every day: MTD equals the day
    assert p2["plant"]["epi600_mtd"] == pytest.approx(p2["plant"]["epi600_day"], abs=1e-4)
    # a past day page with no budget left: None (the page shows the pending card)
    b3 = L.Builder(db, cfgs, MX, now, fp, cache_dir=cache, fill_per_run=0)
    assert b3.card_payload("DEMO", dt.date(2026, 3, 3)) is not None   # cached now
    # another configuration: the cache is not used
    b4 = L.Builder(db, cfgs, MX, now, {"DEMO": "other"}, cache_dir=cache, fill_per_run=0)
    assert b4.card_payload("DEMO", dt.date(2026, 3, 1)) is None


def test_today_and_unsettled_days_are_not_cached(tmp_path):
    epi, lay = config_dirs(tmp_path)
    cfgs = C.load(epi, lay)
    fp = {"DEMO": L.fingerprint(epi, lay, "DEMO")}
    d = dt.date(2026, 3, 1)
    db = FakeDB([d])
    early = dt.datetime(2026, 3, 2, 3, 0, tzinfo=MX)                  # 3 h after the day ended < SETTLE_H
    L.Builder(db, cfgs, MX, early, fp, cache_dir=str(tmp_path / "c")).card_payload("DEMO", d)
    assert not (tmp_path / "c").exists()
    assert not L.is_settled(d, MX, early) and L.is_settled(d, MX, early + dt.timedelta(hours=3))


def test_bad_plant_key_never_reaches_sql():
    with pytest.raises(ValueError):
        L.load_day(lambda s: [], "X'; DROP", DAY, MX)


def test_generator_wiring_is_fail_soft():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    body = src[src.index("def epi_views_card"):src.index("def sev_pill")]
    assert "except Exception" in body and "return ''" in body
    page = src[src.index("def plant_page"):]
    assert page.index("{epi_views_card(pk, d)}") < page.index("{string_layout_card(pk, d)}")
