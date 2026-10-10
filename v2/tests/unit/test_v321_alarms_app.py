"""v321 - ARGIA for Prologis: the Alarms pages and the --alarm-run job end
to end (Flask test client, temp SQLite, synthetic registry): roles, the
engine CLI (also with the store locked), triage from the page into a
ticket that keeps the alarm time, mail preferences and settings, the daily
review log, the outbox, the denied-warranty-claim mail, both languages."""
from __future__ import annotations

import importlib
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

from argia.prologis import alarms as AL    # noqa: E402
from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

NEW_PW = "Sunny-rooftops-2026"
PAGES = ("/alarms/", "/alarms/review")


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


def test_alarm_cli_skips_a_locked_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "locked" / "prologis.db"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    assert app_mod.main(["x", "--alarm-run"]) == 0
    assert "alarm run skipped: the Prologis store is not available" in capsys.readouterr().out
    assert not (tmp_path / "locked").exists()                                 # nothing created on a bare mount point


def test_alarms_need_login_and_roles(env):
    app_mod, pws, _ = env
    cl = app_mod.app.test_client()
    for path in PAGES + ("/alarms/outbox",):
        r = cl.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    v = sign_in(app_mod, "viewer", pws["viewer"])
    for path in PAGES:
        h = v.get(path).get_data(as_text=True)
        _ok(h, path)
        assert "Triage within 4 business hours" in h or "Daily performance review" in h
        assert "/alarms/outbox" not in h and 'action="/alarms/settings"' not in h and "Record the review" not in h
    tok = _csrf(v.get("/alarms/").get_data(as_text=True))
    assert v.get("/alarms/outbox").status_code == 403
    assert v.post("/alarms/1/triage", data={"action": "dismiss", "note": "x", "csrf": tok}).status_code == 403
    assert v.post("/alarms/review", data={"day": "2026-10-01", "findings": "x", "csrf": tok}).status_code == 403
    assert v.post("/alarms/settings", data={"mail_mode": "live", "csrf": tok}).status_code == 403
    op = sign_in(app_mod, "operator", pws["operator"])
    tok = _csrf(op.get("/alarms/").get_data(as_text=True))
    assert op.post("/alarms/settings", data={"mail_mode": "live", "csrf": tok}).status_code == 403   # only an admin switches mail on
    assert op.post("/alarms/999/triage", data={"action": "dismiss", "note": "x", "csrf": tok}).status_code == 404


def test_engine_run_triage_into_ticket_and_digest(env, capsys):
    app_mod, pws, tmp = env
    assert app_mod.main(["x", "--alarm-run", "2026-10-06T12:00"]) == 0
    out = capsys.readouterr().out
    assert "alarm run 2026-10-06 12:00 MX: sites=7, opened=1" in out and "(mail mode dry_run)" in out
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get("/alarms/").get_data(as_text=True)
    _ok(h)
    assert "Parque Lago Bldg 5" in h and "Production loss" in h and "SAMPLE" in h and "E-mail: DRY-RUN" in h
    c = S.connect(str(tmp / "pl.db"))
    aid = c.execute("SELECT id FROM alarms").fetchone()[0]
    tok = _csrf(h)
    assert "Not triaged: a dismissal needs a reason" in op.post(f"/alarms/{aid}/triage", data={"action": "dismiss", "csrf": tok}).get_data(as_text=True)
    h = op.post(f"/alarms/{aid}/triage", data={"action": "ticket", "note": "crew Thursday", "csrf": tok}).get_data(as_text=True)
    assert f"Alarm #{aid} triaged - ticket PL-0001." in h
    t = c.execute("SELECT * FROM tickets WHERE number='PL-0001'").fetchone()
    a = AL.alarm(c, aid)
    assert t["detected_utc"] == a["detected_utc"] and t["sample"] == 1 and t["sla_class"] == "STRING_100"
    tk = op.get("/tickets/PL-0001/").get_data(as_text=True)
    assert f"From alarm #{aid}" in tk
    assert "Not triaged: already triaged" in op.post(f"/alarms/{aid}/triage", data={"action": "dismiss", "note": "x", "csrf": tok}).get_data(as_text=True)
    assert app_mod.main(["x", "--alarm-run", "2026-10-07T08:00"]) == 0
    ob = op.get("/alarms/outbox").get_data(as_text=True)
    _ok(ob)
    assert "Daily alarm digest 07 Oct 2026" in ob and "No recipient" in ob     # no desk address set yet


