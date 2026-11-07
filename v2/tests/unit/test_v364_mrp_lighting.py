"""v364 - the LED lighting part of ARGIA for Grupo MRP.

Tomasz, 7 Nov 2026: "also add the lighting part, I know they would like to
change luminaires to LED in their industrial portfolio". Pinned here:

* a property may carry a "lighting" block (interior LED of an industrial
  building); the numbers below are worked by hand;
* lighting assumptions are required only when a property has lighting, so
  v363 files stay valid;
* the Lighting menu and /dd/lighting/ exist only when a property has
  lighting; the property and summary pages count the interior fixtures.
"""
from __future__ import annotations

import copy
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.prologis import dd as DD                         # noqa: E402
from tests.unit.test_v363_mrp_dd import A, MALL, TOWER      # noqa: E402

LED = {"led_m2_per_fixture": 70, "led_w_before": 458, "led_w_after": 150, "led_controls_share": 0.30, "led_hours_interior": 5000}
PARK = {"code": "IND1", "name": "Parque Test", "kind": "Industrial park", "status": "development", "city": "Tijuana",
        "lat": 32.47, "lon": -116.86, "gla_m2": 7000, "roof_kwp": 500, "parking": 0,
        "lighting": {"status": "not_started", "baseline": "hid"}}


def doc(*props, led=True):
    a = dict(A, **LED) if led else dict(A)
    return {"schema": 1, "title": "Test portfolio", "as_of": "2026-11-07", "company": {"name": "Test Co", "url": "https://example.test/"},
            "assumptions": a, "properties": [copy.deepcopy(p) for p in (props or (MALL, TOWER, PARK))]}


class TestLight:
    def test_a_metal_halide_warehouse_worked_by_hand(self):
        pf = DD.parse(doc())
        li = DD.light(pf.prop("IND1"), pf.assumptions)
        assert li["fixtures"] == 100                                     # 7,000 / 70
        assert li["mwh_before"] == pytest.approx(229.0)                  # 100 x 458 W x 5,000 h
        assert li["mwh_after"] == pytest.approx(52.5)                    # 100 x 150 W x 5,000 h x 0.70
        assert li["mwh"] == pytest.approx(176.5)
        e = DD.estimate(pf.prop("IND1"), pf.assumptions)
        assert e["light_fixtures"] == 100 and e["light_mxn"] == pytest.approx(176.5 * 1000 * 2.80)
        assert "led_interior" in e["opps"]
        assert e["value_mxn"] == pytest.approx(e["pv_mxn"] + e["led_mxn"] + e["light_mxn"])
        assert e["co2_t"] == pytest.approx((e["new_mwh"] + e["led_mwh"] + 176.5) * 0.444)

    def test_a_new_build_saves_only_what_the_sensors_add(self):
        pf = DD.parse(doc(dict(PARK, lighting={"baseline": "plain_led", "w_before": 150})))
        li = DD.light(pf.props[0], pf.assumptions)
        assert li["mwh"] == pytest.approx(100 * 150 * 5000 * 0.30 / 1e6)  # 22.5

    def test_own_area_hours_and_spacing_win(self):
        pf = DD.parse(doc(dict(PARK, lighting={"area_m2": 1000, "m2_per_fixture": 101, "hours": 8760})))
        li = DD.light(pf.props[0], pf.assumptions)
        assert li["fixtures"] == 10 and li["hours"] == 8760                # ceil(1000 / 101)

    def test_no_lighting_block_no_interior_numbers(self):
        pf = DD.parse(doc())
        e = DD.estimate(pf.prop("MALL1"), pf.assumptions)
        assert e["light_fixtures"] == 0 and e["light_mwh"] == 0 and "led_interior" not in e["opps"]

    def test_totals_count_the_interior(self):
        t = DD.totals(DD.parse(doc()))
        assert t["light_fixtures"] == 100 and t["light_mwh"] == pytest.approx(176.5) and t["lighting"] == 1

    def test_v363_files_without_lighting_still_load(self):
        pf = DD.parse(doc(MALL, TOWER, led=False))
        assert DD.totals(pf)["light_fixtures"] == 0

    @pytest.mark.parametrize("bad, msg", [
        (dict(PARK, lighting={"status": "done"}), "lighting status must be one of"),
        (dict(PARK, lighting={"baseline": "candles"}), "lighting baseline must be one of"),
        (dict(PARK, lighting={"hours": 9000}), "lighting hours above 8760"),
        (dict(PARK, gla_m2=None, lighting={}), "lighting needs area_m2 or gla_m2"),
        (dict(PARK, lighting={"w_before": 100, "w_after": 120}), "lighting w_after above w_before"),
        (dict(PARK, lighting="yes"), "lighting must be an object"),
    ])
    def test_bad_lighting_is_refused(self, bad, msg):
        with pytest.raises(ValueError, match=msg):
            DD.parse(doc(bad))

    def test_lighting_needs_its_assumptions(self):
        with pytest.raises(ValueError, match="lighting needs led_m2_per_fixture"):
            DD.parse(doc(PARK, led=False))

    def test_lighting_assumptions_are_range_checked(self):
        d = doc()
        d["assumptions"]["led_controls_share"] = 0.95
        with pytest.raises(ValueError, match="led_controls_share=0.95 outside"):
            DD.parse(d)


pytest.importorskip("flask", reason="flask not installed here")
from tests.unit import test_v363_mrp_dd as T363              # noqa: E402

env = T363.env                                              # the same signed-in portal fixture
_write = T363._write


def test_no_lighting_no_menu_and_no_page(env):
    mod, tmp, cl = env
    _write(tmp, doc(MALL, TOWER, led=False))
    assert 'href="/dd/lighting/"' not in cl.get("/dd/").get_data(as_text=True)
    assert cl.get("/dd/lighting/").status_code == 404


def test_the_lighting_page(env):
    mod, tmp, cl = env
    _write(tmp, doc())
    assert 'href="/dd/lighting/"' in cl.get("/dd/").get_data(as_text=True)
    r = cl.get("/dd/lighting/")
    h = r.get_data(as_text=True)
    assert r.status_code == 200 and "Parque Test" in h and "lc_a" in h            # the card and the calculator
    assert "<b>176 MWh" in h                                                        # 176.5 shown rounded
    assert 'href="/dd/MALL1"' in h                                                  # parking table
    p = cl.get("/dd/IND1").get_data(as_text=True)
    assert "LED interior fixtures" in p
    cl.set_cookie("pl_lang", "es")
    assert "Cómo ARGIA ejecuta un proyecto LED" in cl.get("/dd/lighting/").get_data(as_text=True)


def test_the_lighting_page_needs_a_session(env):
    mod, tmp, _ = env
    _write(tmp, doc())
    r = mod.app.test_client().get("/dd/lighting/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_lighting_notes_are_escaped(env):
    mod, tmp, cl = env
    _write(tmp, doc(dict(PARK, lighting={"note": "<img src=x onerror=alert(1)>"})))
    assert "<img src=x onerror" not in cl.get("/dd/lighting/").get_data(as_text=True)


def test_an_approximate_location_says_so_under_the_picture(env):
    mod, tmp, cl = env
    _write(tmp, doc(dict(PARK, approx=True), MALL))
    assert "Approximate location" in cl.get("/dd/lighting/").get_data(as_text=True)
    assert "Approximate location" in cl.get("/dd/IND1").get_data(as_text=True)
    assert "Approximate location" not in cl.get("/dd/MALL1").get_data(as_text=True)
