"""v383 - "open a ticket" from the string and inverter views (invented plant and serials only)."""
from __future__ import annotations

import json
import pathlib
import re
from urllib.parse import parse_qs, urlparse

import pytest

from argia.epi import views as V
from argia.maintenance import ticket_links as TL
from argia.report import epi_views_html as H
from argia.report import string_layout_html as SH
from tests.unit.test_v382_epi_views import DAY, MX, faults, synth

V2 = pathlib.Path(__file__).resolve().parents[2]
EM = chr(0x2014)


def form(url):
    u = urlparse(url)
    assert u.path == "/maintenance/new/"
    return {k: v[0] for k, v in parse_qs(u.query).items()}


def test_tokens():
    assert TL.input_token("str", 4) == "input 4" and TL.input_token("pv", 16) == "input 16"
    assert TL.input_token("mppt", 2) == "MPPT 2"
    assert TL.token_of_channel("s12") == "input 12" and TL.token_of_channel("m1") == "MPPT 1"


TICKETS = [("T-1", "DEMO", "SN1", "Inverter 1 input 12: no output (2026-03-20)"),
           ("T-2", "DEMO", "SN2", "Inverter 2: -40% vs the other inverters (2026-03-20)"),
           ("T-3", "OTHER", "SN1", "Inverter 1 input 2: low"),
           ("T-4", "DEMO", "SN3", "inverter 3 MPPT 2 fuse")]


def test_match_open_whole_input_only():
    assert TL.match_open(TICKETS, "DEMO", "SN1", "input 12") == "T-1"
    assert TL.match_open(TICKETS, "DEMO", "SN1", "input 1") is None          # 'input 1' is not 'input 12'
    assert TL.match_open(TICKETS, "DEMO", "SN1", "input 2") is None          # T-3 is another plant
    assert TL.match_open(TICKETS, "demo", "SN3", "MPPT 2") == "T-4"           # case-insensitive
    assert TL.match_open(TICKETS, "DEMO", "SN2") == "T-2"                     # inverter level
    assert TL.match_open(TICKETS, "DEMO", "SN1") is None                      # T-1 is a string ticket


def test_new_url_carries_the_fields():
    f = form(TL.new_url("demo", "SN1", "x" * 200, "line 1\nline 2 & more", "array/string", "P2"))
    assert f == {"plant": "DEMO", "sn": "SN1", "category": "array/string", "priority": "P2",
                 "title": "x" * 140, "description": "line 1\nline 2 & more"}


@pytest.fixture(scope="module")
def payload():
    (pm, strs, chan, sn_of), station, samples, ac, _tel = synth(faults)
    p = V.build(pm, strs, chan, sn_of, station, samples, ac, DAY, MX,
                inv_labels={f"INV{i}": f"Inverter {i}" for i in (1, 2, 3, 4)})
    return V.apply_mtd(p, None, 1), {s.uid: TL.input_token(c.kind, c.index) for s, c in ((s, chan[s.uid]) for s in strs)}


def test_epi_tickets_what_gets_a_button(payload):
    p, tokens = payload
    tk = TL.epi_tickets(p, "DEMO", "Demo plant", [], tokens)
    # low string INV1:S2 (-30 %), the three dead strings of INV2; not the high one (INV2:S5 +25 %)
    assert set(tk["s"]) == {"INV1:S2", "INV2:S3", "INV2:S4", "INV2:S6"}
    low = form(tk["s"]["INV1:S2"]["url"])
    assert low["plant"] == "DEMO" and low["sn"] == "SN1" and low["category"] == "array/string" and low["priority"] == "P3"
    assert low["title"] == "Inverter 1 input 2: -30% vs the inverter's other strings (2026-03-21)"
    assert "Plant: Demo plant. Day: 2026-03-21." in low["description"] and "String EPI-600 month to date" in low["description"]
    dead = form(tk["s"]["INV2:S4"]["url"])
    assert dead["priority"] == "P2" and dead["title"] == "Inverter 2 input 4: no output (2026-03-21)"
    # inverters with a dip against their peers: INV2 (half dead) and INV3 (11:00-11:25)
    assert set(tk["i"]) == {"INV2", "INV3"}
    inv3 = form(tk["i"]["INV3"]["url"])
    assert inv3["category"] == "inverter/derating" and inv3["title"] == "Inverter 3: -33% vs the other inverters (2026-03-21)"
    assert "11:00-11:25 (-33%)" in inv3["description"]


