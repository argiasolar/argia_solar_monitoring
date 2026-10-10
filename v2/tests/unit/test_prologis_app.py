"""v292 - ARGIA for Prologis: the web app end to end (Flask test client,
temp SQLite, synthetic registry). Login + MFA + forced password change,
roles, CSRF, tickets with dispatch approval, documents, users, audit,
export, security headers, no em dash, both languages."""
from __future__ import annotations

import importlib
import io
import pathlib
import re
import sys
import zipfile

import pytest

pytest.importorskip("flask", reason="flask not installed here")

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

NEW_PW = "Sunny-rooftops-2026"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return app_mod, pws, tmp_path


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    return m.group(1) if m else ""


def sign_in(app_mod, user, pw, tmp_path):
    cl = app_mod.app.test_client()
    r = cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    assert r.status_code == 302 and "/mfa" in r.headers["Location"]
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    code = TOTP.code_at(secret, int(__import__("time").time() // 30))
    r = cl.post("/mfa", data={"code": code, "pending": secret, "csrf": _csrf(page), "next": "/"})
    assert r.status_code == 302
    r = cl.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/password")
    page = cl.get("/password").get_data(as_text=True)
    r = cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert r.status_code == 302
    return cl


def test_everything_needs_a_login(env):
    app_mod, _, _ = env
    cl = app_mod.app.test_client()
    for path in ("/", "/map/", "/sites/", "/tickets/", "/projects/", "/docs/", "/admin/users/", "/audit/", "/export.zip", "/security/"):
        r = cl.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    assert cl.get("/healthz").status_code == 200
    assert cl.get("/brand/../../etc/passwd").status_code == 404
    assert cl.get("/brand/other.png").status_code == 404


def test_wrong_password_is_refused_audited_and_locked(env):
    app_mod, pws, tmp = env
    cl = app_mod.app.test_client()
    for _ in range(S.FAIL_MAX):
        assert cl.post("/login", data={"username": "viewer", "password": "nope"}).status_code == 401
    r = cl.post("/login", data={"username": "viewer", "password": pws["viewer"]})
    assert r.status_code == 429                                   # locked even with the right password
    c = S.connect(str(tmp / "pl.db"))
    acts = [x["action"] for x in c.execute("SELECT action FROM audit")]
    assert acts.count("login_fail") == S.FAIL_MAX and "login_locked" in acts


def test_password_alone_does_not_open_the_platform(env):
    app_mod, pws, _ = env
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": "viewer", "password": pws["viewer"]})
    r = cl.get("/sites/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/mfa")
    page = cl.get("/mfa").get_data(as_text=True)
    r = cl.post("/mfa", data={"code": "000000", "pending": re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1),
                              "csrf": _csrf(page)})
    assert "Wrong code" in r.get_data(as_text=True) or r.status_code == 302


def test_full_tour_as_admin(env):
    app_mod, pws, tmp = env
    cl = sign_in(app_mod, "admin", pws["admin"], tmp)
    for path in ("/", "/map/", "/sites/", "/sites/TST001/", "/sites/TST009/", "/tickets/", "/tickets/new", "/projects/",
                 "/docs/", "/admin/users/", "/audit/", "/security/"):
        r = cl.get(path)
        assert r.status_code == 200, path
        h = r.get_data(as_text=True)
        assert chr(0x2014) not in h, path
        assert "None" not in re.sub(r"<script.*?</script>", "", h, flags=re.S), path
    home = cl.get("/").get_data(as_text=True)
    assert "SAMPLE DATA" in home and "Power now" in home and "TST001" in cl.get("/sites/").get_data(as_text=True)
    r = cl.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    cl.get("/lang/es")
    assert "Potencia ahora" in cl.get("/").get_data(as_text=True)


def test_csrf_is_enforced(env):
    app_mod, pws, tmp = env
    cl = sign_in(app_mod, "manager", pws["manager"], tmp)
    r = cl.post("/tickets/new", data={"site": "TST001", "title": "x", "sla": "OTHER"})
    assert r.status_code == 400


def test_ticket_flow_with_dispatch_approval(env):
    app_mod, pws, tmp = env
    mgr = sign_in(app_mod, "manager", pws["manager"], tmp)
    page = mgr.get("/tickets/new?site=TST002&kind=inverter_fault").get_data(as_text=True)
    assert 'value="TST002" selected' in page
    r = mgr.post("/tickets/new", data={"site": "TST002", "title": "Inverter 2 down", "sla": "OUT_100_500", "kw": "150",
                                       "est": "12,500", "approval": "1", "desc": "seen in SolarEdge", "csrf": _csrf(page)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/tickets/PL-0001/")
    t = mgr.get("/tickets/PL-0001/").get_data(as_text=True)
    assert "Awaiting dispatch approval" in t and "Approve" in t
    mgr.post("/tickets/PL-0001/approve", data={"ok": "1", "note": "PO 4711", "csrf": _csrf(t)})
    # the manager may not work the ticket; the ARGIA operator may
    assert mgr.post("/tickets/PL-0001/status", data={"to": "RESPONDED", "csrf": _csrf(t)}).status_code == 403
    op = sign_in(app_mod, "operator", pws["operator"], tmp)
    t2 = op.get("/tickets/PL-0001/").get_data(as_text=True)
    assert op.post("/tickets/PL-0001/status", data={"to": "RESPONDED", "csrf": _csrf(t2)}).status_code == 302
    assert op.post("/tickets/PL-0001/status", data={"to": "NEW", "csrf": _csrf(t2)}).status_code == 400
    up = op.post("/tickets/PL-0001/comment", data={"body": "On site", "csrf": _csrf(t2),
                                                    "file": (io.BytesIO(b"\x89PNG..."), "photo.png")},
                 content_type="multipart/form-data")
    assert up.status_code == 302
    t3 = op.get("/tickets/PL-0001/").get_data(as_text=True)
    assert "Met" in t3 and "photo.png" in t3 and "PO 4711" in t3
    lst = op.get("/tickets/").get_data(as_text=True)
    assert "100%" in lst                                           # one closed clock, met


def test_viewer_is_read_only(env):
    app_mod, pws, tmp = env
    v = sign_in(app_mod, "viewer", pws["viewer"], tmp)
    assert v.get("/tickets/new").status_code == 403
    assert v.get("/admin/users/").status_code == 403
    assert v.get("/export.zip").status_code == 403
    assert "Users" not in v.get("/").get_data(as_text=True).split("<main>")[0]


def test_documents_upload_download_audited(env):
    app_mod, pws, tmp = env
    op = sign_in(app_mod, "operator", pws["operator"], tmp)
    page = op.get("/docs/").get_data(as_text=True)
    r = op.post("/docs/upload", data={"site": "TST003", "folder": "as_built", "csrf": _csrf(page),
                                      "file": (io.BytesIO(b"%PDF-1.7 fake"), "AsBuilt_TST003.pdf")}, content_type="multipart/form-data")
    assert r.status_code == 302
    bad = op.post("/docs/upload", data={"site": "TST003", "folder": "as_built", "csrf": _csrf(page),
                                        "file": (io.BytesIO(b"MZ"), "run.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 415
    lib = op.get("/docs/?site=TST003").get_data(as_text=True)
    doc_id = re.search(r"/docs/(\d+)/download", lib).group(1)
    assert op.get(f"/docs/{doc_id}/download").status_code == 409          # v324: not before the malware scan
    c = S.connect(str(tmp / "pl.db"))
    c.execute("UPDATE documents SET scan_status='clean' WHERE id=?", (doc_id,))   # what the scan job does (test_v324_security)
    c.commit()
    d = op.get(f"/docs/{doc_id}/download")
    assert d.status_code == 200 and d.data == b"%PDF-1.7 fake"
    acts = [x["action"] for x in c.execute("SELECT action FROM audit")]
    assert "doc_upload" in acts and "doc_download" in acts


def test_admin_manages_users_and_exports(env):
    app_mod, pws, tmp = env
    ad = sign_in(app_mod, "admin", pws["admin"], tmp)
    page = ad.get("/admin/users/").get_data(as_text=True)
    r = ad.post("/admin/users/", data={"username": "carla", "name": "Carla Prologis", "email": "c@prologis.test",
                                       "org": "Prologis", "role": "viewer", "csrf": _csrf(page)})
    h = r.get_data(as_text=True)
    once = re.search(r'class="once">([A-Za-z0-9]{16})<', h).group(1)
    c = S.connect(str(tmp / "pl.db"))
    assert S.check_pw(S.user(c, "carla")["pw"], once)
    assert "own role" in ad.post("/admin/users/admin", data={"op": "disable", "csrf": _csrf(h)}).get_data(as_text=True)
    ad.post("/admin/users/carla", data={"op": "disable", "csrf": _csrf(h)})
    assert S.user(c, "carla")["disabled"] == 1
    z = ad.get("/export.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert {"sites.csv", "daily_energy_90d.csv", "tickets.csv", "audit.csv", "users.csv", "README.txt"} <= set(names)
    users_csv = zipfile.ZipFile(io.BytesIO(z.data)).read("users.csv").decode()
    assert "scrypt" not in users_csv and "totp" not in users_csv.split("\n")[0]       # no secrets exported
    a = ad.get("/audit/?csv=1")
    assert a.status_code == 200 and "user_create" in a.get_data(as_text=True)


def test_open_redirect_is_refused(env):
    app_mod, pws, tmp = env
    cl = app_mod.app.test_client()
    r = cl.post("/login", data={"username": "viewer", "password": pws["viewer"], "next": "//evil.example"})
    assert "evil" not in r.headers["Location"]
