"""Tests for argia.telemetry.huawei_row.

Drives the row builders with the real Huawei ``getDevRealKpi`` fixture
(three inverters: two online, one offline).
"""

from __future__ import annotations

import datetime as dt
from typing import List
from unittest.mock import patch

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.telemetry.growatt_row import EMPTY_WEATHER, WeatherSnapshot
from argia.telemetry.huawei_row import build_common_row, build_plant_row
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    PLANT_SCHEMA,
    TYPED_INVERTER_COLS,
    VENDOR_HUAWEI,
)
from argia.vendors.base import InverterSnapshot
from argia.vendors.huawei import HuaweiClient
from tests.conftest import load_fixture


@pytest.fixture
def plant() -> PlantConfig:
    return PlantConfig(
        plant_key="MEX1",
        customer="VITALMEX",
        brand="HUAWEI",
        site_id="NE=12345",
        kwp_dc=500.0,
        kwp_ac=400.0,
        lat=19.4326,
        lon=-99.1332,
        expected_factor=0.8,
        pr_target=0.85,
        installation_date="2024-01-01",
        secret_api_name="",
        secret_user_name="HUAWEI_USERNAME",
        secret_pass_name="HUAWEI_PASSWORD",
        weather_plant_id="9275498",
        datalogger_sn="DYD0E8501G",
        datalogger_addr=1,
        active=True,
    )


@pytest.fixture
def inverters() -> List[InverterConfig]:
    return [
        InverterConfig("MEX1", "ES2470051825", "Inverter 1", 100.0, True),
        InverterConfig("MEX1", "GR2499018270", "Inverter 2", 100.0, True),
        InverterConfig("MEX1", "GR2499018271", "Inverter 3", 100.0, True),
    ]


@pytest.fixture
def snapshots(plant, inverters) -> List[InverterSnapshot]:
    """Parse the real fixture via HuaweiClient to get InverterSnapshots."""
    client = HuaweiClient(username="u", password="p")
    client._logged_in = True
    with patch.object(
        client, "_post_json",
        return_value=load_fixture("huawei", "getDevRealKpi_three_inverters.json"),
    ):
        snaps = client.fetch_inverter_snapshots(plant, inverters)
    return snaps


@pytest.fixture
def online_snap(snapshots) -> InverterSnapshot:
    """The first online inverter (ES2470051825)."""
    for s in snapshots:
        if s.inverter_sn == "ES2470051825":
            return s
    pytest.fail("ES2470051825 not in snapshots")


@pytest.fixture
def offline_snap(snapshots) -> InverterSnapshot:
    """The offline inverter (GR2499018271, devStatus=3)."""
    for s in snapshots:
        if s.inverter_sn == "GR2499018271":
            return s
    pytest.fail("GR2499018271 not in snapshots")


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=825.5,
        irradiance_kwh_m2_5m=0.068792,
        cloud_cover_pct=18.5,
        ambient_temp_c=29.0,
    )


# ============================================================
# build_plant_row — wide row, mostly empty
# ============================================================


