"""The session-login service end to end, through Flask's test client.

The point of this file is item 1 of the 2026-08-28 feedback: "I cannot
log out". Under HTTP Basic that was unfixable — the browser re-sent the
cached password on the next click. test_logout_really_ends_the_session
is the proof that it is fixed: after logout the session row is gone, so
even replaying the exact cookie the browser had is anonymous again.
"""
import importlib
import sys
import time
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))

# The service is Flask; the pure authorisation rules it enforces are
# covered without it in test_auth_core.py, so a machine without Flask
# still gets the important half rather than a red suite.
pytest.importorskip("flask", reason="flask not installed here")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """A fresh service with its own auth dir, users db and session db."""
    monkeypatch.setenv("ARGIA_AUTH_DIR", str(tmp_path))
    import setup_app as sa
    importlib.reload(sa)
    sa.AUTH_DIR = str(tmp_path)
    sa.DB_PATH = str(tmp_path / "users.db")
    import auth_core as ac
    import auth_app as aa
    importlib.reload(ac)
    importlib.reload(aa)
    aa.AUTH_DIR = str(tmp_path)
    aa.SESSION_DB = str(tmp_path / "sessions.db")
    aa.SECRET_PATH = str(tmp_path / "session.key")
    aa._SECRET = None
    aa.sa = sa
    aa.app.config["TESTING"] = True

    # two users: an ARGIA employee (admin) and a CAPEX client
    c = sa.db()
    c.execute("INSERT INTO users(username,hash,level,reports,is_admin)"
              " VALUES('tomasz','x','argia','',1)")
    c.execute("INSERT INTO users(username,hash,level,reports,is_admin)"
              " VALUES('cliente','x','custom','gto2,capex',0)")
    c.execute("INSERT INTO users(username,hash,level,reports,is_admin,disabled)"
              " VALUES('gone','x','argia','',0,1)")
    c.commit()
    c.close()

    monkeypatch.setattr(sa, "verify_pw",
                        lambda u, p: p == "correct-horse-battery")
    sa._PW_FAILS.clear()
    return aa


@pytest.fixture()
def cli(app):
    return app.app.test_client()


def sign_in(cli, user="tomasz", pw="correct-horse-battery", nxt="/"):
    return cli.post("/login", data={"user": user, "password": pw,
                                    "next": nxt})


def check(cli, uri):
    return cli.get("/check", headers={"X-Original-URI": uri})


class TestSigningIn:
    def test_the_form_is_served_without_a_session(self, cli):
        r = cli.get("/login")
        assert r.status_code == 401
        assert b"Sign in" in r.data

    def test_the_form_sends_no_www_authenticate_header(self, cli):
        """That header is what makes the browser show its own dialog —
        the exact behaviour we are replacing."""
        r = cli.get("/login")
        assert "WWW-Authenticate" not in r.headers

    def test_the_form_is_never_cached(self, cli):
        assert "no-store" in cli.get("/login").headers.get("Cache-Control", "")

    def test_good_credentials_set_a_cookie_and_redirect(self, cli, app):
        r = sign_in(cli, nxt="/gto1/")
        assert r.status_code == 303
        assert r.headers["Location"] == "/gto1/"
        assert any(v.startswith(app.ac.COOKIE + "=")
                   for v in r.headers.getlist("Set-Cookie"))

    def test_the_cookie_is_httponly_secure_and_samesite(self, cli):
        raw = [v for v in sign_in(cli).headers.getlist("Set-Cookie")
               if v.startswith("argia_s=")][0]
        assert "HttpOnly" in raw and "Secure" in raw
        assert "SameSite=Lax" in raw and "Path=/" in raw

    def test_wrong_password_does_not_sign_in(self, cli):
        r = cli.post("/login", data={"user": "tomasz", "password": "nope"})
        assert r.status_code == 401
        assert check(cli, "/").status_code == 401

    def test_wrong_password_says_so_without_naming_which_half(self, cli):
        r = cli.post("/login", data={"user": "tomasz", "password": "nope"})
        assert b"Wrong user or password" in r.data

    def test_the_username_is_normalised_like_it_is_stored(self, cli):
        """Capitalised usernames used to 401 for ever (2026-08-28)."""
        r = sign_in(cli, user="Tomasz")
        assert r.status_code == 303

    def test_a_pasted_trailing_space_still_works(self, cli):
        r = sign_in(cli, pw="correct-horse-battery ")
        assert r.status_code == 303

    def test_a_disabled_account_cannot_sign_in(self, cli):
        r = sign_in(cli, user="gone")
        assert r.status_code == 401
        assert b"disabled" in r.data

    def test_an_unknown_user_cannot_sign_in(self, cli):
        assert sign_in(cli, user="nobody").status_code == 401

    def test_repeated_failures_are_throttled(self, cli, app):
        for _ in range(app.sa.PW_FAIL_MAX):
            cli.post("/login", data={"user": "tomasz", "password": "nope"})
        r = cli.post("/login", data={"user": "tomasz", "password": "nope"})
        assert b"Too many attempts" in r.data

    def test_the_throttle_blocks_even_the_right_password(self, cli, app):
        for _ in range(app.sa.PW_FAIL_MAX):
            cli.post("/login", data={"user": "tomasz", "password": "nope"})
        assert sign_in(cli).status_code == 401

    def test_a_success_clears_the_counter(self, cli, app):
        cli.post("/login", data={"user": "tomasz", "password": "nope"})
        assert sign_in(cli).status_code == 303
        assert app.sa.pw_lock_left("tomasz") == 0

    def test_visiting_the_form_while_signed_in_bounces_onward(self, cli):
        sign_in(cli)
        r = cli.get("/login?next=/capex/")
        assert r.status_code == 303 and r.headers["Location"] == "/capex/"


