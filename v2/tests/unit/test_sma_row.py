"""Tests for argia.telemetry.sma_row."""

from __future__ import annotations

import datetime as dt

import pytest

from argia.telemetry.growatt_row import EMPTY_WEATHER, WeatherSnapshot
from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA, VENDOR_SMA
from argia.telemetry.sma_row import build_common_row, build_plant_row
from argia.vendors.sma_telemetry import SMATelemetryRow


def _ts() -> dt.datetime:
    return dt.datetime(2026, 5, 14, 18, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def rich_row() -> SMATelemetryRow:
    return SMATelemetryRow(
        plant_key="SMA_SANDBOX",
        inverter_sn="DEV001",
        timestamp_utc=_ts(),
        status=1,
        raw_status="Ok",
        power_w=25400.0,
        reactive_power_var=-500.0,
        apparent_power_va=25410.0,
        power_factor=1.0,
        vac_v=230.0,
        iac_a=110.0,
        fac_hz=60.0,
        etoday_kwh=185.5,
        etotal_kwh=4500.0,
        dc_voltage_v=400.0,
        dc_current_a=63.0,
        dc_power_w=25500.0,
        temperature_c=42.5,
    )


@pytest.fixture
def offline_row() -> SMATelemetryRow:
    return SMATelemetryRow(
        plant_key="SMA_SANDBOX",
        inverter_sn="DEV002",
        timestamp_utc=_ts(),
        status=3,
        raw_status="FAULT",
        power_w=0.0,
        etoday_kwh=100.0,
    )


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=800.0,
        irradiance_kwh_m2_5m=0.067,
        cloud_cover_pct=10.0,
        ambient_temp_c=None,
    )


# ============================================================
# Wide row
# ============================================================


class TestPlantRowShape:
    def test_length_matches_schema(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count

    def test_no_none_values(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert all(c is not None for c in result)


class TestPlantRowPopulated:
    def _idx(self, name):
        return PLANT_SCHEMA.columns.index(name)

    def test_identity(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[2] == "DEV001"
        assert result[3] == "Inverter 1"

    def test_status_online(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("status")] == 1

    def test_status_offline(self, offline_row):
        result = build_plant_row(offline_row, "Inverter 2")
        assert result[self._idx("status")] == 3

    def test_power_w_int(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("power_w")] == 25400

    def test_pac_w_float(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("pac_w")] == 25400.0

    def test_iac_a(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("iac_a")] == 110.0

    def test_pf(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("pf")] == 1.0

    def test_fac_hz(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("fac_hz")] == 60.0

    def test_etoday(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("etoday_kwh")] == 185.5

    def test_etotal(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("epv_total_kwh")] == 4500.0

    def test_temperature(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("temperature_c")] == 42.5

    def test_vpv1_is_dc_voltage(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vpv1_v")] == 400.0

    def test_ppv_is_dc_power(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("ppv_w")] == 25500


class TestPlantRowBlankByDesign:
    def _idx(self, name):
        return PLANT_SCHEMA.columns.index(name)

    def test_per_phase_voltages_blank(self, rich_row):
        """SMA doesn't break per-phase. vacr/s/t leave blank, vac_rs holds the
        single phase voltage (or could be empty if SMA doesn't expose it)."""
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vacr_v")] == ""
        assert result[self._idx("vacs_v")] == ""
        assert result[self._idx("vact_v")] == ""

    def test_per_string_blank(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        for i in range(1, 33):
            assert result[self._idx(f"vstring{i}_v")] == ""

    def test_per_mppt_power_blank(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        for i in range(1, 10):
            assert result[self._idx(f"ppv{i}_w")] == ""

    def test_vpv2_to_vpv16_blank(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        for i in range(2, 17):
            assert result[self._idx(f"vpv{i}_v")] == ""

    def test_growatt_fault_codes_blank(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("fault_code_1")] == ""
        assert result[self._idx("fault_code_2")] == ""


class TestPlantRowMissingFields:
    """If SMA returns sparse data, missing fields should be blank, not None."""

    def _idx(self, name):
        return PLANT_SCHEMA.columns.index(name)

    def test_offline_row_blank_metrics(self, offline_row):
        result = build_plant_row(offline_row, "Inverter 2")
        # power_w from offline_row is 0.0, not None
        assert result[self._idx("power_w")] == 0
        # iac_a, pf, fac_hz, dc_voltage_v all None in offline_row → blank
        assert result[self._idx("iac_a")] == ""
        assert result[self._idx("pf")] == ""
        assert result[self._idx("fac_hz")] == ""
        assert result[self._idx("temperature_c")] == ""


# ============================================================
# Common (narrow) row
# ============================================================


class TestCommonRow:
    def _idx(self, name):
        return ARGIA_SCHEMA.columns.index(name)

    def test_length(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count

    def test_vendor_is_sma(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("vendor")] == VENDOR_SMA
        assert result[self._idx("vendor")] == "SMA"

    def test_plant_key(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("plant_key")] == "SMA_SANDBOX"

    def test_status_online(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("status")] == 1

    def test_status_offline(self, offline_row, weather):
        result = build_common_row(offline_row, "Inverter 2", weather)
        assert result[self._idx("status")] == 3

    def test_power_w(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("power_w")] == 25400

    def test_temperature(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("temperature_c")] == 42.5

    def test_fault_code_healthy(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("fault_code")] == "0"

    def test_fault_code_with_state(self, offline_row, weather):
        result = build_common_row(offline_row, "Inverter 2", weather)
        assert result[self._idx("fault_code")] == "STATE=FAULT"

    def test_weather_at_end(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[-5:] == [800.0, 0.067, 10.0, "", ""]


# ============================================================
# Cross-vendor invariants
# ============================================================


class TestCrossVendor:
    def test_same_argia_width(self, rich_row, weather):
        """SMA narrow row must match Growatt/Huawei/SolarEdge narrow row shape."""
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count

    def test_same_plant_width(self, rich_row):
        """SMA wide row must match Growatt/Huawei/SolarEdge wide row shape."""
        result = build_plant_row(rich_row, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count
        assert len(result) == 143

    def test_vendor_string_is_sma(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[ARGIA_SCHEMA.columns.index("vendor")] == "SMA"
