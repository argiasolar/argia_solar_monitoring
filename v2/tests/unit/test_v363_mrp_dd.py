"""v363 - ARGIA for Grupo MRP (mrp.argia.com.mx) and the due diligence pages.

Tomasz, 7 Nov 2026: "build a portal similar to prologis.argia.com.mx for the
grupo mrp, use their colors, style, fonts ... do a due diligence on properties
and mark them on the map of Mexico". Pinned here:

* argia/prologis/dd.py validates the server-only properties file (all problems
  at once) and computes every estimate from facts and written assumptions;
  the numbers below are worked by hand;
* /dd/ exists only where the file exists (Prologis and Irritec unchanged);
* names from the file are escaped; Google pictures only on dd pages;
* the client font is served with its own type, nothing else new is served;
* the third instance has its own store, port, vhost and drift checks.
"""
from __future__ import annotations

import copy
import importlib
import json
import math
import os
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.prologis import dd as DD                         # noqa: E402

A = {"roof_use": 0.40, "m2_per_kwp": 6.0, "tariff_mxn_kwh": 2.80, "carport_share": 0.30, "kwp_per_space": 2.2,
     "spaces_per_fixture": 11, "led_w_saved": 310, "led_hours": 4380, "co2_kg_kwh": 0.444, "yield_default": 1650}
MALL = {"code": "MALL1", "name": "Portal Test", "kind": "Shopping center", "status": "operating", "city": "Querétaro",
        "state": "Querétaro", "address": "Av. 1", "lat": 20.64, "lon": -100.42, "gla_m2": 10000, "parking": 100,
        "pv_seen": True, "pv_share": 0.30, "yield": 1800, "sources": ["https://example.test/mall"]}
TOWER = {"code": "TWR1", "name": "Torre Test", "kind": "Office", "status": "development", "city": "CDMX",
         "lat": 19.43, "lon": -99.17, "roof_kwp": 150, "carport": False}


def doc(*props):
    return {"schema": 1, "title": "Test portfolio", "as_of": "2026-11-07",
            "company": {"name": "Test Co", "url": "https://example.test/"}, "assumptions": dict(A),
            "properties": [copy.deepcopy(p) for p in (props or (MALL, TOWER))]}


class TestParse:
    def test_a_good_file(self):
        pf = DD.parse(doc())
        assert [p.code for p in pf.props] == ["MALL1", "TWR1"] and pf.prop("mall1").name == "Portal Test"
        assert pf.assumptions["tariff_mxn_kwh"] == 2.80

    def test_every_problem_is_listed_at_once(self):
        d = doc(dict(MALL, code="x"), dict(MALL, status="sold"), dict(MALL, code="DUP1"), dict(MALL, code="DUP1"),
                dict(MALL, code="FAR1", lat=40.0), dict(MALL, code="SHR1", pv_seen=False),
                dict(MALL, code="SHR2", pv_share=1.5), dict(MALL, code="SRC1", sources=["http://plain.test"]))
        d["schema"] = 2
        del d["assumptions"]["led_hours"]
        d["assumptions"]["roof_use"] = 2
        with pytest.raises(ValueError) as ei:
            DD.parse(d)
        msg = str(ei.value)
        for part in ("schema must be 1", "led_hours missing", "roof_use=2 outside", "code must be 2-8",
                     "status must be operating or development", "DUP1: duplicate code", "FAR1: coordinates outside Mexico",
                     "SHR1: pv_share given but pv_seen is false", "SHR2: pv_share must be 0..1", "SRC1: sources must be"):
            assert part in msg, part

    @pytest.mark.parametrize("bad", [[], "x", {"schema": 1, "assumptions": A, "properties": []}])
    def test_nonsense_is_refused(self, bad):
        with pytest.raises(ValueError):
            DD.parse(bad)

    def test_negative_or_text_numbers_are_refused(self):
        with pytest.raises(ValueError, match="gla_m2 must be a number >= 0"):
            DD.parse(doc(dict(MALL, gla_m2=-5)))
        with pytest.raises(ValueError, match="parking is not a number"):
            DD.parse(doc(dict(MALL, parking="many")))


