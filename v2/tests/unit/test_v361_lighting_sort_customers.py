"""v361 - Tomasz, 5 Nov 2026:

* "in many tables sorting is not working in all columns" - two cells without a value ('-') in one column made the
  sort's comparison throw, so the browser dropped the whole sort; numbers with units sorted as text; 46 of 54 tables
  had no sorting at all. Now every data table sorts, empty cells last in both directions.
* "in the buildings not yet delivered we should add column for customer and we should have indicator that the customer
  is actually already having buildings if they do"
* "lets change name from SOLAR O&M PLATFORM to O&M PLATFORM"

All data invented (public repo).
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, _clean, _p, env, sign_in   # noqa: F401  (fixture re-used)
from argia.prologis import lighting as LT


def _data():
    d = copy.deepcopy(DATA)
    d["opportunities"] = [
        {"id": 1, "yardi": "MXC09904", "customer": "Vacante", "area_m2": 20000, "audit": True, "study": True, "proforma": True, "last_proforma": "2025-06-05"},
        {"id": 2, "yardi": "MXC09907", "customer": "Acme Logistics SA de CV", "area_m2": 15000, "audit": True, "study": False, "proforma": True, "last_proforma": "2026-01-10"},
        {"id": 3, "yardi": "MXC09907", "customer": "XB", "area_m2": 15000, "audit": True, "study": False, "proforma": False, "last_proforma": None},
    ]
    return d


def _write(tmp, d):
    f = tmp / "lighting" / "lighting.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 9, time.time() + 9))


class TestOccupants:
    def test_who_is_in_the_building(self):
        ds = LT.parse(_data())
        rows = {r.building.yardi: r for r in LT.pipeline(ds)}
        fp = LT.footprint(ds)
        acme = LT.occupants(rows["MXC09907"], ds, fp)
        assert [(o.name, o.source, o.led_elsewhere, o.group) for o in acme] == [("Acme Logistics SA de CV", "opportunity", 1, "acme"),
                                                                              ("XB", "opportunity", 0, None)]
        assert [(o.name, o.source, o.led_elsewhere) for o in LT.occupants(rows["MXC09902"], ds, fp)] == [("Bravo Foods", "project", 0)]
        assert LT.occupants(rows["MXC09904"], ds, fp) == [] and LT.vacant(rows["MXC09904"])
        assert LT.occupants(rows["MXC09909"] if "MXC09909" in rows else rows["MXC09904"], ds, fp) == []

    def test_name_matching_is_strict(self):
        fp = {"tecma civco": ("tecma", {"A"}), "epc": ("epc", {"B"}), "rogers": ("rogers", {"C"})}
        assert LT._find(fp, "tecma")[0] == "tecma" and LT._find(fp, "rogers foam")[0] == "rogers"
        assert LT._find(fp, "ep logistica")[0] is None and LT._find(fp, "tec")[0] is None and LT._find(fp, "epc")[0] == "epc"
        assert LT._find(fp, "tecmatic")[0] is None                                    # whole words only
        for v in ("Vacante", "Vacant space", "Common areas", "Exterior lighting", "Replacement stock", "", None):
            assert LT.is_vacant(v)
        assert not LT.is_vacant("Acme")


class TestPipelinePage:
    def test_customer_column_and_star(self, env):
        mod, pws, tmp = env
        _write(tmp, _data())
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        h = cl.get("/lighting/pipeline/").get_data(as_text=True)
        _clean(h, "pipeline")
        assert "<th>Customer</th>" in h
        row = re.search(r"<tr><td class=\"nw\"><b>MXC09907</b>.*?</tr>", h, re.S).group(0)
        assert "<b>Acme Logistics SA de CV</b>" in row and '<a class="pill ledpill" href="/lighting/buildings/?c=acme"' in row
        assert "★ LED in 1 other building</a>" in row and "<b>XB</b>" in row and row.count("ledpill") == 1
        row2 = re.search(r"<tr><td class=\"nw\"><b>MXC09902</b>.*?</tr>", h, re.S).group(0)
        assert "Bravo Foods" in row2 and "part already LED" in row2 and "ledpill" not in row2
        row4 = re.search(r"<tr><td class=\"nw\"><b>MXC09904</b>.*?</tr>", h, re.S).group(0)
        assert '<td data-v="Vacant"><span class="muted">Vacant</span></td>' in row4
        assert cl.get("/lighting/buildings/?c=acme").status_code == 200

    def test_csv_has_the_customer(self, env):
        mod, pws, tmp = env
        _write(tmp, _data())
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        rows = list(csv.reader(io.StringIO(cl.get("/lighting/pipeline.csv").get_data(as_text=True).lstrip("﻿"))))
        assert rows[0][-2:] == ["customer", "customer_led_other_buildings"]
        by = {r[0]: r for r in rows[1:]}
        assert by["MXC09907"][-2:] == ["Acme Logistics SA de CV; XB", "1; 0"] and by["MXC09904"][-2:] == ["Vacant", ""]


def test_header_says_om_platform(env):
    mod, pws, tmp = env
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
    assert '<span class="tag">O&M platform</span>' in h.replace("&amp;", "&") and "Solar O&M" not in h.replace("&amp;", "&")
    login = mod.app.test_client().get("/login").get_data(as_text=True).replace("&amp;", "&")
    assert "O&M platform - Mexico" in login and "Solar O&M" not in login


pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")

TABLE = """<table class="t" id="a"><tr><th>Name</th><th>Share</th><th>When</th><th>WO</th><th>Money</th><th></th></tr>
<tr><td>Building 10</td><td>100 % LED</td><td>2024-01-05</td><td>2025-339</td><td>-$5</td><td>x</td></tr>
<tr><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>x</td></tr>
<tr><td>Building 9</td><td>20 % LED</td><td data-v="">- <a>Simulate</a></td><td>2025-258</td><td>$1,234</td><td>x</td></tr>
<tr><td>Ábaco</td><td>5 % LED</td><td colspan="2">merged</td><td>$7</td><td>x</td></tr>
<tr><td>-</td><td>0 % LED</td><td>2023-12-31</td><td>2024-1001</td><td>-</td><td>x</td></tr></table>
<table class="t" id="grp"><tr><th>Park</th><th>m²</th></tr><tr><td colspan="2">Park title</td></tr><tr><td>B</td><td>2</td></tr><tr><td>A</td><td>1</td></tr></table>
<table class="t nosort" id="no"><tr><th>K</th><th>V</th></tr><tr><td>b</td><td>2</td></tr><tr><td>a</td><td>1</td></tr></table>"""


def test_sorting_in_a_browser(env):
    """Every column sorts both ways; empty cells stay last; units, money, dates and codes in the right order."""
    mod, pws, tmp = env
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.set_content(TABLE + mod.SORT_JS)

        def col(i):
            return pg.evaluate(f"""() => [].slice.call(document.getElementById('a').rows,1).map(r => {{
                var x=0; for (const c of r.cells) {{ if (x==={i}) return c.textContent.trim(); x+=c.colSpan; if (x>{i}) return '~'; }} return '~'; }})""")

        def click(i):
            pg.click(f"#a tr:first-child th:nth-child({i + 1})")
        # (ascending values, values that must come last in both directions); a merged cell counts where it starts
        expect = {0: (["Ábaco", "Building 9", "Building 10"], {"-"}),
                  1: (["0 % LED", "5 % LED", "20 % LED", "100 % LED"], {"-"}),
                  2: (["2023-12-31", "2024-01-05", "merged"], {"- Simulate", "-"}),
                  3: (["2024-1001", "2025-258", "2025-339"], {"-", "~"}),
                  4: (["-$5", "$7", "$1,234"], {"-"})}
        for i, (asc, last) in expect.items():
            click(i)
            got = col(i)
            assert got[:len(asc)] == asc and set(got[len(asc):]) == last, (i, "asc", got)
            click(i)
            got = col(i)
            assert got[:len(asc)] == asc[::-1] and set(got[len(asc):]) == last, (i, "desc", got)
        assert pg.eval_on_selector_all("#a th.srt", "e => e.length") == 5                  # the empty header is not a column
        assert pg.eval_on_selector_all("#grp th.srt", "e => e.length") == 0                 # group rows: left as it is
        assert pg.eval_on_selector_all("#no th.srt", "e => e.length") == 0
        assert errs == []
        b.close()
