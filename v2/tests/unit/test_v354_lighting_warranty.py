"""v354 - warranty claims section (Tomasz, 5 Nov 2026: "add warranty claims section and add all details we have
in our databases, files, emails related to it").

The register is a server-only file (warranty.json, built on the laptop from ARGIA's two warranty trackers);
here every claim is invented. The page never shows costs, POs, phone numbers or people.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, TODAY, env, sign_in   # noqa: F401  (fixture re-used)
from argia.prologis import warranty as W


def _c(ref, **kw):
    c = {"ref": ref, "ticket": ref if ref.startswith("W-") else None, "code": "MXC09901", "market": "cdmx", "park": "Parque Uno",
         "group": "acme", "customer": "Acme Logistics", "qty": 2, "models": ["PLD L24 24000LM"], "cause": "Doesn't turn on",
         "reported": "2026-03-01", "closed": "2026-03-21", "status": "closed", "days": 20, "source": "warranty management"}
    c.update(kw)
    return c


WDATA = {"meta": {"schema": 1, "generated": "2026-11-05", "warranty_years": 5, "excluded": {"solar": 3}},
         "claims": [_c("W-100-00"),
                    _c("W-101-00", reported="2026-10-01", closed=None, status="open", days=None, qty=10, cause="Intermittent"),
                    _c("OV-7", code=None, market="gdl", park=None, group=None, customer="old tenant", reported="2023-05-02",
                       closed="2023-06-11", days=40, qty=5, source="overview 2025"),
                    _c("W-102-00", code="GDL09902", market="gdl", park="Parque Dos", group="bravo", customer="Bravo Foods",
                       reported="2025-11-10", closed="2025-11-15", days=5, qty=1)]}


def _write(tmp, d=None):
    f = tmp / "lighting" / "warranty.json"
    f.write_text(json.dumps(d or WDATA), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))


class TestPure:
    def test_parse_and_summary(self):
        r = W.parse(copy.deepcopy(WDATA))
        s = W.summary(r.claims)
        assert (s.claims, s.fixtures, s.open, s.buildings) == (4, 18, 1, 2)
        assert s.median_days == 20 and s.within_30 == pytest.approx(2 / 3)
        assert W.per_year(r.claims) == [(2023, 1, 5), (2025, 1, 1), (2026, 2, 12)]
        assert r.claims[2].customer == "Old tenant"                                  # capitalised when read
        assert r.excluded == {"solar": 3}

    @pytest.mark.parametrize("field,value", [("status", "maybe"), ("code", "MX-1"), ("qty", -1), ("qty", "3"),
                                             ("reported", "05/03/2026"), ("closed", "2026-02-01"), ("ref", "<x>"), ("days", -2)])
    def test_a_bad_row_refuses_the_file(self, field, value):
        d = copy.deepcopy(WDATA)
        d["claims"][0][field] = value
        with pytest.raises(ValueError):
            W.parse(d)

    def test_duplicates_and_schema_are_refused(self):
        d = copy.deepcopy(WDATA)
        d["claims"].append(copy.deepcopy(d["claims"][0]))
        with pytest.raises(ValueError):
            W.parse(d)
        with pytest.raises(ValueError):
            W.parse({"meta": {"schema": 2}, "claims": []})

    def test_warranty_until_and_open_days(self):
        assert W.warranty_until("2021-04-13") == "2026-04-13" and W.warranty_until("2020-02-29") == "2025-02-28"
        assert W.warranty_until(None) is None and W.warranty_until("13/04/2021") is None
        r = W.parse(copy.deepcopy(WDATA))
        assert r.claims[1].days_open(dt.date(2026, 11, 5)) == 35 and r.claims[0].days_open(dt.date(2026, 11, 5)) == 20

    def test_filters(self):
        r = W.parse(copy.deepcopy(WDATA))
        assert [c.ref for c in W.filtered(r.claims, status="open")] == ["W-101-00"]
        assert [c.ref for c in W.filtered(r.claims, market="gdl")] == ["OV-7", "W-102-00"]
        assert [c.ref for c in W.filtered(r.claims, q="gdl09902")] == ["W-102-00"]
        assert len(W.for_building(r.claims, "MXC09901")) == 2 and W.for_building(r.claims, None) == []


class TestPages:
    def test_no_file_no_tab_no_page(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert "/lighting/warranty/" not in cl.get("/lighting/").get_data(as_text=True)
        assert cl.get("/lighting/warranty/").status_code == 404

    def test_warranty_page(self, env):
        mod, pws, tmp = env
        _write(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        home = cl.get("/lighting/").get_data(as_text=True)
        assert '<a class="tab" href="/lighting/warranty/">Warranty<b>1</b></a>' in home         # tab with the open count
        h = cl.get("/lighting/warranty/").get_data(as_text=True)
        assert '<table class="t sortable"><tr><th>Claim</th><th>Reported</th><th>Prologis building code</th>' in h
        assert h.count("<td class=\"nw\">W-10") == 3 and "Old tenant" in h
        # two Acme projects share MXC09901 here, so the code opens every project in that building
        assert '<td class="nw"><a href="/lighting/buildings/?q=MXC09901">MXC09901</a></td>' in h
        assert "How the warranty works" in h and "5-year limited warranty" in h
        assert re.search(r'<td class="n">\d+\+</td><td><span class="pill"[^>]*>Open</span>', h)     # open: days so far
        shown = re.sub(r"<script.*?</script>", "", h.split("<main>")[1].split("</main>")[0], flags=re.S)
        for bad in ("MXN", "USD", "$", "@", "PO "):
            assert bad not in shown, bad

    def test_filters_on_the_page(self, env):
        mod, pws, tmp = env
        _write(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/warranty/?s=open").get_data(as_text=True)
        assert "W-101-00" in h and "W-100-00" not in h
        h = cl.get("/lighting/warranty/?m=gdl").get_data(as_text=True)
        assert "W-102-00" in h and "W-100-00" not in h
        h = cl.get("/lighting/warranty/?q=zzzz").get_data(as_text=True)
        assert "No claim matches these filters." in h
        assert cl.get("/lighting/warranty/?s=<x>&m=../").status_code == 200                        # junk filters ignored

    def test_project_page_shows_warranty_end_and_claims(self, env):
        mod, pws, tmp = env
        _write(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert '<td class="muted">Warranty until</td><td><b>2027-07-01 <span class="small muted">(5 years from handover)</span></b></td>' in h
        card = h.split("Warranty claims in this building")[1]
        assert "W-100-00" in card and "W-101-00" in card and "W-102-00" not in card
        old = cl.get("/lighting/p/5").get_data(as_text=True)                                        # handed over 2019
        assert "2024-11-01 <span class=\"small muted\">(ended)</span>" in old

    def test_a_broken_file_keeps_the_last_good_one(self, env):
        mod, pws, tmp = env
        _write(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert "W-100-00" in cl.get("/lighting/warranty/").get_data(as_text=True)
        f = tmp / "lighting" / "warranty.json"
        f.write_text("{not json", encoding="utf-8")
        os.utime(f, (time.time() + 30, time.time() + 30))
        assert "W-100-00" in cl.get("/lighting/warranty/").get_data(as_text=True)
