"""v356 - Tomasz, 5 Nov 2026:

* "keep track of 5 years warranty period from handover" - every delivered building: in warranty, ends within
  12 months, ended
* "let prologis employees open warranty ticket inside the portal" - any Prologis user reports a failed fixture
  (building code, fixtures, what happens, photos/video); ARGIA's desk gets a mail; ARGIA moves it on, gives its
  W- ticket, schedules the visit (it lands on the calendar); the reporter is told about each step
* "lets add calendar for scheduling audit, and showing any projects work onsite where Prologis can see what is
  happening" - month view + next 60 days; Prologis managers request an audit, ARGIA confirms and adds any work

All data invented (public repo).
"""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, TODAY, _p, env, sign_in   # noqa: F401  (fixture re-used)
from tests.unit.test_v354_lighting_warranty import WDATA, _write as _write_w
from argia.prologis import lightops as LO
from argia.prologis import lighting as LT
from argia.prologis import store as S


def _csrf(h):
    return re.search(r'name="csrf" value="([^"]+)"', h).group(1)


def _post(cl, url, data, page=None):
    h = cl.get(page or url).get_data(as_text=True)
    data = dict(data, csrf=_csrf(h))
    return cl.post(url, data=data, content_type="multipart/form-data")


@pytest.fixture
def db(tmp_path):
    c = S.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


class TestPure:
    def test_coverage_states(self):
        ds = LT.parse(copy.deepcopy(DATA))
        cov = {c.project_id: c for c in LO.coverage(ds.projects, dt.date(2026, 11, 5))}
        assert cov[1].until == "2027-07-01" and cov[1].state == "ending" and cov[1].days_left == 238
        assert cov[5].state == "ended" and cov[5].days_left < 0                   # handed over 2019-11-01
        assert all(LT.parse(copy.deepcopy(DATA)).project(i).status == "delivered" for i in cov)
        assert 3 not in cov                                                     # in progress: no warranty yet

    def test_claim_lifecycle(self, db):
        cl = LO.create_claim(db, "mxc09901", "Acme", "3", "aisle 4", "off", "", "Juan 555", "pl1", today=dt.date(2026, 11, 5))
        assert cl["number"] == "LW-2026-0001" and cl["code"] == "MXC09901" and cl["status"] == "NEW"
        assert LO.create_claim(db, "MXC09901", "", "", "", "other", "x", "", "pl1", today=dt.date(2026, 11, 5))["number"] == "LW-2026-0002"
        with pytest.raises(LO.Invalid):
            LO.set_claim_status(db, cl["number"], "CLOSED", "op")                 # NEW -> CLOSED is not a step
        LO.set_claim_status(db, cl["number"], "RECEIVED", "op", "seen", "w-1460-00")
        c2 = LO.claim(db, cl["number"])
        assert c2["status"] == "RECEIVED" and c2["w_ticket"] == "W-1460-00"
        with pytest.raises(LO.Invalid):
            LO.set_claim_status(db, cl["number"], "PARTS", "op", "", "W1460")    # not a W- ticket
        LO.set_claim_status(db, cl["number"], "CLOSED", "op")
        assert LO.claim(db, cl["number"])["closed_utc"]
        kinds = [e["kind"] for e in LO.claim_events(db, cl["id"])]
        assert kinds == ["created", "status", "status"]
        assert [r["number"] for r in LO.claims(db, open_only=True)] == ["LW-2026-0002"]

    @pytest.mark.parametrize("args", [("MX-1", "off", "1"), ("MXC09901", "boom", "1"), ("MXC09901", "off", "-2"),
                                      ("MXC09901", "off", "lots"), ("MXC09901", "other", "")])
    def test_bad_claims_are_refused(self, db, args):
        code, sym, n = args
        with pytest.raises(LO.Invalid):
            LO.create_claim(db, code, "", n, "", sym, "", "", "pl1")

    def test_events_and_month(self, db):
        eid = LO.add_event(db, "audit", "MXC09901", "", "2026-11-10", "", "09:00", "requested", "", "pl1", requested_by="pl1")
        assert LO.event(db, eid)["title"] == "Audit" and LO.event(db, eid)["end_date"] == "2026-11-10"
        LO.add_event(db, "installation", "GDL09903", "Install", "2026-11-28", "2026-12-03", "", "confirmed", "", "op")
        with pytest.raises(LO.Invalid):
            LO.add_event(db, "audit", "", "", "2026-11-10", "2026-11-01", "", "confirmed", "", "op")   # ends before it starts
        with pytest.raises(LO.Invalid):
            LO.add_event(db, "audit", "", "", "2026-01-01", "2026-12-31", "", "confirmed", "", "op")   # too long
        with pytest.raises(LO.Invalid):
            LO.add_event(db, "party", "", "", "2026-11-10", "", "", "confirmed", "", "op")
        LO.update_event(db, eid, "op", "confirmed", "2026-11-12")
        assert (LO.event(db, eid)["status"], LO.event(db, eid)["start_date"]) == ("confirmed", "2026-11-12")
        weeks = LO.month_grid(2026, 11)
        assert weeks[0][0] == dt.date(2026, 10, 26) and weeks[0][0].weekday() == 0 and weeks[-1][-1] >= dt.date(2026, 11, 30)
        ds = LT.parse(copy.deepcopy(DATA))
        ents = LO.entries(db, dt.date(2026, 11, 1), dt.date(2026, 11, 30), ds.projects)
        assert [e.kind for e in ents][:2] == ["audit", "installation"]
        assert LO.month_of("2026-13", dt.date(2026, 11, 5)) == (2026, 11) and LO.shift_month(2026, 12, 1) == (2027, 1)


