"""Tests for argia.vendors.sma_telemetry parser.

Without real sandbox captures we test the parser's defensive behavior:
multiple key variants, missing fields, kW-vs-W heuristic, status mapping.

Stage 6.1 will add real-shape fixtures once we capture from the sandbox.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.vendors.sma import SMAAPIError, SMAAuthError, SMAConsentError
from argia.vendors.sma_telemetry import (
    SMATelemetryRow,
    fetch_inverter_telemetry,
    parse_telemetry_response,
)


def _pv_response(**fields):
    """Build a /devices/.../pvGeneration-shaped response."""
    return {
        "device": {"deviceId": "DEV1", "name": "Inverter 1"},
        "setType": "pvGeneration",
        "set": fields,
        "status": "Ok",
    }


# ============================================================
# Happy path parsing
# ============================================================


class TestRichParse:
    def test_returns_telemetry_row(self):
        response = _pv_response(
            time="2026-05-14T12:00:00Z",
            power=25.0,  # kW → 25000 W after heuristic
            energyDay=120.5,
            energyTotal=4500.0,
            temperature=42.5,
            dcVoltage=400.0,
            acFrequency=60.0,
            powerFactor=0.99,
            status="Ok",
        )
        row = parse_telemetry_response(response, "SMA_SANDBOX", "DEV1")
        assert isinstance(row, SMATelemetryRow)
        assert row.plant_key == "SMA_SANDBOX"
        assert row.inverter_sn == "DEV1"
        assert row.power_w == 25000.0
        assert row.etoday_kwh == 120.5
        assert row.etotal_kwh == 4500.0
        assert row.temperature_c == 42.5
        assert row.dc_voltage_v == 400.0
        assert row.fac_hz == 60.0
        assert row.power_factor == 0.99
        assert row.status == 1


class TestKwHeuristic:
    def test_small_power_converted_to_watts(self):
        response = _pv_response(power=25.4)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.power_w == 25400.0  # 25.4 kW = 25400 W

    def test_large_power_passes_through(self):
        response = _pv_response(power=80000.0)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.power_w == 80000.0

    def test_dc_power_same_heuristic(self):
        response = _pv_response(dcPower=12.5)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.dc_power_w == 12500.0


class TestEnergyWhHeuristic:
    def test_normal_kwh_passes_through(self):
        response = _pv_response(energyDay=125.5)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.etoday_kwh == 125.5

    def test_huge_value_converted_from_wh(self):
        # 5_500_000 looks like Wh, convert to 5500 kWh
        response = _pv_response(energyDay=5_500_000)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.etoday_kwh == 5500.0


# ============================================================
# Field name variants (defensive)
# ============================================================


class TestKeyVariants:
    @pytest.mark.parametrize("key", ["power", "pac", "activePower", "totalActivePower"])
    def test_power_keys(self, key):
        response = _pv_response(**{key: 25000.0})
        row = parse_telemetry_response(response, "X", "Y")
        assert row.power_w == 25000.0

    @pytest.mark.parametrize("key", ["energyDay", "yieldDay", "totalEnergyDay", "eToday"])
    def test_etoday_keys(self, key):
        response = _pv_response(**{key: 100.0})
        row = parse_telemetry_response(response, "X", "Y")
        assert row.etoday_kwh == 100.0

    @pytest.mark.parametrize("key", ["temperature", "inverterTemperature", "internalTemperature"])
    def test_temperature_keys(self, key):
        response = _pv_response(**{key: 38.0})
        row = parse_telemetry_response(response, "X", "Y")
        assert row.temperature_c == 38.0


# ============================================================
# Status mapping
# ============================================================


class TestStatus:
    def test_default_online(self):
        response = _pv_response(power=10000.0)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.status == 1

    @pytest.mark.parametrize("state", ["OFF", "ERROR", "FAULT", "OFFLINE", "STANDBY"])
    def test_offline_states(self, state):
        response = _pv_response(status=state)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.status == 3
        assert row.raw_status == state

    def test_status_from_device_block(self):
        response = {
            "device": {"status": "ERROR"},
            "set": {"power": 0},
            "setType": "pvGeneration",
        }
        row = parse_telemetry_response(response, "X", "Y")
        assert row.status == 3


# ============================================================
# Timestamp parsing
# ============================================================


class TestTimestampParse:
    def test_iso_z_format(self):
        response = _pv_response(time="2026-05-14T12:00:00Z")
        row = parse_telemetry_response(response, "X", "Y")
        assert row.timestamp_utc.tzinfo is not None
        assert row.timestamp_utc.hour == 12  # already UTC

    def test_iso_with_offset(self):
        response = _pv_response(time="2026-05-14T12:00:00-06:00")
        row = parse_telemetry_response(response, "X", "Y")
        # 12:00 -06:00 = 18:00 UTC
        assert row.timestamp_utc.hour == 18

    def test_missing_timestamp_uses_now(self):
        response = _pv_response(power=10000.0)
        row = parse_telemetry_response(response, "X", "Y")
        assert row.timestamp_utc is not None


# ============================================================
# Edge cases
# ============================================================


class TestEdgeCases:
    def test_missing_set_returns_none(self):
        assert parse_telemetry_response({}, "X", "Y") is None
        assert parse_telemetry_response({"device": {}}, "X", "Y") is None

    def test_non_dict_returns_none(self):
        assert parse_telemetry_response(None, "X", "Y") is None
        assert parse_telemetry_response("string", "X", "Y") is None

    def test_set_is_not_dict(self):
        response = {"set": "garbage"}
        assert parse_telemetry_response(response, "X", "Y") is None

    def test_completely_empty_set(self):
        response = _pv_response()  # no fields at all
        row = parse_telemetry_response(response, "X", "Y")
        assert row is not None
        assert row.power_w is None
        assert row.etoday_kwh is None
        assert row.temperature_c is None
        assert row.status == 1  # default online

    def test_sn_normalized(self):
        response = _pv_response(power=10.0)
        row = parse_telemetry_response(response, "X", "  abc-123  ")
        assert row.inverter_sn == "ABC-123"

    def test_raw_set_preserved(self):
        response = _pv_response(power=10.0, customField="something")
        row = parse_telemetry_response(response, "X", "Y")
        assert "customField" in row.raw_set


# ============================================================
# fetch_inverter_telemetry — integration with mocked client
# ============================================================


class _FakeInverter:
    def __init__(self, sn):
        self.inverter_sn = sn


class _FakePlant:
    def __init__(self, key="SMA_SANDBOX", site_id="SITE_123"):
        self.plant_key = key
        self.site_id = site_id


class TestFetchInverterTelemetry:
    def test_empty_list_no_calls(self):
        client = MagicMock()
        result = fetch_inverter_telemetry(client, _FakePlant(), [])
        assert result == []
        client._get_json.assert_not_called()

    def test_one_inverter_one_call(self):
        client = MagicMock()
        client._get_json.return_value = _pv_response(power=10000.0)
        result = fetch_inverter_telemetry(
            client, _FakePlant(), [_FakeInverter("DEV1")],
        )
        assert len(result) == 1
        client._get_json.assert_called_once()

    def test_correct_endpoint(self):
        client = MagicMock()
        client._get_json.return_value = _pv_response(power=10000.0)
        fetch_inverter_telemetry(
            client, _FakePlant(), [_FakeInverter("DEV1")],
        )
        path = client._get_json.call_args.args[0]
        assert path == "/devices/DEV1/measurements/sets/pvGeneration"

    def test_multiple_inverters(self):
        client = MagicMock()
        client._get_json.return_value = _pv_response(power=10000.0)
        result = fetch_inverter_telemetry(
            client, _FakePlant(),
            [_FakeInverter(f"DEV{i}") for i in range(3)],
        )
        assert len(result) == 3

    def test_404_continues_to_next_inverter(self):
        client = MagicMock()
        client._get_json.side_effect = [
            SMAAPIError("404 not available"),
            _pv_response(power=10000.0),
        ]
        result = fetch_inverter_telemetry(
            client, _FakePlant(),
            [_FakeInverter("BAD"), _FakeInverter("GOOD")],
        )
        assert len(result) == 1
        assert result[0].inverter_sn == "GOOD"

    def test_auth_error_raises(self):
        client = MagicMock()
        client._get_json.side_effect = SMAAuthError("token rejected")
        with pytest.raises(SMAAuthError):
            fetch_inverter_telemetry(
                client, _FakePlant(), [_FakeInverter("DEV1")],
            )

    def test_consent_error_raises(self):
        client = MagicMock()
        client._get_json.side_effect = SMAConsentError("revoked")
        with pytest.raises(SMAConsentError):
            fetch_inverter_telemetry(
                client, _FakePlant(), [_FakeInverter("DEV1")],
            )

    def test_rate_limit_raises(self):
        client = MagicMock()
        client._get_json.side_effect = SMAAPIError("rate-limited HTTP 429")
        with pytest.raises(SMAAPIError, match="rate-limited"):
            fetch_inverter_telemetry(
                client, _FakePlant(), [_FakeInverter("DEV1")],
            )

    def test_empty_response_returns_no_row(self):
        client = MagicMock()
        client._get_json.return_value = {}  # no set
        result = fetch_inverter_telemetry(
            client, _FakePlant(), [_FakeInverter("DEV1")],
        )
        assert result == []
