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
