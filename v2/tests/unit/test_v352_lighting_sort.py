"""v352 - "make sure we can sort this by each column" (Tomasz, 5 Nov 2026, on the By park table).

Every lighting table with rows of things (by city, by park, all projects, buildings not yet on LED) sorts
by any column with a click (or Enter on the focused header): numbers as numbers, names A-Z ignoring
accents, empty cells always last, a "nothing matches" row left alone. The header of the scrolling By park
box stays visible. The browser part runs where Playwright + Chromium are installed.

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

PAGES = ("/lighting/", "/lighting/buildings/", "/lighting/pipeline/")


def _data(tmp):
    d = copy.deepcopy(DATA)
    for i, (park, fx) in enumerate([("Álamos", 50), ("Zeta", 9000), ("Beta", 700), ("Ébano", 120)]):
        d["projects"].append(_p(200 + i, park=park, fixtures=fx, customer=f"Co {i}", group="acme"))
    f = tmp / "lighting" / "lighting.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    return d


class TestMarkup:
    def test_the_tables_are_sortable_and_the_sorter_comes_with_them(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        for path in PAGES:
            h = cl.get(path).get_data(as_text=True)
            assert '<table class="t sortable">' in h and "table.t.sortable" in h, path
        h = cl.get("/lighting/").get_data(as_text=True)
        assert "<tr><th>City</th><th class=\"n\">" in h and "<tr><th>Park</th><th>City</th>" in h           # every column has a header
        p = cl.get("/lighting/p/1").get_data(as_text=True)                           # no sortable table: no script
        assert "table.t.sortable" not in p

    def test_sticky_header_and_sort_marks_in_the_css(self):
        import prologis_ui as UI
        assert ".sitebox table.t tr:first-child th{position:sticky;top:-4px" in UI.CSS
        assert ".sitebox>div{overflow:visible!important}" in UI.CSS                  # else the inner scroller swallows sticky
        assert 'th.srt[aria-sort=ascending]::after{content:" ▲"' in UI.CSS and "\x11" not in UI.CSS


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


def _page(browser, html):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.route("**/*", lambda r: r.abort() if r.request.url.startswith("http") else r.continue_())
    pg.set_content(html)
    return pg


def _col(pg, table_sel, ci):
    return pg.eval_on_selector(table_sel, f"t => [...t.rows].slice(1).map(r => r.cells[{ci}].textContent.trim())")


class TestInTheBrowser:
    def test_park_table_sorts_by_name_and_by_number(self, env, browser):
        mod, pws, tmp = env
        _data(tmp)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
        pg = _page(browser, h)
        tbl = ".sitebox table.t"
        pg.click(f"{tbl} th:nth-child(1)")                                           # park A-Z, accents ignored
        names = _col(pg, tbl, 0)
        assert names.index("Álamos") < names.index("Beta") < names.index("Ébano") < names.index("Zeta")
        assert pg.get_attribute(f"{tbl} th:nth-child(1)", "aria-sort") == "ascending"
        pg.click(f"{tbl} th:nth-child(4)")                                           # fixtures, numbers ascending
        fx = [int(x.replace(",", "")) for x in _col(pg, tbl, 3)]
        assert fx == sorted(fx)
        pg.click(f"{tbl} th:nth-child(4)")                                           # again: descending
        fx = [int(x.replace(",", "")) for x in _col(pg, tbl, 3)]
        assert fx == sorted(fx, reverse=True) and fx[0] == 9000
        assert pg.get_attribute(f"{tbl} th:nth-child(1)", "aria-sort") == "none"
        pg.eval_on_selector(".sitebox", "e => { e.style.maxHeight = '120px'; e.scrollTop = 300; }")   # header stays in view
        assert pg.eval_on_selector(".sitebox", "e => e.scrollTop") > 0
        pg.wait_for_timeout(100)
        box = pg.eval_on_selector(".sitebox", "e => e.getBoundingClientRect().top")
        head = pg.eval_on_selector(f"{tbl} th:nth-child(1)", "e => e.getBoundingClientRect().top")
        assert abs(head - box) < 8, (head, box)
        pg.focus(f"{tbl} th:nth-child(2)")
        pg.keyboard.press("Enter")                                                   # keyboard works too
        assert pg.get_attribute(f"{tbl} th:nth-child(2)", "aria-sort") == "ascending"
        pg.close()

    def test_empty_cells_last_and_no_match_row_untouched(self, env, browser):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        pg = _page(browser, cl.get("/lighting/buildings/").get_data(as_text=True))
        tbl = "table.t.sortable"
        heads = pg.eval_on_selector(tbl, "t => [...t.rows[0].cells].map(c => c.textContent.trim())")
        ci = heads.index("Year")
        for _ in range(2):
            pg.click(f"{tbl} th:nth-child({ci + 1})")
            years = _col(pg, tbl, ci)
            filled = [y for y in years if y not in ("", "-")]
            assert years[:len(filled)] == filled                                     # blanks at the end either way
        pg.close()
        pg = _page(browser, cl.get("/lighting/buildings/?q=zzzz-nothing").get_data(as_text=True))
        pg.click("table.t.sortable th:nth-child(2)")
        assert pg.eval_on_selector("table.t.sortable", "t => t.rows[1].cells[0].colSpan") == 11
        pg.close()
