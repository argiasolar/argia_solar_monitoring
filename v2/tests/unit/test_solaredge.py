"""Tests for argia.vendors.solaredge."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.core.time_utils import MX_TZ, UTC
from argia.vendors.base import InverterSnapshot
from argia.vendors.solaredge import (
    SolarEdgeAPIError,
    SolarEdgeAuthError,
    SolarEdgeClient,
)
from tests.conftest import load_fixture


# ----------------- helpers -----------------


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig(
        plant_key="MEX2",
        customer="VITALMEX",
        brand="SOLAREDGE",
        site_id="123456",
        kwp_dc=610.0,
        lat=19.4326,
        lon=-99.1332,
        weather_plant_id="9275498",
        datalogger_sn="DYD0E8501G",
        datalogger_addr=1,
        kwp_ac=0.0,
        expected_factor=0.8,
        pr_target=0.85,
        installation_date='2025-01-01',
        secret_api_name='',
        secret_user_name='',
        secret_pass_name='',
        active=True,
    )


@pytest.fixture
def inverters() -> list:
    return [
        InverterConfig("MEX2", "7E1A2B3C-FF", "Inverter 1", 100.0, True),
        InverterConfig("MEX2", "7E1A2B3D-FF", "Inverter 2", 100.0, True),
    ]


@pytest.fixture
def client() -> SolarEdgeClient:
    return SolarEdgeClient(api_key="fake-key")


# ----------------- constructor -----------------


class TestConstructor:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key"):
            SolarEdgeClient(api_key="")

    def test_brand_label(self, client):
        assert client.brand == "SOLAREDGE"

    def test_login_is_noop(self, client):
        # Doesn't call HTTP, just returns
        client.login()


# ----------------- HTTP error handling -----------------


class TestHttpErrorHandling:
    def test_401_raises_auth_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "unauthorized"
        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(SolarEdgeAuthError):
                client._get_json("/site/x/energy", {})

    def test_403_raises_auth_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "forbidden"
        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(SolarEdgeAuthError):
                client._get_json("/site/x/energy", {})

    def test_429_raises_api_error_with_quota_message(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = "rate limited"
        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(SolarEdgeAPIError, match="rate-limited"):
                client._get_json("/site/x/energy", {})

    def test_500_raises_api_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "boom"
        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(SolarEdgeAPIError):
                client._get_json("/site/x/energy", {})

    def test_invalid_json_raises_api_error(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(SolarEdgeAPIError, match="invalid JSON"):
                client._get_json("/site/x/energy", {})

    def test_api_key_in_query_params(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client._get_json("/site/x/energy", {"foo": "bar"})
        # Verify api_key was added to params
        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs["params"]["api_key"] == "fake-key"
        assert call_kwargs["params"]["foo"] == "bar"


# ----------------- fetch_day_kwh -----------------


class TestFetchDayKwh:
    def test_success_converts_wh_to_kwh(self, client, plant):
        # Fixture has 1245500 Wh → 1245.5 kWh
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "site_energy_day_success.json"),
        ):
            assert client.fetch_day_kwh(plant, "2026-04-15") == 1245.5

    def test_calls_correct_endpoint(self, client, plant):
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "site_energy_day_success.json"),
        ) as mock:
            client.fetch_day_kwh(plant, "2026-04-15")
        mock.assert_called_once_with(
            "/site/123456/energy",
            {"timeUnit": "DAY", "startDate": "2026-04-15", "endDate": "2026-04-15"},
        )

    def test_null_value_returns_none(self, client, plant):
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "site_energy_day_null.json"),
        ):
            assert client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_empty_values_returns_none(self, client, plant):
        with patch.object(
            client, "_get_json",
            return_value={"energy": {"timeUnit": "DAY", "unit": "Wh", "values": []}},
        ):
            assert client.fetch_day_kwh(plant, "2026-04-15") is None

    def test_wrong_date_returns_none(self, client, plant):
        # Fixture has 2026-04-15; ask for a different date
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "site_energy_day_success.json"),
        ):
            assert client.fetch_day_kwh(plant, "2026-04-16") is None


class TestParseDayKwh:
    def test_unit_kwh_no_division(self):
        # Hypothetical: API returns unit=kWh
        resp = {
            "energy": {
                "timeUnit": "DAY",
                "unit": "kWh",
                "values": [{"date": "2026-04-15 00:00:00", "value": 1245.5}],
            }
        }
        assert SolarEdgeClient._parse_day_kwh(resp, "2026-04-15") == 1245.5

    def test_unit_wh_divides(self):
        resp = {
            "energy": {
                "timeUnit": "DAY",
                "unit": "Wh",
                "values": [{"date": "2026-04-15 00:00:00", "value": 1245500.0}],
            }
        }
        assert SolarEdgeClient._parse_day_kwh(resp, "2026-04-15") == 1245.5

    def test_missing_energy_key(self):
        assert SolarEdgeClient._parse_day_kwh({}, "2026-04-15") is None

    def test_none_response(self):
        assert SolarEdgeClient._parse_day_kwh(None, "2026-04-15") is None  # type: ignore[arg-type]

    def test_non_list_values(self):
        resp = {"energy": {"values": "garbage"}}
        assert SolarEdgeClient._parse_day_kwh(resp, "2026-04-15") is None

    def test_skips_non_dict_entries(self):
        resp = {
            "energy": {
                "unit": "Wh",
                "values": [
                    "string-not-dict",
                    None,
                    {"date": "2026-04-15 00:00:00", "value": 1000.0},
                ],
            }
        }
        assert SolarEdgeClient._parse_day_kwh(resp, "2026-04-15") == 1.0


# ----------------- inverter snapshots -----------------


class TestFetchInverterSnapshots:
    def test_success_two_inverters(self, client, plant, inverters):
        responses = iter([
            load_fixture("solaredge", "equipment_data_online.json"),
            load_fixture("solaredge", "equipment_data_online.json"),
        ])
        with patch.object(client, "_get_json", side_effect=lambda p, params: next(responses)):
            snaps = client.fetch_inverter_snapshots(plant, inverters)
        assert len(snaps) == 2
        assert all(isinstance(s, InverterSnapshot) for s in snaps)

    def test_one_inverter_call_per_serial(self, client, plant, inverters):
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "equipment_data_online.json"),
        ) as mock:
            client.fetch_inverter_snapshots(plant, inverters)
        assert mock.call_count == 2

    def test_skips_failed_inverter_continues_with_others(
        self, client, plant, inverters
    ):
        # First inverter raises, second succeeds
        responses = iter([
            SolarEdgeAPIError("boom"),
            load_fixture("solaredge", "equipment_data_online.json"),
        ])

        def fake(_path, _params):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(client, "_get_json", side_effect=fake):
            snaps = client.fetch_inverter_snapshots(plant, inverters)
        assert len(snaps) == 1

    def test_empty_inverter_list(self, client, plant):
        assert client.fetch_inverter_snapshots(plant, []) == []

    def test_correct_endpoint_path(self, client, plant, inverters):
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "equipment_data_online.json"),
        ) as mock:
            client.fetch_inverter_snapshots(plant, [inverters[0]])
        path = mock.call_args.args[0]
        assert path == "/equipment/123456/7E1A2B3C-FF/data"

    def test_request_window_today_in_site_local(self, client, plant, inverters):
        """Window must be 'YYYY-MM-DD HH:MM:SS' from midnight site-local to now."""
        with patch.object(
            client, "_get_json",
            return_value=load_fixture("solaredge", "equipment_data_online.json"),
        ) as mock:
            client.fetch_inverter_snapshots(plant, [inverters[0]])
        params = mock.call_args.args[1]
        assert "startTime" in params
        assert "endTime" in params
        # Format check
        for value in (params["startTime"], params["endTime"]):
            dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # should not raise
        assert params["startTime"].endswith(" 00:00:00")


class TestParseInverterData:
    def test_returns_snapshot_with_latest_telemetry(self, client, plant):
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_online.json"),
            "MEX2", "7E1A2B3C-FF",
        )
        assert result is not None
        # Latest telemetry power = 45800 W
        assert result.power_w == 45800.0

    def test_status_online_for_mppt(self, client, plant):
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_online.json"),
            "MEX2", "ABC",
        )
        assert result is not None
        assert result.status == 1

    def test_status_offline_for_fault(self, client, plant):
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_fault.json"),
            "MEX2", "ABC",
        )
        assert result is not None
        assert result.status == 3
        assert result.raw_status == "FAULT"

    def test_etoday_computed_from_total_energy_diff(self, client, plant):
        # First telemetry total = 285420800.0 Wh, latest = 285643050.0 Wh
        # Diff = 222250 Wh = 222.25 kWh
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_online.json"),
            "MEX2", "ABC",
        )
        assert result is not None
        assert result.etoday_kwh == 222.25

    def test_etoday_clamped_non_negative(self, client, plant):
        # If the data is somehow inverted, we shouldn't return negative kWh
        weird = {
            "data": {
                "telemetries": [
                    {"date": "2026-04-15 00:00:00", "totalEnergy": 1000.0,
                     "totalActivePower": 0, "inverterMode": "MPPT"},
                    {"date": "2026-04-15 12:00:00", "totalEnergy": 500.0,
                     "totalActivePower": 0, "inverterMode": "MPPT"},
                ]
            }
        }
        result = client._parse_inverter_data(weird, "MEX2", "ABC")
        assert result is not None
        assert result.etoday_kwh == 0.0

    def test_empty_telemetries_returns_none(self, client):
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_empty.json"),
            "MEX2", "ABC",
        )
        assert result is None

    def test_missing_data_key_returns_none(self, client):
        assert client._parse_inverter_data({}, "MEX2", "ABC") is None

    def test_sn_normalized(self, client):
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_online.json"),
            "MEX2", "  abc-123  ",
        )
        assert result is not None
        assert result.inverter_sn == "ABC-123"

    def test_timestamp_converted_from_site_local_to_utc(self, client):
        # Latest telemetry is "2026-04-15 12:30:00" in MX local
        # MX is UTC-6 → UTC equivalent is 18:30:00
        result = client._parse_inverter_data(
            load_fixture("solaredge", "equipment_data_online.json"),
            "MEX2", "ABC",
        )
        assert result is not None
        assert result.timestamp_utc.tzinfo == UTC
        # MX TZ converts 12:30 local → 18:30 UTC
        assert result.timestamp_utc.hour == 18
        assert result.timestamp_utc.minute == 30


class TestInverterModeToStatus:
    @pytest.mark.parametrize("mode,expected", [
        ("MPPT", 1),
        ("THROTTLED", 1),
        ("IDLE", 1),
        ("OFF", 3),
        ("FAULT", 3),
        ("STANDBY", 3),
        ("SHUTTING_DOWN", 3),
        ("NIGHT", 3),
        ("SLEEPING", 3),
        ("mppt", 1),  # case insensitive
        ("fault", 3),
        ("UNKNOWN_MODE", 1),  # default to online
        (None, 1),
        ("", 1),
    ])
    def test_modes(self, mode, expected):
        assert SolarEdgeClient._inverter_mode_to_status(mode) == expected


class TestSiteLocalParsing:
    def test_iso_with_T_separator(self, client):
        # Some endpoints use "T" instead of space
        result = client._parse_site_local_to_utc("2026-04-15T12:30:00")
        assert result.tzinfo == UTC
        assert result.hour == 18  # MX 12:30 → UTC 18:30

    def test_slash_format(self, client):
        result = client._parse_site_local_to_utc("2026/04/15 12:30:00")
        assert result.tzinfo == UTC
        assert result.hour == 18

    def test_empty_string_returns_now(self, client):
        result = client._parse_site_local_to_utc("")
        assert result.tzinfo == UTC

    def test_none_returns_now(self, client):
        result = client._parse_site_local_to_utc(None)
        assert result.tzinfo == UTC
