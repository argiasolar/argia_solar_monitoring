"""v359 - Tomasz, 5 Nov 2026:

* "in the buildings overview there should be a button to schedule audit and add it to calendar for particular building"
  -> a "Schedule audit" button on every building still to convert and on the building page, opening the calendar form
     with the building filled in (managers request, ARGIA adds) - not shown to viewers
* "a feature that is going to be simulating the proforma (an button next to Last Proforma date on Buildings section) -
  it will take numbers from proforma and it will display the PROLOGIS ESSENTIALS and SAVING CALCULATOR so the
  Prologis manager can see what conditions he can offer to customer, what IRR he will be getting and maybe there should
  be a way to reduce the costs or reduce the margin"
  -> /lighting/offer/<id>/: the proforma's numbers, the price per m2, contract, Prologis fee, customer contribution,
     ARGIA profit (lower = needs ARGIA approval), electricity price and target IRR as levers; Prologis IRR, payback,
     customer's net saving, the fee range that works for both, ideas; "Send to ARGIA" mails the desk

All data invented (public repo).
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import _clean, env, sign_in   # noqa: F401  (fixture re-used)
from tests.unit.test_v356_lighting_claims_calendar import _post
from tests.unit.test_v359_lighting_essentials import RAW
from argia.prologis import essentials as E
from argia.prologis import store as S


def _offers(tmp, raw=None, extra=()):
    d = copy.deepcopy(RAW)
    d["offers"][0].update(code="MXC09904")                     # opportunity 1 of the test data is building MXC09904
    p1 = copy.deepcopy(d["offers"][0])
    p1.update(id="P001-1", key="P001", code="MXC09901", tenant="Acme Logistics")
    d["offers"] += [p1, *extra]
    f = tmp / "lighting" / "offers.json"
    f.write_text(raw if raw is not None else json.dumps(d), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    return E.parse(d) if raw is None else None


class TestButtons:
    def test_pipeline_has_simulate_and_audit_for_managers_not_viewers(self, env):
        mod, pws, tmp = env
        _offers(tmp)
        h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/pipeline/").get_data(as_text=True)
        assert '<a class="btn sm" href="/lighting/offer/O901-1/"' not in h                # O901 is not an opportunity of the data
        assert 'href="/lighting/calendar/new?kind=audit&amp;code=MXC09904"' in h and "📅 Audit</a>" in h
        _clean(h, "pipeline")
        v = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/pipeline/").get_data(as_text=True)
        assert "📅" not in v and "/lighting/offer/" not in v

    def test_simulate_next_to_the_last_proforma(self, env):
        mod, pws, tmp = env
        o = copy.deepcopy(RAW["offers"][0])
        o.update(id="O001-1", key="O001", code="MXC09904", date="2025-06-05")
        _offers(tmp, extra=[o])
        h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/pipeline/").get_data(as_text=True)
        cell = re.search(r'<td class="nw" data-v="2025-06-05">2025-06-05 (.*?)</td>', h).group(1)
        assert 'href="/lighting/offer/O001-1/"' in cell and "Simulate" in cell

    def test_building_page_buttons(self, env):
        mod, pws, tmp = env
        _offers(tmp)
        h = sign_in(mod, "operator", pws["operator"], tmp).get("/lighting/p/1").get_data(as_text=True)
        assert 'href="/lighting/calendar/new?kind=audit&amp;code=MXC09901"' in h and "📅 Schedule audit" in h and 'href="/lighting/offer/P001-1/"' in h
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
        assert "Schedule audit" not in h and "Simulate" not in h

    def test_the_audit_button_fills_the_calendar_form(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/calendar/new?kind=audit&code=MXC09901").get_data(as_text=True)
        assert re.search(r'<option value="MXC09901"[^>]*selected', h)


class TestSimulator:
    def test_page_shows_the_proforma_and_the_result(self, env):
        mod, pws, tmp = env
        o = _offers(tmp)[0]
        r = E.compute(o, E.standard(o))
        h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/offer/O901-1/").get_data(as_text=True)
        _clean(h, "offer")
        for want in ("Prologis Essentials offer simulator", "Saving calculator (customer)", "Where the deal works", "Ideas for this offer",
                     f"{100 * r.irr:,.1f} %", f"${r.monthly_fee:,.0f}", f"${r.investment:,.0f}", f"${r.net_saving_m:,.0f}", "Send to ARGIA"):
            assert want in h, want
        c = S.connect(os.environ["ARGIA_PL_DB"])
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='lighting_offer_view' AND target='O901-1'").fetchone()[0] == 1

    def test_levers_recalculate(self, env):
        mod, pws, tmp = env
        o = _offers(tmp)[0]
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        s = E.Scenario({"production": 6, "warehouse": 2.5, "office": 5}, 84, 0.0, 0.06, 2.5, None, 100000)
        r = E.compute(o, s)
        q = "&".join(f"{k}={v}" for k, v in E.to_args(s).items())
        j = cl.get(f"/lighting/offer/O901-1/calc?{q}")
        assert j.status_code == 200 and j.mimetype == "application/json" and j.headers["Cache-Control"] == "private, no-store"
        h = j.get_json()["html"]
        assert f"{100 * r.irr:,.1f} %" in h and f"${r.monthly_fee:,.0f}" in h and "needs ARGIA approval" in h
        h = cl.get(f"/lighting/offer/O901-1/?{q}").get_data(as_text=True)                # same scenario without JavaScript
        assert f"{100 * r.irr:,.1f} %" in h and 'value="84" selected' in h

    def test_a_deal_that_cannot_work_says_so(self, env):
        mod, pws, tmp = env
        _offers(tmp)
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        h = cl.get("/lighting/offer/O901-1/calc?t=60&kp=0.6").get_json()["html"]
        assert "No fee works for both" in h
        h = cl.get("/lighting/offer/O901-1/calc?r_warehouse=9&r_office=9").get_json()["html"]
        assert "pays more than it saves" in h and "keeps -" not in h                       # not "keeps -566 % of the saving"

    def test_missing_consumption_asks_for_it(self, env):
        mod, pws, tmp = env
        o = copy.deepcopy(RAW["offers"][0])
        o.update(id="O002-1", key="O002", code=None, energy={"before": {"hours": None, "kw": None}, "after": {"hours": 3500, "kw": 50}})
        _offers(tmp, extra=[o])
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        h = cl.get("/lighting/offer/O002-1/").get_data(as_text=True)
        assert "not in the proforma - please enter" in h and "enter it to see the customer" in h
        assert "enter it to see the customer" not in cl.get("/lighting/offer/O002-1/calc?kb=600000").get_json()["html"]

    def test_who_and_what_is_refused(self, env):
        mod, pws, tmp = env
        _offers(tmp)
        v = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert v.get("/lighting/offer/O901-1/").status_code == 403 and v.get("/lighting/offer/O901-1/calc").status_code == 403
        m = sign_in(mod, "manager", pws["manager"], tmp)
        assert m.get("/lighting/offer/O999-1/").status_code == 404 and m.get("/lighting/offer/..%2Fx/").status_code == 404
        assert mod.app.test_client().get("/lighting/offer/O901-1/").status_code in (302, 401, 403)

    def test_send_to_argia(self, env):
        mod, pws, tmp = env
        _offers(tmp)
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        r = _post(cl, "/lighting/offer/O901-1/send", {"qs": "r_warehouse=2.8&m=72&pr=5", "note": "customer wants 72 months"},
                  page="/lighting/offer/O901-1/")
        h = r.get_data(as_text=True)
        assert r.status_code == 200 and "Sent to ARGIA" in h and 'value="72" selected' in h
        c = S.connect(os.environ["ARGIA_PL_DB"])
        m = c.execute("SELECT subject, body FROM outbox WHERE kind='light_offer'").fetchone()
        assert "MXC09904" in m[0] and "10,000 m2 x 2.8 MXN/m2" in m[1] and "Contract: 72 months" in m[1]
        assert "BELOW THE PROFORMA - needs ARGIA approval" in m[1] and "customer wants 72 months" in m[1]
        assert "/lighting/offer/O901-1/?" in m[1] and "m=72" in m[1]
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='lighting_offer_send'").fetchone()[0] == 1
        assert cl.post("/lighting/offer/O901-1/send", data={"qs": ""}).status_code in (400, 403)          # no CSRF token

    def test_no_file_no_simulator_and_a_broken_file_keeps_the_last(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        assert cl.get("/lighting/offer/O901-1/").status_code == 404
        assert "Simulate" not in cl.get("/lighting/p/1").get_data(as_text=True)
        _offers(tmp)
        assert cl.get("/lighting/offer/O901-1/").status_code == 200
        _offers(tmp, raw='{"schema": 1, "offers": [{"id": "bad"}]}')
        assert cl.get("/lighting/offer/O901-1/").status_code == 200                       # last good list kept


def test_no_function_of_the_portal_is_defined_twice():
    """v359 regression: a second `def _mxn` silently replaced the tickets' MXN formatter (Python keeps the last one)."""
    import ast
    import collections
    from pathlib import Path
    for f in ("prologis_app.py", "prologis_ui.py"):
        tree = ast.parse((Path(__file__).resolve().parents[2] / "server" / "bundle" / f).read_text(encoding="utf-8"))
        names = collections.Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        assert [k for k, v in names.items() if v > 1] == [], f
