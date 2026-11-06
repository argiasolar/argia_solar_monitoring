"""v362 - Tomasz, 5 Nov 2026: "rearrange the title, currently the city and park name is not alligned with anything,
the name, and the description under as well doesnt match anything, make it more professional".

One header card: the logo in a fixed tile, one text column (city and park, customer, a facts line: Prologis code,
building, type, status) and the actions on the right. All data invented.
"""
from __future__ import annotations

import re

from tests.unit.test_v345_lighting import _clean, env, sign_in   # noqa: F401  (fixture re-used)


def test_header_card(env):
    mod, pws, tmp = env
    h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/p/1").get_data(as_text=True)
    _clean(h, "project")
    head = re.search(r'<section class="card phead">(.*?)</section>', h, re.S).group(1)
    logo, main, act = re.search(r'<div class="phlogo">(.*?)</div>\s*<div class="phmain">(.*?)</div></div>\s*<div class="phact">(.*?)</div>$', head, re.S).groups()
    assert "/lighting/logo/acme.png" in logo
    assert '<div class="kick">' in main and "Parque Uno</div><h1>Acme Logistics</h1>" in main
    facts = re.findall(r"<span>([^<]+)<b>([^<]+)</b></span>", main)
    assert facts == [("Prologis code", "MXC09901"), ("Building", "Building 1"), ("Type", "Tenant space")]
    assert 'class="pill"' in main and "Delivered" in main
    assert "Schedule audit" in act and h.count('class="card phead"') == 1


def test_viewer_has_no_actions_column_content(env):
    mod, pws, tmp = env
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
    assert '<div class="phact"></div>' in h                                         # empty: hidden by CSS (.phact:empty)
    import prologis_ui as UI
    assert ".phact:empty{display:none}" in UI.CSS
