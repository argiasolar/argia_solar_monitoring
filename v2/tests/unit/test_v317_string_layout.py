"""v317: string layout monitoring (Tomasz 2026-10-07: "build the monitoring
like you did for the Culligan mock for [the PPA plants], add the detailed
interactive picture / layout under the intraday production graph").

Three parts, each tested here:
* argia.telemetry.string_sample - the 5-minute per-string currents the
  collector already downloads, now kept (Growatt string inputs 1-32 and
  MPPT currents, Huawei PV inputs 1-24; telemetry_detail only ever kept
  Growatt strings 20-29 and no Huawei input);
* argia.analytics.string_layout - layout validation and each string's
  current against its peers;
* argia.report.string_layout_html - the card under the intraday chart.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import sys
import types

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

from argia.analytics import string_layout as SL  # noqa: E402
from argia.report import string_layout_html as SH  # noqa: E402
from argia.telemetry import string_sample as SS  # noqa: E402
from argia.vendors.growatt_web_parser import parse_max_history  # noqa: E402

FIX = V2 / "tests" / "fixtures" / "growatt_web" / "GTO1_getMAXHistory_JFM5D8900B_2026-05-11.json"


def growatt_rows():
    return parse_max_history(json.loads(FIX.read_text(encoding="utf-8"))["response"])


# ======================================================== string_sample
def test_growatt_sample_keeps_every_string_input_and_mppt_current():
    row = growatt_rows()[-1]                                   # 17:20:40 MX, strings carry current
    s = SS.from_growatt("GTO1", "JFM5D8900B", row, "2026-05-11T23:20:40+00:00")
    assert len(s.str_a) == 32 and len(s.mppt_a) == 16 and len(s.mppt_v) == 16
    assert s.str_a[0] == 1.0 and s.str_a[1] == 0.3 and s.str_a[14] == 0.2   # string 15 (< 20: dropped by telemetry_detail)
    assert s.mppt_a[0] == 1.5 and s.has_current()


def test_a_night_sample_is_not_stored_and_a_repeat_is_stored_once():
    night = SS.from_growatt("GTO1", "X", growatt_rows()[0], "2026-05-12T05:56:18+00:00")    # 23:56, all 0 A
    assert night is not None and not night.has_current()
    assert SS.build_upsert_sql([night]) is None
    day = SS.from_growatt("GTO1", "X", growatt_rows()[-1], "2026-05-11T23:20:40+00:00")
    sql = SS.build_upsert_sql([day, day, night])
    assert sql.count("\n(") == 1 and "ON CONFLICT (plant_key, inverter_sn, ts_utc)" in sql
    assert "ARRAY[1.0,0.3," in sql and "::real[]" in sql


def test_huawei_inputs_beyond_16_are_kept():
    """SAG strings sit on PV19-PV21; the wide-row parser only read 16 inputs."""
    m = {f"pv{i}_i": 9.0 + i / 10 for i in range(1, 22)}
    m.update({f"pv{i}_u": 650.0 for i in range(1, 22)})
    m["pv16_i"] = None                                        # a spare input
    tel = types.SimpleNamespace(inverter_sn="ES2470051825", raw_data_item_map=m)
    s = SS.from_huawei("MEX1", tel, "2026-10-07T18:00:00+00:00")
    assert len(s.str_a) == 21 and s.str_a[20] == 11.1 and s.str_a[15] is None and s.mppt_a == ()
    assert "NULL" in SS.build_upsert_sql([s])
    assert SS.from_huawei("MEX1", types.SimpleNamespace(inverter_sn="X", raw_data_item_map={}), "t") is None


def test_quotes_cannot_break_the_insert():
    s = SS.Sample("2026-10-07T18:00:00+00:00", "P'1", "S'N", (1.0,), (), ())
    assert "'P''1'" in SS.build_upsert_sql([s])


def test_store_is_fail_soft_and_prunes(monkeypatch, caplog):
    from argia.store import pg_mirror, pgq
    s = SS.Sample("2026-10-07T18:00:00+00:00", "GTO1", "A", (5.0,), (), ())
    monkeypatch.setattr(pg_mirror, "enabled", lambda: False)
    assert SS.store([s]) == 0
    monkeypatch.setattr(pg_mirror, "enabled", lambda: True)
    sent = []
    monkeypatch.setattr(pgq, "psql_exec", lambda sql, timeout=60: sent.append(sql))
    assert SS.store([s]) == 1
    assert "CREATE TABLE IF NOT EXISTS string_sample" in sent[0] and "DELETE FROM string_sample" in sent[0]
    assert SS.store([s], dry_run=True) == 1 and len(sent) == 1

    def boom(sql, timeout=60):
        raise RuntimeError("disk full")
    monkeypatch.setattr(pgq, "psql_exec", boom)
    with caplog.at_level(logging.WARNING):
        assert SS.store([s]) == 0
    assert "string_sample write failed" in caplog.text


class _Web:
    def __init__(self):
        self.resp = json.loads(FIX.read_text(encoding="utf-8"))["response"]

    def get_max_history(self, sn, date_iso):
        return self.resp


def _growatt_run(monkeypatch):
    import telemetry_5m as T
    T.STRING_SAMPLES.clear()
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    plant = types.SimpleNamespace(plant_key="GTO1", weather_plant_id="1")
    invs = [types.SimpleNamespace(inverter_sn=sn, inverter_label=sn) for sn in ("JFM5D8900B", "JFM7DXN00T")]
    common, errors = T._process_growatt_plant(plant, invs, "2026-05-11", None, _Web(),
                                              T.WeatherSnapshot(), True, logging.getLogger("t"))
    return T, common, errors


def test_the_collector_keeps_one_sample_per_inverter(monkeypatch):
    T, common, errors = _growatt_run(monkeypatch)
    assert errors == 0 and len(common) == 2
    assert [s.inverter_sn for s in T.STRING_SAMPLES] == ["JFM5D8900B", "JFM7DXN00T"]
    assert T.STRING_SAMPLES[0].ts_utc.startswith("2026-05-12T05:56:18")    # the latest row, in UTC


def test_a_string_sample_problem_never_costs_the_telemetry(monkeypatch):
    def boom(*a, **k):
        raise ValueError("odd row")
    monkeypatch.setattr(SS, "from_growatt", boom)
    T, common, errors = _growatt_run(monkeypatch)
    assert errors == 0 and len(common) == 2 and T.STRING_SAMPLES == []


def test_huawei_path_keeps_samples(monkeypatch):
    import telemetry_5m as T
    T.STRING_SAMPLES.clear()
    tel = types.SimpleNamespace(inverter_sn="GR2499018250", raw_data_item_map={"pv1_i": 9.5, "pv2_i": 9.4},
                                timestamp_utc=dt.datetime(2026, 10, 7, 18, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(T, "fetch_huawei_telemetry", lambda c, p, i: [tel])
    monkeypatch.setattr(T.huawei_row, "build_plant_row", lambda *a: ["x"])
    monkeypatch.setattr(T.huawei_row, "build_common_row", lambda *a: ["y"])
    plant = types.SimpleNamespace(plant_key="MEX2")
    inv = [types.SimpleNamespace(inverter_sn="GR2499018250", inverter_label="Inverter 1")]
    common, errors = T._process_huawei_plant(plant, inv, None, None, T.WeatherSnapshot(), True, logging.getLogger("t"))
    assert errors == 0 and common == [["y"]]
    assert T.STRING_SAMPLES[0].str_a == (9.5, 9.4) and T.STRING_SAMPLES[0].ts_utc == "2026-10-07T18:00:00+00:00"


def test_main_stores_the_samples_after_the_telemetry_write():
    src = (V2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
    assert src.index("string_sample.store(STRING_SAMPLES") > src.index("mirror_common_rows(all_common")
    assert "STRING_SAMPLES.clear()" in src


def test_the_production_schema_has_the_table():
    for f in ("server/bundle/schema.sql", "tests/fixtures/pg/schema.sql"):
        assert "string_sample" in (V2 / f).read_text(encoding="utf-8"), f


# ======================================================== layout helpers
def _s(sn, ch, group="M|5|148.7", module="M", conf="order", pos=((10, 10),), per=None, **kw):
    d = {"sn": sn, "ch": ch, "inv": "INV1", "mppt": 1, "group": group, "module": module, "conf": conf,
         "pos": [{"d": "main", "x": x, "y": y, "cad": 1} for x, y in pos], "kwp": 10.0, "modules": 18,
         "tilt": 5, "az": 148.7, "orient": "SE"}
    if per:
        d["per"] = per
    d.update(kw)
    return d


def _doc(strings, areas=()):
    return {"plant": "GTO1", "drawings": [{"id": "main", "image": "GTO1_main.jpg", "box": [0, 0, 100, 100],
                                           "title_en": "Roof", "title_es": "Techo", "src": "rev. V7"}],
            "strings": list(strings), "areas": list(areas), "notes": []}


def test_a_good_layout_validates():
    doc = _doc([_s("A", "s1"), _s("A", "s2"), _s("B", "m1", per=4)], [{"sn": "B", "d": "main", "rects": [[1, 1, 2, 2]]}])
    assert SL.validate(doc, ["A", "B"]) == []
    assert SL.load(json.dumps(doc), ["A", "B"])["plant"] == "GTO1"


@pytest.mark.parametrize("bad, why", [
    (lambda d: d["strings"].append(_s("A", "s1")), "twice"),
    (lambda d: d["strings"].append(_s("A", "x3")), "bad channel"),
    (lambda d: d["strings"].append(_s("Z", "s9")), "inverter not configured"),
    (lambda d: d["strings"].append(_s("A", "s9", pos=((150, 10),))), "outside"),
    (lambda d: d["strings"].append(_s("A", "s9", conf="guess")), "confidence"),
    (lambda d: d["strings"][0]["pos"][0].update(d="other"), "unknown drawing"),
    (lambda d: d["areas"].append({"sn": "Z", "d": "main", "rects": []}), "inverter not configured"),
    (lambda d: d["drawings"][0].update(box=[5, 5, 1, 1]), "bad box"),
])
def test_a_wrong_layout_is_refused_and_says_why(bad, why):
    doc = _doc([_s("A", "s1"), _s("A", "s2")])
    bad(doc)
    probs = SL.validate(doc, ["A"])
    assert any(why in p for p in probs), probs
    with pytest.raises(SL.LayoutError):
        SL.load(json.dumps(doc), ["A"])


def test_a_small_plane_borrows_every_string_of_its_module():
    """Found building v317: a widened group took its median over its own
    label (the lone string itself) instead of every string of the module."""
    st = [_s("A", f"s{i}") for i in range(1, 5)] + [_s("B", "m2", group="M|10|239.8", per=2)]
    groups = SL.peer_groups(st)
    assert groups[-1] == "module:M" and groups[0] == "plane:M|5|148.7"
    assert SL.peer_members(st, groups)["module:M"] == [0, 1, 2, 3, 4]
    b, cur = SL.series(st, [(dt.datetime(2026, 10, 7, 12, 0), "A", [10, 10, 10, 10], []),
                            (dt.datetime(2026, 10, 7, 12, 1), "B", [], [0, 6.0])])
    ev = SL.evaluate(st, b, cur)
    assert ev["cls"][-1] == SL.VLOW and abs(cur[-1][0] - 3.0) < 1e-9       # 6 A over 2 strings vs 10 A peers


@pytest.mark.parametrize("i, med, cls", [
    (None, 10, SL.NODATA), (5, 0.9, SL.DIM), (5, None, SL.DIM), (0.2, 10, SL.ZERO),
    (9.0, 10, SL.OK), (8.99, 10, SL.LOW), (7.5, 10, SL.LOW), (7.49, 10, SL.VLOW),
])
def test_classes_and_their_edges(i, med, cls):
    assert SL.classify(i, med)[0] == cls


def test_buckets_latest_sample_wins_and_missing_channel_is_no_data():
    st = [_s("A", "s1"), _s("A", "s3")]
    smp = [(dt.datetime(2026, 10, 7, 12, 1), "A", [4.0], []),
           (dt.datetime(2026, 10, 7, 12, 4), "A", [5.0], []),
           (dt.datetime(2026, 10, 7, 12, 7), "A", [6.0, None, -1], [])]
    b, cur = SL.series(st, smp)
    assert b == [720, 725] and cur[0] == [5.0, 6.0] and cur[1] == [None, None]


def test_a_dead_string_and_a_weak_one_stand_out():
    st = [_s("A", f"s{i}") for i in range(1, 7)]
    smp = []
    for m in range(0, 120, 5):
        smp.append((dt.datetime(2026, 10, 7, 11, 0) + dt.timedelta(minutes=m), "A", [10, 10, 10, 10, 0.0, 7.0], []))
    b, cur = SL.series(st, smp)
    ev = SL.evaluate(st, b, cur)
    assert set(ev["cls"][0]) == {SL.OK} and set(ev["cls"][4]) == {SL.ZERO} and set(ev["cls"][5]) == {SL.VLOW}
    assert ev["day"][4]["cls"] == SL.ZERO and ev["day"][5]["idx"] == 0.7 and ev["day"][0]["ah"] == 20.0          # 24 x 5 min x 10 A
    inv = SL.inverter_summary(st, ev["cls"], len(b))
    assert inv["A"]["cls"][0] == SL.OK and inv["A"]["bad"][0] == 2


def test_a_data_gap_on_one_inverter_does_not_pull_its_strings_down():
    st = [_s("A", "s1"), _s("A", "s2"), _s("B", "s1"), _s("B", "s2")]
    smp = []
    for m in range(0, 240, 5):
        t = dt.datetime(2026, 10, 7, 10, 0) + dt.timedelta(minutes=m)
        smp.append((t, "A", [10, 10], []))
        if m < 60:                                            # B's logger drops out after an hour
            smp.append((t, "B", [10, 10], []))
    b, cur = SL.series(st, smp)
    ev = SL.evaluate(st, b, cur)
    assert [d["cls"] for d in ev["day"]] == [SL.OK] * 4 and ev["day"][2]["idx"] == 1.0
    assert ev["cls"][2].endswith(SL.NODATA) and ev["day"][2]["ah"] < ev["day"][0]["ah"]


def test_dawn_is_not_judged():
    st = [_s("A", f"s{i}") for i in range(1, 4)]
    smp = [(dt.datetime(2026, 10, 7, 6, 40), "A", [0.4, 0.1, 0.5], [])]
    b, cur = SL.series(st, smp)
    ev = SL.evaluate(st, b, cur)
    assert ev["cls"] == [SL.DIM] * 3 and [d["cls"] for d in ev["day"]] == [SL.DIM] * 3


def test_days_before_v317_use_the_nightly_amp_hours():
    st = [_s("A", f"s{i}") for i in range(1, 5)] + [_s("B", "m1", per=4)]
    day = SL.evaluate_daily(st, {("A", "s1"): 70.0, ("A", "s2"): 71.0, ("A", "s3"): 69.0, ("A", "s4"): 2.0})
    assert [d["cls"] for d in day] == [SL.OK, SL.OK, SL.OK, SL.ZERO, SL.NODATA]
    dim = SL.evaluate_daily(st, {("A", "s1"): 1.0, ("A", "s2"): 1.0})
    assert dim[0]["cls"] == SL.DIM


# ================================================================== card
def _payload(src="5min", age=3, live=True):
    st = [_s("A", f"s{i}", pos=((10 * i, 20),)) for i in range(1, 4)] + [_s("B", "s1", pos=(), conf="none", note="x</script>")]
    doc = _doc(st, [{"sn": "B", "d": "main", "rects": [[1, 1, 5, 5]]}])
    labels = {"A": "Inverter 1", "B": "Inverter <2>"}
    if src == "5min":
        smp = [(dt.datetime(2026, 10, 7, 12, m), sn, [10.0, 9.0, 0.1], []) for m in (0, 5) for sn in ("A", "B")]
        b, cur = SL.series(st, smp)
        return SH.payload("GTO1", doc, labels, live=live, source=src, buckets=b, cur=cur,
                          ev=SL.evaluate(st, b, cur), age_min=age, img_url={"main": "/monitoring/gto1/layout-main-x.jpg"})
    day = SL.evaluate_daily(st, {("A", "s1"): 60.0, ("A", "s2"): 61.0, ("A", "s3"): 59.0}) if src == "daily" else None
    return SH.payload("GTO1", doc, labels, live=live, source=src, day=day, img_url={"main": "img.jpg"})


def test_the_card_carries_everything_the_script_needs():
    p = _payload()
    assert p["t"] == ["12:00", "12:05"] and p["c"][2] == "zz" and p["src"] == "5min"
    assert p["io"] == ["A", "B"] and p["d"][0]["img"].startswith("/monitoring/gto1/layout-main-")
    assert p["s"][3]["p"] == [] and p["s"][0]["p"] == [["main", 10.0, 20.0]] and p["s"][0]["or"] == "5° / 148.7° SE"
    html = SH.card(p)
    assert 'class="card slcard" id="slGTO1"' in html and "x<\\/script>" in html and "x</script>" not in html
    assert 'data-es="Distribución de cadenas' in html and "Whole day" in html and 'type="range"' in html
    assert chr(0x2014) not in html and chr(0x2013) not in html                     # no em / en dash
    assert "banner" not in html.split('<script type="application/json"')[0]


def test_a_stale_feed_says_so_and_an_old_day_has_no_slider():
    assert "min ago" in SH.render(_payload(age=45))
    old = SH.render(_payload(src="daily", live=False))
    assert 'type="range"' not in old and "predates the 5-minute string record" in old
    none = SH.render(_payload(src="none"))
    assert "No string data for this day yet" in none
    p = _payload(src="none")
    assert p["c"] == [] and [d["c"] for d in p["dy"]] == [SL.NODATA] * 4


def test_the_script_escapes_names_it_puts_in_html():
    js = SH.JS
    for needle in ("E(s.il)", "E(s0.il)", "E(s.note)", "E(s.mod)", "E(sn)"):
        assert needle in js, needle