class TestPages:
    def test_coverage_card_on_the_warranty_page(self, env):
        mod, pws, tmp = env
        _write_w(tmp)
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/warranty/").get_data(as_text=True)
        assert "Warranty period of every building" in h and "Ends within 12 months" in h
        assert re.search(r'<a href="/lighting/p/1">MXC09901</a></td>.*?<td class="nw">2027-07-01</td>', h, re.S)

    def test_a_viewer_reports_a_failure_and_argia_works_it(self, env):
        mod, pws, tmp = env
        _write_w(tmp)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = _post(cl, "/lighting/warranty/new", {"code": "MXC09901", "fixtures": "2", "symptom": "intermittent", "area": "dock 3",
                                                 "desc": "flickers at night", "contact": "Ana 555",
                                                 "file": (io.BytesIO(b"\xff\xd8\xff\xe0 fake jpeg"), "fixture.jpg")})
        assert r.status_code == 302 and re.search(r"/lighting/warranty/c/LW-\d{4}-0001/$", r.headers["Location"])
        num = re.search(r"/c/(LW-\d{4}-\d{4})/", r.headers["Location"]).group(1)
        h = cl.get(f"/lighting/warranty/c/{num}/").get_data(as_text=True)
        assert "flickers at night" in h and "fixture.jpg" in h and "Reported" in h
        assert "/status" not in h                                                        # a viewer cannot move it on
        c = S.connect(os.environ["ARGIA_PL_DB"])
        mail = c.execute("SELECT * FROM outbox WHERE kind='light_claim'").fetchone()
        assert mail and num in mail["subject"] and "MXC09901" in mail["subject"]
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='light_claim_new'").fetchone()[0] == 1
        op = sign_in(mod, "operator", pws["operator"], tmp)
        r = _post(op, f"/lighting/warranty/c/{num}/status", {"to": "RECEIVED", "note": "on it", "w": "W-1500-00"}, f"/lighting/warranty/c/{num}/")
        assert r.status_code == 302
        r = _post(op, f"/lighting/warranty/c/{num}/visit", {"date": "2026-11-20", "slot": "10:00"}, f"/lighting/warranty/c/{num}/")
        assert r.status_code == 302
        assert LO.claim(c, num)["status"] == "SCHEDULED" and LO.claim(c, num)["w_ticket"] == "W-1500-00"
        ev = c.execute("SELECT * FROM light_events WHERE ref=?", (num,)).fetchone()
        assert ev["kind"] == "warranty" and ev["start_date"] == "2026-11-20" and ev["status"] == "confirmed"
        cal = op.get("/lighting/calendar/?mo=2026-11").get_data(as_text=True)
        assert f"Warranty visit {num}" in cal and "MXC09901" in cal
        told = c.execute("SELECT to_addrs, subject FROM outbox WHERE kind='light_claim_status' ORDER BY id").fetchall()
        assert [t["to_addrs"] for t in told] == ["viewer@x.test"] * 2 and "Received by ARGIA" in told[0]["subject"]   # the reporter is told
        assert "visit on 2026-11-20" in told[1]["subject"]
        assert "dock 3" in mail["body"] and "Ana 555" in mail["body"]
        c.close()

    def test_claim_needs_a_known_building(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = _post(cl, "/lighting/warranty/new", {"code": "MXC00000", "symptom": "off"})
        assert r.status_code == 200 and "Choose the building from the list." in r.get_data(as_text=True)

    def test_viewer_cannot_work_a_claim_or_the_calendar(self, env):
        mod, pws, tmp = env
        c = S.connect(os.environ["ARGIA_PL_DB"])
        num = LO.create_claim(c, "MXC09901", "", "1", "", "off", "", "", "viewer")["number"]
        c.close()
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get(f"/lighting/warranty/c/{num}/").get_data(as_text=True)
        assert cl.post(f"/lighting/warranty/c/{num}/status", data={"to": "RECEIVED", "csrf": _csrf(h)}).status_code == 403
        assert cl.get("/lighting/calendar/new").status_code == 403                      # viewers see the calendar only
        assert cl.get("/lighting/calendar/").status_code == 200

    def test_manager_requests_an_audit_argia_confirms(self, env):
        mod, pws, tmp = env
        mg = sign_in(mod, "manager", pws["manager"], tmp)
        h = mg.get("/lighting/calendar/new").get_data(as_text=True)
        assert "Request an audit" in h and '<input type="hidden" name="kind" value="audit">' in h
        r = _post(mg, "/lighting/calendar/new", {"kind": "installation", "code": "MXC09902", "start": "2026-11-18", "slot": "08:00",
                                                 "notes": "gate 2"})
        assert r.status_code == 302
        eid = int(re.search(r"/e/(\d+)/", r.headers["Location"]).group(1))
        c = S.connect(os.environ["ARGIA_PL_DB"])
        ev = LO.event(c, eid)
        assert ev["kind"] == "audit" and ev["status"] == "requested" and ev["requested_by"] == "manager"   # a manager cannot add installs
        assert c.execute("SELECT COUNT(*) FROM outbox WHERE kind='light_audit'").fetchone()[0] == 1
        op = sign_in(mod, "operator", pws["operator"], tmp)
        r = _post(op, f"/lighting/calendar/e/{eid}/", {"status": "confirmed", "start": "2026-11-19", "end": "", "slot": "09:00"})
        assert r.status_code == 302 and LO.event(c, eid)["status"] == "confirmed" and LO.event(c, eid)["start_date"] == "2026-11-19"
        cal = mg.get("/lighting/calendar/?mo=2026-11").get_data(as_text=True)
        assert 'class="calgrid"' in cal and "MXC09902" in cal and "Request an audit" in cal
        c.close()

    def test_tabs_and_planned_handover_on_the_calendar(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"][2].update(handover_date="2026-11-25", handover_planned=True)            # project 3 is in progress
        f = tmp / "lighting" / "lighting.json"
        f.write_text(json.dumps(d), encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/calendar/?mo=2026-11").get_data(as_text=True)
        assert '<a class="tab on" href="/lighting/calendar/">Calendar</a>' in h
        assert "Charlie Parts - planned handover" in h
