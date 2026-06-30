"""Tests for argia.telemetry.growatt_row.

Drives the row builders with the real TAIGENE MAXHistory fixture.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from argia.telemetry.growatt_row import (
    EMPTY_WEATHER,
    WeatherSnapshot,
    build_common_row,
    build_plant_row,
)
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    PLANT_SCHEMA,
    TYPED_INVERTER_COLS,
    VENDOR_GROWATT,
)
from argia.vendors.growatt_web_parser import (
    extract_latest_row,
    parse_max_history,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "growatt_web"
    / "GTO1_getMAXHistory_JFM7DXN00T_2026-05-11.json"
)


@pytest.fixture(scope="module")
def envelope() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def latest_row(envelope):
    rows = parse_max_history(envelope)
    assert rows, "fixture parsed to empty list"
    return extract_latest_row(rows)


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=825.5,
        irradiance_kwh_m2_5m=0.068792,
        cloud_cover_pct=18.5,
        ambient_temp_c=29.0,
        module_temp_c=45.0,
    )


# ============================================================
# build_plant_row — wide vendor-shaped row
# ============================================================


class TestBuildPlantRowShape:
    def test_returns_list(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert isinstance(result, list)

    def test_length_matches_schema(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count

    def test_no_none_values(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert all(c is not None for c in result)


class TestBuildPlantRowIdentity:
    def test_inverter_sn_in_col_2(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[2] == "JFM7DXN00T"

    def test_inverter_label_in_col_3(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[3] == "Inverter 1"

    def test_timestamp_utc_is_iso_format(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        parsed = dt.datetime.fromisoformat(result[0])
        assert parsed.tzinfo is not None

    def test_timestamp_mx_is_space_separated(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        ts_mx = result[1]
        assert isinstance(ts_mx, str)
        assert len(ts_mx) == 19
        assert ts_mx[10] == " "


class TestBuildPlantRowTypedFields:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_status_is_int_one_or_three(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[self._col_index("status")] in (1, 3)

    def test_pf_present(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[self._col_index("pf")] == 1.0

    def test_fac_is_realistic_60hz(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        fac = result[self._col_index("fac_hz")]
        assert 59 < fac < 61

    def test_fault_code_1_present(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[self._col_index("fault_code_1")] == 0


class TestBuildPlantRowMpptStringFields:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vpv_columns_are_floats_or_empty(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        for i in range(1, 17):
            v = result[self._col_index(f"vpv{i}_v")]
            assert v == "" or isinstance(v, (int, float))

    def test_vstring_columns_high_indexes_are_zero(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        for i in range(30, 33):
            v = result[self._col_index(f"vstring{i}_v")]
            assert v == 0.0 or v == 0

    def test_istring_20_to_29_present(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        for i in range(20, 30):
            v = result[self._col_index(f"istring{i}_a")]
            assert v == "" or isinstance(v, (int, float))


class TestBuildPlantRowWeather:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_weather_blank_with_empty_weather(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1", EMPTY_WEATHER)
        for col in ("irradiance_wm2", "irradiance_kwh_m2_5m",
                    "cloud_cover_pct", "ambient_temp_c"):
            assert result[self._col_index(col)] == ""

    def test_weather_populated(self, latest_row, weather):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1", weather)
        assert result[self._col_index("irradiance_wm2")] == 825.5
        assert result[self._col_index("irradiance_kwh_m2_5m")] == 0.068792
        assert result[self._col_index("cloud_cover_pct")] == 18.5
        assert result[self._col_index("ambient_temp_c")] == 29.0


# ============================================================
# build_common_row — narrow cross-vendor row
# ============================================================


class TestBuildCommonRow:
    def _col_index(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length_matches_argia_schema(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert len(result) == ARGIA_SCHEMA.column_count
        assert len(result) == 16

    def test_vendor_column_is_growatt(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[self._col_index("vendor")] == VENDOR_GROWATT
        assert result[self._col_index("vendor")] == "GROWATT"

    def test_plant_key_populated(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[self._col_index("plant_key")] == "GTO1"

    def test_inverter_sn_and_label(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[self._col_index("inverter_sn")] == "JFM7DXN00T"
        assert result[self._col_index("inverter_label")] == "Inverter 1"

    def test_status_is_int(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[self._col_index("status")] in (1, 3)

    def test_temperature_c_populated(self, latest_row, weather):
        # The fixture has real temperatures (~30-40°C for healthy inverters)
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        temp = result[self._col_index("temperature_c")]
        assert temp != ""
        assert 0 < temp < 100  # plausible inverter operating range

    def test_fault_code_zero_for_clean_row(self, latest_row, weather):
        # Fixture rows all have faultCode1=faultCode2=faultType=0
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[self._col_index("fault_code")] == "0"

    def test_weather_at_end(self, latest_row, weather):
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[-5:] == [825.5, 0.068792, 18.5, 29.0, 45.0]

    def test_natural_key_columns_extract_correctly(self, latest_row, weather):
        # Verify the natural key indices line up with the schema's claimed cols
        result = build_common_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        ts_utc = result[ARGIA_SCHEMA.natural_key_columns[0]]
        plant_key = result[ARGIA_SCHEMA.natural_key_columns[1]]
        sn = result[ARGIA_SCHEMA.natural_key_columns[2]]
        assert isinstance(ts_utc, str) and "T" in ts_utc
        assert plant_key == "GTO1"
        assert sn == "JFM7DXN00T"


# ============================================================
# Edge cases
# ============================================================


class TestEdgeCases:
    def _row_from_modified_raw(self, source_row, mutations: dict):
        from copy import deepcopy

        from argia.vendors.growatt_web_parser import parse_max_history_row

        raw_copy = deepcopy(source_row.raw)
        raw_copy.update(mutations)
        return parse_max_history_row(raw_copy)

    def test_fault_code_marks_status_offline(self, latest_row):
        modified = self._row_from_modified_raw(latest_row, {"faultCode1": 42})
        result = build_plant_row(modified, "TESTSN", "Test")
        status_idx = PLANT_SCHEMA.columns.index("status")
        assert result[status_idx] == 3

    def test_fault_code_string_formats_non_zero(self, latest_row):
        modified = self._row_from_modified_raw(latest_row, {"faultCode1": 42})
        result = build_common_row(modified, "GTO1", "TESTSN", "Test")
        fc_idx = ARGIA_SCHEMA.columns.index("fault_code")
        assert result[fc_idx] == "FC1=42"

    def test_fault_code_multiple_fields(self, latest_row):
        modified = self._row_from_modified_raw(
            latest_row, {"faultCode1": 1, "faultCode2": 2, "faultType": 3},
        )
        result = build_common_row(modified, "GTO1", "TESTSN", "Test")
        fc_idx = ARGIA_SCHEMA.columns.index("fault_code")
        assert result[fc_idx] == "FC1=1,FC2=2,FT=3"

    def test_clean_row_status_is_one(self, latest_row):
        modified = self._row_from_modified_raw(
            latest_row, {"faultCode1": 0, "faultCode2": 0, "faultType": 0},
        )
        result = build_plant_row(modified, "TESTSN", "Test")
        status_idx = PLANT_SCHEMA.columns.index("status")
        assert result[status_idx] == 1