def test_a_ticket_opened_from_the_card_is_found_next_time(payload):
    p, tokens = payload
    first = TL.epi_tickets(p, "DEMO", "Demo plant", [], tokens)
    made = [(f"T-{k}", "DEMO", form(e["url"])["sn"], form(e["url"])["title"])
            for k, e in enumerate(list(first["s"].values()) + list(first["i"].values()))]
    again = TL.epi_tickets(p, "DEMO", "Demo plant", made, tokens)
    assert all("open" in e for e in again["s"].values()) and all("open" in e for e in again["i"].values())
    assert again["s"]["INV1:S2"]["open"] == made[0][0]


def test_no_serial_no_button(payload):
    p, tokens = payload
    p2 = json.loads(json.dumps(p))
    for i in p2["inverters"]:
        i["sn"] = ""
    tk = TL.epi_tickets(p2, "DEMO", "Demo", [], tokens)
    assert tk == {"s": {}, "i": {}}


def test_layout_open():
    strings = [{"sn": "SN1", "ch": "s12"}, {"sn": "SN1", "ch": "s1"}, {"sn": "SN3", "ch": "m2"}, {"sn": "SN9", "ch": "bad"}]
    assert TL.layout_open(strings, "DEMO", TICKETS) == {"0": "T-1", "2": "T-4"}


def test_cards_render_the_buttons(payload):
    p, tokens = payload
    p = json.loads(json.dumps(p))
    p["tk"] = TL.epi_tickets(p, "DEMO", "Demo plant", TICKETS, tokens)
    h = H.card("DEMO", p)
    assert "function tkcell" in h and "Open ticket" in h and "Abrir ticket" in h and EM not in h
    blob = re.search(r'<script type="application/json" class="evdata">(.*?)</script>', h, re.S).group(1)
    assert json.loads(blob.replace("<\\/", "</"))["tk"]["i"]["INV3"]["url"].startswith("/maintenance/new/?")
    js = SH.JS
    assert "function tklink" in js and "c!=='z'&&c!=='r'" in js          # only very low / no current
    assert "'array/string'" in js and EM not in js and EM not in SH.CSS


def test_layout_card_puts_the_picture_full_width():
    css = SH.CSS
    assert ".slcard .slmain{display:flex;flex-direction:column" in css
    assert ".slcard .sldraw{width:100%" in css and ".slcard .sllegend{display:contents}" in css
    assert '<div class="slflag"><div class="slsum"></div><div class="sllist"></div></div>' in pathlib.Path(SH.__file__).read_text(encoding="utf-8")


def test_generator_wiring():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    on = src[src.index("def tickets_on"):src.index("TICKET_BY_ALERT = {}")]
    assert "== 'PPA'" in on                                                # never on CAPEX owners' pages
    assert src.count("if tickets_on(pk):") == 2


@pytest.mark.parametrize("js", [SH.JS, H.JS], ids=["layout", "epi"])
def test_card_scripts_parse(js, tmp_path):
    """v383: a literal line break inside a JS string killed the whole layout card once - parse both scripts."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    f = tmp_path / "card.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_wide_cards_beat_the_portal_card_rule():
    """v383.1: the two big cards use up to 1600 px, centred on the window. The portal scopes the
    monitoring CSS under .monbody, and '.monbody .card{margin:14px 0}' silently cancelled the
    centring once (cards ran 76 px off screen) - the rule must carry .monbody .card.<cls>."""
    for css, cls in ((SH.CSS, "slcard"), (H.CSS, "evcard")):
        assert f".card.{cls},.monbody .card.{cls}{{width:min(1600px,calc(100vw - 32px))" in css
        assert "margin-left:calc(50% - min(800px,50vw - 16px))" in css
    assert ".slcard .slnm{white-space:nowrap}" in SH.CSS
