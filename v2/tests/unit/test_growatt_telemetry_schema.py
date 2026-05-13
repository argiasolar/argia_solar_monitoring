"""Tests for argia.telemetry.schema."""

from __future__ import annotations

import pytest

from argia.telemetry.schema import (
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
    WEATHER_COLS,
    plant_tab_name,
)


# ----------------- column counts -----------------


def _expected_plant_count() -> int:
    """Compute the expected plant-tab column count from the constants.

    If this and PLANT_SCHEMA.column_count diverge, someone changed one without
    the other.
    """
    identity = 4  # timestamp_utc, timestamp_mx, inverter_sn, inverter_label
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
        # Columns 0 and 2: timestamp_utc + inverter_sn
        assert PLANT_SCHEMA.natural_key_columns == (0, 2)
        assert PLANT_SCHEMA.columns[0] == "timestamp_utc"
        assert PLANT_SCHEMA.columns[2] == "inverter_sn"

    def test_header_is_a_list(self):
        h = PLANT_SCHEMA.header
        assert isinstance(h, list)
        assert h == list(PLANT_SCHEMA.columns)

    def test_per_mppt_voltage_cols_are_sequential(self):
        # vpv1_v..vpv16_v in order
        for i in range(1, MPPT_VOLTAGE_COUNT + 1):
            assert f"vpv{i}_v" in PLANT_SCHEMA.columns

    def test_per_string_voltage_cols_are_sequential(self):
        for i in range(1, STRING_VOLTAGE_COUNT + 1):
            assert f"vstring{i}_v" in PLANT_SCHEMA.columns

    def test_per_string_current_cols_only_20_to_29(self):
        # 20..29 present
        for i in range(STRING_CURRENT_LOW, STRING_CURRENT_HIGH + 1):
            assert f"istring{i}_a" in PLANT_SCHEMA.columns
        # 19 not present
        assert "istring19_a" not in PLANT_SCHEMA.columns
        # 30 not present
        assert "istring30_a" not in PLANT_SCHEMA.columns

    def test_weather_cols_at_end(self):
        # Last 4 columns should be the weather group, in WEATHER_COLS order
        n = len(WEATHER_COLS)
        assert PLANT_SCHEMA.columns[-n:] == WEATHER_COLS


class TestArgiaSchema:
    def test_column_count_is_plant_plus_one(self):
        assert ARGIA_SCHEMA.column_count == PLANT_SCHEMA.column_count + 1

    def test_first_five_cols_include_plant_key(self):
        assert ARGIA_SCHEMA.columns[:5] == (
            "timestamp_utc",
            "timestamp_mx",
            "plant_key",
            "inverter_sn",
            "inverter_label",
        )

    def test_natural_key_includes_plant_key(self):
        # Columns 0, 2, 3: timestamp_utc, plant_key, inverter_sn
        assert ARGIA_SCHEMA.natural_key_columns == (0, 2, 3)
        assert ARGIA_SCHEMA.columns[0] == "timestamp_utc"
        assert ARGIA_SCHEMA.columns[2] == "plant_key"
        assert ARGIA_SCHEMA.columns[3] == "inverter_sn"

    def test_no_duplicate_column_names(self):
        cols = list(ARGIA_SCHEMA.columns)
        assert len(cols) == len(set(cols)), "duplicate column names in argia schema"

    def test_weather_cols_at_end(self):
        n = len(WEATHER_COLS)
        assert ARGIA_SCHEMA.columns[-n:] == WEATHER_COLS

    def test_typed_inverter_cols_match_plant_schema(self):
        # After identity (5 cols for argia, 4 for plant), the typed group should match
        argia_typed = ARGIA_SCHEMA.columns[5 : 5 + len(TYPED_INVERTER_COLS)]
        plant_typed = PLANT_SCHEMA.columns[4 : 4 + len(TYPED_INVERTER_COLS)]
        assert argia_typed == plant_typed
        assert tuple(plant_typed) == TYPED_INVERTER_COLS


# ----------------- tab naming -----------------


class TestPlantTabName:
    def test_prepends_telemetry(self):
        assert plant_tab_name("GTO1") == "Telemetry_GTO1"

    def test_preserves_case(self):
        # If portfolio uses lowercase, tab name should too — we don't normalize
        assert plant_tab_name("slp1") == "Telemetry_slp1"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            plant_tab_name("")


class TestArgiaTabName:
    def test_argia_tab_name_constant(self):
        assert ARGIA_TAB_NAME == "Telemetry_Argia"


class TestVersionConstant:
    def test_version_is_one(self):
        # Sanity: changing this constant is intentional; this test reminds us.
        assert COLUMN_VERSION == 1