class TestBuildPlantRowShape:
    def test_length_matches_plant_schema(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count
        assert len(result) == 142

    def test_no_none_values(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert all(c is not None for c in result)


class TestBuildPlantRowPopulatedFields:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_inverter_sn_at_col_2(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[2] == "ES2470051825"

    def test_inverter_label_at_col_3(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[3] == "Inverter 1"

    def test_timestamp_utc_iso(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        parsed = dt.datetime.fromisoformat(result[0])
        assert parsed.tzinfo is not None

    def test_status_online(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[self._col_index("status")] == 1

    def test_status_offline(self, offline_snap):
        result = build_plant_row(offline_snap, "Inverter 3")
        assert result[self._col_index("status")] == 3

    def test_power_w_real_number(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        # Fixture: active_power=45.5 (kW) → 45500 W
        assert result[self._col_index("power_w")] == 45500

    def test_etoday_kwh_populated(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        # Fixture: day_cap=120.5
        assert result[self._col_index("etoday_kwh")] == 120.5

    def test_pac_w_is_float(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        pac = result[self._col_index("pac_w")]
        assert pac == 45500.0


class TestBuildPlantRowBlankFields:
    """Huawei doesn't expose most fields — they should be blank ("")."""

    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_pf_blank(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[self._col_index("pf")] == ""

    def test_fac_hz_blank(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[self._col_index("fac_hz")] == ""

    def test_temperature_c_blank(self, online_snap):
        # Stage 4 doesn't extract temperature from Huawei yet
        result = build_plant_row(online_snap, "Inverter 1")
        assert result[self._col_index("temperature_c")] == ""

    def test_per_mppt_voltages_blank(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        for i in range(1, 17):
            assert result[self._col_index(f"vpv{i}_v")] == ""

    def test_per_string_voltages_blank(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        for i in range(1, 33):
            assert result[self._col_index(f"vstring{i}_v")] == ""

    def test_fault_codes_blank(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1")
        # Huawei doesn't have Growatt-style faultCode1/2 — leave blank
        assert result[self._col_index("fault_code_1")] == ""
        assert result[self._col_index("fault_code_2")] == ""


class TestBuildPlantRowWeather:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_weather_populated_when_provided(self, online_snap, weather):
        result = build_plant_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("irradiance_wm2")] == 825.5
        assert result[self._col_index("cloud_cover_pct")] == 18.5

    def test_weather_blank_with_empty_weather(self, online_snap):
        result = build_plant_row(online_snap, "Inverter 1", EMPTY_WEATHER)
        assert result[self._col_index("irradiance_wm2")] == ""
        assert result[self._col_index("cloud_cover_pct")] == ""


# ============================================================
# build_common_row — narrow cross-vendor row
# ============================================================


class TestBuildCommonRow:
    def _col_index(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length_matches_argia_schema(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count
        assert len(result) == 15

    def test_vendor_column_is_huawei(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("vendor")] == VENDOR_HUAWEI
        assert result[self._col_index("vendor")] == "HUAWEI"

    def test_plant_key_from_snapshot(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("plant_key")] == "MEX1"

    def test_inverter_sn_normalized(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("inverter_sn")] == "ES2470051825"

    def test_status_online(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("status")] == 1

    def test_status_offline(self, offline_snap, weather):
        result = build_common_row(offline_snap, "Inverter 3", weather)
        assert result[self._col_index("status")] == 3

    def test_power_and_etoday(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("power_w")] == 45500
        assert result[self._col_index("etoday_kwh")] == 120.5

    def test_temperature_blank(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("temperature_c")] == ""

    def test_fault_code_uses_raw_status(self, online_snap, weather):
        # online_snap has devStatus="1" → raw_status="1"
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[self._col_index("fault_code")] == "1"

    def test_fault_code_offline(self, offline_snap, weather):
        result = build_common_row(offline_snap, "Inverter 3", weather)
        assert result[self._col_index("fault_code")] == "3"

    def test_weather_at_end(self, online_snap, weather):
        result = build_common_row(online_snap, "Inverter 1", weather)
        assert result[-4:] == [825.5, 0.068792, 18.5, 29.0]


# ============================================================
# Cross-vendor invariants (sanity check)
# ============================================================


class TestCrossVendorInvariants:
    """A Huawei common row and a Growatt common row should have the same
    SHAPE, so they can coexist in Telemetry_Argia."""

    def test_huawei_and_growatt_common_rows_same_width(self, online_snap, weather):
        from argia.telemetry.growatt_row import build_common_row as build_growatt
        from copy import deepcopy
        from argia.vendors.growatt_web_parser import parse_max_history_row

        # Build a Growatt common row from a minimal raw dict
        # (the row builder reads from row.raw so any complete-enough dict works)
        raw = {
            "calendar": {
                "year": 2026, "month": 4, "dayOfMonth": 13,
                "hourOfDay": 15, "minute": 35, "second": 0,
            },
            "pac": 116777.1,
            "eacToday": 726.4,
            "pf": 1.0,
            "fac": 60.0,
            "temperature": 48.3,
            "faultCode1": 0, "faultCode2": 0, "faultType": 0,
        }
        gr_row = parse_max_history_row(raw)
        gr_common = build_growatt(gr_row, "GTO1", "JFM7DXN00T", "Inv 1", weather)

        # Build a Huawei common row
        hw_common = build_common_row(online_snap, "Inverter 1", weather)

        # Same length, same column meanings (defined by ARGIA_SCHEMA)
        assert len(gr_common) == len(hw_common) == ARGIA_SCHEMA.column_count

    def test_vendor_columns_differ(self, online_snap, weather):
        """Just confirming the vendor column actually carries the vendor."""
        hw_common = build_common_row(online_snap, "Inverter 1", weather)
        vendor_idx = ARGIA_SCHEMA.columns.index("vendor")
        assert hw_common[vendor_idx] == "HUAWEI"
