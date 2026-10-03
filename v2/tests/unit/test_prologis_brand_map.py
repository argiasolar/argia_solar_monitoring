"""v294 - ARGIA for Prologis: the reversed logo in the header (white
wordmark on the dark green, as on the Prologis buildings) with fallbacks,
and the map opening on the street layer. Synthetic registry; the brand
files here are dummies (the real logo is server-only)."""
from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here")

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2))

from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(V2 / "tests" / "fixtures" / "prologis" / "registry.json"))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pw = S.create_user(c, "viewer", "Viewer", "v@x.test", "Prologis", "viewer", "test")
    c.close()
    (tmp_path / "brand").mkdir()
    return app_mod, pw, tmp_path / "brand"


def _signed_in(app_mod, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": "viewer", "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    cl.post("/mfa", data={"code": TOTP.code_at(secret, int(__import__("time").time() // 30)), "pending": secret, "csrf": csrf})
    page = cl.get("/password").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    cl.post("/password", data={"pw1": "Sunny-rooftops-2026", "pw2": "Sunny-rooftops-2026", "csrf": csrf})
    return cl


def _header(html: str) -> str:
    return html.split("<main>")[0]


def test_logo_prefers_reversed_then_colour_then_none(env):
    app_mod, pw, brand = env
    cl = _signed_in(app_mod, pw)
    h = _header(cl.get("/sites/").get_data(as_text=True))
    assert "/brand/" not in h and "PROLOGIS" in h                         # no files: drawn mark + word
    (brand / "prologis_logo.png").write_bytes(PNG)
    h = _header(cl.get("/sites/").get_data(as_text=True))
    assert 'class="pl" src="/brand/prologis_logo.png"' in h               # colour logo on its white chip
    (brand / "prologis_logo_white.png").write_bytes(PNG)
    h = _header(cl.get("/sites/").get_data(as_text=True))
    assert 'class="pl rev" src="/brand/prologis_logo_white.png"' in h and "prologis_logo.png" not in h
    login = app_mod.app.test_client().get("/login").get_data(as_text=True)
    assert 'class="pl rev" src="/brand/prologis_logo_white.png"' in login


def test_brand_route_serves_only_known_files(env):
    app_mod, _, brand = env
    (brand / "prologis_logo_white.png").write_bytes(PNG)
    (brand / "other.png").write_bytes(PNG)
    cl = app_mod.app.test_client()
    r = cl.get("/brand/prologis_logo_white.png")
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert cl.get("/brand/other.png").status_code == 404
    assert cl.get("/brand/prologis_logo.png").status_code == 404          # known name, file absent


def test_css_reversed_logo_has_no_white_chip():
    import prologis_ui as UI
    css = UI.CSS.replace("\n", "")
    assert ".brand img.pl.rev{height:32px;background:none" in css


@pytest.mark.parametrize("path", ["/map/", "/"])
def test_map_opens_on_streets(env, path):
    app_mod, pw, _ = env
    h = _signed_in(app_mod, pw).get(path).get_data(as_text=True)
    js = re.search(r"var st=L\.tileLayer\(.*?L\.control\.layers\(\{'Streets':st,'Satellite':sat\}\)", h, flags=re.S)
    assert js, "street layer must be defined first and listed first"
    assert "World_Street_Map" in js.group(0).split("var sat=")[0] and ".addTo(m);\nvar sat=" in js.group(0)
    sat_def = js.group(0).split("var sat=")[1].split(";\n")[0]
    assert "addTo(m)" not in sat_def                                       # satellite is the option, not the default


def test_tab_icon_is_the_prologis_globe(env):
    """v300 (Tomasz): a small Prologis logo on the browser tab, before and after sign-in."""
    app_mod, pw, brand = env
    cl = app_mod.app.test_client()
    assert cl.get("/favicon.png").status_code == 404                      # no file yet: no icon, no error page
    (brand / "prologis_mark.png").write_bytes(PNG + b"mark")
    r = cl.get("/favicon.png")
    assert r.status_code == 200 and r.data.endswith(b"mark")             # the mark is the fallback
    (brand / "prologis_favicon.png").write_bytes(PNG + b"fav")
    (brand / "prologis_touch.png").write_bytes(PNG + b"touch")
    for path, tail in (("/favicon.png", b"fav"), ("/favicon.ico", b"fav"), ("/apple-touch-icon.png", b"touch")):
        r = cl.get(path)
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data.endswith(tail), path   # public: no login
    assert '<link rel="icon" type="image/png" href="/favicon.png">' in cl.get("/login").get_data(as_text=True)
    signed = _signed_in(app_mod, pw)
    for path in ("/", "/map/", "/tickets/", "/shop/"):
        assert 'href="/favicon.png"' in signed.get(path).get_data(as_text=True).split("</head>")[0], path


def test_map_scrolls_under_the_sticky_header():
    """v301 (Tomasz: the map overlays the menu bar when scrolling). Leaflet's
    panes carry z-index 400-1000; the map is now its own stacking context and
    the sticky header sits above any of them."""
    import prologis_ui as UI
    css = UI.CSS.replace("\n", "")
    m = re.search(r"#map\{([^}]*)\}", css).group(1)
    assert "isolation:isolate" in m and "position:relative" in m and "z-index:0" in m
    top = re.search(r"\.top\{([^}]*)\}", css).group(1)
    assert "position:sticky" in top and int(re.search(r"z-index:(\d+)", top).group(1)) > 1000
