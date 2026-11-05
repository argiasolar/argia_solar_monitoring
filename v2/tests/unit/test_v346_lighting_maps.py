"""v346 - lighting pages after Tomasz's first look (5 Nov 2026).

* "the map is not responsive, I cannot zoom with the mouse; at park level I would expect a satellite photo"
* "we have a Google Maps API key, why can we not show it inside the portal?" (browser key, referrer
  restricted to https://prologis.argia.com.mx/*) - kept server-only, used for Static Maps pictures only
* "keep small logo in black/white centred over the customer name without any frame; in the building
  list small on the left without the frame"
* "Projects delivered per year is interesting but add other graph options"
* "if something is partly converted does it mean it was audited?" - no; the table now says so
* "recalculate the number of fixtures" - replacement stock is not installed lighting

All data invented (public repo).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re

import pytest

from tests.unit.test_v345_lighting import DATA, TODAY, _p, env, sign_in   # noqa: F401  (fixture re-used)
from argia.prologis import lighting as LT

KEY = "AIzaTestKey_0123456789abcdefghijklmnopq"


def _ds_with_stock():
    d = copy.deepcopy(DATA)
    d["projects"].append(_p(8, kind="stock", customer="Replacement stock", group=None, yardi=None, fixtures=500,
                            area_m2=None, kwh_before=None, kwh_after=None, year=2025, handover_date="2025-03-01"))
    return LT.parse(d)


class TestPure:
    def test_stock_is_not_installed(self):
        t = LT.totals(_ds_with_stock().projects, TODAY)
        assert t.stock_fixtures == 500
        assert t.fixtures == LT.totals(LT.parse(copy.deepcopy(DATA)).projects, TODAY).fixtures

    def test_year_series_adds_up(self):
        ds = LT.parse(copy.deepcopy(DATA))
        ys = LT.year_series(ds.projects, TODAY)
        t = LT.totals(ds.projects, TODAY)
        assert [y for y, _ in ys["projects"]][0] == 2019 and [y for y, _ in ys["projects"]][-1] == TODAY.year
        assert sum(v for _, v in ys["projects"]) == t.projects
        assert sum(v for _, v in ys["fixtures"]) == t.fixtures
        assert ys["co2_cum"][-1][1] == pytest.approx(t.co2_t_since, rel=1e-6)      # the chart ends at the counter
        cum = [v for _, v in ys["co2_cum"]]
        assert cum == sorted(cum)                                                   # a running total never falls


def _with_key(tmp, key=KEY):
    (tmp / "lighting" / "google_maps_key").write_text(key + "\n", encoding="utf-8")


class TestGoogle:
    def test_without_a_key_esri_and_the_csp_is_unchanged(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = cl.get("/lighting/p/1")
        assert "maps.googleapis.com" not in r.headers["Content-Security-Policy"]
        assert "World_Imagery/MapServer/export" in r.get_data(as_text=True)

    def test_with_a_key_google_pictures_on_lighting_pages_only(self, env):
        mod, pws, tmp = env
        _with_key(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = cl.get("/lighting/p/1")
        h = r.get_data(as_text=True)
        assert "img-src 'self' data: https://server.arcgisonline.com https://cdnjs.cloudflare.com https://maps.googleapis.com;" \
            in r.headers["Content-Security-Policy"]
        assert re.search(r'<img class="satimg" src="https://maps\.googleapis\.com/maps/api/staticmap\?center=19\.700000,-99\.200000'
                         r'&amp;zoom=16&amp;size=640x360&amp;scale=2&amp;maptype=satellite&amp;key=' + KEY + '" referrerpolicy="origin"', h)
        assert "frame-src" not in r.headers["Content-Security-Policy"] and "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com;" in r.headers["Content-Security-Policy"]
        home = cl.get("/")                                                    # the rest of the platform: untouched
        assert "maps.googleapis.com" not in home.headers["Content-Security-Policy"] and KEY not in home.get_data(as_text=True)

    @pytest.mark.parametrize("bad", ["", "not a key", "x" * 10, "AIza<script>alert(1)</script>xxxxxxxxxxxxxxxx"])
    def test_a_bad_key_file_is_ignored(self, env, bad):
        mod, pws, tmp = env
        _with_key(tmp, bad)
        r = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1")
        assert "maps.googleapis.com" not in r.headers["Content-Security-Policy"]
        assert "maps.googleapis.com" not in r.get_data(as_text=True)


class TestPages:
    def test_map_zooms_with_the_wheel_goes_satellite_and_flies_to_the_park(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
        assert "scrollWheelZoom:true" in h and "scrollWheelZoom:false" not in h
        assert "m.on('zoomend',auto)" in h and "z>=14" in h and "flyTo" in h
        # only a real click on the layer switch counts as the reader's choice - Leaflet fires 'baselayerchange'
        # for programmatic swaps too, which silently disabled the switch (found in the browser, 5 Nov)
        assert "baselayerchange" not in h and "closest('.leaflet-control-layers-base')" in h
        pts = json.loads(re.search(r"var P=(\[.*?\]);var m=", h).group(1))
        assert pts and all(p["ph"] for p in pts)                              # every park popup has a picture

    def test_five_chart_views(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
        assert h.count('class="chpane"') == 5 and h.count('data-ch="co2_cum"') == 2
        assert "Year by year" in h

    def test_logos_frameless_bw_in_customer_views_colour_in_tables(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        cust = cl.get("/lighting/customers/").get_data(as_text=True)
        assert 'class="clogo mid plain bw"><img src="/lighting/logo/acme.png"' in cust
        table = cl.get("/lighting/buildings/").get_data(as_text=True).split("<h2>Projects")[1]
        assert 'class="clogo plain"><img src="/lighting/logo/acme.png"' in table and "plain bw" not in table

    def test_pipeline_says_whether_the_rest_was_audited(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/pipeline/").get_data(as_text=True)
        assert "ARGIA audit of the rest" in h and "does not mean the rest was audited" in h
        row = lambda code: re.search(r"<b>" + code + r"</b>.*?</tr>", h, re.S).group(0)
        assert "<td>yes</td>" in row("MXC09904")                             # audited, never converted
        assert "<td>no</td>" in row("MXC09902")                              # partly converted, rest not audited
