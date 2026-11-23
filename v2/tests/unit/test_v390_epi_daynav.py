"""v390 - the troubleshooting card has its own day picker (Tomasz: "add a date picker to this graph, so the
user doesnt have to go up the page in order to move days back and forth"). Invented plant only."""
from __future__ import annotations

import pathlib

import pytest

from argia.report import epi_views_html as H
from tests.unit.test_v386_consumption_views import _payload

V2 = pathlib.Path(__file__).resolve().parents[2]
NAV = {"day": "2026-11-20", "today": "2026-11-23", "first": "2026-10-01", "base": "/monitoring/demo",
       "prev": "2026-11-19", "next": "2026-11-21"}


def test_links_land_back_on_the_card():
    h = H.day_nav("DEMO", NAV)
    assert 'href="/monitoring/demo/d/2026-11-19.html#evDEMO"' in h                  # previous day
    assert 'href="/monitoring/demo/d/2026-11-21.html#evDEMO"' in h                  # next day
    assert 'href="/monitoring/demo/#evDEMO"' in h and ">Today<" in h               # the live page, same card
    assert 'value="2026-11-20" min="2026-10-01" max="2026-11-23"' in h
    assert 'data-base="/monitoring/demo" data-today="2026-11-23" data-card="evDEMO"' in h


def test_today_has_no_next_and_no_today_button():
    h = H.day_nav("DEMO", dict(NAV, day="2026-11-23", next=None, prev="2026-11-22"))
    assert 'class="evnb off evnext"' in h and 'aria-disabled="true"' in h and "evtoday" not in h
    assert 'href="/monitoring/demo/d/2026-11-22.html#evDEMO"' in h


def test_oldest_day_has_no_previous():
    h = H.day_nav("DEMO", dict(NAV, day="2026-10-01", prev=None))
    assert 'class="evnb off evprev"' in h


def test_the_next_link_to_today_is_the_live_page():
    h = H.day_nav("DEMO", dict(NAV, day="2026-11-22", next="2026-11-23"))
    assert 'href="/monitoring/demo/#evDEMO" title="Next day: 2026-11-23"' in h


def test_in_the_card_head_next_to_this_day():
    h = H.card("DEMO", _payload(), nav=NAV)
    head = h[h.index('class="evhead"'):h.index('class="evmain"')]
    assert 'class="evnav"' in head and head.index('class="evnav"') < head.index("this day")
    assert 'id="evDEMO"' in h                                                        # the anchor exists
    assert 'class="evnav"' not in H.card("DEMO", _payload())                         # without nav: unchanged
    assert chr(0x2014) not in h


def test_view_and_inverter_survive_a_day_change():
    js = H.JS
    assert "sessionStorage.getItem(VK)" in js and "sessionStorage.setItem(VK" in js
    assert "view=b.dataset.v;keep();draw();" in js and "sel=b.dataset.sel;keep();draw();" in js
    assert "d<dp.min||d>dp.max" in js                                                # no page outside the range


def test_generator_wiring():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    assert "nav=day_nav(pk, d)" in src and "def day_nav(pk, d):" in src
    assert "days = sorted(set(DATES) | {TODAY})" in src


def test_in_a_browser():
    """Strings view chosen, a date picked: the browser goes to that day's page at this card, and the new
    page opens in the Strings view (the pages are served from memory, no server)."""
    pw = pytest.importorskip("playwright.sync_api")
    html = '<!doctype html><html><body>' + H.card("DEMO", _payload(), nav=NAV) + '</body></html>'
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                                   # noqa: BLE001
            pytest.skip(f"no browser: {e}")
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route("http://portal.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
        pg.goto("http://portal.test/monitoring/demo/d/2026-11-20.html")
        assert pg.get_attribute('.evseg button[data-v="i"]', "class") == "on"        # default view
        pg.click('.evseg button[data-v="s"]')
        pg.evaluate("""() => { const d = document.querySelector('.evdate'); d.value = '2026-11-18';
                       d.dispatchEvent(new Event('change')); }""")
        pg.wait_for_url("http://portal.test/monitoring/demo/d/2026-11-18.html#evDEMO", timeout=5000)
        pg.wait_for_selector(".evseg button.on")
        assert pg.get_attribute('.evseg button[data-v="s"]', "class") == "on"        # the view came along
        pg.click("a.evprev")                                                           # the arrow
        pg.wait_for_url("http://portal.test/monitoring/demo/d/2026-11-19.html#evDEMO", timeout=5000)
        b.close()
    assert not errors
