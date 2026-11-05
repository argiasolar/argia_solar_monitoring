"""v349 - customer logos after Tomasz's look on 5 Nov 2026.

* "make the logos full color again, the B&W doesn't look good"  (pinned in test_v346: no grayscale anywhere)
* "on the customer page show all logos" - a logo wall with every customer that has a logo, in the
  register's order (largest first), each linked to its buildings; every customer still has a card,
  with its logo or its initials, and the page says how many have a logo.

All data invented (public repo).
"""
from __future__ import annotations

import copy
import re

from tests.unit.test_v345_lighting import DATA, _png, env, sign_in   # noqa: F401  (fixture re-used)
from argia.prologis import lighting as LT


def _wall(h):
    return re.search(r'<div class="lwall hrow">(.*?)</div><button', h, re.S).group(1)


class TestCustomerPage:
    def test_every_customer_with_a_logo_is_on_the_wall_once(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        ds = LT.parse(copy.deepcopy(DATA))
        groups = {p.group for p in ds.projects if p.group}
        expect = sorted(k for k in groups if ds.customers.get(k) and ds.customers[k].logo)
        wall = re.findall(r'href="/lighting/buildings/\?c=([^"]+)" title="[^"]+"(?: data-q="[^"]*")?><img src="/lighting/logo/([^"]+)\.png\?v=[0-9a-f]+"', _wall(h))
        assert sorted(k for k, _ in wall) == expect and all(k == f for k, f in wall)
        assert f"{len(expect)} of {len(groups)} customers with their logo" in h

    def test_every_customer_has_a_card_with_logo_or_initials(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        ds = LT.parse(copy.deepcopy(DATA))
        groups = {p.group for p in ds.projects if p.group}
        cards = re.findall(r'<a class="svc ccard" href="/lighting/buildings/\?c=([^"]+)"(?: data-q="[^"]*")?><span class="clogo mid plain( mono)?"', h)
        assert sorted(k for k, _ in cards) == sorted(groups)
        for k, mono in cards:
            assert bool(mono) == (not (ds.customers.get(k) and ds.customers[k].logo)), k

    def test_a_new_logo_appears_on_the_wall_without_a_restart(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["customers"]["bravo"]["logo"] = "bravo.png"
        import json
        (tmp / "lighting" / "logos" / "bravo.png").write_bytes(_png())
        (tmp / "lighting" / "lighting.json").write_text(json.dumps(d), encoding="utf-8")
        import os, time
        os.utime(tmp / "lighting" / "lighting.json", (time.time() + 5, time.time() + 5))
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert '/lighting/logo/bravo.png' in _wall(cl.get("/lighting/customers/").get_data(as_text=True))
        assert cl.get("/lighting/logo/bravo.png").status_code == 200
