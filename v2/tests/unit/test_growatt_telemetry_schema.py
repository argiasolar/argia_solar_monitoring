"""Tests for argia.telemetry.schema."""

from __future__ import annotations

import pytest

from argia.telemetry.schema import (
    ARGIA_COMMON_COLS,
    ARGIA_SCHEMA,
    ARGIA_TAB_NAME,
    COLUMN_VERSION,
    MPPT_EDAY_COUNT,
    MPPT_POWER_COUNT,
    MPPT_VOLTAGE_COUNT,
    PLANT_SCHEMA,
    STRING_CURRENT_HIGH,
    STRING_CURRENT_LOW,
    STRING_VOLTAGE_COUNT,
    TYPED_INVERTER_COLS,
    VENDOR_GROWATT,
    VENDOR_HUAWEI,
    VENDOR_SMA,
    VENDOR_SOLAREDGE,
    WEATHER_COLS,
    plant_tab_name,
)


# ============================================================
# PLANT_SCHEMA — wide, vendor-shaped (unchanged from Stage 3)
# ============================================================


def _expected_plant_count() -> int:
    """Compute the expected plant-tab column count from the constants."""
    identity = 4
    typed = len(TYPED_INVERTER_COLS)
    mppt_v = MPPT_VOLTAGE_COUNT
    mppt_p = MPPT_POWER_COUNT
    str_v = STRING_VOLTAGE_COUNT
    str_i = STRING_CURRENT_HIGH - STRING_CURRENT_LOW + 1
    eday_today = MPPT_EDAY_COUNT
    eday_total = MPPT_EDAY_COUNT
    weather = len(WEATHER_COLS)
    return (
        identity + typed + mppt_v + mppt_p + str_v + str_i
        + eday_today + eday_total + weather
    )


class TestPlantSchema:
    def test_column_count_matches_formula(self):
        assert PLANT_SCHEMA.column_count == _expected_plant_count()

    def test_first_four_cols_are_identity(self):
        assert PLANT_SCHEMA.columns[:4] == (
            "timestamp_utc", "timestamp_mx", "inverter_sn", "inverter_label",
        )

    def test_no_duplicate_column_names(self):
        cols = list(PLANT_SCHEMA.columns)
        assert len(cols) == len(set(cols)), "duplicate column names in plant schema"

    def test_natural_key_picks_timestamp_and_sn(self):
        assert PLANT_SCHEMA.natural_key_columns == (0, 2)
        assert PLANT_SCHEMA.columns[0] == "timestamp_utc"
        assert PLANT_SCHEMA.columns[2] == "inverter_sn"

    def test_header_is_a_list(self):
        h = PLANT_SCHEMA.header
        assert isinstance(h, list)
        assert h == list(PLANT_SCHEMA.columns)

    def test_per_mppt_voltage_cols_present(self):
        for i in range(1, MPPT_VOLTAGE_COUNT + 1):
            assert f"vpv{i}_v" in PLANT_SCHEMA.columns

    def test_per_string_voltage_cols_present(self):
        for i in range(1, STRING_VOLTAGE_COUNT + 1):
            assert f"vstring{i}_v" in PLANT_SCHEMA.columns

    def test_per_string_current_cols_only_20_to_29(self):
        for i in range(STRING_CURRENT_LOW, STRING_CURRENT_HIGH + 1):
            assert f"istring{i}_a" in PLANT_SCHEMA.columns
        assert "istring19_a" not in PLANT_SCHEMA.columns
        assert "istring30_a" not in PLANT_SCHEMA.columns

    def test_weather_cols_at_end(self):
        n = len(WEATHER_COLS)
        assert PLANT_SCHEMA.columns[-n:] == WEATHER_COLS

    def test_total_count_is_142(self):
        # Regression: ensure the column count hasn't drifted unexpectedly
        assert PLANT_SCHEMA.column_count == 143


# ============================================================
# ARGIA_SCHEMA — narrow common (NEW Stage 4 shape)
# ============================================================


class TestArgiaSchema:
    def test_argia_schema_uses_common_cols(self):
        assert ARGIA_SCHEMA.columns == ARGIA_COMMON_COLS

    def test_column_count_is_15(self):
        assert ARGIA_SCHEMA.column_count == 16

    def test_first_six_cols_are_identity_plus_vendor(self):
        assert ARGIA_SCHEMA.columns[:6] == (
            "timestamp_utc",
            "timestamp_mx",
            "vendor",
            "plant_key",
            "inverter_sn",
            "inverter_label",
        )

    def test_no_duplicate_column_names(self):
        cols = list(ARGIA_SCHEMA.columns)
        assert len(cols) == len(set(cols))

    def test_natural_key_is_timestamp_plant_sn(self):
        # Columns 0, 3, 4: timestamp_utc, plant_key, inverter_sn
        # vendor is NOT in the key — within one moment, one inverter has one vendor
        assert ARGIA_SCHEMA.natural_key_columns == (0, 3, 4)
        assert ARGIA_SCHEMA.columns[0] == "timestamp_utc"
        assert ARGIA_SCHEMA.columns[3] == "plant_key"
        assert ARGIA_SCHEMA.columns[4] == "inverter_sn"

    def test_status_power_etoday_in_order(self):
        # Must match the column index assumptions in growatt_row.build_common_row
        # and huawei_row.build_common_row
        assert ARGIA_SCHEMA.columns[6] == "status"
        assert ARGIA_SCHEMA.columns[7] == "power_w"
        assert ARGIA_SCHEMA.columns[8] == "etoday_kwh"

    def test_fault_code_present(self):
        assert "fault_code" in ARGIA_SCHEMA.columns

    def test_temperature_c_present(self):
        assert "temperature_c" in ARGIA_SCHEMA.columns

    def test_weather_cols_at_end(self):
        # Last 4 columns are the 4 weather fields in canonical order
        assert ARGIA_SCHEMA.columns[-5:] == (
            "irradiance_wm2",
            "irradiance_kwh_m2_5m",
            "cloud_cover_pct",
            "ambient_temp_c",
            "module_temp_c",
        )


# ============================================================
# Tab naming + vendor constants
# ============================================================


class TestPlantTabName:
    def test_prepends_telemetry(self):
        assert plant_tab_name("GTO1") == "Telemetry_GTO1"

    def test_preserves_case(self):
        assert plant_tab_name("slp1") == "Telemetry_slp1"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            plant_tab_name("")


class TestArgiaTabName:
    def test_argia_tab_name_constant(self):
        assert ARGIA_TAB_NAME == "Telemetry_Argia"


class TestVendorConstants:
    def test_four_vendors_defined(self):
        assert VENDOR_GROWATT == "GROWATT"
        assert VENDOR_HUAWEI == "HUAWEI"
        assert VENDOR_SOLAREDGE == "SOLAREDGE"
        assert VENDOR_SMA == "SMA"

    def test_vendor_constants_are_unique(self):
        vendors = [VENDOR_GROWATT, VENDOR_HUAWEI, VENDOR_SOLAREDGE, VENDOR_SMA]
        assert len(vendors) == len(set(vendors))


class TestVersionConstant:
    def test_version_bumped_to_3(self):
        # Bumped to 3 when module_temp_c (Backplane Temp) was appended
        assert COLUMN_VERSION == 3
