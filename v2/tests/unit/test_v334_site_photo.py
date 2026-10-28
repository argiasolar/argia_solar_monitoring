"""v334 - a picture of each site in the Prologis map popup.

Tomasz, 28 Oct 2026: the map screenshot should show the plant "and its
picture". Pictures are server-only (PHOTO_DIR/<SITE CODE>.jpg), served to
signed-in users at /sites/<code>/photo.jpg. Pinned here:

* no picture = no <img> and a plain 404, never an error;
* only registry codes reach the disk (unknown or odd codes are 404);
* the picture needs the login;
* the popup shows it above the name.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here")
V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))
from argia.prologis import store as S                      # noqa: E402
from tests.unit.test_prologis_app import sign_in            # noqa: E402

JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00sample\xff\xd9"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("ARGIA_PL_DIR", tmp_path), ("ARGIA_PL_DB", tmp_path / "pl.db"), ("ARGIA_PL_FILES", tmp_path / "files"),
                 ("ARGIA_PL_REGISTRY", FIX), ("ARGIA_PL_INSECURE_COOKIE", "1")):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("ARGIA_PL_PHOTO_DIR", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pw = S.create_user(c, "viewer", "Viewer User", "v@x.test", "Prologis", "viewer", "test")
    c.close()
    code = mod.reg().sites[0].code
    return mod, pw, tmp_path, code


def _put(tmp, code, data=JPG):
    d = tmp / "photos"
    d.mkdir(exist_ok=True)
    (d / f"{code}.jpg").write_bytes(data)


def test_default_folder_is_inside_the_data_dir(env):
    mod, _, tmp, _ = env
    assert mod.PHOTO_DIR == str(tmp / "photos")


def test_no_picture_no_img_and_a_plain_404(env):
    mod, pw, tmp, code = env
    cl = sign_in(mod, "viewer", pw, tmp)
    assert mod.site_photo(code) is None
    assert cl.get(f"/sites/{code}/photo.jpg").status_code == 404
    assert 'class="ph"' not in cl.get("/map/").get_data(as_text=True).split("function pop")[0]
    assert f'"ph": "/sites/{code}/photo.jpg"' not in cl.get("/map/").get_data(as_text=True)


def test_the_picture_is_served_and_shown_in_the_popup(env):
    mod, pw, tmp, code = env
    _put(tmp, code)
    cl = sign_in(mod, "viewer", pw, tmp)
    r = cl.get(f"/sites/{code}/photo.jpg")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data == JPG
    r.close()
    page = cl.get("/map/").get_data(as_text=True)
    assert f'"ph": "/sites/{code}/photo.jpg"' in page
    assert "'<img class=\"ph\" src=\"'+p.ph+'\" alt=\"\">'" in page            # above the name in the popup
    other = mod.reg().sites[1].code
    assert f'"ph": "/sites/{other}/photo.jpg"' not in page                      # only sites that have one


@pytest.mark.parametrize("bad", ["NOPE", "..", "%2e%2e%2fpl", "x.jpg"])
def test_only_registry_codes_reach_the_disk(env, bad):
    mod, pw, tmp, code = env
    _put(tmp, code)
    (tmp / "photos" / "NOPE.jpg").write_bytes(JPG)                             # a stray file is never served
    cl = sign_in(mod, "viewer", pw, tmp)
    assert cl.get(f"/sites/{bad}/photo.jpg").status_code == 404


def test_the_picture_needs_the_login(env):
    mod, _, tmp, code = env
    _put(tmp, code)
    r = mod.app.test_client().get(f"/sites/{code}/photo.jpg")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_route_is_in_the_contract():
    text = (V2 / "tests" / "contracts" / "routes.txt").read_text(encoding="utf-8")
    assert "prologis_app  GET    /sites/<code>/photo.jpg" in text
