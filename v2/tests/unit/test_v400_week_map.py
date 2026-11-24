"""v400 - the strings x days map of the week view had cells far larger than the month's (Tomasz: "when we
are showing week inverters it is super large, please adjust the size to match the month, also add day
numbers so we know which day is where, and ... keep percentages inside the cells"). Invented plant only."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from argia.epi import views as V
from argia.report import epi_views_html as H
from tests.unit.test_v386_consumption_views import _payload

N_DAYS = 30


def _hist_payload():
    p = _payload()
    t = p["tallies"]
    days, d0 = [], dt.date(2026, 2, 20)
    for k in range(N_DAYS):
        tl = json.loads(json.dumps(t))
        if k == N_DAYS - 1:                                  # the last day: one string above 100 %
            u = next(iter(p["strings"].values()))["uids"][0]
            r, e, n = tl["epi600"][u]
            tl["epi600"][u] = [e * 1.08, e, n]
        days.append(((d0 + dt.timedelta(days=k)).isoformat(), tl))
    p["hist"] = V.period(days, {"inv": [i["inv"] for i in p["inverters"]],
                                "str": [u for s in p["strings"].values() for u in s["uids"]]})
    return p


def test_layout_is_fixed_to_the_month():
    js = H.JS
    assert "var CM=30,cw=Math.max(10,Math.min(28,Math.floor(820/CM)))" in js     # never the number of days shown
    assert "Wm=110+CM*cw" in js and "max-width:'+Wm+'px" in js                      # no stretching to the card
    assert "d.slice(8)" in js                                                        # day numbers over the columns
    assert "Math.round(e*100)" in js and "<title>" in js                             # value in the cell + hover
    assert chr(0x2014) not in js and chr(0x2013) not in js


CHROME = "<style>svg{max-width:100%;height:auto;display:block}</style>"            # the portal's own rule


def _measure(pg, view):
    pg.click(f'.evseg button[data-v="{view}"]')
    return pg.evaluate("""() => {
        const m = document.querySelector('svg.evmap');
        const r = m.querySelector('rect').getBoundingClientRect();
        const labels = [...m.querySelectorAll('text[font-size="9"]')].map(t => t.textContent);
        const cells = [...m.querySelectorAll('text[pointer-events="none"]')].map(t => t.textContent);
        const titles = [...m.querySelectorAll('rect title')].map(t => t.textContent);
        return {w: r.width, h: r.height, svgw: m.getBoundingClientRect().width, labels, cells, titles,
                maps: document.querySelectorAll('svg.evmap').length};
    }""")


@pytest.mark.parametrize("width", [1280, 700])
def test_in_a_browser_week_cells_equal_month_cells(width):
    pw = pytest.importorskip("playwright.sync_api")
    html = "<!doctype html><html><head>" + CHROME + "</head><body>" + H.card("DEMO", _hist_payload()) + "</body></html>"
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                                   # noqa: BLE001
            pytest.skip(f"no browser: {e}")
        pg = b.new_page(viewport={"width": width, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.set_content(html)
        wk, mo = _measure(pg, "w"), _measure(pg, "m")
        b.close()
    assert not errors
    assert wk["maps"] == mo["maps"] >= 2                                             # one map per inverter
    assert wk["w"] == pytest.approx(mo["w"], abs=0.01) and wk["h"] == pytest.approx(mo["h"], abs=0.01)
    assert wk["svgw"] == pytest.approx(mo["svgw"], abs=0.01) and wk["svgw"] <= width  # same drawing width
    if width >= 1000:
        assert 20 <= wk["w"] <= 30                                                   # month-sized, not stretched
    first = wk["labels"][:7]
    assert first == ["15", "16", "17", "18", "19", "20", "21"]                       # the last 7 of 20 Feb..21 Mar
    assert mo["labels"][:30][0] == "20" and mo["labels"][:30][-1] == "21"
    assert all(c.isdigit() for c in wk["cells"]) and wk["cells"]                    # whole percent in the cells
    assert max(int(c) for c in mo["cells"]) == 100                                   # 108 % shown as 100
    assert any(t.endswith("100.0 %") for t in wk["titles"]) and all(t.endswith(" %") or t.endswith("n/a") for t in wk["titles"])
