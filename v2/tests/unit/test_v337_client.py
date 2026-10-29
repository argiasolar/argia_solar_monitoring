"""v337 - the same platform for another client (irritec.argia.com.mx).

Tomasz, 29 Oct 2026: "make the same portal for Irritec ... same features and
monitoring as Prologis", with plants in Querétaro and worldwide. Pinned here:

* client.json (server-only) names the client and rewords the pages; without
  it the Prologis platform is byte-for-byte unchanged;
* a broken or odd client.json is ignored, never a crash;
* the /about manual and file downloads are never rewritten;
* the registry may declare its own bounds (worldwide sites); without them the
  Mexico check stays exactly as before.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.prologis import registry as R                    # noqa: E402

SITE = {"code": "ITCDO", "name": "Capo d'Orlando plant", "city": "Capo d'Orlando", "kwp": 1200, "lat": 38.15, "lon": 14.74}


class TestRegistryBounds:
    def test_mexico_is_the_default_and_keeps_its_message(self):
        with pytest.raises(ValueError, match="outside Mexico"):
            R.parse({"sites": [SITE]})

    def test_a_registry_with_bounds_accepts_sites_anywhere_inside(self):
        rg = R.parse({"bounds": [[-60, -180], [70, 180]], "sites": [SITE, dict(SITE, code="MXQRO", lat=20.83, lon=-100.44)]})
        assert [s.code for s in rg.sites] == ["ITCDO", "MXQRO"]

    def test_a_site_outside_the_bounds_is_refused(self):
        with pytest.raises(ValueError, match="outside the registry bounds"):
            R.parse({"bounds": [[30, -10], [50, 20]], "sites": [dict(SITE, lat=-23.1, lon=-47.2)]})

    @pytest.mark.parametrize("bad", [[[50, 0], [30, 10]], [[0, 0]], "world", [[-100, 0], [10, 10]]])
    def test_nonsense_bounds_are_refused(self, bad):
        with pytest.raises(ValueError, match="registry"):
            R.parse({"bounds": bad, "sites": [SITE]})


pytest.importorskip("flask", reason="flask not installed here")
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
from argia.prologis import store as S                      # noqa: E402
from tests.unit.test_prologis_app import sign_in            # noqa: E402

IRRITEC = {"name": "Irritec", "replace": [["--deep:#1B4D4A", "--deep:#00543F"], ["your rooftops", "your plants"]]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("ARGIA_PL_DIR", tmp_path), ("ARGIA_PL_DB", tmp_path / "pl.db"), ("ARGIA_PL_FILES", tmp_path / "files"),
                 ("ARGIA_PL_REGISTRY", FIX), ("ARGIA_PL_INSECURE_COOKIE", "1")):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("ARGIA_PL_CLIENT", raising=False)
    monkeypatch.delenv("ARGIA_PL_ABOUT", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    return mod, tmp_path


def _write(tmp, d):
    p = tmp / "client.json"
    p.write_text(json.dumps(d) if not isinstance(d, str) else d, encoding="utf-8")
    os.utime(p, (os.path.getmtime(p) + 1, os.path.getmtime(p) + 1))       # a new mtime for the cache
    return p


def test_default_file_is_inside_the_data_dir(env):
    mod, tmp = env
    assert mod.CLIENT_FILE == str(tmp / "client.json")


def test_no_file_leaves_the_prologis_pages_unchanged(env):
    mod, _ = env
    page = mod.app.test_client().get("/login").get_data(as_text=True)
    assert "ARGIA for Prologis" in page and mod.client_text(page) == page


def test_the_client_name_and_wording_replace_prologis_everywhere_on_the_page(env):
    mod, tmp = env
    _write(tmp, IRRITEC)
    page = mod.app.test_client().get("/login").get_data(as_text=True)
    assert "ARGIA for Irritec" in page and "Prologis" not in page and "PROLOGIS" not in page
    assert "--deep:#00543F" in page and "--deep:#1B4D4A" not in page          # the client's colours


def test_signed_in_pages_follow_too(env):
    mod, tmp = env
    _write(tmp, IRRITEC)
    c = S.connect(str(tmp / "pl.db"))
    pw = S.create_user(c, "viewer", "Viewer User", "v@x.test", "Irritec", "viewer", "test")
    c.close()
    cl = sign_in(mod, "viewer", pw, tmp)
    for path in ("/", "/sites/", "/security/", "/maintenance/"):
        t = cl.get(path).get_data(as_text=True)
        assert "Prologis" not in t, path


@pytest.mark.parametrize("bad", ["{not json", {"name": "", "replace": []}, {"name": "Irritec<script>", "replace": []},
                                 {"name": "Irritec", "replace": [["", "x"]]}, {"name": "Irritec", "replace": [["a"]]},
                                 {"name": "Irritec", "replace": "x"}])
def test_a_file_that_makes_no_sense_is_ignored(env, bad):
    mod, tmp = env
    _write(tmp, bad)
    assert mod.client_cfg() is None
    assert "ARGIA for Prologis" in mod.app.test_client().get("/login").get_data(as_text=True)


def test_the_manual_and_downloads_are_never_rewritten(env):
    mod, tmp = env
    _write(tmp, IRRITEC)
    (tmp / "public").mkdir()
    (tmp / "public" / "about.html").write_text("<!doctype html><title>x</title><p>Prologis manual</p>", encoding="utf-8")
    r = mod.app.test_client().get("/about")
    assert r.status_code == 200 and b"Prologis manual" in r.data
    r.close()


def test_a_changed_file_is_picked_up_without_a_restart(env):
    mod, tmp = env
    _write(tmp, IRRITEC)
    assert mod.client_cfg()["name"] == "Irritec"
    _write(tmp, dict(IRRITEC, name="Acme Foods"))
    assert mod.client_cfg()["name"] == "Acme Foods"
    (tmp / "client.json").unlink()
    assert mod.client_cfg() is None


class TestIrritecDeployFiles:
    """The second instance must never share data, port, login zone or users with Prologis."""
    B = V2 / "server" / "bundle"

    def test_vhost(self):
        v = (self.B / "irritec.argia.com.mx.conf").read_text()
        assert "server_name irritec.argia.com.mx;" in v and "/etc/letsencrypt/live/irritec.argia.com.mx/" in v
        assert v.count("proxy_pass http://127.0.0.1:8521;") == 3 and "8520" not in v
        assert "zone=ir_login" in v and "pl_login" not in v and "/var/log/argia-irritec/" in v
        assert "auth_basic_user_file /opt/argia/irritec_about.htpasswd;" in v
        vh443 = v[v.index("listen 443"):]
        assert vh443.index("location ~ ^/about/?$") < vh443.index("    location / {")

    @pytest.mark.parametrize("unit", ["argia-irritec.service", "argia-irritec-alarms.service", "argia-irritec-scan.service"])
    def test_units_use_only_the_irritec_store(self, unit):
        u = (self.B / unit).read_text()
        for k, v in (("ARGIA_PL_DIR", "/opt/argia/irritec"), ("ARGIA_PL_DB", "/opt/argia/irritec/irritec.db"),
                     ("ARGIA_PL_FILES", "/opt/argia/irritec/files"), ("ARGIA_PL_REGISTRY", "/opt/argia/irritec/registry.json")):
            assert f"Environment={k}={v}\n" in u, (unit, k)
        assert "/opt/argia/prologis" not in u and "prologis_vault" not in u

    def test_its_own_port(self):
        assert "Environment=ARGIA_PL_PORT=8521\n" in (self.B / "argia-irritec.service").read_text()

    def test_drift_check_knows_the_files(self):
        sys.path.insert(0, str(V2 / "scripts"))
        import drift_check as D
        assert ("v2/server/bundle/irritec.argia.com.mx.conf", "/etc/nginx/sites-enabled/irritec.argia.com.mx.conf") in D.EXPLICIT
        assert "irritec.argia.com.mx.http.conf" in D.NOT_DEPLOYED
        assert ("irritec-login", "https://irritec.argia.com.mx/login", (200,)) in D.SMOKE_HTTP
