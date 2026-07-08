"""Tests: Growatt session persistence + login backoff.

Incident 2026-07-07: ~200 fresh logins/day from the Pi's single IP got
soft-blocked (HTTP 200, no assToken), and every call retried the login,
hammering the block permanent. The session file drops logins to ~1-2 a
day; the backoff marker makes a refused login STOP all attempts for the
cooldown so the block can cool.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from argia.vendors import growatt_session as gs


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_GROWATT_SESSION_FILE",
                       str(tmp_path / "session.json"))
    monkeypatch.setenv("ARGIA_GROWATT_BACKOFF_FILE",
                       str(tmp_path / "backoff"))
    monkeypatch.setenv("ARGIA_GROWATT_LOGIN_COOLDOWN_S", "900")


class TestCookiePersistence:
    def test_roundtrip(self):
        s1 = requests.Session()
        s1.cookies.set("assToken", "T1")
        s1.cookies.set("JSESSIONID", "J1")
        gs.save_cookies(s1)
        s2 = requests.Session()
        assert gs.load_cookies(s2) is True        # assToken present
        assert s2.cookies.get("assToken") == "T1"
        assert s2.cookies.get("JSESSIONID") == "J1"

    def test_no_file_means_fresh_login(self):
        assert gs.load_cookies(requests.Session()) is False

    def test_session_without_asstoken_not_trusted(self):
        s1 = requests.Session()
        s1.cookies.set("JSESSIONID", "only")
        gs.save_cookies(s1)
        assert gs.load_cookies(requests.Session()) is False

    def test_corrupt_file_degrades_to_fresh_login(self):
        gs.session_file().write_text("{not json")
        assert gs.load_cookies(requests.Session()) is False

    def test_drop_session(self):
        gs.save_cookies(requests.Session())
        gs.drop_session()
        assert not gs.session_file().exists()


class TestBackoff:
    def test_mark_then_blocked_then_expires(self):
        assert gs.backoff_remaining_s() == 0
        gs.mark_login_failure()
        assert 0 < gs.backoff_remaining_s() <= 900
        with pytest.raises(gs.LoginBackoff):
            gs.check_backoff()
        # simulate cooldown passing
        gs.backoff_file().write_text(str(time.time() - 901))
        assert gs.backoff_remaining_s() == 0
        gs.check_backoff()                        # no raise

    def test_clear(self):
        gs.mark_login_failure()
        gs.clear_backoff()
        gs.check_backoff()


class TestWebClientIntegration:
    def _client(self):
        from argia.vendors.growatt_web import GrowattWebClient
        return GrowattWebClient(username="u", password="p")

    def test_restored_session_skips_login_entirely(self):
        s = requests.Session()
        s.cookies.set("assToken", "T1")
        gs.save_cookies(s)
        c = self._client()
        with patch.object(c, "_session", wraps=c._session) as spy:
            c.login()
            spy.post.assert_not_called()          # zero login HTTP

    def test_backoff_blocks_login_without_any_http(self):
        gs.mark_login_failure()
        c = self._client()
        with patch.object(c._session, "post") as post, \
             patch.object(c._session, "get") as get:
            with pytest.raises(gs.LoginBackoff):
                c.login()
            post.assert_not_called()
            get.assert_not_called()

    def test_refused_login_marks_backoff_and_drops_session(self):
        gs.save_cookies(requests.Session())       # a (stale) file exists
        c = self._client()
        c._logged_in = False
        resp = MagicMock(status_code=200, headers={})
        resp.json.side_effect = ValueError
        with patch.object(c._session, "get"), \
             patch.object(c._session, "post", return_value=resp):
            c._session.cookies.clear()
            with pytest.raises(Exception, match="backoff engaged"):
                c.login()
        assert gs.backoff_remaining_s() > 0
        assert not gs.session_file().exists()

    def test_successful_login_persists_and_clears(self):
        gs.mark_login_failure()
        gs.backoff_file().write_text(str(time.time() - 901))  # cooled
        c = self._client()
        c._logged_in = False

        def do_post(url, **kw):
            c._session.cookies.set("assToken", "FRESH")
            return MagicMock(status_code=200, headers={})
        with patch.object(c._session, "get"), \
             patch.object(c._session, "post", side_effect=do_post):
            c.login()
        assert gs.backoff_remaining_s() == 0
        saved = json.loads(gs.session_file().read_text())
        assert saved["cookies"]["assToken"] == "FRESH"


class TestSessionAgeGate:
    """2026-07-08: cookies saved 12:30 were dead by 05:00 and v47 trusted
    them forever — 14 errors/run, zero re-login attempts, all morning."""

    def _save_with_age(self, age_s):
        import time as _t
        s = requests.Session()
        s.cookies.set("assToken", "OLD")
        gs.save_cookies(s)
        raw = json.loads(gs.session_file().read_text())
        raw["saved_at"] = _t.time() - age_s
        gs.session_file().write_text(json.dumps(raw))

    def test_old_session_not_trusted_and_dropped(self):
        self._save_with_age(21 * 3600)
        assert gs.load_cookies(requests.Session()) is False
        assert not gs.session_file().exists()      # dropped, not lingering

    def test_fresh_session_still_trusted(self):
        self._save_with_age(2 * 3600)
        assert gs.load_cookies(requests.Session()) is True

    def test_age_limit_env_override(self, monkeypatch):
        monkeypatch.setenv("ARGIA_GROWATT_SESSION_MAX_AGE_S", "60")
        self._save_with_age(120)
        assert gs.load_cookies(requests.Session()) is False


class TestSessionValidator:
    @responses.activate
    def test_valid_session_stays_on_index(self):
        responses.add(responses.GET, "https://server.growatt.com/index",
                      status=200, body="<html>dashboard</html>")
        s = requests.Session()
        assert gs.validate_web_session(s, "https://server.growatt.com") is True

    @responses.activate
    def test_expired_session_redirected_to_login(self):
        responses.add(responses.GET, "https://server.growatt.com/index",
                      status=302,
                      headers={"Location": "https://server.growatt.com/login"})
        responses.add(responses.GET, "https://server.growatt.com/login",
                      status=200, body="<html>login</html>")
        s = requests.Session()
        assert gs.validate_web_session(s, "https://server.growatt.com") is False

    @responses.activate
    def test_probe_error_means_invalid(self):
        responses.add(responses.GET, "https://server.growatt.com/index",
                      body=ConnectionError("down"))
        assert gs.validate_web_session(
            requests.Session(), "https://server.growatt.com") is False


class TestEnsureSession:
    """v50's /index probe validated zombies (Growatt serves a 200 SPA
    shell to dead sessions — login redirect is client-side JS; observed
    live 2026-07-08). Staleness detection now lives at the point of
    truth: _post/_get see HTML-instead-of-JSON and re-auth once."""

    def _client_with_restored_session(self):
        from argia.vendors.growatt_web import GrowattWebClient
        s = requests.Session()
        s.cookies.set("assToken", "T1")
        gs.save_cookies(s)
        c = GrowattWebClient(username="u", password="p")
        assert c._logged_in is True
        return c

    def test_restored_session_trusted_without_probe(self):
        c = self._client_with_restored_session()
        with patch.object(c, "login") as login, \
             patch.object(c._session, "get") as get:
            c.ensure_session()
            login.assert_not_called()
            get.assert_not_called()               # no /index probe

    def test_not_logged_in_goes_straight_to_login(self):
        from argia.vendors.growatt_web import GrowattWebClient
        c = GrowattWebClient(username="u", password="p")
        with patch.object(c, "login") as login:
            c.ensure_session()
            login.assert_called_once()

    def test_telemetry_calls_ensure_for_both_clients(self):
        from pathlib import Path
        v2 = Path(__file__).resolve().parents[2]
        tel = (v2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
        assert tel.count("ensure_session()") == 2


class TestSignatureReauth:
    def _client(self):
        from argia.vendors.growatt_web import GrowattWebClient
        s = requests.Session()
        s.cookies.set("assToken", "DEAD")
        gs.save_cookies(s)
        return GrowattWebClient(username="u", password="p")

    def _resp(self, text):
        r = MagicMock(status_code=200)
        r.text = text
        r.json.side_effect = ValueError
        return r

    def test_html_response_triggers_one_relogin_and_retry(self):
        c = self._client()
        html = self._resp("<html>login page</html>")
        good = MagicMock(status_code=200, text='{"obj": {}}')
        with patch.object(c._session, "post",
                          side_effect=[html, good]) as post, \
             patch.object(c, "login") as login:
            c._post("/panel/getPlantData", {})
            assert post.call_count == 2           # retried once
            login.assert_called()                 # fresh login happened
        assert not gs.session_file().exists()     # dead session dropped

    def test_reauth_happens_at_most_once_per_run(self):
        c = self._client()
        html = self._resp("<html>still login</html>")
        with patch.object(c._session, "post",
                          side_effect=[html, html, html]) as post, \
             patch.object(c, "login"):
            c._post("/panel/getPlantData", {})    # reauth + retry, gives up
            c._post("/panel/getPlantData", {})    # NO second reauth
            assert post.call_count == 3           # 2 + 1, not 2 + 2

    def test_backoff_during_reauth_propagates(self):
        c = self._client()
        gs.mark_login_failure()
        html = self._resp("<html>login</html>")
        c._logged_in = True                        # restored, so first
        with patch.object(c._session, "post", return_value=html):
            with pytest.raises(gs.LoginBackoff):
                c._post("/panel/getPlantData", {})  # reauth hits backoff

    def test_json_response_never_touches_reauth(self):
        c = self._client()
        good = MagicMock(status_code=200, text='{"obj": {}}')
        with patch.object(c._session, "post", return_value=good) as post, \
             patch.object(c, "login") as login:
            c._post("/panel/getPlantData", {})
            assert post.call_count == 1
        assert gs.session_file().exists()          # session untouched
