"""v369 - Prologis's PM package on the portal (Tomasz, 11 Nov 2026):

"please take this excel and add these information to our prologis.argia.com.mx portal ... add details to solar
plants that we do not yet have in our portal ... try locating these plants and adding google maps or google earth
pictures of these installations if possible, if there is no accurate pin please leave window where we can place pin
and save the picture manually".

The registry carries the package's fields (tenant, solar ID, interconnection, in operation since, Prologis priority,
last cleaning, requested services); the site page shows them, a map where Prologis managers and ARGIA place the pin
on the building, and the site picture (satellite view at the pin, or uploaded). All data invented.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import pathlib
import sqlite3
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from argia.prologis import registry as R           # noqa: E402
from argia.prologis import sitepins as SP          # noqa: E402

FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 6000             # enough of a JPEG for the checks (magic + size)
PM = {"solar_id": "SLTST001", "customer": "Ficticia Logistics", "market": "MEXICO", "scheme": "NET METERING",
      "in_operation": "2025-01-10", "pl_priority": 2, "last_cleaning": "2026-04-16",
      "scope": ["pm", "thermography"], "geo_note": "park point"}


def _data(**extra) -> dict:
    d = json.loads(FIX.read_text(encoding="utf-8"))
    d["sites"][0].update(PM, **extra)
    return d


# ------------------------------------------------------------------ registry
class TestRegistry:
    def test_the_package_fields_are_read(self):
        s = R.parse(_data()).site("TST001")
        assert (s.solar_id, s.customer, s.scheme, s.in_operation, s.pl_priority, s.last_cleaning, s.scope) == (
            "SLTST001", "Ficticia Logistics", "NET METERING", "2025-01-10", 2.0, "2026-04-16", ("pm", "thermography"))

    def test_old_registries_still_load(self):
        s = R.parse(json.loads(FIX.read_text(encoding="utf-8"))).sites[0]
        assert (s.customer, s.scope, s.pl_priority, s.last_cleaning, s.pinned_by) == ("", (), None, "", "")

    @pytest.mark.parametrize("bad, why", [({"scope": ["pm", "painting"]}, "scope"), ({"last_cleaning": "April 16 2026"}, "YYYY-MM-DD"),
                                          ({"in_operation": "2025-13-01"}, "YYYY-MM-DD"), ({"pl_priority": "high"}, "TST001|site #1")])
    def test_bad_rows_are_refused(self, bad, why):
        import re
        with pytest.raises(ValueError) as e:
            R.parse(_data(**bad))
        assert re.search(why, str(e.value)), str(e.value)

    @pytest.mark.parametrize("today, months", [("2026-11-11", 6), ("2026-05-15", 0), ("2026-05-16", 1), ("2027-04-16", 12)])
    def test_months_since_cleaning(self, today, months):
        s = R.parse(_data()).site("TST001")
        assert s.months_since_cleaning(dt.date.fromisoformat(today)) == months

    def test_a_placed_pin_wins_and_is_no_longer_approximate(self):
        rg = R.parse(_data())
        assert R.apply_pins(rg, {}) is rg
        got = R.apply_pins(rg, {"TST001": {"lat": 19.661, "lon": -99.211, "username": "manager"}})
        s = got.site("TST001")
        assert (s.lat, s.lon, s.geo_approx, s.pinned_by) == (19.661, -99.211, False, "manager")
        assert rg.site("TST001").lat == 19.66 and got.sites[1] is rg.sites[1]        # original untouched


# ------------------------------------------------------------------ pins and pictures
def _db(tmp_path):
    from argia.prologis import store as S
    return S.connect(str(tmp_path / "pl.db"))


class TestPinsAndPictures:
    def test_set_move_reset_audited(self, tmp_path):
        c = _db(tmp_path)
        assert SP.set_pin(c, "TST001", "19.6612344", -99.21, (19.66, -99.21), "manager", "1.2.3.4") == (19.661234, -99.21)
        SP.set_pin(c, "TST001", 19.662, -99.212, (19.66, -99.21), "operator")
        assert SP.pins(c)["TST001"]["username"] == "operator"
        rows = c.execute("SELECT action, target, detail FROM audit ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("site_pin", "TST001", "19.661234,-99.21 was 19.66,-99.21 (registry)"),
                                            ("site_pin", "TST001", "19.662,-99.212 was 19.661234,-99.21")]
        assert SP.clear_pin(c, "TST001", "operator") and not SP.clear_pin(c, "TST001", "operator") and SP.pins(c) == {}

    @pytest.mark.parametrize("lat, lon, why", [(40.7, -74.0, "not in Mexico"), (None, 1, "missing"), (20.6, -103.3, "more than 60 km")])
    def test_pins_refused(self, tmp_path, lat, lon, why):
        c = _db(tmp_path)
        with pytest.raises(SP.Invalid, match=why):
            SP.set_pin(c, "TST001", lat, lon, (19.66, -99.21), "manager")
        assert SP.pins(c) == {}

    def test_only_real_pictures(self):
        assert SP.as_jpeg(JPEG) is JPEG
        for bad in (b"", JPEG[:100], b"<html>" + b"x" * 6000, b"GIF89a" + b"x" * 6000, JPEG + b"0" * SP.MAX_PHOTO):
            with pytest.raises(SP.Invalid):
                SP.as_jpeg(bad)

    def test_png_becomes_jpeg(self):
        PIL = pytest.importorskip("PIL.Image")
        import io
        buf = io.BytesIO()
        PIL.new("RGB", (400, 300), (40, 120, 60)).save(buf, "PNG", compress_level=0)
        out = SP.as_jpeg(buf.getvalue())
        assert out[:3] == b"\xff\xd8\xff"

    def test_google_first_with_the_portal_referrer_then_esri(self):
        calls = []

        def opener(url, headers):
            calls.append((url, headers))
            if "googleapis" in url:
                return b'{"error": "REQUEST_DENIED"}'           # not a picture -> next source
            return JPEG
        data, src = SP.fetch_satellite(19.6, -99.2, "AIzaFAKE", opener=opener)
        assert (data, src) == (JPEG, "Esri World Imagery")
        assert "maptype=satellite" in calls[0][0] and "key=AIzaFAKE" in calls[0][0]
        assert calls[0][1] == {"Referer": "https://prologis.argia.com.mx/"}
        assert "World_Imagery/MapServer/export" in calls[1][0]
        calls.clear()
        assert SP.fetch_satellite(19.6, -99.2, "", opener=opener)[1] == "Esri World Imagery" and len(calls) == 1
        assert SP.fetch_satellite(19.6, -99.2, "AIzaFAKE", opener=lambda u, h: JPEG)[1] == "Google Maps"

    def test_no_picture_anywhere_says_why(self):
        def opener(url, headers):
            raise OSError("timed out")
        with pytest.raises(SP.Invalid, match="Google Maps: timed out; Esri World Imagery: timed out"):
            SP.fetch_satellite(19.6, -99.2, "AIzaFAKE", opener=opener)

    def test_save_photo_atomic_private_and_safe(self, tmp_path):
        import os
        p = SP.save_photo(str(tmp_path / "photos"), "TST001", JPEG)
        assert open(p, "rb").read() == JPEG and not os.path.exists(p + ".tmp")
        if os.name != "nt":
            assert oct(os.stat(p).st_mode & 0o777) == "0o600"
        with pytest.raises(SP.Invalid):
            SP.save_photo(str(tmp_path / "photos"), "../../etc/x", JPEG)


def test_the_photo_script_only_fetches_building_positions():
    sys.path.insert(0, str(V2 / "scripts"))
    import prologis_site_photos as PS
    d = _data()
    d["sites"][1]["geo_approx"] = False
    d["sites"][2]["geo_approx"] = False
    rg = R.apply_pins(R.parse(d), {d["sites"][3]["code"]: {"lat": 19.6, "lon": -99.2, "username": "manager"}})
    todo, skip = PS.plan(rg, have={d["sites"][2]["code"]})
    assert [(x.code, why) for x, why in todo] == [(d["sites"][1]["code"], "building position"), (d["sites"][3]["code"], "pinned by hand")]
    assert dict((x.code, why) for x, why in skip)[d["sites"][2]["code"]] == "has a picture"
    assert all(why.startswith("approximate") for x, why in skip if x.code not in (d["sites"][2]["code"],))
    todo, _ = PS.plan(rg, have={d["sites"][2]["code"]}, only=d["sites"][2]["code"].lower(), force=True)
    assert [x.code for x, _ in todo] == [d["sites"][2]["code"]]


# ------------------------------------------------------------------ the portal
pytest.importorskip("flask", reason="flask not installed here")


@pytest.fixture
def env(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_data(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(reg))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    sys.path.insert(0, str(V2 / "server" / "bundle"))
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    from argia.prologis import store as S
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return mod, pws, tmp_path


def _sign_in(mod, role, pws, tmp):
    from tests.unit.test_prologis_app import sign_in
    return sign_in(mod, role, pws[role], tmp)


def _csrf(h):
    from tests.unit.test_prologis_app import _csrf as f
    return f(h)


def test_site_page_shows_the_package_and_the_location_card(env):
    mod, pws, tmp = env
    cl = _sign_in(mod, "viewer", pws, tmp)
    h = cl.get("/sites/TST001/").get_data(as_text=True)
    for needle in ("Maintenance requested by Prologis", "Ficticia Logistics", "SLTST001", "NET METERING", "2025-01-10",
                   "2026-04-16", "Preventive maintenance", "Thermography", "Location and picture", 'id="pmap"', "park point"):
        assert needle in h, needle
    assert "Cleaning</span>" not in h                       # not requested here
    assert 'action="/sites/TST001/pin"' not in h and "Upload a picture" not in h      # a viewer only looks
    lst = cl.get("/sites/").get_data(as_text=True)
    assert "Ficticia Logistics" in lst


def test_viewer_cannot_place_pins_or_pictures(env):
    mod, pws, tmp = env
    cl = _sign_in(mod, "viewer", pws, tmp)
    tok = next(t for t in (_csrf(cl.get(u).get_data(as_text=True)) for u in ("/password",)) if t)
    assert cl.post("/sites/TST001/pin", data={"lat": "19.661", "lon": "-99.211", "csrf": tok}).status_code == 403
    assert cl.post("/sites/TST001/photo", data={"src": "map", "csrf": tok}).status_code == 403


def test_manager_places_the_pin_and_saves_the_picture(env, monkeypatch):
    mod, pws, tmp = env
    seen = []
    monkeypatch.setattr(mod.SP, "fetch_satellite", lambda lat, lon, key: (seen.append((lat, lon)) or JPEG, "Esri World Imagery"))
    cl = _sign_in(mod, "manager", pws, tmp)
    h = cl.get("/sites/TST001/").get_data(as_text=True)
    assert 'action="/sites/TST001/pin"' in h and "Upload a picture" in h and "No picture yet." in h
    tok = _csrf(h)
    assert cl.post("/sites/TST001/pin", data={"lat": "19.661", "lon": "-99.211", "csrf": "bad"}).status_code == 400
    r = cl.post("/sites/TST001/pin", data={"lat": "19.661", "lon": "-99.211", "photo": "1", "csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("pin=photo#location") and seen == [(19.661, -99.211)]
    h = cl.get("/sites/TST001/").get_data(as_text=True)
    assert "Pin placed by hand by manager" in h and '"lat": 19.661' in h and "/sites/TST001/photo.jpg?v=" in h
    assert cl.get("/sites/TST001/photo.jpg").data == JPEG
    m = cl.get("/map/").get_data(as_text=True)
    assert "/sites/TST001/photo.jpg" in m                    # the map popup shows it too
    # out of Mexico: refused, nothing stored
    r = cl.post("/sites/TST001/pin", data={"lat": "40.7", "lon": "-74.0", "csrf": tok})
    assert r.status_code == 400 and b"not in Mexico" in r.data
    # back to the registry point
    r = cl.post("/sites/TST001/pin", data={"reset": "1", "csrf": tok})
    assert r.headers["Location"].endswith("pin=reset#location")
    assert "Pin placed by hand" not in cl.get("/sites/TST001/").get_data(as_text=True)
    acts = [x[0] for x in sqlite3.connect(str(tmp / "pl.db")).execute("SELECT action FROM audit WHERE action LIKE 'site_%' ORDER BY id")]
    assert acts == ["site_pin", "site_photo", "site_pin_reset"]


def test_upload_a_picture_by_hand(env):
    import io
    mod, pws, tmp = env
    cl = _sign_in(mod, "operator", pws, tmp)
    tok = _csrf(cl.get("/sites/TST001/").get_data(as_text=True))
    r = cl.post("/sites/TST001/photo", data={"src": "upload", "csrf": tok, "file": (io.BytesIO(JPEG), "roof.jpg")},
                content_type="multipart/form-data")
    assert r.status_code == 302 and cl.get("/sites/TST001/photo.jpg").data == JPEG
    r = cl.post("/sites/TST001/photo", data={"src": "upload", "csrf": tok, "file": (io.BytesIO(b"MZ" + b"0" * 9000), "x.jpg")},
                content_type="multipart/form-data")
    assert r.status_code == 400 and cl.get("/sites/TST001/photo.jpg").data == JPEG     # the old picture stays
    assert cl.post("/sites/NOPE/photo", data={"src": "map", "csrf": tok}).status_code == 404


def test_satellite_failure_keeps_the_pin_and_says_so(env, monkeypatch):
    mod, pws, tmp = env

    def boom(lat, lon, key):
        raise mod.SP.Invalid("no satellite picture: Esri World Imagery: timed out")
    monkeypatch.setattr(mod.SP, "fetch_satellite", boom)
    cl = _sign_in(mod, "manager", pws, tmp)
    tok = _csrf(cl.get("/sites/TST001/").get_data(as_text=True))
    r = cl.post("/sites/TST001/pin", data={"lat": "19.661", "lon": "-99.211", "photo": "1", "csrf": tok})
    assert r.headers["Location"].endswith("pin=nophoto#location")
    h = cl.get("/sites/TST001/?pin=nophoto").get_data(as_text=True)
    assert "no satellite picture could be fetched" in h and "Pin placed by hand by manager" in h


def test_merge_adds_sites_and_fields_but_keeps_what_the_registry_says():
    sys.path.insert(0, str(V2 / "scripts"))
    import prologis_registry_merge as M
    reg = json.loads(FIX.read_text(encoding="utf-8"))
    first = reg["sites"][0]
    new_site = dict(PM, code="REY99901", name="Ficticio Ind Ctr #1", park="Ficticio", city="Reynosa", state="Tamaulipas",
                    address="Calle 1", kwp=600.0, pto="Q1 2025", status="operating", lat=26.03, lon=-98.21, geo_approx=True,
                    geo_note="park point", monitoring="To confirm at onboarding")
    pkg = {"portfolio": {"market": "Mexico City and Reynosa"},
           "update": {first["code"]: dict(PM, rfp_kwp=500.0, rfp_name="Other name", lat=19.661, lon=-99.211, geo_approx=False,
                                          geo_note="Google Maps place of the tenant"),
                      "NOPE01": dict(PM)},
           "add": [new_site, dict(first, name="dup")]}
    out, rep = M.merge(reg, pkg)
    s = {x["code"]: x for x in out["sites"]}
    assert len(out["sites"]) == len(reg["sites"]) + 1 and reg["sites"][0].get("customer") is None      # input untouched
    assert (s[first["code"]]["name"], s[first["code"]]["kwp"]) == (first["name"], first["kwp"])          # kept
    assert (s[first["code"]]["customer"], s[first["code"]]["lat"], s[first["code"]]["geo_approx"]) == ("Ficticia Logistics", 19.661, False)
    assert s["REY99901"]["name"] == "Ficticio Ind Ctr #1" and out["portfolio"]["market"] == "Mexico City and Reynosa"
    text = "\n".join(rep)
    for needle in ("update NOPE01: not in the registry - skipped", f"DIFF   {first['code']}: kWp registry {first['kwp']} vs package 500.0 (kept)",
                   f"add    {first['code']}: already in the registry - not added", "add    REY99901: Ficticio Ind Ctr #1"):
        assert needle in text, needle
    # a building position already in the registry is not replaced by the package
    reg2 = copy_with(reg, first["code"], geo_approx=False, lat=19.7, lon=-99.2)
    out2, _ = M.merge(reg2, pkg)
    assert {x["code"]: x for x in out2["sites"]}[first["code"]]["lat"] == 19.7
    # an invalid package stops before anything could be written
    with pytest.raises(ValueError):
        M.merge(reg, {"add": [dict(new_site, code="REY99902", scope=["painting"])]})


def copy_with(reg, code, **kw):
    import copy
    r = copy.deepcopy(reg)
    for s in r["sites"]:
        if s["code"] == code:
            s.update(kw)
    return r
