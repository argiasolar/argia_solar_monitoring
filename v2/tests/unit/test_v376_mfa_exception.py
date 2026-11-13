"""v376 - a dated, per-instance exception to the authenticator (Tomasz, 13 Nov 2026, mrp.argia.com.mx: "turn off
the security that requires authenticator for this moment"; chosen: MRP only, until a date). Plus a console command
that creates a user with a password the operator types. Invented users and passwords."""
from __future__ import annotations

import datetime as dt
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

from argia.prologis import store as S      # noqa: E402


def make(tmp_path, monkeypatch, until=None):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    if until is None:
        monkeypatch.delenv("ARGIA_PL_MFA_OFF_UNTIL", raising=False)
    else:
        monkeypatch.setenv("ARGIA_PL_MFA_OFF_UNTIL", until)
    import prologis_app
    m = importlib.reload(prologis_app)
    m.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    S.create_user_with_password(c, "andy", "Andy Test", "andy@x.test", "Grupo Demo", "manager", "Short1!x", "test",
                                allow_simple=True)
    c.close()
    return m


def future():
    return (dt.date.today() + dt.timedelta(days=30)).isoformat()


class TestTheSwitch:
    def test_dates(self, tmp_path, monkeypatch):
        m = make(tmp_path, monkeypatch)
        d = dt.date(2026, 12, 31)
        assert m.mfa_off(d, "2026-12-31") and m.mfa_off(dt.date(2026, 11, 13), "2026-12-31")
        assert not m.mfa_off(dt.date(2027, 1, 1), "2026-12-31")          # back by itself the day after
        for bad in ("", "soon", "2026-13-01", "31/12/2026"):
            assert not m.mfa_off(d, bad)
        assert not m.mfa_off()                                          # not set: MFA as always

    def test_only_the_mrp_unit_sets_it(self):
        units = {p.name: p.read_text(encoding="utf-8") for p in BUNDLE.glob("argia-*.service")}
        assert "Environment=ARGIA_PL_MFA_OFF_UNTIL=2026-12-31" in units["argia-mrp.service"]
        assert [n for n, s in units.items() if "ARGIA_PL_MFA_OFF_UNTIL" in s] == ["argia-mrp.service"]


class TestSignIn:
    def test_with_the_exception_the_password_is_enough_and_logged(self, tmp_path, monkeypatch):
        m = make(tmp_path, monkeypatch, future())
        cl = m.app.test_client()
        assert "the password alone signs you in" in cl.get("/login").get_data(as_text=True)
        r = cl.post("/login", data={"username": "andy", "password": "Short1!x", "next": "/sites/"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/sites/")
        assert cl.get("/").status_code == 200                           # no /mfa, no forced password change
        assert "temporary exception" in cl.get("/security/").get_data(as_text=True)
        c = S.connect(str(tmp_path / "pl.db"))
        acts = [(x["action"], x["detail"]) for x in c.execute("SELECT action, detail FROM audit")]
        assert ("login_ok_no_mfa", f"MFA exception until {future()}") in acts

    def test_wrong_password_still_refused(self, tmp_path, monkeypatch):
        m = make(tmp_path, monkeypatch, future())
        assert m.app.test_client().post("/login", data={"username": "andy", "password": "nope"}).status_code == 401

    def test_expired_exception_asks_for_the_authenticator(self, tmp_path, monkeypatch):
        m = make(tmp_path, monkeypatch, "2020-01-01")
        cl = m.app.test_client()
        assert "second factor" in cl.get("/login").get_data(as_text=True)
        r = cl.post("/login", data={"username": "andy", "password": "Short1!x", "next": "/"})
        assert r.status_code == 302 and "/mfa" in r.headers["Location"]
        assert cl.get("/").headers["Location"].endswith("/mfa")

    def test_without_the_setting_nothing_changes(self, tmp_path, monkeypatch):
        m = make(tmp_path, monkeypatch)
        r = m.app.test_client().post("/login", data={"username": "andy", "password": "Short1!x", "next": "/"})
        assert "/mfa" in r.headers["Location"]


class TestConsoleUser:
    def test_rules(self, tmp_path):
        c = S.connect(str(tmp_path / "x.db"))
        with pytest.raises(ValueError, match="12 characters"):
            S.create_user_with_password(c, "bob", "Bob", "b@x.test", "Demo", "viewer", "Sun-ray-9", "t")
        with pytest.raises(ValueError, match="at least 8"):
            S.create_user_with_password(c, "bob", "Bob", "b@x.test", "Demo", "viewer", "Short1", "t", allow_simple=True)
        S.create_user_with_password(c, "bob", "Bob", "b@x.test", "Demo", "viewer", "Sun-ray-9", "t", allow_simple=True)
        row = S.user(c, "bob")
        assert row["must_change"] == 0 and S.check_pw(row["pw"], "Sun-ray-9") and "Sun-ray-9" not in row["pw"]
        assert "simple password allowed" in c.execute("SELECT detail FROM audit WHERE action='user_create'").fetchone()[0]
        with pytest.raises(ValueError, match="exists"):
            S.create_user_with_password(c, "BOB", "Bob", "b@x.test", "Demo", "viewer", "Sun-ray-9", "t", allow_simple=True)

    def test_cli_reads_the_password_from_the_prompt(self, tmp_path, monkeypatch, capsys):
        m = make(tmp_path, monkeypatch, future())
        import getpass
        typed = iter(["Simple-1!", "Simple-1!"])
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(typed))
        rc = m.main(["prologis_app.py", "--add-user", "pat", "Pat Test", "pat@x.test", "Grupo Demo", "manager",
                     "--simple-password"])
        out = capsys.readouterr().out
        assert rc == 0 and "user pat created" in out and "Simple-1!" not in out
        assert S.check_pw(S.user(S.connect(str(tmp_path / "pl.db")), "pat")["pw"], "Simple-1!")

    def test_cli_password_from_stdin(self, tmp_path, monkeypatch, capsys):
        import io
        m = make(tmp_path, monkeypatch, future())
        monkeypatch.setattr(sys, "stdin", io.StringIO("Moon-lit-7\r\n"))        # the Windows pipe ends with CRLF
        rc = m.main(["prologis_app.py", "--add-user", "kim", "Kim Test", "", "Grupo Demo", "manager",
                     "--simple-password", "--password-stdin"])
        out = capsys.readouterr().out
        assert rc == 0 and "Moon-lit-7" not in out
        row = S.user(S.connect(str(tmp_path / "pl.db")), "kim")
        assert S.check_pw(row["pw"], "Moon-lit-7") and row["must_change"] == 0 and row["role"] == "manager"

    def test_cli_refuses_simple_passwords_without_the_exception(self, tmp_path, monkeypatch, capsys):
        m = make(tmp_path, monkeypatch)
        rc = m.main(["prologis_app.py", "--add-user", "pat", "Pat", "p@x.test", "Demo", "viewer", "--simple-password"])
        assert rc == 2 and "refused" in capsys.readouterr().out

    def test_cli_mismatch_creates_nothing(self, tmp_path, monkeypatch, capsys):
        m = make(tmp_path, monkeypatch, future())
        import getpass
        typed = iter(["Simple-1!", "Simple-2!"])
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(typed))
        assert m.main(["prologis_app.py", "--add-user", "pat", "Pat", "p@x.test", "Demo", "viewer", "--simple-password"]) == 1
        assert S.user(S.connect(str(tmp_path / "pl.db")), "pat") is None
