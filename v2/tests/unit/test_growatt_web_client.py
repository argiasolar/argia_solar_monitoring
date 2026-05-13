"""
Tests for argia.vendors.growatt_web.GrowattWebClient.

The HTTP client is thin — most logic is in the parser — so these tests
focus on:
  * Constructor validation
  * Safety guards (refusing mutation-shaped paths)
  * Login envelope (cookie / redirect / JSON response handling)
  * That each endpoint method builds the right URL + body
  * That responses are wrapped in the same {_meta, response} shape the
    fixtures use, so parser code is environment-agnostic.

No real network is touched. We pass in a stub Session.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from argia.vendors.growatt_web import (
    DEFAULT_USER_AGENT,
    WEB_BASE,
    GrowattAPIError,
    GrowattAuthError,
    GrowattUnsafePathError,
    GrowattWebClient,
)


# =====================================================================
# Fake Session — captures requests, returns scripted responses
# =====================================================================

class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        url: str = "https://server.growatt.com/",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json_body
        self.url = url

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


class FakeCookieJar:
    def __init__(self) -> None:
        self._cookies: Dict[str, str] = {}

    def get_dict(self) -> Dict[str, str]:
        return dict(self._cookies)

    def set(self, name: str, value: str) -> None:
        self._cookies[name] = value


class FakeSession:
    """Captures every call. Set ``next_response`` before each call."""

    def __init__(self) -> None:
        self.cookies = FakeCookieJar()
        self.headers: Dict[str, str] = {}
        self.calls: List[Dict[str, Any]] = []
        self._queue: List[FakeResponse] = []

    def setdefault(self, key, default):
        # requests.Session.headers uses dict-like interface
        self.headers.setdefault(key, default)
        return self.headers[key]

    def queue(self, *responses: FakeResponse) -> None:
        self._queue.extend(responses)

    def _pop(self) -> FakeResponse:
        if not self._queue:
            raise AssertionError("FakeSession: no scripted response queued")
        return self._queue.pop(0)

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self._pop()

    def post(self, url, data=None, headers=None, timeout=None,
             allow_redirects=None, **kwargs):
        self.calls.append({
            "method": "POST", "url": url, "data": data, "headers": headers,
            "allow_redirects": allow_redirects,
        })
        return self._pop()


def _make_client(session: Optional[FakeSession] = None) -> GrowattWebClient:
    session = session or FakeSession()
    return GrowattWebClient(
        username="user", password="pass", session=session
    )


# =====================================================================
# 1. Constructor
# =====================================================================

class TestConstructor:
    def test_requires_username(self):
        with pytest.raises(ValueError, match="username"):
            GrowattWebClient(username="", password="x")

    def test_requires_password(self):
        with pytest.raises(ValueError, match="password"):
            GrowattWebClient(username="x", password="")

    def test_sets_default_user_agent(self):
        session = FakeSession()
        _make_client(session)
        # Note: requests.Session.headers.setdefault is what we test;
        # FakeSession proxies through .headers dict
        # The client uses self._session.headers.setdefault on the real
        # Session, which our FakeSession exposes via .headers
        assert session.headers.get("User-Agent") == DEFAULT_USER_AGENT


# =====================================================================
# 2. Safety guards
# =====================================================================

class TestSafetyGuards:
    @pytest.mark.parametrize("path", [
        "/commonDeviceSetC/setMaxParam",
        "/commonDeviceSetC/foo",
        "/some/setMax/path",
        "/api/setTlx",
        "/foo/delete",
        "/save/something",
        "/api/setInverter",
    ])
    def test_refuses_mutation_paths(self, path):
        session = FakeSession()
        # Pre-set assToken so login() short-circuits
        session.cookies.set("assToken", "abc")
        client = _make_client(session)
        client._logged_in = True  # bypass login HTTP

        with pytest.raises(GrowattUnsafePathError):
            client._post(path)
        with pytest.raises(GrowattUnsafePathError):
            client._get(path)

    @pytest.mark.parametrize("path", [
        "/device/getMAXHistory",
        "/panel/getPlantData",
        "/panel/alertPlantEvent",
        "/panel/max/getMAXTotalData",
        "/returnDevice/listDevice",
    ])
    def test_allows_safe_paths(self, path):
        # If guard would reject these, that's a regression
        from argia.vendors.growatt_web import _is_unsafe
        assert _is_unsafe(path) is False


# =====================================================================
# 3. login()
# =====================================================================

class TestLogin:
    def test_login_via_asstoken_cookie(self):
        session = FakeSession()
        # GET /login (prime), POST /login (auth) — both 200, second sets cookie
        session.queue(
            FakeResponse(200, text=""),  # the prime
            FakeResponse(200, text="ok"),  # the auth (cookie set below)
        )
        # Cookie has to be set BEFORE post returns; simulate it via the queue order
        session.cookies.set("assToken", "abc-123")

        client = _make_client(session)
        client.login()
        assert client._logged_in is True

    def test_login_via_302_redirect(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, text=""),  # prime
            FakeResponse(302, headers={"Location": "/index"}),
        )
        client = _make_client(session)
        client.login()
        assert client._logged_in is True

    def test_login_via_json_success(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, text=""),
            FakeResponse(200, headers={"Content-Type": "application/json"},
                         json_body={"result": 1}),
        )
        client = _make_client(session)
        client.login()
        assert client._logged_in is True

    def test_login_failure_no_cookie_raises(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, text=""),
            FakeResponse(200, text="login failed"),
        )
        client = _make_client(session)
        with pytest.raises(GrowattAuthError):
            client.login()

    def test_login_idempotent(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, text=""),
            FakeResponse(200, text="ok"),
        )
        session.cookies.set("assToken", "x")
        client = _make_client(session)
        client.login()
        # Second call must not hit the session again
        before = len(session.calls)
        client.login()
        assert len(session.calls) == before


# =====================================================================
# 4. Endpoint methods — verify URL + request body shape
# =====================================================================

class TestEndpointMethods:
    def _logged_in_client(self) -> tuple[GrowattWebClient, FakeSession]:
        session = FakeSession()
        client = _make_client(session)
        client._logged_in = True  # skip login HTTP
        return client, session

    def test_get_max_history_builds_correct_request(self):
        client, session = self._logged_in_client()
        session.queue(FakeResponse(
            200, text='{"result":1,"obj":{"datas":[]}}',
            url=f"{WEB_BASE}/device/getMAXHistory",
        ))
        envelope = client.get_max_history(sn="JFM7DXN00T", date_iso="2026-05-11")
        # Check the call
        call = session.calls[-1]
        assert call["method"] == "POST"
        assert call["url"] == f"{WEB_BASE}/device/getMAXHistory"
        assert call["data"] == {
            "maxSn": "JFM7DXN00T",
            "startDate": "2026-05-11",
            "endDate": "2026-05-11",
            "start": "0",
        }
        # Envelope shape: fixture-compatible
        assert "_meta" in envelope
        assert "response" in envelope

    def test_get_plant_data_uses_query_param(self):
        client, session = self._logged_in_client()
        session.queue(FakeResponse(200, text='{"result":1}'))
        client.get_plant_data(plant_id="9309575")
        assert session.calls[-1]["url"] == (
            f"{WEB_BASE}/panel/getPlantData?plantId=9309575"
        )

    def test_get_devices_by_plant_url(self):
        client, session = self._logged_in_client()
        session.queue(FakeResponse(200, text='{"result":1}'))
        client.get_devices_by_plant(plant_id="9309575")
        assert session.calls[-1]["url"] == (
            f"{WEB_BASE}/panel/getDevicesByPlant?plantId=9309575"
        )

    def test_envelope_wraps_text_response_in_raw_text(self):
        """Growatt's habit: JSON body but text/html content-type. The client
        must produce the same {_raw_text: ...} shape as captured fixtures."""
        client, session = self._logged_in_client()
        session.queue(FakeResponse(
            200,
            text='{"result":1,"obj":{}}',
            headers={"Content-Type": "text/html"},
        ))
        envelope = client.get_max_total_data("9309575")
        assert envelope["response"]["_raw_text"] == '{"result":1,"obj":{}}'

    def test_envelope_uses_parsed_json_when_content_type_json(self):
        client, session = self._logged_in_client()
        session.queue(FakeResponse(
            200,
            json_body={"result": 1, "obj": {"x": 1}},
            headers={"Content-Type": "application/json"},
        ))
        envelope = client.get_max_total_data("9309575")
        # When Content-Type is JSON, response is parsed
        assert envelope["response"] == {"result": 1, "obj": {"x": 1}}

    def test_non_200_status_raises_api_error(self):
        client, session = self._logged_in_client()
        session.queue(FakeResponse(500, text="server down"))
        with pytest.raises(GrowattAPIError):
            client.get_plant_data("9309575")
