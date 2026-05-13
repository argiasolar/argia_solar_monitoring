"""Tests for argia.telemetry.growatt_row.

Drives the row builders with the real TAIGENE MAXHistory fixture so we know
the wide schema actually populates from real Growatt JSON.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from argia.telemetry.growatt_row import (
    EMPTY_WEATHER,
    WeatherSnapshot,
    build_argia_row,
    build_plant_row,
)
from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA, TYPED_INVERTER_COLS
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
    assert rows, "fixture parsed to empty list — fixture problem"
    return extract_latest_row(rows)


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=825.5,
        irradiance_kwh_m2_5m=0.068792,
        cloud_cover_pct=18.5,
        ambient_temp_c=29.0,
    )


# ============================================================
# build_plant_row
# ============================================================


class TestBuildPlantRowShape:
    def test_returns_list(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert isinstance(result, list)

    def test_length_matches_schema(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count

    def test_no_none_values(self, latest_row):
        # None becomes empty string in the row
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert all(c is not None for c in result), (
            "row should not contain None — None values should become ''"
        )


class TestBuildPlantRowIdentity:
    def test_inverter_sn_in_col_2(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[2] == "JFM7DXN00T"

    def test_inverter_label_in_col_3(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        assert result[3] == "Inverter 1"

    def test_timestamp_utc_is_iso_format(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        # Parse it — should succeed and have a TZ
        parsed = dt.datetime.fromisoformat(result[0])
        assert parsed.tzinfo is not None

    def test_timestamp_mx_is_space_separated(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        ts_mx = result[1]
        assert isinstance(ts_mx, str)
        # Format: "YYYY-MM-DD HH:MM:SS"
        assert len(ts_mx) == 19
        assert ts_mx[10] == " "
        assert ts_mx[4] == "-"
        assert ts_mx[13] == ":"


class TestBuildPlantRowTypedFields:
    """Check the typed inverter columns extract real values from the fixture."""

    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_status_is_int_one_or_three(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        status = result[self._col_index("status")]
        assert status in (1, 3)

    def test_pf_present(self, latest_row):
        # The fixture always shows pf=1.0
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        pf = result[self._col_index("pf")]
        assert pf == 1.0

    def test_fac_is_realistic_60hz(self, latest_row):
        # The fixture is from a Mexican plant — 60Hz grid
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        fac = result[self._col_index("fac_hz")]
        assert 59 < fac < 61

    def test_fault_code_1_present(self, latest_row):
        # The fixture rows are all healthy
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        fc = result[self._col_index("fault_code_1")]
        assert fc == 0


class TestBuildPlantRowMpptStringFields:
    def _col_index(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vpv_columns_are_floats_or_empty(self, latest_row):
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        for i in range(1, 17):
            v = result[self._col_index(f"vpv{i}_v")]
            assert v == "" or isinstance(v, (int, float))

    def test_vstring_columns_high_indexes_are_zero(self, latest_row):
        # vString30..32 are always 0 in the captured fixture
        result = build_plant_row(latest_row, "JFM7DXN00T", "Inverter 1")
        for i in range(30, 33):
            v = result[self._col_index(f"vstring{i}_v")]
            assert v == 0.0 or v == 0  # accept either numeric form

    def test_istring_20_to_29_present(self, latest_row):
        # Real currentString20..29 values appear; check they're floats
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
# build_argia_row
# ============================================================


class TestBuildArgiaRow:
    def _col_index(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length_matches_argia_schema(self, latest_row, weather):
        result = build_argia_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert len(result) == ARGIA_SCHEMA.column_count

    def test_plant_key_at_col_2(self, latest_row, weather):
        result = build_argia_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[2] == "GTO1"

    def test_inverter_sn_at_col_3(self, latest_row, weather):
        result = build_argia_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        assert result[3] == "JFM7DXN00T"

    def test_same_typed_values_as_plant_row(self, latest_row, weather):
        """The typed inverter slice should match between the two row shapes."""
        plant_row = build_plant_row(
            latest_row, "JFM7DXN00T", "Inverter 1", weather,
        )
        argia_row = build_argia_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        # Plant typed group: cols 4..4+len(TYPED_INVERTER_COLS)
        # Argia typed group: cols 5..5+len(TYPED_INVERTER_COLS)
        n = len(TYPED_INVERTER_COLS)
        assert plant_row[4 : 4 + n] == argia_row[5 : 5 + n]

    def test_weather_at_end(self, latest_row, weather):
        result = build_argia_row(
            latest_row, "GTO1", "JFM7DXN00T", "Inverter 1", weather,
        )
        # Last 4 columns are the weather group
        assert result[-4:] == [825.5, 0.068792, 18.5, 29.0]


# ============================================================
# Edge cases — driven by parsing modified fixture rows
# ============================================================
#
# Earlier draft of this file used a ``FakeRow`` class for these tests; the
# parser's column-family accessors strictly require MAXHistoryRow or Mapping
# types and rejected the fake class. Tests now build proper rows by patching
# the raw dict and re-parsing it through ``parse_max_history_row``.


class TestEdgeCases:
    def _row_from_modified_raw(self, source_row, mutations: dict):
        """Patch source_row.raw with mutations, re-parse, return new row."""
        from copy import deepcopy

        from argia.vendors.growatt_web_parser import parse_max_history_row

        raw_copy = deepcopy(source_row.raw)
        raw_copy.update(mutations)
        return parse_max_history_row(raw_copy)

    def test_fault_code_marks_status_offline(self, latest_row):
        """If faultCode1 is non-zero, status should be 3 not 1."""
        modified = self._row_from_modified_raw(latest_row, {"faultCode1": 42})
        result = build_plant_row(modified, "TESTSN", "Test")
        status_idx = PLANT_SCHEMA.columns.index("status")
        assert result[status_idx] == 3

    def test_clean_row_status_is_one(self, latest_row):
        """Companion to the test above — confirm a fault-free row is online.

        The fixture rows are already clean, but being explicit removes any
        ambiguity if Growatt ever sends a row with a stray non-zero fault.
        """
        modified = self._row_from_modified_raw(
            latest_row,
            {"faultCode1": 0, "faultCode2": 0, "faultType": 0},
        )
        result = build_plant_row(modified, "TESTSN", "Test")
        status_idx = PLANT_SCHEMA.columns.index("status")
        assert result[status_idx] == 1
