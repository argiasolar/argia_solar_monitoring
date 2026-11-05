"""v355 - Tomasz, 5 Nov 2026 (on All buildings and Customers):

* "lets add city column here" - All buildings has Park and City as separate (sortable) columns
* "remove the logo carousel from all customers, and make them a-z sorted"
* "when I search for a it should already remove companies that doesnt start with an a" - the search matches the
  START of the company name (accents ignored); from 3 letters a park also matches (a word of its name)

All data invented (public repo).
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, _p, env, sign_in   # noqa: F401  (fixture re-used)

NAMES = ["Zeta Foods", "acme Parts", "Álamo Logistics", "Beta", "Arco Steel", "Tata Consultancy", "Cooper Standard"]


def _data(tmp):
    d = copy.deepcopy(DATA)
    for i, n in enumerate(NAMES):
        g = f"x-{i}"
        d["projects"].append(_p(300 + i, customer=n, group=g, park="Agave Logistic Center" if i == 3 else "Parque Uno"))
        d["customers"][g] = {"name": n, "logo": None}
    f = tmp / "lighting" / "lighting.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))


def _names(h):
    return re.findall(r'<a class="svc ccard"[^>]*>.*?<h3>([^<]+)</h3>', h, re.S)


class TestMarkup:
    def test_all_buildings_has_a_city_column(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/buildings/").get_data(as_text=True)
        assert "<th>Park</th><th>City</th>" in h
        assert re.search(r'<td>Parque Uno</td><td>Mexico City</td><td class="nw">MXC09901</td>', h)

    def test_customers_a_to_z_without_the_logo_wall(self, env):
        mod, pws, tmp = env
        _data(tmp)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        names = _names(h)
        key = lambda s: s.lower().replace("á", "a")
        assert names == sorted(names, key=key) and names[0] == "Acme Logistics"           # accents and case ignored
        assert names.index("Álamo Logistics") < names.index("Arco Steel") < names.index("Beta")
        assert 'class="lwall' not in h and "HSCROLL" not in h and 'class="lsb"' not in h
        assert "A to Z" in h

    def test_cards_carry_name_and_park_keys(self, env):
        mod, pws, tmp = env
        _data(tmp)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        assert 'data-n="alamo logistics" data-p="parque uno"' in h
        assert 'data-n="beta" data-p="agave logistic center"' in h


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


def test_search_matches_the_start_of_the_name(env, browser):
    mod, pws, tmp = env
    _data(tmp)
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
    pg.set_content(h)

    def shown(q):
        pg.fill("#csearch", q)
        pg.wait_for_timeout(60)
        return pg.eval_on_selector_all(".ccard", "els => els.filter(e => !e.hidden).map(e => e.querySelector('h3').textContent)")
    a = shown("a")
    assert a and all(n.lower().replace("á", "a").startswith("a") for n in a)          # "Tata", "Beta" are gone
    assert "Tata Consultancy" not in a and "Álamo Logistics" in a and "Arco Steel" in a
    assert shown("co") == ["Cooper Standard"]                                            # not "Tata Consultancy"
    assert shown("ÁLA") == ["Álamo Logistics"]
    assert shown("agave") == ["Beta"]                                                    # a park, from 3 letters
    assert pg.inner_text("#ccount").startswith("1 of ")
    assert shown("zz") == [] and pg.is_visible("#cnone")
    assert len(shown("")) == len(_names(h))
    pg.close()