class TestEstimate:
    def test_a_mall_with_pv_worked_by_hand(self):
        pf = DD.parse(doc())
        e = DD.estimate(pf.prop("MALL1"), pf.assumptions)
        assert e["roof_m2"] == 10000
        assert e["existing_kwp"] == pytest.approx(500.0)                 # 10,000 x 0.30 / 6
        assert e["existing_mwh"] == pytest.approx(900.0)                 # 500 x 1,800 / 1,000
        assert e["new_roof_kwp"] == pytest.approx(10000 * 0.4 / 6 - 500)  # 166.67
        assert e["carport_kwp"] == pytest.approx(66.0)                   # 30 spaces x 2.2
        assert e["new_mwh"] == pytest.approx((10000 * 0.4 / 6 - 500 + 66) * 1.8)
        assert e["led_fixtures"] == 10                                   # ceil(100 / 11)
        assert e["led_mwh"] == pytest.approx(10 * 310 * 4380 / 1e6)      # 13.578
        assert e["value_mxn"] == pytest.approx((e["new_mwh"] + e["led_mwh"]) * 1000 * 2.80)
        assert e["co2_t"] == pytest.approx((e["new_mwh"] + e["led_mwh"]) * 0.444)
        assert e["opps"] == ["om", "roof", "carport", "led"]

    def test_existing_pv_above_the_usable_share_leaves_no_new_roof(self):
        pf = DD.parse(doc(dict(MALL, pv_share=0.6)))
        assert DD.estimate(pf.props[0], pf.assumptions)["new_roof_kwp"] == 0.0

    def test_a_tower_uses_its_own_roof_size_and_no_parking(self):
        pf = DD.parse(doc())
        e = DD.estimate(pf.prop("TWR1"), pf.assumptions)
        assert e["new_kwp"] == 150 and e["carport_kwp"] == 0 and e["led_fixtures"] == 0 and e["existing_kwp"] == 0
        assert e["yield"] == 1650 and e["new_mwh"] == pytest.approx(247.5)
        assert e["opps"] == ["design", "roof"]

    def test_roof_area_overrides_the_leasable_area_and_carport_can_be_off(self):
        pf = DD.parse(doc(dict(MALL, roof_m2=5000, carport=False)))
        e = DD.estimate(pf.props[0], pf.assumptions)
        assert e["roof_m2"] == 5000 and e["existing_kwp"] == pytest.approx(250.0) and e["carport_kwp"] == 0
        assert e["led_fixtures"] == 10                                   # the lights stay

    def test_ranking_puts_existing_pv_first_then_value(self):
        big = dict(MALL, code="BIG1", pv_seen=False, pv_share=0, gla_m2=90000)
        pf = DD.parse(doc(TOWER, big, MALL))
        assert [p.code for p in DD.ranked(pf)] == ["MALL1", "BIG1", "TWR1"]

    def test_totals_are_the_sum_of_the_properties(self):
        pf = DD.parse(doc())
        t = DD.totals(pf)
        es = [DD.estimate(p, pf.assumptions) for p in pf.props]
        assert t["new_kwp"] == pytest.approx(sum(e["new_kwp"] for e in es))
        assert t["value_mxn"] == pytest.approx(sum(e["value_mxn"] for e in es))
        assert (t["properties"], t["operating"], t["development"], t["pv_seen"], t["parking"]) == (2, 1, 1, 1, 100)
        assert all(math.isfinite(v) for v in t.values())

    def test_every_opportunity_has_both_languages(self):
        assert all(len(v) == 2 and all(v) for v in DD.OPPS.values())