class TestNextIsNotAnOpenRedirect:
    @pytest.mark.parametrize("bad", [
        "//evil.example/", "https://evil.example/", "/\\evil.example",
        "http://evil.example", "javascript:alert(1)", "", None,
    ])
    def test_offsite_targets_are_dropped(self, cli, bad):
        r = cli.post("/login", data={"user": "tomasz",
                                     "password": "correct-horse-battery",
                                     "next": bad})
        assert r.headers["Location"] == "/"

    def test_it_never_bounces_back_to_the_login(self, cli):
        r = sign_in(cli, nxt="/login")
        assert r.headers["Location"] == "/"

    def test_a_normal_path_is_kept(self, cli):
        r = sign_in(cli, nxt="/monitoring/gto2/d/2026-08-01.html")
        assert r.headers["Location"] == "/monitoring/gto2/d/2026-08-01.html"


class TestCheckEndpoint:
    def test_public_pages_need_no_session(self, cli):
        for p in ("/logged-out.html", "/no-access.html", "/login"):
            assert check(cli, p).status_code == 200

    def test_anonymous_gets_401_not_403(self, cli):
        """401 is what nginx turns into the sign-in page; 403 is the
        no-access body. Confusing them sends people to the wrong page."""
        assert check(cli, "/").status_code == 401
        assert check(cli, "/gto1/").status_code == 401

    def test_employee_reaches_everything_but_setup_is_admin_gated(self, cli):
        sign_in(cli)
        for p in ("/", "/gto1/", "/financial/", "/monitoring/", "/capex/"):
            assert check(cli, p).status_code == 200
        assert check(cli, "/setup/").status_code == 200   # tomasz is admin

    def test_the_username_is_passed_back_to_nginx(self, cli):
        sign_in(cli)
        assert check(cli, "/").headers["X-Argia-User"] == "tomasz"

    def test_a_client_reaches_only_their_own_pages(self, cli):
        sign_in(cli, user="cliente")
        assert check(cli, "/").status_code == 200
        assert check(cli, "/capex/").status_code == 200
        assert check(cli, "/gto2/").status_code == 200
        assert check(cli, "/monitoring/gto2/").status_code == 200
        assert check(cli, "/account/").status_code == 200

    def test_a_client_is_refused_everything_else(self, cli):
        sign_in(cli, user="cliente")
        for p in ("/gto1/", "/slp1/", "/financial/", "/monitoring/",
                  "/monitoring/ppa/", "/setup/"):
            assert check(cli, p).status_code == 403, p

    def test_ppa_stays_internal(self, cli):
        sign_in(cli, user="cliente")
        for ppa in ("gto1", "mex1", "mex2", "nl1", "slp1", "slp2"):
            assert check(cli, f"/{ppa}/").status_code == 403
            assert check(cli, f"/monitoring/{ppa}/").status_code == 403

    def test_a_forged_cookie_is_anonymous(self, cli):
        sign_in(cli)
        cli.set_cookie("argia_s", "bogus.value", domain="localhost")
        assert check(cli, "/").status_code == 401

    def test_disabling_a_user_ends_their_access_on_the_next_click(
            self, cli, app):
        sign_in(cli)
        assert check(cli, "/").status_code == 200
        c = app.sa.db()
        c.execute("UPDATE users SET disabled=1 WHERE username='tomasz'")
        c.commit()
        c.close()
        assert check(cli, "/").status_code == 401

    def test_removing_a_grant_takes_effect_on_the_next_click(self, cli, app):
        sign_in(cli, user="cliente")
        assert check(cli, "/gto2/").status_code == 200
        c = app.sa.db()
        c.execute("UPDATE users SET reports='capex' WHERE username='cliente'")
        c.commit()
        c.close()
        assert check(cli, "/gto2/").status_code == 403


