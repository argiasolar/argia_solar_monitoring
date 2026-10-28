"""v331 - the public product manual at prologis.argia.com.mx/about.

Tomasz, 28 Oct 2026: the manual must open for anyone with the link, no login.
The page itself is server-only (it carries the offer's prices); these tests
use a stand-in file. What they pin:

* /about and /about/ answer without a session, and only those two paths
  joined the public set (everything else still needs the login);
* the file is served byte for byte (no rewriting, no session cookie);
* the page gets its own strict CSP: nothing may load from the network;
* no file installed = 404, never a crash or a redirect to the login.
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

PAGE = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>ARGIA Portal Manual</title></head>'
        '<body><p>Module 1: Živý monitoring · 4,8 MWp</p><img src="data:image/jpeg;base64,AA=="></body></html>\n')


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    monkeypatch.delenv("ARGIA_PL_ABOUT", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    return mod, tmp_path


def _install(tmp_path, text=PAGE):
    d = tmp_path / "public"
    d.mkdir(exist_ok=True)
    (d / "about.html").write_text(text, encoding="utf-8")
    return d / "about.html"


def test_default_location_is_inside_the_server_data_dir(app_env):
    mod, tmp = app_env
    assert mod.ABOUT_FILE == str(tmp / "public" / "about.html")


def test_no_file_installed_is_a_plain_404_not_a_login_redirect(app_env):
    mod, _ = app_env
    cl = mod.app.test_client()
    for path in ("/about", "/about/"):
        r = cl.get(path)
        assert r.status_code == 404, path
        assert "Location" not in r.headers


def test_anyone_can_read_the_manual_without_a_session(app_env):
    mod, tmp = app_env
    f = _install(tmp)
    cl = mod.app.test_client()
    for path in ("/about", "/about/"):
        r = cl.get(path)
        assert r.status_code == 200, path
        assert r.mimetype == "text/html"
        assert r.data == f.read_bytes()                       # byte for byte, Czech letters intact
        assert "Set-Cookie" not in r.headers                  # no session is created for a reader
        r.close()


def test_the_manual_has_its_own_strict_csp_and_the_usual_headers(app_env):
    mod, tmp = app_env
    _install(tmp)
    r = mod.app.test_client().get("/about")
    csp = r.headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'none';")
    for part in ("img-src data:", "font-src data:", "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert part in csp
    assert "https:" not in csp and "'self'" not in csp       # nothing from the network, not even this host
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-cache"           # a new version shows at once
    r.close()


def test_other_pages_keep_their_csp(app_env):
    mod, _ = app_env
    csp = mod.app.test_client().get("/login").headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self';") and "server.arcgisonline.com" in csp


def test_only_about_joined_the_public_set(app_env):
    mod, _ = app_env
    assert mod.PUBLIC == {"login", "login_post", "healthz", "brand", "lang", "favicon", "about"}


def test_everything_else_still_needs_the_login(app_env):
    mod, tmp = app_env
    _install(tmp)
    cl = mod.app.test_client()
    for path in ("/", "/sites/", "/security/", "/reports/", "/docs/", "/export.zip", "/audit/",
                 "/about/x", "/about.html", "/about/../export.zip"):
        r = cl.get(path)
        assert r.status_code in (302, 404), path
        if r.status_code == 302:
            assert "/login" in r.headers["Location"], path


def test_about_is_read_only(app_env):
    mod, tmp = app_env
    _install(tmp)
    cl = mod.app.test_client()
    assert cl.post("/about").status_code == 405
    assert cl.put("/about").status_code == 405


def test_an_env_override_points_at_another_file(tmp_path, monkeypatch, app_env):
    mod, _ = app_env
    other = tmp_path / "elsewhere.html"
    other.write_text("<!doctype html><title>x</title>", encoding="utf-8")
    monkeypatch.setenv("ARGIA_PL_ABOUT", str(other))
    import prologis_app
    mod = importlib.reload(prologis_app)
    r = mod.app.test_client().get("/about")
    assert r.status_code == 200 and r.data == other.read_bytes()
    r.close()


def test_route_is_in_the_route_contract():
    text = (V2 / "tests" / "contracts" / "routes.txt").read_text(encoding="utf-8")
    assert "prologis_app  GET    /about" in text
