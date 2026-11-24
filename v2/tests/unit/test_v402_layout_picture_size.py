"""v402 - the string layout picture was far too big for square or tall drawings (Tomasz, 24 Nov 2026: "it is way
to big, currently only Taigene and Plastic Omnium looks ok, the rest is way too big"). Every picture was drawn at
the card's full width: a drawing as tall as it is wide (SAG 1.10, Vitalmex 1.14, Turistica Arizona 1.19) came out
1,100-1,300 px high. Now a picture is never taller than 75 % of the card width (the height the landscape plants
already had), and keeps its shape. Invented plant only."""
from __future__ import annotations

import base64

import pytest

from argia.report import string_layout_html as SH
from tests.unit.test_v318_string_layout_views import _doc


def _img(w, h):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="{w}" height="{h}" fill="#888"/></svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def test_the_script_caps_the_picture():
    js = SH.JS
    assert "PIC_MAX_RATIO=0.75;" in js
    assert "pic.style.maxWidth=Math.min(100,PIC_MAX_RATIO*100/ar)" in js and "margin:0 auto" in SH.CSS
    assert chr(0x2014) not in js and chr(0x2013) not in js


@pytest.mark.parametrize("vw,vh", [(1280, 900), (1280, 600), (420, 860)])
def test_in_a_browser_tall_pictures_shrink_wide_ones_keep_the_width(vw, vh):
    pw = pytest.importorskip("playwright.sync_api")
    doc = _doc()
    doc["drawings"][0]["box"] = [0, 0, 600, 660]          # aspect 1.10, like SAG
    doc["drawings"][1]["box"] = [0, 0, 1000, 730]         # aspect 0.73, like Taigene
    p = SH.payload("GTO1", doc, {"A": "Inverter 5"}, live=False, source="none",
                   img_url={"drawing": _img(600, 660), "helio": _img(1000, 730)})
    html = ('<!doctype html><html><head><style>svg{max-width:100%;height:auto;display:block}</style></head>'
            '<body style="margin:0;padding:16px"><div style="max-width:1100px;margin:auto">' + SH.card(p) + '</div></body></html>')
    with pw.sync_playwright() as pl:
        try:
            b = pl.chromium.launch()
        except Exception as e:                                   # noqa: BLE001
            pytest.skip(f"no browser: {e}")
        pg = b.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.set_content(html)
        pg.wait_for_function("document.querySelector('.slpic img').complete && document.querySelector('.slpic img').naturalWidth > 0")
        m = lambda: pg.evaluate("""() => { const p = document.querySelector('.slpic').getBoundingClientRect(),
            d = document.querySelector('.sldraw').getBoundingClientRect(),
            s = document.querySelector('.slsvg').getBoundingClientRect();
            return {w: p.width, h: p.height, dw: d.width, sw: s.width, sh: s.height, left: p.left - d.left}; }""")
        tall = m()
        pg.click('.sltabs [data-dr="1"]')
        pg.wait_for_function("document.querySelector('.slpic img').naturalWidth === 1000")
        wide = m()
        b.close()
    assert not errors
    # the tall drawing: never taller than 75 % of the card width, shape kept, centred
    assert tall["h"] == pytest.approx(0.75 * tall["dw"], abs=4)                   # + the 1 px border
    assert tall["h"] / tall["w"] == pytest.approx(1.10, abs=0.02)
    assert tall["left"] == pytest.approx((tall["dw"] - tall["w"]) / 2, abs=1.5)
    # the overlay with the string dots covers exactly the picture (inside its 1 px border)
    assert (tall["sw"], tall["sh"]) == (pytest.approx(tall["w"] - 2, abs=1), pytest.approx(tall["h"] - 2, abs=1))
    # the landscape picture (Taigene, Plastic Omnium) keeps the full width: unchanged
    assert wide["w"] == pytest.approx(wide["dw"], abs=1)
