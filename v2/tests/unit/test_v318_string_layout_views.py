"""v318: the string layout card, readable (Tomasz 7 Oct 2026: "double check the
number of inverters, map each inverter's connected strings with separate
colours, make sure we have a good and transparent legend for the layouts,
I am also missing the Helioscope versions, I do not understand what the
picture is showing").

* each string judged against its neighbours AND its own 30-day normal: a
  Growatt MAX reads the second string of each MPPT pair 10-20 % low at every
  PPA site, which painted a third of a plant amber; now only a change shows,
  and a string that is always below 80 % never shows as ok;
* inverter colours (never green / amber / red: those mean health), an
  inverter table, the counts from every source side by side;
* two pictures: the installation drawing and the Helioscope design.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from argia.analytics import string_layout as SL
from argia.report import string_layout_html as SH

DAY = dt.date(2026, 10, 7)


def _s(sn, ch, **kw):
    d = {"sn": sn, "ch": ch, "inv": "INV1", "mppt": 1, "group": "M|5|148.7", "module": "M", "conf": "order",
         "pos": [{"d": "drawing", "x": 10.0, "y": 10.0, "cad": int(ch[1:])}], "kwp": 10.0, "modules": 18,
         "tilt": 5, "az": 148.7, "orient": "SE"}
    d.update(kw)
    return d


STR = [_s("A", "s1"), _s("A", "s2"), _s("A", "s3"), _s("A", "s4")]


def _history(ratio_s2=0.85, days=10, ratio_s4=1.0):
    return {DAY - dt.timedelta(days=k): {("A", "s1"): 70.0, ("A", "s2"): 70.0 * ratio_s2, ("A", "s3"): 70.0,
                                         ("A", "s4"): 70.0 * ratio_s4}
            for k in range(1, days + 1)}


# ------------------------------------------------------------------ normals
def test_a_string_s_normal_is_its_median_index_over_the_last_30_days():
    n = SL.normals(STR, _history(), DAY)
    assert n[1] == pytest.approx(0.85, abs=0.005) and n[0] == pytest.approx(1.0, abs=0.005)


def test_too_few_days_or_days_after_the_page_give_no_normal():
    assert SL.normals(STR, _history(days=6), DAY) == [None] * 4
    later = {DAY + dt.timedelta(days=k): v for k, v in enumerate(_history().values())}
    assert SL.normals(STR, later, DAY) == [None] * 4          # the day itself and later never count
    old = {DAY - dt.timedelta(days=40 + k): v for k, v in enumerate(_history().values())}
    assert SL.normals(STR, old, DAY) == [None] * 4


@pytest.mark.parametrize("ratio, normal, cls", [
    (0.85, 0.85, SL.OK),       # the Growatt pair bias: normal for this string, not flagged
    (0.85, None, SL.LOW),      # without history: judged against its neighbours alone
    (0.70, 0.85, SL.LOW),      # 82 % of its own normal: a change
    (0.55, 0.85, SL.VLOW),     # 65 % of its own normal
    (0.75, 0.75, SL.LOW),      # always below 80 %: never ok
    (0.95, 1.0, SL.OK),
])
def test_classes_against_the_own_normal(ratio, normal, cls):
    assert SL.classify(10 * ratio, 10.0, normal)[0] == cls


def test_a_dead_string_is_dead_whatever_its_normal():
    assert SL.classify(0.1, 10.0, 0.85)[0] == SL.ZERO


def test_evaluate_uses_the_normal_and_hands_it_on():
    smp = [(dt.datetime(2026, 10, 7, 12, m), "A", [10.0, 8.5, 10.0, 10.0], []) for m in range(0, 60, 5)]
    b, cur = SL.series(STR, smp)
    plain = SL.evaluate(STR, b, cur)
    fair = SL.evaluate(STR, b, cur, SL.normals(STR, _history(), DAY))
    assert set(plain["cls"][1]) == {SL.LOW} and set(fair["cls"][1]) == {SL.OK}
    assert plain["day"][1]["cls"] == SL.LOW and fair["day"][1]["cls"] == SL.OK
    assert fair["normal"][1] == pytest.approx(0.85, abs=0.005)


def test_a_day_of_amp_hours_can_carry_mppt_channels():
    st = STR + [_s("B", "m1", per=4), _s("B", "m2", per=2)]
    day = SL.evaluate_daily(st, {("A", "s1"): 70, ("A", "s2"): 70, ("A", "s3"): 70, ("A", "s4"): 70,
                                 ("B", "m1"): 69, ("B", "m2"): 20})
    assert day[4]["cls"] == SL.OK and day[5]["cls"] == SL.VLOW


# ------------------------------------------------------------------ payload
def _doc():
    return {"plant": "GTO1", "drawings": [
        {"id": "drawing", "kind": "drawing", "image": "d.jpg", "box": [0, 0, 100, 100], "title_en": "Installation drawing",
         "title_es": "Plano de instalación", "src": "rev. V7"},
        {"id": "helio", "kind": "helioscope", "image": "h.jpg", "box": [0, 0, 500, 400], "title_en": "Helioscope design",
         "title_es": "Diseño Helioscope", "src": "Helioscope X", "markers": True, "fit": 0.8}],
        "inverters": [{"sn": "A", "inv": "INV5", "model": "Growatt MAX 124KTL3-X2 MV", "kwp": 145.6},
                      {"sn": "B", "inv": "INV6", "model": "Growatt MAC 70KTL3-X MV", "kwp": 63.7}],
        "checks": {"inverters": {"workbook": 2, "drawing": 2, "helioscope": 2},
                   "strings": {"workbook": 10, "drawing_labels": 10, "helioscope": 9}},
        "strings": [dict(s, pos=s["pos"] + [{"d": "helio", "x": 50.0, "y": 60.0, "cad": int(s["ch"][1:])}])
                    for s in STR] + [_s("B", "m1", per=4, pos=[]), _s("B", "m2", per=2, pos=[])],
        "areas": [{"sn": "A", "inv": "INV5", "d": "drawing", "rects": [[1, 1, 2, 2]]},
                  {"sn": "A", "inv": "INV5", "d": "helio", "polys": [[1, 1, 3, 1, 3, 3, 1, 3]]}],
        "notes": [["en note", "nota es"], "old style note"]}


def test_a_v2_layout_validates_and_a_helio_mark_off_the_picture_does_not():
    doc = _doc()
    assert SL.validate(doc, ["A", "B"]) == []
    doc["strings"][0]["pos"][1]["x"] = 900
    assert any("outside" in p for p in SL.validate(doc, ["A", "B"]))


def test_the_inverters_get_their_own_colours_never_the_health_ones():
    p = SH.payload("GTO1", _doc(), {"A": "Inverter 5", "B": "Inverter 6"}, live=True, source="none",
                   monitored_inverters=2)
    cols = [v["col"] for v in p["inv"]]
    assert len(set(cols)) == 2 and not set(cols) & set(SH.COLORS.values())
    assert set(SH.INV_COLORS).isdisjoint(SH.COLORS.values()) and len(SH.INV_COLORS) >= 6
    a, b = p["inv"]
    assert (a["il"], a["dn"], a["n"], a["placed"], a["kwp"]) == ("Inverter 5", "INV5", 4, 4, 145.6)
    assert (b["n"], b["inputs"], b["placed"]) == (6, 2, 0)                   # 4 + 2 strings on 2 MPPT inputs
    assert p["chk"]["inverters"] == {"workbook": 2, "drawing": 2, "helioscope": 2, "monitoring": 2}
    assert p["notes"] == [["en note", "nota es"], ["old style note", "old style note"]]
    assert p["a"][1]["pl"] == [[1, 1, 3, 1, 3, 3, 1, 3]] and p["d"][1]["kind"] == "helioscope"
    assert p["s"][0]["p"][1] == ["helio", 50.0, 60.0, 1] and p["s"][0]["cad"] == [1]


def test_the_card_explains_itself_and_offers_both_views_and_both_pictures():
    p = SH.payload("GTO1", _doc(), {"A": "Inverter 5"}, live=False, source="none")
    html = SH.render(p)
    assert "Each numbered dot is one string" in html and 'data-es="Cada punto numerado es una cadena' in html
    assert 'data-v="h"' in html and 'data-v="i"' in html                      # Health / Inverters
    assert 'data-dr="0"' in html and 'data-dr="1"' in html and "Helioscope design" in html
    assert 'class="slchk"' in html and 'class="sllegend"' in html
    assert "The Inverters view still shows the wiring" in html
    assert chr(0x2014) not in html and chr(0x2013) not in html


def test_the_script_draws_helio_polygons_rings_and_the_legend():
    js = SH.JS
    for needle in ("'polygon'", "a.pl.forEach", "view==='h'?hcol(c):icol(s.sn)", "function legend(", "function checks(",
                   "s.cf==='assumed'", "P.rule[c][l]", "Its normal", "'#f28c28'"):
        assert needle in js, needle
    # every class has a rule text in both languages
    assert set(SH.CLASS_RULE) == set(SL.CLASS_TEXT) and all(len(v) == 2 for v in SH.CLASS_RULE.values())


def test_the_payload_survives_json_and_never_closes_the_script():
    doc = _doc()
    doc["strings"][0]["note"] = "x</script><b>"
    html = SH.card(SH.payload("GTO1", doc, {}, live=True, source="none"))
    raw = re.search(r'class="sldata">(.*?)</script>', html, re.S).group(1)
    assert json.loads(raw.replace("<\\/", "</"))["s"][0]["note"] == "x</script><b>"