class TestLogoutActuallyWorks:
    def test_logout_really_ends_the_session(self, cli):
        """The whole reason this replaced HTTP Basic."""
        sign_in(cli)
        assert check(cli, "/").status_code == 200
        cli.get("/logout")
        assert check(cli, "/").status_code == 401

    def test_the_session_row_is_gone_not_just_the_cookie(self, cli, app):
        """Clearing the cookie alone would leave a live session that a
        copied cookie could still use."""
        sign_in(cli)
        c = app.ac.open_sessions(app.SESSION_DB)
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        c.close()
        cli.get("/logout")
        c = app.ac.open_sessions(app.SESSION_DB)
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        c.close()

    def test_replaying_the_old_cookie_does_not_get_back_in(self, cli, app):
        """A correctly signed cookie for a deleted session is worthless
        — which is precisely what Basic auth could not offer."""
        sign_in(cli)
        c = app.ac.open_sessions(app.SESSION_DB)
        sid = c.execute("SELECT sid FROM sessions").fetchone()[0]
        c.close()
        stolen = app.ac.sign(sid, app.secret())
        assert app.ac.verify(stolen, app.secret()) == sid   # it IS valid
        cli.get("/logout")
        cli.set_cookie(app.ac.COOKIE, stolen, domain="localhost")
        assert check(cli, "/").status_code == 401

    def test_logout_clears_the_cookie_too(self, cli):
        sign_in(cli)
        r = cli.get("/logout")
        raw = [v for v in r.headers.getlist("Set-Cookie")
               if v.startswith("argia_s=")]
        assert raw and ("Max-Age=0" in raw[0] or "01 Jan 1970" in raw[0])

    def test_logout_lands_on_the_logged_out_page(self, cli):
        sign_in(cli)
        r = cli.get("/logout")
        assert r.status_code == 303
        assert r.headers["Location"] == "/logged-out.html"

    def test_logout_without_a_session_is_harmless(self, cli):
        assert cli.get("/logout").status_code == 303

    def test_logging_out_does_not_end_someone_else_s_session(self, cli, app):
        other = app.app.test_client()
        sign_in(other, user="cliente")
        sign_in(cli, user="tomasz")
        cli.get("/logout")
        assert check(other, "/capex/").status_code == 200


class TestSessionExpiry:
    def test_an_idle_session_stops_working(self, cli, app):
        sign_in(cli)
        old = int(time.time()) - app.ac.IDLE_MAX - 5
        c = app.ac.open_sessions(app.SESSION_DB)
        c.execute("UPDATE sessions SET last=?", (old,))
        c.commit()
        c.close()
        assert check(cli, "/").status_code == 401

    def test_the_expired_row_is_cleaned_up(self, cli, app):
        sign_in(cli)
        old = int(time.time()) - app.ac.IDLE_MAX - 5
        c = app.ac.open_sessions(app.SESSION_DB)
        c.execute("UPDATE sessions SET last=?", (old,))
        c.commit()
        c.close()
        check(cli, "/")
        c = app.ac.open_sessions(app.SESSION_DB)
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        c.close()

    def test_use_keeps_a_session_alive(self, cli, app):
        sign_in(cli)
        c = app.ac.open_sessions(app.SESSION_DB)
        c.execute("UPDATE sessions SET last=?", (int(time.time()) - 300,))
        c.commit()
        c.close()
        assert check(cli, "/").status_code == 200
        c = app.ac.open_sessions(app.SESSION_DB)
        last = c.execute("SELECT last FROM sessions").fetchone()[0]
        c.close()
        assert time.time() - last < 5


class TestWhoami:
    def test_anonymous_is_blank(self, cli):
        assert cli.get("/session/whoami").get_json()["user"] == ""

    def test_signed_in_reports_the_user(self, cli):
        sign_in(cli)
        d = cli.get("/session/whoami").get_json()
        assert d["user"] == "tomasz" and d["admin"] is True

    def test_a_client_is_not_flagged_as_admin(self, cli):
        sign_in(cli, user="cliente")
        assert cli.get("/session/whoami").get_json()["admin"] is False

    def test_it_is_never_cached(self, cli):
        h = cli.get("/session/whoami").headers.get("Cache-Control", "")
        assert "no-store" in h
