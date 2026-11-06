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
        assert "<th>Park</th><th>City</th>\n<th>Prologis building code</th>" in lst
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert '<td class="muted">Prologis building code</td><td><b>MXC09901</b></td>' in h
        assert "<span>Prologis code<b>MXC09901</b></span>" in h                       # under the title (v362: facts line)
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
        assert '<div class="phlogo"><span class="clogo big plain"><img class="" src="/brand/prologis_logo.png" alt="Prologis">' in h

    def test_without_the_brand_file_grey_initials(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"].append(_p(91, kind="stock", customer="Replacement stock", group=None))
        _write(tmp, d)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/91").get_data(as_text=True)
        assert '<span class="clogo big plain mono" style="background:#9aa4ad">RS</span>' in h


class TestPin:
    def test_closer_view_with_a_pin_and_maps_link_on_a_pin(self, env):
        """v360: an interactive map opened at the zoom the pictures had (park 17, building 19)."""
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert 'id="pmap"' in h and '"z": 17, "pin": true' in h and "The pin marks the park" in h
        assert "google.com/maps/place/19.700000,-99.200000/" in h
        d = copy.deepcopy(DATA)
        d["projects"][0]["geo"] = "building"
        _write(tmp, d)
        g = cl.get("/lighting/p/1").get_data(as_text=True)
        assert '"z": 19, "pin": true' in g and "The pin marks the building" in g

    def test_no_pin_when_only_the_city_is_known(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"][0]["geo"] = "city"
        _write(tmp, d)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
        assert 'id="pmap"' in h and '"z": 12, "pin": false' in h
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


def test_the_pin_tip_is_the_point(env, browser):
    """v360: the pin's tip (bottom centre of the 34 x 46 icon) is anchored on the position - checked in a browser on the
    icon as the page draws it (the map library itself is not loaded offline)."""
    mod, pws, tmp = env
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
    assert "iconSize:[34,46],iconAnchor:[17,46]" in h
    svg = re.search(r"html:'(<svg class=\"lpin\".*?</svg>)'", h).group(1)
    css = re.search(r"<style>(.*?)</style>", h, re.S).group(1)
    pg = browser.new_page(viewport={"width": 400, "height": 300})
    pg.set_content(f"<style>{css}</style><div style='position:absolute;left:100px;top:100px;width:34px;height:46px'>{svg}</div>")
    box = pg.eval_on_selector(".lpin", "e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }")
    assert box == [34, 46], box
    pg.close()
