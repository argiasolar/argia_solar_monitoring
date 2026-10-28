"""v333 - Prologis platform: maintenance plan, solar layer on the map, /about
behind its own login.

Tomasz, 28 Oct 2026:
* "calendar for preventive maintenance and cleaning" - recurring tasks per
  site or for every site, a year planner, a month calendar, the O&M team plans
  and records visits, an .ics export for Outlook;
* "plants on map, solar potential layer" - the map popup carries the site's
  numbers and the map has a PVOUT layer when the server has one;
* "move the about page under password protection" - nginx asks for the
  readers' own HTTP Basic login before /about reaches the app.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.prologis import maintplan as PM                 # noqa: E402

TODAY = dt.date(2026, 10, 21)


def _task(**kw):
    t = {"id": 1, "site_code": "", "kind": "cleaning", "title": "Module cleaning", "every_months": 6,
         "first_due": "2026-04-15", "duration_days": 2, "active": 1}
    t.update(kw)
    return t


# ------------------------------------------------------------------ pure
class TestOccurrences:
    def test_add_months_keeps_the_day_or_the_month_end(self):
        assert PM.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
        assert PM.add_months(dt.date(2026, 11, 15), 3) == dt.date(2027, 2, 15)
        assert PM.add_months(dt.date(2026, 10, 1), -1) == dt.date(2026, 9, 1)

    def test_a_task_for_every_site_repeats_per_site(self):
        occ = PM.occurrences([_task()], [], ["A", "B"], dt.date(2026, 1, 1), dt.date(2027, 12, 31), TODAY)
        assert [(o.site_code, o.due.isoformat()) for o in occ] == [
            ("A", "2026-04-15"), ("B", "2026-04-15"), ("A", "2026-10-15"), ("B", "2026-10-15"),
            ("A", "2027-04-15"), ("B", "2027-04-15"), ("A", "2027-10-15"), ("B", "2027-10-15")]

    def test_status_done_planned_overdue_due(self):
        vis = [{"task_id": 1, "site_code": "A", "due": "2026-04-15", "done": "2026-04-20", "planned": ""},
               {"task_id": 1, "site_code": "B", "due": "2026-10-15", "planned": "2026-11-03", "done": ""}]
        occ = {(o.site_code, o.due.isoformat()): o for o in
               PM.occurrences([_task()], vis, ["A", "B"], dt.date(2026, 1, 1), dt.date(2026, 12, 31), TODAY)}
        assert occ[("A", "2026-04-15")].status == PM.DONE and occ[("A", "2026-04-15")].day == dt.date(2026, 4, 20)
        assert occ[("B", "2026-04-15")].status == PM.OVERDUE
        assert occ[("A", "2026-10-15")].status == PM.OVERDUE          # due 15 Oct, today 21 Oct, nothing recorded
        assert occ[("B", "2026-10-15")].status == PM.PLANNED and occ[("B", "2026-10-15")].day == dt.date(2026, 11, 3)

    def test_an_old_overdue_job_stays_in_view(self):
        occ = PM.occurrences([_task()], [], ["A"], dt.date(2026, 10, 1), dt.date(2026, 10, 31), TODAY)
        assert [(o.due.isoformat(), o.status) for o in occ] == [("2026-04-15", PM.OVERDUE), ("2026-10-15", PM.OVERDUE)]

    def test_a_paused_task_and_a_site_task(self):
        occ = PM.occurrences([_task(active=0), _task(id=2, site_code="B", kind="thermography", every_months=12, first_due="2026-07-01")],
                             [], ["A", "B"], dt.date(2026, 1, 1), dt.date(2026, 12, 31), TODAY)
        assert [(o.task_id, o.site_code) for o in occ] == [(2, "B")]

    def test_summary(self):
        vis = [{"task_id": 1, "site_code": "A", "due": "2026-04-15", "done": "2026-04-20", "planned": ""}]
        occ = PM.occurrences([_task(), _task(id=2, kind="inspection", first_due="2026-11-05", every_months=12)], vis, ["A"],
                             dt.date(2026, 1, 1), dt.date(2026, 12, 31), TODAY)
        assert PM.summary(occ, TODAY) == {"overdue": 1, "next30": 1, "done_year": 1, "planned": 0}

    def test_month_grid_starts_on_monday(self):
        g = PM.month_grid(2026, 10)
        assert g[0][:3] == [None, None, None] and g[0][3] == dt.date(2026, 10, 1)
        assert sum(1 for w in g for d in w if d) == 31 and all(len(w) == 7 for w in g)

    def test_ics_is_a_valid_calendar_with_stable_ids(self):
        occ = PM.occurrences([_task()], [], ["A"], dt.date(2026, 10, 1), dt.date(2027, 6, 30), TODAY)
        s = PM.ics(occ, {"A": "Park, DC1"}, "prologis.argia.com.mx")
        assert s.startswith("BEGIN:VCALENDAR\r\n") and s.endswith("END:VCALENDAR\r\n")
        assert s.count("BEGIN:VEVENT") == 3
        assert "UID:pm-1-A-20261015@prologis.argia.com.mx" in s
        assert "SUMMARY:Module cleaning - Park\\, DC1" in s            # commas escaped
        assert "DTSTART;VALUE=DATE:20261015" in s and "DTEND;VALUE=DATE:20261017" in s   # 2 days on site


class TestStore:
    @pytest.fixture
    def c(self, tmp_path):
        import sqlite3
        con = sqlite3.connect(str(tmp_path / "pm.db"))
        con.row_factory = sqlite3.Row
        return con

    def test_add_validates(self, c):
        with pytest.raises(PM.PlanError):
            PM.add_task(c, "X", "cleaning", "", 6, "2026-04-15", 1, "", "u", ["A"])
        with pytest.raises(PM.PlanError):
            PM.add_task(c, "", "painting", "", 6, "2026-04-15", 1, "", "u", ["A"])
        with pytest.raises(PM.PlanError):
            PM.add_task(c, "", "cleaning", "", 5, "2026-04-15", 1, "", "u", ["A"])
        with pytest.raises(PM.PlanError):
            PM.add_task(c, "", "cleaning", "", 6, "15/04/2026", 1, "", "u", ["A"])
        tid = PM.add_task(c, "", "cleaning", "", 6, "2026-04-15", 2, "", "u", ["A"])
        assert PM.tasks(c)[0]["title"] == "" and tid == 1                       # no title: the kind's name, per language
        assert PM.title_of("", "cleaning", "en") == "Module cleaning" and PM.title_of("", "cleaning", "es") == "Limpieza de módulos"
        assert PM.title_of(" Winter wash ", "cleaning", "es") == "Winter wash" and PM.title_of("", "nonsense") == "Other"

    def test_record_only_real_occurrences_and_no_future_done(self, c):
        tid = PM.add_task(c, "A", "cleaning", "", 6, "2026-04-15", 1, "", "u", ["A", "B"])
        with pytest.raises(PM.PlanError):
            PM.record(c, tid, "A", "2026-05-15", "u", planned="2026-05-20", today=TODAY)   # not a due date
        with pytest.raises(PM.PlanError):
            PM.record(c, tid, "B", "2026-10-15", "u", planned="2026-11-01", today=TODAY)   # another site
        with pytest.raises(PM.PlanError):
            PM.record(c, tid, "A", "2026-10-15", "u", done="2026-10-30", today=TODAY)     # in the future
        PM.record(c, tid, "A", "2026-10-15", "u", planned="2026-11-02", today=TODAY)
        PM.record(c, tid, "A", "2026-10-15", "u", done="2026-10-21", today=TODAY)
        v = PM.visits(c)
        assert len(v) == 1 and v[0]["planned"] == "2026-11-02" and v[0]["done"] == "2026-10-21"


# ------------------------------------------------------------------ the app
pytest.importorskip("flask", reason="flask not installed here")
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
from argia.prologis import store as S                      # noqa: E402
from tests.unit.test_prologis_app import sign_in, _csrf     # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    monkeypatch.delenv("ARGIA_PL_MAP_DIR", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return mod, pws, tmp_path


def _sites(mod):
    return [s.code for s in mod.reg().operating]


class TestMaintenancePages:
    def test_pages_need_the_login(self, env):
        mod, _, _ = env
        cl = mod.app.test_client()
        for path in ("/maintenance/", "/maintenance/month", "/maintenance/tasks", "/maintenance/plan.ics"):
            r = cl.get(path)
            assert r.status_code == 302 and "/login" in r.headers["Location"], path
        assert cl.post("/maintenance/visit").status_code == 302

    def test_operator_adds_plans_and_records_a_visit(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "operator", pws["operator"], tmp)
        page = cl.get("/maintenance/tasks").get_data(as_text=True)
        assert "Add a recurring task" in page
        r = cl.post("/maintenance/tasks", data={"csrf": _csrf(page), "kind": "cleaning", "site": "", "every": "6",
                                               "first": "2026-04-15", "days": "2", "title": "", "notes": "dry brush"})
        assert r.status_code == 200 and "Task added" in r.get_data(as_text=True)
        year = cl.get("/maintenance/?y=2026").get_data(as_text=True)
        assert year.count('class="pmc overdue"') >= len(_sites(mod))           # April and October not done yet
        assert "Next 60 days and overdue" in year and "Done today" in year
        site = _sites(mod)[0]
        r = cl.post("/maintenance/visit", data={"csrf": _csrf(year), "task": "1", "site": site, "due": "2026-10-15",
                                                "act": "done", "back": "/maintenance/?y=2026"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/maintenance/?y=2026")
        c = S.connect(str(tmp / "pl.db"))
        v = [dict(x) for x in c.execute("SELECT * FROM pm_visits")]
        assert len(v) == 1 and v[0]["site_code"] == site and v[0]["done"]
        assert c.execute("SELECT count(*) FROM audit WHERE action IN ('pm_task_add','pm_done')").fetchone()[0] == 2

    def test_a_manager_sees_the_plan_but_cannot_change_it(self, env):
        mod, pws, tmp = env
        c = S.connect(str(tmp / "pl.db"))
        PM.add_task(c, "", "thermography", "", 12, "2026-07-01", 1, "", "x", _sites(mod))
        c.close()
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        page = cl.get("/maintenance/").get_data(as_text=True)
        assert "TH" in page and "Done today" not in page and "Add a recurring task" not in cl.get("/maintenance/tasks").get_data(as_text=True)
        tok = _csrf(cl.get("/password").get_data(as_text=True))        # a valid token: the refusal is the role, not CSRF
        assert tok
        assert cl.post("/maintenance/visit", data={"csrf": tok, "task": "1", "site": _sites(mod)[0], "due": "2026-07-01", "act": "done"}).status_code == 403
        assert cl.post("/maintenance/tasks", data={"csrf": tok, "kind": "cleaning", "every": "6", "first": "2026-01-01"}).status_code == 403
        assert cl.post("/maintenance/tasks/1/active", data={"csrf": tok, "on": "0"}).status_code == 403

    def test_month_calendar_and_ics(self, env):
        mod, pws, tmp = env
        c = S.connect(str(tmp / "pl.db"))
        PM.add_task(c, "", "inspection", "", 6, "2026-11-04", 1, "", "x", _sites(mod))
        c.close()
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        m = cl.get("/maintenance/month?m=2026-11").get_data(as_text=True)
        assert "November 2026" in m and m.count('class="ev due"') == len(_sites(mod))
        assert cl.get("/maintenance/month?m=nonsense").status_code == 200
        r = cl.get("/maintenance/plan.ics")
        assert r.mimetype == "text/calendar" and "attachment" in r.headers["Content-Disposition"]
        assert r.get_data(as_text=True).count("BEGIN:VEVENT") >= len(_sites(mod))

    def test_a_bad_back_link_cannot_leave_the_site(self, env):
        mod, pws, tmp = env
        c = S.connect(str(tmp / "pl.db"))
        PM.add_task(c, "", "cleaning", "", 6, "2026-04-15", 1, "", "x", _sites(mod))
        c.close()
        cl = sign_in(mod, "operator", pws["operator"], tmp)
        page = cl.get("/maintenance/").get_data(as_text=True)
        r = cl.post("/maintenance/visit", data={"csrf": _csrf(page), "task": "1", "site": _sites(mod)[0], "due": "2026-10-15",
                                                "act": "plan", "planned": "2026-11-02", "back": "https://evil.example/"})
        assert r.headers["Location"].endswith("/maintenance/")

    def test_nav_has_maintenance_and_the_permission_exists(self, env):
        mod, pws, tmp = env
        assert S.PERMS["pm_edit"] == {"operator", "admin"}
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert 'href="/maintenance/"' in cl.get("/").get_data(as_text=True)


class TestMapLayer:
    def _layer(self, d, bounds=((49.0, 12.0), (51.0, 19.0)), stops=((940, "#ffffcc"), (1220, "#7a0177"))):
        d.mkdir(parents=True, exist_ok=True)
        (d / "pvout.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (d / "pvout.json").write_text(json.dumps({"bounds": [list(b) for b in bounds], "stops": [list(s) for s in stops],
                                                  "unit": "kWh/kWp", "label_en": "Solar potential (PVOUT)", "label_es": "Potencial solar (PVOUT)",
                                                  "note_en": "n", "note_es": "n", "attribution": "PVGIS"}))

    def test_no_files_no_layer(self, env):
        mod, pws, tmp = env
        assert mod.pvout_layer() is None
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert "imageOverlay" not in cl.get("/map/").get_data(as_text=True)
        assert cl.get("/map/pvout.png").status_code == 404

    def test_the_layer_and_its_legend(self, env):
        mod, pws, tmp = env
        self._layer(tmp / "map")
        assert mod.pvout_layer()["bounds"] == [[49.0, 12.0], [51.0, 19.0]]
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        s = cl.get("/map/").get_data(as_text=True)
        assert "L.imageOverlay('/map/pvout.png',[[49.0, 12.0], [51.0, 19.0]]" in s
        assert "Solar potential (PVOUT)" in s and "linear-gradient(90deg,#ffffcc 0%, #7a0177 100%)" in s
        assert "attribution:\"PVGIS\"});" in s                                   # in the layer control, off until switched on
        assert cl.get("/map/pvout.png").status_code == 200
        assert "imageOverlay" not in cl.get("/").get_data(as_text=True)            # the overview mini map stays plain

    @pytest.mark.parametrize("bounds,stops", [(((51.0, 12.0), (49.0, 19.0)), ((940, "#ffffcc"), (1220, "#7a0177"))),
                                              (((49.0, 12.0), (51.0, 19.0)), ((940, "red"), (1220, "#7a0177"))),
                                              (((49.0, 12.0), (51.0, 19.0)), ((940, "#ffffcc"),))])
    def test_a_description_that_makes_no_sense_gives_no_layer(self, env, bounds, stops):
        mod, _, tmp = env
        self._layer(tmp / "map", bounds, stops)
        assert mod.pvout_layer() is None

    def test_a_label_cannot_close_the_map_script(self, env):
        mod, pws, tmp = env
        self._layer(tmp / "map")
        d = json.loads((tmp / "map" / "pvout.json").read_text())
        d["attribution"] = "PVGIS</script><script>alert(1)</script>"
        d["label_en"] = "Solar <b>potential</b>"
        (tmp / "map" / "pvout.json").write_text(json.dumps(d))
        s = sign_in(mod, "viewer", pws["viewer"], tmp).get("/map/").get_data(as_text=True)
        assert "<script>alert(1)" not in s and "</script><script>alert" not in s
        assert '"PVGIS&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in s.replace("<\\/", "</")
        assert "<b>potential</b>" not in s                                        # legend and layer name are text

    def test_the_layer_picture_needs_the_login(self, env):
        mod, _, tmp = env
        self._layer(tmp / "map")
        r = mod.app.test_client().get("/map/pvout.png")
        assert r.status_code == 302 and "/login" in r.headers["Location"]

    def test_the_popup_carries_the_site_numbers(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        s = cl.get("/map/").get_data(as_text=True)
        assert '"x": {"e30":' in s and "Performance ratio 30 d" in s and "Open the site" in s


class TestAboutLogin:
    VH = (BUNDLE / "prologis.argia.com.mx.conf").read_text()

    def test_about_asks_for_its_own_login_in_nginx(self):
        m = re.search(r"location ~ \^/about/\?\$ \{(.*?)\n    \}", self.VH, re.S)
        assert m, "no /about location"
        block = m.group(1)
        assert 'auth_basic "ARGIA product manual";' in block
        assert "auth_basic_user_file /opt/argia/prologis_about.htpasswd;" in block
        assert "limit_req zone=pl_login" in block and "proxy_pass http://127.0.0.1:8520;" in block
        vh443 = self.VH[self.VH.index("listen 443"):]                 # the port-80 server only redirects
        assert vh443.index("location ~ ^/about/?$") < vh443.index("    location / {")

    def test_the_user_file_is_not_in_the_repo_and_no_hash_is(self):
        assert not list(V2.parent.rglob("*about*.htpasswd"))
        assert not re.search(r"\$apr1\$|\$2[aby]\$", self.VH)
