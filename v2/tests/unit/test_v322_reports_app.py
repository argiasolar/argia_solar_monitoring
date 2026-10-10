"""v322 - ARGIA for Prologis: the Reports pages end to end (Flask test
client, temp SQLite, synthetic registry): drafts are ARGIA's, publishing
freezes and opens the report to Prologis, the spreadsheet, the per-site
page, the HSE register, the design yield CSV, the ticket ETA, the
guarantee setting, both languages."""
from __future__ import annotations

import importlib
import io
import pathlib
import re
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here")

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

from argia.prologis import monthly as MR   # noqa: E402
from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

NEW_PW = "Sunny-rooftops-2026"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
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


def sign_in(app_mod, user, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    code = TOTP.code_at(secret, int(__import__("time").time() // 30))
    cl.post("/mfa", data={"code": code, "pending": secret, "csrf": _csrf(page), "next": "/"})
    page = cl.get("/password").get_data(as_text=True)
    cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert cl.get("/").status_code == 200
    return cl


def _ok(h: str, where: str = "") -> None:
    assert chr(0x2014) not in h and "None" not in re.sub(r"<script.*?</script>", "", h, flags=re.S) and "Traceback" not in h, where


def test_draft_is_argia_only_then_published(env):
    openpyxl = pytest.importorskip("openpyxl")
    app_mod, pws, tmp = env
    prev = MR.prev_month(app_mod.today_mx())
    v = sign_in(app_mod, "viewer", pws["viewer"])
    h = v.get("/reports/").get_data(as_text=True)
    _ok(h)
    assert prev in h and f'href="/reports/{prev}/"' not in h and "Draft (ARGIA only)" in h
    assert v.get(f"/reports/{prev}/").status_code == 404 and v.get(f"/reports/{prev}/report.xlsx").status_code == 404
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get(f"/reports/{prev}/").get_data(as_text=True)
    _ok(h)
    assert "DRAFT - ARGIA only" in h and "Publish to Prologis" in h and "Parque Norte Bldg 1" in h and "SAMPLE DATA" in h
    x = op.get(f"/reports/{prev}/report.xlsx")
    assert x.status_code == 200 and x.headers["Content-Disposition"].endswith(f"_{prev}_DRAFT.xlsx")
    tok = _csrf(h)
    assert "Not published: write the summary" in op.post(f"/reports/{prev}/publish", data={"notes": " ", "csrf": tok}).get_data(as_text=True)
    cur = f"{app_mod.today_mx():%Y-%m}"
    assert "Not published: the month is not over" in op.post(f"/reports/{cur}/publish", data={"notes": "x", "csrf": tok}).get_data(as_text=True)
    assert v.post(f"/reports/{prev}/publish", data={"notes": "x", "csrf": _csrf(v.get('/alarms/').get_data(as_text=True))}).status_code == 403
    h = op.post(f"/reports/{prev}/publish", data={"notes": "Portfolio stable; no outage over 3 days.", "csrf": tok}).get_data(as_text=True)
    assert "Published." in h and "Portfolio stable" in h
    h = v.get(f"/reports/{prev}/").get_data(as_text=True)
    _ok(h)
    assert "Portfolio stable" in h and "DRAFT" not in h and "Publish to Prologis" not in h
    assert "Published on time" in v.get("/reports/").get_data(as_text=True)
    wb = openpyxl.load_workbook(io.BytesIO(v.get(f"/reports/{prev}/report.xlsx").data))
    assert "Portfolio" in wb.sheetnames and wb["Sites"].max_row == 8
    s = v.get(f"/reports/{prev}/TST001/").get_data(as_text=True)
    _ok(s)
    assert "Parque Norte Bldg 1" in s and "Energy per day" in s
    assert v.get(f"/reports/{prev}/NOPE/").status_code == 404 and v.get("/reports/2026-13/").status_code == 404
    c = S.connect(str(tmp / "pl.db"))
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert "monthly_report_publish" in acts and acts.count("monthly_report_export") == 2


def test_hse_design_eta_and_guarantee(env):
    app_mod, pws, tmp = env
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get("/reports/hse").get_data(as_text=True)
    _ok(h)
    tok = _csrf(h)
    assert "Not recorded: describe" in op.post("/reports/hse", data={"occurred": "2026-10-01 09:00", "kind": "incident", "description": "", "csrf": tok}).get_data(as_text=True)
    h = op.post("/reports/hse", data={"occurred": "2026-10-01 09:00", "site": "TST001", "kind": "near_miss", "description": "ladder slipped",
                                       "reported": "2026-10-01 10:00", "mojo": "SM-77", "csrf": tok}).get_data(as_text=True)
    assert "Recorded." in h and "ladder slipped" in h and "reported within 24 h" in h and "SM-77" in h
    # design yield CSV
    bad = "site,m1\nTST001,5\n"
    h = op.post("/reports/design", data={"source": "PVsyst", "file": (io.BytesIO(bad.encode()), "d.csv"), "csrf": tok},
                content_type="multipart/form-data").get_data(as_text=True)
    assert "Nothing loaded: 1 problem(s)" in h and "header must be" in h
    good = "site," + ",".join(f"m{i}" for i in range(1, 13)) + "\nTST001," + ",".join(["80000"] * 12) + "\n"
    h = op.post("/reports/design", data={"source": "PVsyst v7", "file": (io.BytesIO(good.encode()), "d.csv"), "csrf": tok},
                content_type="multipart/form-data").get_data(as_text=True)
    assert "12 values loaded" in h and "960,000" in h
    assert op.get("/reports/design_template.csv").get_data(as_text=True).startswith("site,m1,")
    # ticket ETA
    c = S.connect(str(tmp / "pl.db"))
    tk = S.create_ticket(c, "TST001", "Inverter down", "", "OUT_100_500", "op")
    page = op.get(f"/tickets/{tk['number']}/").get_data(as_text=True)
    assert "Estimated return to service" in page
    assert op.post(f"/tickets/{tk['number']}/eta", data={"eta": "20/10/2026", "csrf": tok}).status_code == 400
    op.post(f"/tickets/{tk['number']}/eta", data={"eta": "2026-10-20", "csrf": tok})
    page = op.get(f"/tickets/{tk['number']}/").get_data(as_text=True)
    assert "Back in service by 2026-10-20" in page and "Estimated return to service: 2026-10-20" in page
    v = sign_in(app_mod, "viewer", pws["viewer"])
    assert v.post(f"/tickets/{tk['number']}/eta", data={"eta": "2026-10-21", "csrf": _csrf(v.get('/alarms/').get_data(as_text=True))}).status_code == 403
    assert v.post("/reports/hse", data={"csrf": _csrf(v.get('/alarms/').get_data(as_text=True))}).status_code == 403
    # guarantee: admin only
    assert op.post("/reports/guarantee", data={"g": "0.97", "csrf": tok}).status_code == 403
    ad = sign_in(app_mod, "admin", pws["admin"])
    at = _csrf(ad.get("/reports/").get_data(as_text=True))
    assert "Not saved: the guarantee must be between" in ad.post("/reports/guarantee", data={"g": "1.5", "csrf": at}).get_data(as_text=True)
    assert "<b>97.5%</b>" in ad.post("/reports/guarantee", data={"g": "0.975", "csrf": at}).get_data(as_text=True)
    ad.get("/lang/es")
    for path in ("/reports/", "/reports/hse", "/reports/design", f"/reports/{MR.prev_month(app_mod.today_mx())}/"):
        h = ad.get(path).get_data(as_text=True)
        _ok(h, path)
        assert "Reportes" in h, path
