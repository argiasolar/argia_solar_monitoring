"""Tests for argia.vendors.sma.SMAClient.

Mocks the HTTP transport at the requests.Session level so we test:
- 3-step OAuth flow (token → bc-authorize → consent simulation)
- Endpoint URL correctness for sandbox vs production
- Error class mapping (401/403 → SMAAuthError; 404 → SMAAPIError;
  429 → rate-limited SMAAPIError; consent state transitions)
- Token expiry handling
- _parse_day_kwh + _parse_inverter_data pure functions
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from argia.vendors.sma import (
    ENDPOINTS,
    SMAAPIError,
    SMAAuthError,
    SMAClient,
    SMAConsentError,
)


# ============================================================
# Helpers
# ============================================================


def _mock_response(status_code=200, json_data=None, text=""):
    m = MagicMock()
    m.status_code = status_code
    m.json = MagicMock(
        return_value=json_data if json_data is not None else {}
    )
    if json_data is None:
        m.json.side_effect = ValueError("no JSON")
    m.text = text or (str(json_data) if json_data else "")
    return m


def _make_client(env="sandbox"):
    return SMAClient(
        client_id="test_id",
        client_secret="test_secret",
        login_hint="apiTestUser@apiSandbox.com",
        environment=env,
    )


# ============================================================
# Construction
# ============================================================


class TestConstruction:
    def test_requires_client_id(self):
        with pytest.raises(ValueError, match="client_id"):
            SMAClient("", "secret", "login@test", "sandbox")

    def test_requires_client_secret(self):
        with pytest.raises(ValueError, match="client_secret"):
            SMAClient("id", "", "login@test", "sandbox")

    def test_requires_login_hint(self):
        with pytest.raises(ValueError, match="login_hint"):
            SMAClient("id", "secret", "", "sandbox")

    def test_rejects_bad_environment(self):
        with pytest.raises(ValueError, match="environment"):
            SMAClient("id", "secret", "login@test", "production-fake")

    def test_sandbox_endpoints_used(self):
        c = _make_client("sandbox")
        assert c._endpoints["token"] == ENDPOINTS["sandbox"]["token"]
        assert "sandbox" in c._endpoints["api_base"]

    def test_production_endpoints_used(self):
        c = _make_client("production")
        assert c._endpoints["token"] == ENDPOINTS["production"]["token"]
        assert "sandbox" not in c._endpoints["api_base"]

    def test_brand_is_sma(self):
        assert _make_client().brand == "SMA"


# ============================================================
# Token fetch (Step 1)
# ============================================================


class TestFetchClientToken:
    def test_success_stores_token_and_expiry(self):
        c = _make_client()
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"access_token": "abc123", "expires_in": 3600},
        )
        c._fetch_client_token()
        assert c._client_token == "abc123"
        assert c._token_expires_at is not None
        # Expiry should be ~3600s in future
        assert c._token_expires_at > time.time() + 3500

    def test_missing_token_raises(self):
        c = _make_client()
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(200, {"foo": "bar"})
        with pytest.raises(SMAAPIError, match="access_token"):
            c._fetch_client_token()

    def test_401_raises_auth(self):
        c = _make_client()
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            401, text="invalid client credentials",
        )
        with pytest.raises(SMAAuthError):
            c._fetch_client_token()

    def test_500_raises_api(self):
        c = _make_client()
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(500, text="server error")
        with pytest.raises(SMAAPIError):
            c._fetch_client_token()

    def test_uses_correct_endpoint_for_sandbox(self):
        c = _make_client("sandbox")
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"access_token": "x", "expires_in": 3600},
        )
        c._fetch_client_token()
        url_called = c._session.post.call_args.args[0]
        assert "sandbox-auth.smaapis.de" in url_called

    def test_uses_correct_endpoint_for_production(self):
        c = _make_client("production")
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"access_token": "x", "expires_in": 3600},
        )
        c._fetch_client_token()
        url_called = c._session.post.call_args.args[0]
        assert url_called.startswith("https://auth.smaapis.de")


# ============================================================
# Token validity check
# ============================================================


class TestTokenValid:
    def test_no_token_invalid(self):
        c = _make_client()
        assert not c._token_valid()

    def test_expired_invalid(self):
        c = _make_client()
        c._client_token = "x"
        c._token_expires_at = time.time() - 100
        assert not c._token_valid()

    def test_valid_token_with_slack(self):
        c = _make_client()
        c._client_token = "x"
        c._token_expires_at = time.time() + 100
        assert c._token_valid()

    def test_about_to_expire_within_slack(self):
        c = _make_client()
        c._client_token = "x"
        c._token_expires_at = time.time() + 10  # less than 30s slack
        assert not c._token_valid()


# ============================================================
# Consent flow (Step 2 + Step 3)
# ============================================================


class TestSandboxConsent:
    def test_sandbox_auto_accepts(self):
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._token_expires_at = time.time() + 3600
        c._session = MagicMock()
        # Step 2 (bc-authorize POST) returns 201 created
        # Step 3 (status PUT) returns 200
        c._session.post.return_value = _mock_response(
            201, {"loginHint": "x", "state": "pending"},
        )
        c._session.put.return_value = _mock_response(200, {"state": "accepted"})
        c._ensure_consent()
        # Both calls were made
        assert c._session.post.called
        assert c._session.put.called

    def test_sandbox_consent_step3_failure_raises(self):
        """If ALL body shapes fail, raise with details of each attempt."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            201, {"loginHint": "x", "state": "pending"},
        )
        # All PUT attempts fail with 500
        c._session.put.return_value = _mock_response(500, text="oops")
        with pytest.raises(SMAConsentError) as exc_info:
            c._ensure_consent()
        # Should have tried every shape
        assert c._session.put.call_count == len(SMAClient._SANDBOX_CONSENT_BODY_SHAPES)
        # Error message should list all attempts
        assert "tried" in str(exc_info.value)
        assert "HTTP 500" in str(exc_info.value)

    def test_sandbox_first_shape_wins(self):
        """First body shape returns 200, no other shapes tried."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(201, {"state": "pending"})
        c._session.put.return_value = _mock_response(200, text="ok")
        c._ensure_consent()
        # Only one PUT, even though multiple shapes exist
        assert c._session.put.call_count == 1
        # And the first shape was used (a dict, sent via json=)
        first_call = c._session.put.call_args_list[0]
        assert first_call.kwargs.get("json") == {"status": "accepted"}

    def test_sandbox_falls_back_on_400(self):
        """First shape gets 400 (.NET deserializer error) → try next shape."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(201, {"state": "pending"})
        # First two shapes fail with 400, third succeeds
        c._session.put.side_effect = [
            _mock_response(400, text="Error converting value"),
            _mock_response(400, text="Error converting value"),
            _mock_response(200, text="ok"),
        ]
        c._ensure_consent()
        assert c._session.put.call_count == 3

    def test_sandbox_204_accepted(self):
        """HTTP 204 No Content is also a valid success."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(201, {"state": "pending"})
        c._session.put.return_value = _mock_response(204, text="")
        c._ensure_consent()
        assert c._session.put.call_count == 1

    def test_bc_authorize_409_treated_as_existing_consent(self):
        """If bc-authorize returns 409 (consent already exists), continue
        to step 3 anyway."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(409, text="exists")
        c._session.put.return_value = _mock_response(200, text="ok")
        c._ensure_consent()  # should NOT raise

    def test_bc_authorize_401_raises_auth(self):
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(401, text="bad token")
        with pytest.raises(SMAAuthError):
            c._ensure_consent()

    # ===== Stage 6.2: state-aware consent flow =====

    def test_state_already_accepted_skips_put(self):
        """bc-authorize POST returns state=accepted → no PUT call."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"loginHint": "x", "state": "accepted"},
        )
        c._ensure_consent()
        assert c._session.post.called
        assert not c._session.put.called

    def test_state_accepted_case_insensitive(self):
        """State='Accepted' or 'ACCEPTED' both recognized."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"state": "Accepted"},
        )
        c._ensure_consent()
        assert not c._session.put.called

    def test_state_rejected_raises(self):
        """If bc-authorize returns state=rejected, raise consent error
        without attempting PUT."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"state": "rejected"},
        )
        with pytest.raises(SMAConsentError, match="rejected"):
            c._ensure_consent()
        assert not c._session.put.called

    @pytest.mark.parametrize("terminal", ["expired", "revoked"])
    def test_state_other_terminal_raises(self, terminal):
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            200, {"state": terminal},
        )
        with pytest.raises(SMAConsentError, match=terminal):
            c._ensure_consent()

    def test_put_404_treated_as_success(self):
        """If PUT returns 404 (no pending consent record), short-circuit
        rotation and treat as success."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        # POST returns 400 (consent already exists) so we still fall through to PUT
        c._session.post.return_value = _mock_response(400, text="exists")
        c._session.put.return_value = _mock_response(
            404, {"message": "Not Found", "code": "404"},
        )
        c._ensure_consent()  # should NOT raise
        # Only the first shape was tried before 404 short-circuited
        assert c._session.put.call_count == 1

    def test_put_404_after_400_short_circuits(self):
        """If first shape returns 400 and second returns 404, the 404
        short-circuits the remaining shapes."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(400, text="exists")
        c._session.put.side_effect = [
            _mock_response(400, text="bad shape"),
            _mock_response(404, text="not found"),
        ]
        c._ensure_consent()
        # 2 PUTs, not 4 (404 short-circuited)
        assert c._session.put.call_count == 2

    def test_state_pending_triggers_put(self):
        """Sanity check: state=pending still triggers the PUT."""
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._session = MagicMock()
        c._session.post.return_value = _mock_response(
            201, {"state": "pending"},
        )
        c._session.put.return_value = _mock_response(204, text="")
        c._ensure_consent()
        assert c._session.put.called


# ============================================================
# login() (orchestrates all 3 steps)
# ============================================================


class TestLogin:
    def test_login_calls_all_three_steps(self):
        c = _make_client("sandbox")
        c._session = MagicMock()
        # Token endpoint returns 200 with token
        # bc-authorize returns 201
        # status PUT returns 200
        c._session.post.side_effect = [
            _mock_response(200, {"access_token": "tok", "expires_in": 3600}),
            _mock_response(201, {"loginHint": "x", "state": "pending"}),
        ]
        c._session.put.return_value = _mock_response(200, text="ok")
        c.login()
        # 2 POSTs (token + bc-authorize) and 1 PUT (status)
        assert c._session.post.call_count == 2
        assert c._session.put.call_count == 1

    def test_login_idempotent_when_token_valid(self):
        c = _make_client("sandbox")
        c._client_token = "tok"
        c._token_expires_at = time.time() + 3600
        c._logged_in_at_consent = True
        c._session = MagicMock()
        c.login()
        # No HTTP calls because already logged in
        assert not c._session.post.called

    def test_login_reauth_after_expiry(self):
        c = _make_client("sandbox")
        c._client_token = "old"
        c._token_expires_at = time.time() - 100
        c._logged_in_at_consent = True  # had logged in previously
        c._session = MagicMock()
        c._session.post.side_effect = [
            _mock_response(200, {"access_token": "new", "expires_in": 3600}),
            _mock_response(201, {"state": "pending"}),
        ]
        c._session.put.return_value = _mock_response(200, text="ok")
        c.login()
        assert c._client_token == "new"


# ============================================================
# _get_json error handling
# ============================================================


class TestGetJsonErrors:
    def _ready_client(self):
        c = _make_client()
        c._client_token = "tok"
        c._token_expires_at = time.time() + 3600
        c._session = MagicMock()
        return c

    def test_no_token_raises_auth_error(self):
        c = _make_client()
        # No login() called → no token
        with pytest.raises(SMAAuthError):
            c._get_json("/plants", {})

    def test_200_returns_json(self):
        c = self._ready_client()
        c._session.get.return_value = _mock_response(200, {"plants": []})
        result = c._get_json("/plants", {})
        assert result == {"plants": []}

    def test_401_raises_auth(self):
        c = self._ready_client()
        c._session.get.return_value = _mock_response(401, text="rejected")
        with pytest.raises(SMAAuthError):
            c._get_json("/plants", {})

    def test_404_raises_api(self):
        c = self._ready_client()
        c._session.get.return_value = _mock_response(404, text="not in sandbox")
        with pytest.raises(SMAAPIError, match="404"):
            c._get_json("/plants/x/devices", {})

    def test_429_raises_rate_limited(self):
        c = self._ready_client()
        c._session.get.return_value = _mock_response(429, text="slow down")
        with pytest.raises(SMAAPIError, match="rate-limited"):
            c._get_json("/plants", {})

    def test_500_raises_api(self):
        c = self._ready_client()
        c._session.get.return_value = _mock_response(500, text="oops")
        with pytest.raises(SMAAPIError, match="500"):
            c._get_json("/plants", {})

    def test_invalid_json_raises_api(self):
        c = self._ready_client()
        m = MagicMock()
        m.status_code = 200
        m.json.side_effect = ValueError("not json")
        m.text = "<html>not json</html>"
        c._session.get.return_value = m
        with pytest.raises(SMAAPIError, match="invalid JSON"):
            c._get_json("/plants", {})


# ============================================================
# _parse_day_kwh (pure function)
# ============================================================


class TestParseDayKwh:
    def test_extracts_total_energy_day(self):
        resp = {"set": {"totalEnergyDay": 250.5}}
        assert SMAClient._parse_day_kwh(resp, "2026-05-14") == 250.5

    def test_extracts_alternate_key(self):
        resp = {"set": {"energyDay": 100.0}}
        assert SMAClient._parse_day_kwh(resp, "2026-05-14") == 100.0

    def test_wh_to_kwh_heuristic(self):
        # 5_000_000 Wh = 5000 kWh — flagged as > 1e6 → divide
        resp = {"set": {"totalEnergyDay": 5_000_000}}
        assert SMAClient._parse_day_kwh(resp, "2026-05-14") == 5000.0

    def test_kwh_passes_through(self):
        resp = {"set": {"totalEnergyDay": 250.5}}
        assert SMAClient._parse_day_kwh(resp, "2026-05-14") == 250.5

    def test_missing_set_returns_none(self):
        assert SMAClient._parse_day_kwh({}, "2026-05-14") is None

    def test_non_dict_returns_none(self):
        assert SMAClient._parse_day_kwh(None, "2026-05-14") is None
        assert SMAClient._parse_day_kwh("nope", "2026-05-14") is None

    def test_no_matching_key_returns_none(self):
        resp = {"set": {"foo": "bar"}}
        assert SMAClient._parse_day_kwh(resp, "2026-05-14") is None


# ============================================================
# _parse_inverter_data
# ============================================================


class TestParseInverterData:
    def _client(self):
        return _make_client()

    def test_parses_power_in_watts(self):
        c = self._client()
        # totalActivePower in W (>1000 means already W per heuristic)
        resp = {"set": {"totalActivePower": 5000.0, "inverterMode": "MPPT"}}
        snap = c._parse_inverter_data(resp, "SMA_SANDBOX", "DEV1")
        assert snap.power_w == 5000.0

    def test_converts_power_kw_to_w(self):
        c = self._client()
        # 25.4 looks like kW (<=1000) → convert to 25400 W
        resp = {"set": {"power": 25.4}}
        snap = c._parse_inverter_data(resp, "SMA_SANDBOX", "DEV1")
        assert snap.power_w == 25400.0

    def test_status_online_for_default(self):
        c = self._client()
        resp = {"set": {"power": 10.0}}
        snap = c._parse_inverter_data(resp, "SMA_SANDBOX", "DEV1")
        assert snap.status == 1

    def test_status_offline_for_fault_state(self):
        c = self._client()
        resp = {"set": {"status": "FAULT", "power": 0}}
        snap = c._parse_inverter_data(resp, "SMA_SANDBOX", "DEV1")
        assert snap.status == 3
        assert snap.raw_status == "FAULT"

    def test_status_from_device_block(self):
        c = self._client()
        # status missing from set, present in device
        resp = {"device": {"status": "OFFLINE"}, "set": {"power": 0}}
        snap = c._parse_inverter_data(resp, "SMA_SANDBOX", "DEV1")
        assert snap.status == 3

    def test_missing_set_returns_none(self):
        c = self._client()
        assert c._parse_inverter_data({}, "X", "Y") is None

    def test_sn_normalized(self):
        c = self._client()
        resp = {"set": {"power": 10.0}}
        snap = c._parse_inverter_data(resp, "X", "  abc-123  ")
        assert snap.inverter_sn == "ABC-123"

    def test_returns_none_for_non_dict(self):
        c = self._client()
        assert c._parse_inverter_data(None, "X", "Y") is None
        assert c._parse_inverter_data("garbage", "X", "Y") is None
