"""Tests for argia.vendors.huawei_telemetry parser.

Synthetic dataItemMap fixtures simulating various Huawei response shapes:
- Full rich response (all documented fields)
- Sparse response (only the basics, like the old fixture)
- Variant field naming (camelCase vs snake_case)
- Missing per-MPPT fields beyond a small index
- Missing SN → returns None
- success=false → empty list
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.vendors.huawei_telemetry import (
    HuaweiTelemetryRow,
    fetch_inverter_telemetry,
    parse_telemetry_item,
    parse_telemetry_response,
)


def _rich_item(sn: str = "ES2470051825") -> dict:
    """One item from getDevRealKpi with the full documented dataItemMap."""
    return {
        "sn": sn,
        "devStatus": "1",
        "collectTime": 1747162693000,  # 2026-05-13 22:18:13 UTC
        "dataItemMap": {
            "active_power": 99.166,         # kW
            "reactive_power": 1.5,
            "power_factor": 0.997,
            "efficiency": 98.5,
            "elec_freq": 60.02,
            "ab_u": 480.5,
            "bc_u": 481.1,
            "ca_u": 480.8,
            "a_i": 119.2,
            "b_i": 120.4,
            "c_i": 119.8,
            "day_cap": 1022.61,
            "total_cap": 125_000.0,
            "mppt_total_cap": 125_500.0,
            "temperature": 48.5,
            "mppt_power": 100.2,
            "inverter_state": 512,
            "run_state": 1,
            # Per-MPPT
            "pv1_u": 720.1, "pv1_i": 13.8,
            "pv2_u": 718.4, "pv2_i": 14.1,
            "pv3_u": 721.0, "pv3_i": 13.9,
            "pv4_u": 719.2, "pv4_i": 14.0,
            "mppt_1_cap": 125.4,
            "mppt_2_cap": 127.1,
            "mppt_3_cap": 125.8,
            "mppt_4_cap": 126.3,
        },
    }


# ============================================================
# parse_telemetry_item — happy path
# ============================================================


class TestRichParse:
    def test_returns_huawei_telemetry_row(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert isinstance(row, HuaweiTelemetryRow)

    def test_plant_key_propagated(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.plant_key == "MEX1"

    def test_sn(self):
        row = parse_telemetry_item(_rich_item("ABC123"), "MEX1")
        assert row.inverter_sn == "ABC123"

    def test_status_online(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.status == 1
        assert row.raw_status == "1"

    def test_power_converted_to_watts(self):
        # active_power=99.166 kW → 99166 W
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.power_w == 99166.0

    def test_etoday_kwh(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.etoday_kwh == 1022.61

    def test_etotal_kwh(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.etotal_kwh == 125_000.0

    def test_temperature_c(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.temperature_c == 48.5

    def test_power_factor(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.power_factor == 0.997

    def test_efficiency(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.efficiency_pct == 98.5

    def test_elec_freq(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.elec_freq_hz == 60.02

    def test_three_phase_voltages(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.ab_u_v == 480.5
        assert row.bc_u_v == 481.1
        assert row.ca_u_v == 480.8

    def test_three_phase_currents(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.a_i_a == 119.2
        assert row.b_i_a == 120.4
        assert row.c_i_a == 119.8

    def test_mppt_power_converted_to_watts(self):
        # mppt_power=100.2 kW → 100200 W
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.mppt_power_w == 100_200.0

    def test_state_fields(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.inverter_state == 512
        assert row.run_state == 1

    def test_per_mppt_voltages_tuple_length(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        # Always 16 entries (None for unread MPPTs)
        assert len(row.pv_voltages_v) == 16

    def test_per_mppt_voltages_values(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.pv_voltages_v[0] == 720.1
        assert row.pv_voltages_v[1] == 718.4
        assert row.pv_voltages_v[2] == 721.0
        assert row.pv_voltages_v[3] == 719.2

    def test_per_mppt_voltages_blank_beyond_fixture(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        # pv5 onwards not in fixture → None
        for i in range(4, 16):
            assert row.pv_voltages_v[i] is None

    def test_per_mppt_eday(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.pv_eday_kwh[0] == 125.4
        assert row.pv_eday_kwh[1] == 127.1

    def test_timestamp_parsed(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert isinstance(row.timestamp_utc, dt.datetime)
        assert row.timestamp_utc.tzinfo is not None

    def test_raw_data_item_map_preserved(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert "active_power" in row.raw_data_item_map
        assert "temperature" in row.raw_data_item_map


# ============================================================
# parse_telemetry_item — sparse / variant
# ============================================================


class TestSparseParse:
    def test_only_basics(self):
        item = {
            "sn": "ES1",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {
                "active_power": 50.0,
                "day_cap": 500.0,
            },
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row is not None
        assert row.power_w == 50_000.0
        assert row.etoday_kwh == 500.0
        assert row.temperature_c is None
        assert row.power_factor is None
        assert row.pv_voltages_v == (None,) * 16

    def test_offline_status(self):
        item = {
            "sn": "GR1",
            "devStatus": "3",
            "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 0.0, "day_cap": 100.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.status == 3
        assert row.raw_status == "3"


class TestFieldNameVariants:
    """Huawei docs note some inverter models use camelCase vs snake_case."""

    def test_camelcase_active_power(self):
        item = {
            "sn": "X",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {"activePower": 50.0, "day_cap": 500.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.power_w == 50_000.0

    def test_camelcase_temperature(self):
        item = {
            "sn": "X",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 50.0, "temperature_c": 45.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.temperature_c == 45.0


class TestPowerConversion:
    def test_small_value_treated_as_kw(self):
        item = {
            "sn": "X", "devStatus": "1", "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 100.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.power_w == 100_000.0

    def test_large_value_treated_as_watts(self):
        item = {
            "sn": "X", "devStatus": "1", "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 99166.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.power_w == 99166.0

    def test_zero_power(self):
        item = {
            "sn": "X", "devStatus": "3", "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 0.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.power_w == 0.0


# ============================================================
# parse_telemetry_item — error cases
# ============================================================


class TestParseErrors:
    def test_returns_none_when_no_sn(self):
        item = {"devStatus": "1", "dataItemMap": {"active_power": 50.0}}
        assert parse_telemetry_item(item, "MEX1") is None

    def test_returns_none_for_non_dict(self):
        assert parse_telemetry_item("not a dict", "MEX1") is None
        assert parse_telemetry_item(None, "MEX1") is None
        assert parse_telemetry_item([], "MEX1") is None

    def test_handles_missing_data_item_map(self):
        item = {"sn": "X", "devStatus": "1", "collectTime": 1747162693000}
        row = parse_telemetry_item(item, "MEX1")
        assert row is not None
        assert row.power_w is None
        assert row.temperature_c is None

    def test_handles_non_dict_data_item_map(self):
        item = {
            "sn": "X", "devStatus": "1", "collectTime": 1747162693000,
            "dataItemMap": "not a dict",
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row is not None
        assert row.power_w is None


# ============================================================
# parse_telemetry_response
# ============================================================


class TestParseResponse:
    def test_parses_multiple_items(self):
        response = {
            "success": True,
            "data": [_rich_item("A"), _rich_item("B"), _rich_item("C")],
        }
        rows = parse_telemetry_response(response, "MEX1")
        assert len(rows) == 3
        assert {r.inverter_sn for r in rows} == {"A", "B", "C"}

    def test_skips_invalid_items(self):
        response = {
            "success": True,
            "data": [_rich_item("A"), {"no_sn_here": True}, _rich_item("C")],
        }
        rows = parse_telemetry_response(response, "MEX1")
        assert len(rows) == 2

    def test_success_false_returns_empty(self):
        response = {
            "success": False,
            "failCode": 305,
            "message": "auth invalid",
            "data": [_rich_item("A")],
        }
        rows = parse_telemetry_response(response, "MEX1")
        assert rows == []

    def test_missing_data_returns_empty(self):
        response = {"success": True}
        rows = parse_telemetry_response(response, "MEX1")
        assert rows == []

    def test_non_dict_returns_empty(self):
        assert parse_telemetry_response("nope", "MEX1") == []
        assert parse_telemetry_response(None, "MEX1") == []


# ============================================================
# fetch_inverter_telemetry — integration with mocked client
# ============================================================


class _FakeInverter:
    def __init__(self, sn):
        self.inverter_sn = sn


class _FakePlant:
    def __init__(self, key="MEX1"):
        self.plant_key = key


class TestFetchInverterTelemetry:
    def test_calls_post_json_with_correct_args(self):
        client = MagicMock()
        client._post_json.return_value = {"success": True, "data": []}

        plant = _FakePlant("MEX1")
        inverters = [_FakeInverter("A"), _FakeInverter("B")]

        fetch_inverter_telemetry(client, plant, inverters)

        client._ensure_logged_in.assert_called_once()
        client._post_json.assert_called_once()
        args, _ = client._post_json.call_args
        assert args[0] == "/getDevRealKpi"
        body = args[1]
        assert body["devTypeId"] == "1"
        assert body["sns"] == "A,B"

    def test_returns_parsed_rows(self):
        client = MagicMock()
        client._post_json.return_value = {
            "success": True,
            "data": [_rich_item("A"), _rich_item("B")],
        }

        rows = fetch_inverter_telemetry(
            client, _FakePlant("MEX1"),
            [_FakeInverter("A"), _FakeInverter("B")],
        )
        assert len(rows) == 2
        assert all(isinstance(r, HuaweiTelemetryRow) for r in rows)

    def test_empty_inverter_list_skips_call(self):
        client = MagicMock()
        rows = fetch_inverter_telemetry(client, _FakePlant(), [])
        assert rows == []
        client._post_json.assert_not_called()

    def test_failure_raises(self):
        from argia.vendors.huawei import HuaweiAPIError
        client = MagicMock()
        client._post_json.return_value = {
            "success": False, "failCode": 1, "message": "boom",
        }
        with pytest.raises(HuaweiAPIError):
            fetch_inverter_telemetry(
                client, _FakePlant(), [_FakeInverter("A")],
            )

    def test_batches_when_over_50_inverters(self):
        client = MagicMock()
        client._post_json.return_value = {"success": True, "data": []}

        inverters = [_FakeInverter(f"SN{i}") for i in range(75)]
        fetch_inverter_telemetry(client, _FakePlant(), inverters)

        # 75 / 50 = 2 batches
        assert client._post_json.call_count == 2