def test_preferences_settings_and_claim_denial_mail(env):
    app_mod, pws, tmp = env
    mgr = sign_in(app_mod, "manager", pws["manager"])
    h = mgr.get("/alarms/").get_data(as_text=True)
    assert "My alarm e-mails" in h and "manager@x.test" in h
    mgr.post("/alarms/me", data={"pref": "critical", "csrf": _csrf(h)})
    assert mgr.post("/alarms/me", data={"pref": "sms", "csrf": _csrf(h)}).status_code == 400
    ad = sign_in(app_mod, "admin", pws["admin"])
    h = ad.get("/alarms/").get_data(as_text=True)
    tok = _csrf(h)
    assert "Alarm mail settings" in h
    assert "Not saved: not an e-mail address: desk" in ad.post("/alarms/settings", data={"desk_emails": "desk", "mail_mode": "dry_run", "csrf": tok}).get_data(as_text=True)
    ad.post("/alarms/settings", data={"desk_emails": "desk@argia.test", "mail_mode": "dry_run", "csrf": tok})
    c = S.connect(str(tmp / "pl.db"))
    assert AL.recipients(c, "critical") == ["desk@argia.test", "manager@x.test"]
    # a denied warranty claim is mailed to the desk and the opted-in Prologis manager
    op = sign_in(app_mod, "operator", pws["operator"])
    tok = _csrf(op.get("/assets/claims/").get_data(as_text=True))
    op.post("/assets/claims/new", data={"site": "TST001", "supplier": "SolarEdge", "csrf": tok})
    op.post("/assets/claims/WC-0001/status", data={"to": "SUBMITTED", "csrf": tok})
    op.post("/assets/claims/WC-0001/status", data={"to": "DENIED", "note": "surge damage", "csrf": tok})
    m = c.execute("SELECT * FROM outbox WHERE kind='claim_denied'").fetchone()
    assert m["status"] == "dry_run" and m["to_addrs"] == "desk@argia.test, manager@x.test" and "surge damage" in m["body"]
    atok = _csrf(ad.get("/alarms/").get_data(as_text=True))
    assert ad.post("/alarms/settings", data={"desk_emails": "desk@argia.test", "mail_mode": "live", "csrf": tok}).status_code == 400  # CSRF of another session
    ad.post("/alarms/settings", data={"desk_emails": "desk@argia.test", "mail_mode": "live", "csrf": atok})
    assert "E-mail: LIVE" in ad.get("/alarms/").get_data(as_text=True)
    acts = [r["detail"] for r in c.execute("SELECT detail FROM audit WHERE action='setting' AND target='mail_mode'")]
    assert acts[-1] == "'dry_run' -> 'live'"


def test_daily_review_page(env):
    app_mod, pws, tmp = env
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get("/alarms/review").get_data(as_text=True)
    _ok(h)
    assert "Record the review" in h and "Parque Norte Bldg 1" in h
    yday = re.search(r'<input type="hidden" name="day" value="([0-9-]+)">', h).group(1)
    tok = _csrf(h)
    assert "Not recorded: write what was found" in op.post("/alarms/review", data={"day": yday, "findings": " ", "csrf": tok}).get_data(as_text=True)
    h = op.post("/alarms/review", data={"day": yday, "findings": "no findings", "csrf": tok}).get_data(as_text=True)
    assert f"Review of {yday} recorded" in h and "Reviewed late" in h
    assert "Not recorded: this day is already reviewed" in op.post("/alarms/review", data={"day": yday, "findings": "x", "csrf": tok}).get_data(as_text=True)
    assert "Not recorded: a review cannot be for a future day" in op.post("/alarms/review", data={"day": "2999-01-01", "findings": "x", "csrf": tok}).get_data(as_text=True)
    c = S.connect(str(tmp / "pl.db"))
    r = c.execute("SELECT * FROM daily_review").fetchone()
    assert r["sites_checked"] == 7 and '"site": "TST001"' in r["evidence"] and r["late"] == 1
    ad = sign_in(app_mod, "admin", pws["admin"])
    ad.get("/lang/es")
    for path in PAGES + ("/alarms/outbox", "/"):
        h = ad.get(path).get_data(as_text=True)
        _ok(h, path)
        assert "Alarmas" in h, path
    assert "Revisión diaria de desempeño" in ad.get("/alarms/review").get_data(as_text=True)