# ------------------------------------------------------------------ the pages
pytest.importorskip("flask", reason="flask not installed here")
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
from argia.prologis import store as S                      # noqa: E402
from tests.unit.test_prologis_app import sign_in            # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("ARGIA_PL_DIR", tmp_path), ("ARGIA_PL_DB", tmp_path / "pl.db"), ("ARGIA_PL_FILES", tmp_path / "files"),
                 ("ARGIA_PL_REGISTRY", FIX), ("ARGIA_PL_INSECURE_COOKIE", "1"), ("ARGIA_PL_LIGHTING_DIR", tmp_path / "lighting")):
        monkeypatch.setenv(k, str(v))
    for k in ("ARGIA_PL_CLIENT", "ARGIA_PL_ABOUT", "ARGIA_PL_DD"):
        monkeypatch.delenv(k, raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pw = S.create_user(c, "viewer", "Viewer User", "v@x.test", "MRP", "viewer", "test")
    c.close()
    return mod, tmp_path, sign_in(mod, "viewer", pw, tmp_path)


def _write(tmp, d):
    p = tmp / "dd" / "properties.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(d) if not isinstance(d, str) else d, encoding="utf-8")
    mt = os.path.getmtime(p) + 1
    os.utime(p, (mt, mt))
    return p


def test_default_file_is_inside_the_data_dir(env):
    mod, tmp, _ = env
    assert mod.DD_FILE == str(tmp / "dd" / "properties.json")


def test_no_file_no_menu_and_no_pages(env):
    mod, _, cl = env
    assert 'href="/dd/"' not in cl.get("/").get_data(as_text=True)
    assert cl.get("/dd/").status_code == 404 and cl.get("/dd/MALL1").status_code == 404


def test_the_pages_with_a_file(env):
    mod, tmp, cl = env
    _write(tmp, doc())
    assert 'href="/dd/"' in cl.get("/").get_data(as_text=True)
    h = cl.get("/dd/").get_data(as_text=True)
    assert "Test portfolio" in h and "Portal Test" in h and 'href="/dd/MALL1"' in h and "leaflet" in h
    p = cl.get("/dd/MALL1")
    assert p.status_code == 200 and "Portal Test" in p.get_data(as_text=True)
    assert cl.get("/dd/NOPE").status_code == 404


def test_spanish_wording_from_the_file_when_given(env):
    mod, tmp, cl = env
    d = doc(dict(MALL, kind_es="Centro comercial", notes="Panels seen.", notes_es="Paneles vistos."), TOWER)
    d["title_es"] = "Portafolio de prueba"
    _write(tmp, d)
    cl.set_cookie("pl_lang", "es")
    h = cl.get("/dd/").get_data(as_text=True)
    assert "Portafolio de prueba" in h and "Centro comercial" in h and ">Office<" in h      # no Spanish given = English kept
    p = cl.get("/dd/MALL1").get_data(as_text=True)
    assert "Paneles vistos." in p and "Panels seen." not in p
    cl.set_cookie("pl_lang", "en")
    assert "Panels seen." in cl.get("/dd/MALL1").get_data(as_text=True)


def test_the_pages_need_a_session(env):
    mod, tmp, _ = env
    _write(tmp, doc())
    r = mod.app.test_client().get("/dd/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_names_from_the_file_are_escaped(env):
    mod, tmp, cl = env
    _write(tmp, doc(dict(MALL, name="<script>alert(1)</script>", anchors=["<b>x</b>"])))
    for path in ("/dd/", "/dd/MALL1"):
        h = cl.get(path).get_data(as_text=True)
        assert "<script>alert(1)</script>" not in h and "<b>x</b>" not in h, path       # the map data has <\/script>


def test_a_broken_file_keeps_the_last_good_one(env):
    mod, tmp, cl = env
    _write(tmp, doc())
    assert mod.dd_pf().title == "Test portfolio"
    _write(tmp, "{not json")
    assert mod.dd_pf().title == "Test portfolio"
    assert cl.get("/dd/").status_code == 200


def test_google_pictures_only_on_dd_pages_and_only_with_a_key(env):
    mod, tmp, cl = env
    _write(tmp, doc())
    assert "maps.googleapis.com" not in cl.get("/dd/").headers["Content-Security-Policy"]
    (tmp / "lighting").mkdir()
    (tmp / "lighting" / "google_maps_key").write_text("A" * 39, encoding="utf-8")
    r = cl.get("/dd/MALL1")
    assert "https://maps.googleapis.com" in r.headers["Content-Security-Policy"]
    h = r.get_data(as_text=True)
    assert "maps.googleapis.com/maps/api/staticmap" in h and 'referrerpolicy="origin"' in h
    assert "maps.googleapis.com" not in cl.get("/sites/").headers["Content-Security-Policy"]


def test_the_client_font_is_served_with_its_type_and_nothing_else_new(env):
    mod, tmp, _ = env
    (tmp / "brand").mkdir()
    (tmp / "brand" / "client_font.woff2").write_bytes(b"wOF2fake")
    (tmp / "brand" / "other.woff2").write_bytes(b"wOF2fake")
    c = mod.app.test_client()
    r = c.get("/brand/client_font.woff2")
    assert r.status_code == 200 and r.mimetype == "font/woff2"
    r.close()
    assert c.get("/brand/other.woff2").status_code == 404
    assert c.get("/brand/..%2Fsecret.key").status_code == 404


class TestMrpDeployFiles:
    B = BUNDLE

    def test_vhost(self):
        v = (self.B / "mrp.argia.com.mx.conf").read_text()
        assert "server_name mrp.argia.com.mx;" in v and "/etc/letsencrypt/live/mrp.argia.com.mx/" in v
        assert "proxy_pass http://127.0.0.1:8522;" in v and "8521" not in v
        assert "zone=mrp_login" in v and "ir_login" not in v and "pl_login" not in v and "/var/log/argia-mrp/" in v
        assert "irritec" not in v.lower()

    @pytest.mark.parametrize("unit", ["argia-mrp.service", "argia-mrp-alarms.service", "argia-mrp-scan.service"])
    def test_units_use_only_the_mrp_store(self, unit):
        u = (self.B / unit).read_text()
        for k, v in (("ARGIA_PL_DIR", "/opt/argia/mrp"), ("ARGIA_PL_DB", "/opt/argia/mrp/mrp.db"),
                     ("ARGIA_PL_FILES", "/opt/argia/mrp/files"), ("ARGIA_PL_REGISTRY", "/opt/argia/mrp/registry.json")):
            assert f"Environment={k}={v}\n" in u, (unit, k)
        assert "irritec" not in u.lower() and "/opt/argia/prologis" not in u

    def test_its_own_port(self):
        assert "Environment=ARGIA_PL_PORT=8522\n" in (self.B / "argia-mrp.service").read_text()

    def test_drift_check_knows_the_files(self):
        sys.path.insert(0, str(V2 / "scripts"))
        import drift_check as D
        assert ("v2/server/bundle/mrp.argia.com.mx.conf", "/etc/nginx/sites-enabled/mrp.argia.com.mx.conf") in D.EXPLICIT
        assert ("v2/server/bundle/argia-mrp.logrotate", "/etc/logrotate.d/argia-mrp") in D.EXPLICIT
        assert "mrp.argia.com.mx.http.conf" in D.NOT_DEPLOYED
        assert ("mrp-login", "https://mrp.argia.com.mx/login", (200,)) in D.SMOKE_HTTP
