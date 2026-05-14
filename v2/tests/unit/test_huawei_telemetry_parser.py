"""Tests for argia.vendors.huawei_telemetry parser (Stage 4.2 version).

Stage 4.2 changes:
- ``mppt_X_cap`` values are now converted Wh → kWh on parse, stored in
  ``pv_etotal_kwh`` (renamed from ``pv_eday_kwh``).
- Added line-to-neutral voltages ``a_u``, ``b_u``, ``c_u``.
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
    """A getDevRealKpi item matching the SHAPE of real Huawei production data.

    Key updates from Stage 4.1 test fixture:
    - ``mppt_X_cap`` values are realistic Wh (50000+, not kWh)
    - Includes ``a_u``, ``b_u``, ``c_u`` (line-to-neutral)
    """
    return {
        "sn": sn,
        "devStatus": "1",
        "collectTime": 1747162693000,
        "dataItemMap": {
            "active_power": 99.166,
            "reactive_power": 1.5,
            "power_factor": 0.997,
            "efficiency": 98.5,
            "elec_freq": 60.02,
            # Line-to-line
            "ab_u": 480.5,
            "bc_u": 481.1,
            "ca_u": 480.8,
            # Line-to-neutral (NEW in fixture for 4.2)
            "a_u": 277.4,
            "b_u": 277.8,
            "c_u": 277.5,
            # Per-phase currents
            "a_i": 119.2,
            "b_i": 120.4,
            "c_i": 119.8,
            # Energy
            "day_cap": 1022.61,
            "total_cap": 125_000.0,
            "mppt_total_cap": 125_500.0,
            "temperature": 48.5,
            "mppt_power": 100.2,
            "inverter_state": 512,
            "run_state": 1,
            # Per-MPPT voltages and currents
            "pv1_u": 720.1, "pv1_i": 13.8,
            "pv2_u": 718.4, "pv2_i": 14.1,
            "pv3_u": 721.0, "pv3_i": 13.9,
            "pv4_u": 719.2, "pv4_i": 14.0,
            # Per-MPPT lifetime energy (in Wh, large values)
            "mppt_1_cap": 56_822.68,
            "mppt_2_cap": 56_290.80,
            "mppt_3_cap": 57_142.53,
            "mppt_4_cap": 55_852.81,
        },
    }


# ============================================================
# Stage 4.2-specific tests
# ============================================================


class TestPhaseVoltages:
    """NEW in Stage 4.2: a_u, b_u, c_u line-to-neutral voltages."""

    def test_a_u_parsed(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.a_u_v == 277.4

    def test_b_u_parsed(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.b_u_v == 277.8

    def test_c_u_parsed(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.c_u_v == 277.5

    def test_missing_phase_voltages_become_none(self):
        item = {
            "sn": "X",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 50.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.a_u_v is None
        assert row.b_u_v is None
        assert row.c_u_v is None

    def test_line_to_line_still_parsed(self):
        """Line-to-line voltages must keep working alongside the new phase ones."""
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.ab_u_v == 480.5
        assert row.bc_u_v == 481.1
        assert row.ca_u_v == 480.8

    def test_phase_voltage_variants(self):
        """Defensive: try camelCase variants for a_u/b_u/c_u too."""
        item = {
            "sn": "X",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {
                "active_power": 50.0,
                "aU": 277.0, "bU": 278.0, "cU": 279.0,
            },
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.a_u_v == 277.0
        assert row.b_u_v == 278.0
        assert row.c_u_v == 279.0


class TestPerMpptLifetimeEnergy:
    """STAGE 4.2 FIX: mppt_X_cap is lifetime energy in Wh.

    The parser must:
    - Store values in pv_etotal_kwh (NOT pv_eday_kwh)
    - Convert Wh → kWh by dividing by 1000
    """

    def test_pv_etotal_kwh_attribute_exists(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        # The renamed attribute must exist
        assert hasattr(row, "pv_etotal_kwh")

    def test_pv_eday_kwh_no_longer_exists(self):
        """Verify the old attribute name is gone (frozen dataclass = AttributeError)."""
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert not hasattr(row, "pv_eday_kwh")

    def test_wh_converted_to_kwh(self):
        """mppt_1_cap=56822.68 Wh → 56.82268 kWh."""
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.pv_etotal_kwh[0] == pytest.approx(56.82268)

    def test_multiple_mppts_converted(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.pv_etotal_kwh[0] == pytest.approx(56.82268)
        assert row.pv_etotal_kwh[1] == pytest.approx(56.29080)
        assert row.pv_etotal_kwh[2] == pytest.approx(57.14253)
        assert row.pv_etotal_kwh[3] == pytest.approx(55.85281)

    def test_missing_mppts_stay_none(self):
        """pv5 onwards not in fixture → None (not zero, not 0/1000)."""
        row = parse_telemetry_item(_rich_item(), "MEX1")
        for i in range(4, 16):
            assert row.pv_etotal_kwh[i] is None

    def test_tuple_length_is_16(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert len(row.pv_etotal_kwh) == 16

    def test_sparse_response_no_mppt_caps(self):
        item = {
            "sn": "X",
            "devStatus": "1",
            "collectTime": 1747162693000,
            "dataItemMap": {"active_power": 50.0, "day_cap": 100.0},
        }
        row = parse_telemetry_item(item, "MEX1")
        assert row.pv_etotal_kwh == (None,) * 16


# ============================================================
# Stage 4.1 tests — must keep passing (regression safety net)
# ============================================================


class TestStage41RegressionRichParse:
    """Verify Stage 4.1's functionality is preserved."""

    def test_returns_huawei_telemetry_row(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert isinstance(row, HuaweiTelemetryRow)

    def test_plant_key(self):
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

    def test_three_phase_currents(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.a_i_a == 119.2
        assert row.b_i_a == 120.4
        assert row.c_i_a == 119.8

    def test_mppt_power_converted_to_watts(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.mppt_power_w == 100_200.0

    def test_state_fields(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.inverter_state == 512
        assert row.run_state == 1

    def test_per_mppt_voltages_values(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert row.pv_voltages_v[0] == 720.1
        assert row.pv_voltages_v[1] == 718.4

    def test_timestamp_parsed(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert isinstance(row.timestamp_utc, dt.datetime)
        assert row.timestamp_utc.tzinfo is not None

    def test_raw_data_item_map_preserved(self):
        row = parse_telemetry_item(_rich_item(), "MEX1")
        assert "active_power" in row.raw_data_item_map


class TestStage41RegressionErrors:
    def test_returns_none_when_no_sn(self):
        item = {"devStatus": "1", "dataItemMap": {"active_power": 50.0}}
        assert parse_telemetry_item(item, "MEX1") is None

    def test_returns_none_for_non_dict(self):
        assert parse_telemetry_item("not a dict", "MEX1") is None

    def test_handles_missing_data_item_map(self):
        item = {"sn": "X", "devStatus": "1", "collectTime": 1747162693000}
        row = parse_telemetry_item(item, "MEX1")
        assert row is not None
        assert row.power_w is None


class TestStage41RegressionResponseParser:
    def test_parses_multiple_items(self):
        response = {
            "success": True,
            "data": [_rich_item("A"), _rich_item("B"), _rich_item("C")],
        }
        rows = parse_telemetry_response(response, "MEX1")
        assert len(rows) == 3

    def test_success_false_returns_empty(self):
        response = {
            "success": False,
            "data": [_rich_item("A")],
        }
        rows = parse_telemetry_response(response, "MEX1")
        assert rows == []


class _FakeInverter:
    def __init__(self, sn):
        self.inverter_sn = sn


class _FakePlant:
    def __init__(self, key="MEX1"):
        self.plant_key = key


class TestStage41RegressionFetch:
    def test_calls_post_json_with_correct_args(self):
        client = MagicMock()
        client._post_json.return_value = {"success": True, "data": []}

        plant = _FakePlant("MEX1")
        inverters = [_FakeInverter("A"), _FakeInverter("B")]
        fetch_inverter_telemetry(client, plant, inverters)

        client._ensure_logged_in.assert_called_once()
        args, _ = client._post_json.call_args
        assert args[0] == "/getDevRealKpi"
        assert args[1]["sns"] == "A,B"

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
