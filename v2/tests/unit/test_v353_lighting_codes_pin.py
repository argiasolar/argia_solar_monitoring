"""v353 - after Tomasz's look at a project page and the By park table (5 Nov 2026):

* "make there two columns park and city" (By park)
* "Exterior Lighting and Replacement stock should have Prologis logo" - every Prologis space does
  (vacant, exterior, common areas, stock); a tenant keeps its own logo
* "when I open a plant I would like to see at least a pin showing where it is, not a high level picture of
  half of the city" - closer picture, a pin on its centre, Google Maps opens on a pin
* "use the building codes that Prologis is using" - the Prologis building code (Yardi) names the building
  in the lists, the project page and "other projects in this park"

All data invented (public repo).
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, _p, _png, env, sign_in   # noqa: F401  (fixture re-used)
from tests.unit.test_v346_lighting_maps import KEY, _with_key
from argia.prologis import lighting as LT


def _write(tmp, d):
    f = tmp / "lighting" / "lighting.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))


class TestCodes:
    def test_place_uses_the_prologis_building_code(self):
        ds = LT.parse(copy.deepcopy(DATA))
        p1 = ds.project(1)
        assert p1.place == f"{p1.park} · MXC09901"
        p4 = ds.project(4)                                                          # no code: the building name
        assert p4.yardi is None and p4.place == " · ".join(x for x in (p4.park, p4.bldg) if x)

    def test_lists_and_project_page_show_the_code(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        lst = cl.get("/lighting/buildings/").get_data(as_text=True)
        assert "<th>Park</th>\n<th>Prologis building code</th>" in lst
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert '<td class="muted">Prologis building code</td><td><b>MXC09901</b></td>' in h
        assert re.search(r'<div class="muted">[^<]*· MXC09901 <span class="pill"', h)          # under the title
        other = h.split("Other projects in this park")[1]
        assert '<td class="nw">MXC09901</td>' in other and "Building 1" not in other


class TestByPark:
    def test_park_and_city_are_two_columns(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
        box = h.split('<div class="sitebox"')[1].split("</table>")[0]
        assert "<tr><th>Park</th><th>City</th>" in box
        assert re.search(r'<td><a href="/lighting/buildings/\?pk=[^"]+">Parque Uno</a></td><td>Mexico City</td>', box)
        assert "Parque Uno · " not in box


class TestPrologisLogo:
    @pytest.mark.parametrize("kind", ["vacant", "exterior", "common", "stock"])
    def test_every_prologis_space_shows_the_prologis_logo(self, env, kind):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"].append(_p(90, kind=kind, customer="Prologis space", group=None))
        _write(tmp, d)
        (tmp / "brand").mkdir(exist_ok=True)
        (tmp / "brand" / "prologis_logo.png").write_bytes(_png())
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/90").get_data(as_text=True)
        assert '<div class="lhead"><span class="clogo big plain"><img class="" src="/brand/prologis_logo.png" alt="Prologis">' in h

    def test_without_the_brand_file_grey_initials(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"].append(_p(91, kind="stock", customer="Replacement stock", group=None))
        _write(tmp, d)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/91").get_data(as_text=True)
        assert '<span class="clogo big plain mono" style="background:#9aa4ad">RS</span>' in h


class TestPin:
    def test_closer_picture_with_a_pin_and_maps_link_on_a_pin(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert '<div class="satwrap"><img class="satimg"' in h and '<svg class="satpin"' in h
        assert "The pin marks the park" in h
        assert "google.com/maps/place/19.700000,-99.200000/" in h
        _with_key(tmp)
        g = cl.get("/lighting/p/1").get_data(as_text=True)
        assert "&amp;zoom=17&amp;" in g                                                  # was 16: half a city
        d = copy.deepcopy(DATA)
        d["projects"][0]["geo"] = "building"
        _write(tmp, d)
        g = cl.get("/lighting/p/1").get_data(as_text=True)
        assert "&amp;zoom=19&amp;" in g and "The pin marks the building" in g

    def test_no_pin_when_only_the_city_is_known(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"][0]["geo"] = "city"
        _write(tmp, d)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
        assert '<div class="satwrap"><img class="satimg"' in h and '<svg class="satpin"' not in h
        assert "Only the city is known for this project, so there is no pin" in h

    def test_esri_picture_is_closer_too(self, env):
        mod, pws, tmp = env

        def width(url):
            x0, _, x1, _ = map(float, re.search(r"bbox=([-0-9.]+),([-0-9.]+),([-0-9.]+),([-0-9.]+)", url).groups())
            return x1 - x0
        old_park = LT.satellite_url(19.7, -99.2, 640, 360, half_m=420)
        assert width(mod.sat_photo(19.7, -99.2, False)) < 0.6 * width(old_park)
        assert width(mod.sat_photo(19.7, -99.2, True)) < width(mod.sat_photo(19.7, -99.2, False))


pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as ex:                                                          # noqa: BLE001
            pytest.skip(f"no Chromium: {ex}")
        yield b
        b.close()


def test_the_pin_tip_is_the_centre_of_the_picture(env, browser):
    mod, pws, tmp = env
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
    pg.set_content(h)
    img = pg.eval_on_selector(".satimg", "e => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }")
    tip = pg.eval_on_selector(".satpin", "e => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.bottom]; }")
    assert abs(img[0] - tip[0]) < 2 and abs(img[1] - tip[1]) < 2, (img, tip)
    pg.close()
