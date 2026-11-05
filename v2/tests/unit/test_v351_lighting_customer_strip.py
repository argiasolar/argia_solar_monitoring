"""v351 - the overview's customer logos in two rows that scroll sideways (Tomasz, 5 Nov 2026):
"make the space for customer logos in two rows only with possibility to scroll or move right and left",
"make a search window on the Customers page", "Vacant Space add prologis logo".

Every customer with a delivered project is in the strip (it was cut at 24), largest first; two rows, a
horizontal scroll (swipe on a phone, wheel / trackpad / arrow buttons on a desktop). The browser part runs
where Playwright + Chromium are installed and is skipped elsewhere.

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

N = 30


def _many(tmp):
    d = copy.deepcopy(DATA)
    for i in range(N):
        d["projects"].append(_p(100 + i, customer=f"Company {i:02d}", group=f"co-{i:02d}", fixtures=1000 - i))
        d["customers"][f"co-{i:02d}"] = {"name": f"Company {i:02d}", "logo": None}
    f = tmp / "lighting" / "lighting.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    return d


def _customers(env):
    mod, pws, tmp = env
    d = _many(tmp)
    return d, sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)


def _overview(env):
    mod, pws, tmp = env
    d = _many(tmp)
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
    return d, h


class TestMarkup:
    def test_every_delivered_customer_is_in_the_strip_largest_first(self, env):
        d, h = _overview(env)
        strip = re.search(r'<div class="lgrid2 hrow" id="lgrid2">(.*?)</div><button', h, re.S).group(1)
        keys = re.findall(r'<a class="ltile" href="/lighting/buildings/\?c=([^"]+)"', strip)
        delivered = {p["group"] for p in d["projects"] if p["status"] == "delivered" and p.get("group")}
        assert set(keys) == delivered and len(keys) == len(delivered) > 24           # no longer cut at 24
        assert keys.index("co-00") < keys.index("co-29")                             # largest first

    def test_two_rows_and_both_arrows(self, env):
        _, h = _overview(env)
        assert "grid-auto-flow:column;grid-template-rows:repeat(2,auto)" in h and "overflow-x:auto" in h
        assert h.count('class="lsb"') == 2 and 'data-d="-1"' in h and 'data-d="1"' in h
        assert 'aria-label="Scroll left"' in h and 'aria-label="Scroll right"' in h
        assert '<a class="r" href="/lighting/customers/">All customers' in h            # the full page is one click away


class TestCustomersPage:
    def test_logo_wall_is_two_scrolling_rows(self, env):
        _, h = _customers(env)
        assert '<div class="lwall hrow">' in h and h.count('class="lsb"') == 2
        assert "grid-template-rows:repeat(2,64px)" in h

    def test_search_box_and_keys_without_accents(self, env):
        mod, pws, tmp = env
        d = _many(tmp)
        d["customers"]["co-00"]["name"] = "Sincronía Logística"
        f = tmp / "lighting" / "lighting.json"
        f.write_text(json.dumps(d), encoding="utf-8")
        os.utime(f, (time.time() + 9, time.time() + 9))
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        assert '<input type="search" id="csearch"' in h and 'placeholder="Search a customer or a park"' in h
        assert 'id="cnone" hidden' in h
        card = re.search(r'<a class="svc ccard" href="/lighting/buildings/\?c=co-00" data-q="([^"]+)"', h).group(1)
        assert card == "sincronia logistica co-00 parque uno"                       # name, key and parks; no accents
        n_cards = len(re.findall(r'<a class="svc ccard"', h))
        assert f'id="ccount">{n_cards} customers<' in h


class TestVacantSpace:
    def test_vacant_space_shows_the_prologis_logo(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"].append(_p(77, kind="vacant", customer="Vacant space", group=None))
        f = tmp / "lighting" / "lighting.json"
        f.write_text(json.dumps(d), encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert '<span class="clogo big plain mono" style="background:#9aa4ad">VS</span>' in cl.get("/lighting/p/77").get_data(as_text=True)
        (tmp / "brand").mkdir(exist_ok=True)
        (tmp / "brand" / "prologis_logo.png").write_bytes(_png())
        h = cl.get("/lighting/p/77").get_data(as_text=True)
        assert '<span class="clogo big plain"><img class="" src="/brand/prologis_logo.png" alt="Prologis"></span>' in h
        ext = cl.get("/lighting/p/4").get_data(as_text=True)                              # v353: exteriors are Prologis's too
        assert '<div class="lhead"><span class="clogo big plain"><img class="" src="/brand/prologis_logo.png"' in ext
        assert '<div class="lhead"><span class="clogo big plain"><img src="/lighting/logo/acme.png' in cl.get("/lighting/p/1").get_data(as_text=True)


pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as ex:                                                     # noqa: BLE001
            pytest.skip(f"no Chromium: {ex}")
        yield b
        b.close()


def _page(browser, html, width):
    pg = browser.new_page(viewport={"width": width, "height": 900})
    pg.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())   # offline: no CDN
    pg.set_content(html)
    pg.wait_for_timeout(200)
    return pg


class TestInTheBrowser:
    def test_desktop_two_rows_scroll_with_the_arrows(self, env, browser):
        _, h = _overview(env)
        pg = _page(browser, h, 1440)
        tops = pg.eval_on_selector_all("#lgrid2 .ltile", "els => [...new Set(els.map(e => e.offsetTop))]")
        assert len(tops) == 2, tops
        assert pg.evaluate("(g => g.scrollWidth > g.clientWidth)(document.getElementById('lgrid2'))")
        assert pg.is_disabled('.lsb[data-d="-1"]') and pg.is_enabled('.lsb[data-d="1"]')
        pg.click('.lsb[data-d="1"]')
        pg.wait_for_timeout(700)
        assert pg.evaluate("document.getElementById('lgrid2').scrollLeft") > 300
        assert pg.is_enabled('.lsb[data-d="-1"]')
        assert pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1   # only the strip scrolls
        pg.close()

    def test_phone_swipes_without_arrows_and_no_page_overflow(self, env, browser):
        _, h = _overview(env)
        pg = _page(browser, h, 390)
        assert len(pg.eval_on_selector_all("#lgrid2 .ltile", "els => [...new Set(els.map(e => e.offsetTop))]")) == 2
        assert not pg.is_visible('.lsb[data-d="1"]')
        assert pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        pg.close()

    def test_search_filters_wall_and_cards(self, env, browser):
        _, h = _customers(env)
        pg = _page(browser, h, 1440)
        tops = pg.eval_on_selector_all(".lwall > a", "els => [...new Set(els.map(e => e.offsetTop))]")
        assert len(tops) <= 2
        pg.fill("#csearch", "company 07")
        pg.wait_for_timeout(100)
        vis = pg.eval_on_selector_all(".ccard", "els => els.filter(e => !e.hidden).map(e => e.querySelector('h3').textContent)")
        assert vis == ["Company 07"]
        assert pg.inner_text("#ccount").startswith("1 of ") and pg.is_hidden("#cnone")
        pg.fill("#csearch", "parque uno")                                           # a park finds its customers
        pg.wait_for_timeout(100)
        assert pg.eval_on_selector_all(".ccard", "els => els.filter(e => !e.hidden).length") > 1
        pg.fill("#csearch", "zzzz")
        pg.wait_for_timeout(100)
        assert pg.is_visible("#cnone") and pg.inner_text("#ccount").startswith("0 of ")
        pg.fill("#csearch", "")
        pg.wait_for_timeout(100)
        assert pg.eval_on_selector_all(".ccard", "els => els.filter(e => e.hidden).length") == 0
        pg.close()
