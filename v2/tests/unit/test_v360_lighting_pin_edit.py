"""v360 - Tomasz, 5 Nov 2026: "i want this map to be also interactive so I can zoom in and out and I can edit the pin
location, because in many cases the pin is showing something completely out of nowhere".

The building page shows a map (zoom, pan, satellite / streets). Prologis managers and ARGIA move the pin to the right
building and save it; the position is kept on the server over lighting.json (a rebuilt file does not lose it), every
move and reset is in the audit log, and a move of more than 60 km or outside Mexico is refused. All data invented.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

import pytest

from tests.unit.test_v345_lighting import DATA, _clean, env, sign_in   # noqa: F401  (fixture re-used)
from tests.unit.test_v356_lighting_claims_calendar import _post
from argia.prologis import lighting as LT
from argia.prologis import lightops as LO
from argia.prologis import store as S


def _cfg(h):
    return json.loads(re.search(r"var C=(\{.*?\});", h).group(1))


def _db():
    return S.connect(os.environ["ARGIA_PL_DB"])


class TestStore:
    def test_set_clear_apply(self, tmp_path):
        c = S.connect(str(tmp_path / "x.db"))
        assert LO.set_pin(c, 7, "19.7012344", -99.2, (19.70, -99.20), "op", "1.2.3.4") == (19.701234, -99.2)   # 6 decimals = 0.1 m
        assert LO.pins(c)[7]["lat"] == 19.701234
        a = c.execute("SELECT action, target, detail FROM audit ORDER BY id DESC").fetchone()
        assert tuple(a) == ("light_pin", "P7", "19.701234,-99.2 was 19.7,-99.2 (data file)")
        LO.set_pin(c, 7, 19.71, -99.21, (19.70, -99.20), "op")
        assert c.execute("SELECT detail FROM audit ORDER BY id DESC").fetchone()[0] == "19.71,-99.21 was 19.701234,-99.2"
        ds = LT.parse(copy.deepcopy(DATA))
        moved = LO.apply_pins(ds, LO.pins(c))
        assert moved.project(7).lat == 19.71 and moved.project(7).geo == "building" and ds.project(7).lat != 19.71   # original untouched
        assert LO.apply_pins(ds, {}) is ds
        assert LO.clear_pin(c, 7, "op") and not LO.clear_pin(c, 7, "op") and LO.pins(c) == {}

    @pytest.mark.parametrize("lat, lon, ref, why", [(40.7, -74.0, None, "not in Mexico"), ("x", 1, None, "missing"),
                                                    (None, None, None, "missing"), (20.6, -103.3, (19.70, -99.20), "more than 60 km")])
    def test_refused(self, tmp_path, lat, lon, ref, why):
        c = S.connect(str(tmp_path / "x.db"))
        with pytest.raises(LO.Invalid, match=why):
            LO.set_pin(c, 1, lat, lon, ref, "op")
        assert LO.pins(c) == {}

    def test_city_level_project_can_be_placed_anywhere_in_mexico(self, tmp_path):
        c = S.connect(str(tmp_path / "x.db"))
        assert LO.set_pin(c, 1, 25.7, -100.2, None, "op") == (25.7, -100.2)


class TestPage:
    def test_map_for_everyone_edit_only_for_managers_and_argia(self, env):
        mod, pws, tmp = env
        v = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/p/1").get_data(as_text=True)
        _clean(v, "viewer")
        assert _cfg(v) == {"lat": 19.7, "lon": -99.2, "z": 17, "pin": True} and "Move pin" not in v and 'action="/lighting/p/1/pin"' not in v
        assert "scrollWheelZoom:true" in v and "maxZoom:21" in v and "World_Street_Map" in v      # zoom, satellite + streets
        for role in ("manager", "operator", "admin"):
            h = sign_in(mod, role, pws[role], tmp).get("/lighting/p/1").get_data(as_text=True)
            assert "📍 Move pin" in h and 'action="/lighting/p/1/pin"' in h and "Back to the data file" not in h, role

    def test_move_save_and_everything_uses_it(self, env):
        mod, pws, tmp = env
        mg = sign_in(mod, "manager", pws["manager"], tmp)
        r = _post(mg, "/lighting/p/1/pin", {"lat": "19.712345", "lon": "-99.187654"}, page="/lighting/p/1")
        assert r.status_code == 302 and r.headers["Location"].endswith("/lighting/p/1?pin=saved")
        h = mg.get("/lighting/p/1?pin=saved").get_data(as_text=True)
        assert _cfg(h) == {"lat": 19.712345, "lon": -99.187654, "z": 19, "pin": True}
        when = (__import__("datetime").datetime.strptime(LO.pins(_db())[1]["ts_utc"], "%Y-%m-%d %H:%M:%S") + mod.M.MX_OFFSET).strftime("%Y-%m-%d")
        assert "Position saved." in h and f"Pin placed by hand by manager on {when}" in h and "google.com/maps/place/19.712345,-99.187654/" in h
        assert "Back to the data file" in h
        a = _db().execute("SELECT username, detail FROM audit WHERE action='light_pin'").fetchone()
        assert tuple(a) == ("manager", "19.712345,-99.187654 was 19.7,-99.2 (data file)")
        # the overview map's park point is the mean of the park's projects - with the moved one
        ds = LO.apply_pins(LT.parse(copy.deepcopy(DATA)), LO.pins(_db()))
        park = [p for p in ds.projects if LT.park_key(p) == LT.park_key(ds.project(1)) and p.lat is not None]
        mean = sum(p.lat for p in park) / len(park)
        assert ds.project(1).lat == 19.712345 and f'"lat": {mean}' in mg.get("/lighting/").get_data(as_text=True)

    def test_a_rebuilt_data_file_keeps_the_moved_pin_and_reset_goes_back(self, env):
        mod, pws, tmp = env
        op = sign_in(mod, "operator", pws["operator"], tmp)
        _post(op, "/lighting/p/1/pin", {"lat": "19.705", "lon": "-99.195"}, page="/lighting/p/1")
        d = copy.deepcopy(DATA)
        d["meta"]["generated"] = "2026-11-06"
        f = tmp / "lighting" / "lighting.json"
        f.write_text(json.dumps(d), encoding="utf-8")
        os.utime(f, (time.time() + 9, time.time() + 9))
        assert _cfg(op.get("/lighting/p/1").get_data(as_text=True))["lat"] == 19.705
        r = _post(op, "/lighting/p/1/pin", {"reset": "1"}, page="/lighting/p/1")
        assert r.headers["Location"].endswith("?pin=reset")
        h = op.get("/lighting/p/1?pin=reset").get_data(as_text=True)
        assert _cfg(h) == {"lat": 19.7, "lon": -99.2, "z": 17, "pin": True} and "Back to the position in the data file." in h
        assert _db().execute("SELECT detail FROM audit WHERE action='light_pin_reset'").fetchone()[0] == "was 19.705,-99.195"

    def test_refusals(self, env):
        mod, pws, tmp = env
        mg = sign_in(mod, "manager", pws["manager"], tmp)
        for lat, lon in (("40.7", "-74.0"), ("20.6", "-103.3"), ("abc", "")):
            r = _post(mg, "/lighting/p/1/pin", {"lat": lat, "lon": lon}, page="/lighting/p/1")
            assert r.status_code == 400 and "Position not saved" in r.get_data(as_text=True), (lat, lon)
        assert mg.post("/lighting/p/1/pin", data={"lat": "19.71", "lon": "-99.2"}).status_code in (400, 403)   # no CSRF
        r = _post(mg, "/lighting/p/999/pin", {"lat": "19.71", "lon": "-99.2"}, page="/lighting/p/1")
        assert r.status_code == 404
        assert LO.pins(_db()) == {}
        v = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = v.get("/lighting/p/1").get_data(as_text=True)
        tok = re.search(r'name="csrf" value="([^"]+)"', v.get("/lighting/calendar/").get_data(as_text=True))
        if tok:
            assert v.post("/lighting/p/1/pin", data={"lat": "19.71", "lon": "-99.2", "csrf": tok.group(1)}).status_code == 403

    def test_city_level_project_gets_place_pin(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["projects"][0]["geo"] = "city"
        f = tmp / "lighting" / "lighting.json"
        f.write_text(json.dumps(d), encoding="utf-8")
        os.utime(f, (time.time() + 9, time.time() + 9))
        mg = sign_in(mod, "manager", pws["manager"], tmp)
        h = mg.get("/lighting/p/1").get_data(as_text=True)
        assert "📍 Place pin" in h and _cfg(h)["pin"] is False
        r = _post(mg, "/lighting/p/1/pin", {"lat": "19.9", "lon": "-99.0"}, page="/lighting/p/1")       # no 60 km rule: no park point
        assert r.status_code == 302 and _cfg(mg.get("/lighting/p/1").get_data(as_text=True))["pin"] is True


pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")


def test_save_and_cancel_stay_hidden_until_editing(env):
    """v360 regression: the button style beat the hidden attribute, so Save showed before a position was chosen."""
    mod, pws, tmp = env
    h = sign_in(mod, "manager", pws["manager"], tmp).get("/lighting/p/1").get_data(as_text=True)
    css = re.search(r"<style>(.*?)</style>", h, re.S).group(1)
    bar = re.search(r'(<form method="post" action="/lighting/p/1/pin".*?</form>)', h, re.S).group(1)
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(f"<style>{css}</style>{bar}")
        assert pg.is_visible("#pinedit") and pg.is_hidden("#pinsave") and pg.is_hidden("#pincancel") and pg.is_hidden("#pinhelp")
        b.close()
