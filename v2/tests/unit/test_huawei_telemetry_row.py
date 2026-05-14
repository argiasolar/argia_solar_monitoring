"""Tests for argia.telemetry.huawei_row (Stage 4.1 rich version).

The Stage 4 version drove from ``InverterSnapshot`` (sparse, 5 fields).
Stage 4.1 drives from ``HuaweiTelemetryRow`` (rich, ~25 fields). Tests build
synthetic HuaweiTelemetryRow instances exercising both "rich response" and
"sparse response" paths.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pytest

from argia.telemetry.growatt_row import EMPTY_WEATHER, WeatherSnapshot
from argia.telemetry.huawei_row import build_common_row, build_plant_row
from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA, VENDOR_HUAWEI
from argia.vendors.huawei_telemetry import HuaweiTelemetryRow


# ============================================================
# Fixtures: build HuaweiTelemetryRow instances
# ============================================================


def _ts() -> dt.datetime:
    return dt.datetime(2026, 5, 13, 22, 18, 13, tzinfo=dt.timezone.utc)


@pytest.fixture
def rich_online() -> HuaweiTelemetryRow:
    """A richly-populated telemetry row simulating a healthy SUN2000 inverter."""
    return HuaweiTelemetryRow(
        plant_key="MEX1",
        inverter_sn="ES2470051825",
        timestamp_utc=_ts(),
        status=1,
        raw_status="1",
        inverter_state=512,
        run_state=1,
        power_w=99166.0,           # 99.166 kW → W
        reactive_power_var=1500.0,
        power_factor=0.997,
        efficiency_pct=98.5,
        elec_freq_hz=60.02,
        ab_u_v=480.5,
        bc_u_v=481.1,
        ca_u_v=480.8,
        a_i_a=119.2,
        b_i_a=120.4,
        c_i_a=119.8,
        etoday_kwh=1022.61,
        etotal_kwh=125_000.0,
        mppt_total_kwh=125_500.0,
        temperature_c=48.5,
        mppt_power_w=100_200.0,
        pv_voltages_v=(720.1, 718.4, 721.0, 719.2, 720.5, 718.9, 722.1, 720.8,
                       None, None, None, None, None, None, None, None),
        pv_currents_a=(13.8, 14.1, 13.9, 14.0, 14.2, 13.7, 14.1, 13.9,
                       None, None, None, None, None, None, None, None),
        pv_eday_kwh=(125.4, 127.1, 125.8, 126.3, 127.5, 124.9, 126.7, 125.5,
                     None, None, None, None, None, None, None, None),
        raw_data_item_map={"active_power": 99.166, "temperature": 48.5},
    )


@pytest.fixture
def sparse_response() -> HuaweiTelemetryRow:
    """A row from a hypothetical inverter that only returns the basics
    (analogous to the old Stage 4 sandbox response). Everything beyond
    status/power/etoday is None."""
    return HuaweiTelemetryRow(
        plant_key="MEX1",
        inverter_sn="ES2470051825",
        timestamp_utc=_ts(),
        status=1,
        raw_status="1",
        power_w=99166.0,
        etoday_kwh=1022.61,
    )


@pytest.fixture
def offline_row() -> HuaweiTelemetryRow:
    """An offline inverter (devStatus=3)."""
    return HuaweiTelemetryRow(
        plant_key="MEX1",
        inverter_sn="GR2489022511",
        timestamp_utc=_ts(),
        status=3,
        raw_status="3",
        inverter_state=0,
        run_state=0,
        power_w=0.0,
        etoday_kwh=471.66,
    )


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=583.0,
        irradiance_kwh_m2_5m=0.048583,
        cloud_cover_pct=54.7,
        ambient_temp_c=None,
    )


# ============================================================
# build_plant_row — wide row, rich population
# ============================================================


class TestBuildPlantRowShape:
    def test_length_matches_plant_schema(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count
        assert len(result) == 142

    def test_no_none_values(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert all(c is not None for c in result)

    def test_works_with_sparse_response(self, sparse_response):
        # Should not crash; cells just stay blank
        result = build_plant_row(sparse_response, "Inverter 1")
        assert len(result) == 142


class TestBuildPlantRowIdentity:
    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_inverter_sn_at_col_2(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[2] == "ES2470051825"

    def test_inverter_label_at_col_3(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[3] == "Inverter 1"

    def test_timestamp_utc_iso(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        parsed = dt.datetime.fromisoformat(result[0])
        assert parsed.tzinfo is not None


class TestBuildPlantRowRichFields:
    """Stage 4.1: fields that USED to be blank now populated."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_status_online(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("status")] == 1

    def test_status_offline(self, offline_row):
        result = build_plant_row(offline_row, "Inverter 3")
        assert result[self._col_idx("status")] == 3

    def test_power_w_int(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("power_w")] == 99166

    def test_etoday_kwh(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("etoday_kwh")] == 1022.61

    def test_pac_w_float(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("pac_w")] == 99166.0

    def test_temperature_c_populated(self, rich_online):
        """Stage 4 had this blank — Stage 4.1 fills it."""
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("temperature_c")] == 48.5

    def test_pf_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("pf")] == 0.997

    def test_fac_hz_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("fac_hz")] == 60.02

    def test_ppv_w_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("ppv_w")] == 100_200.0

    def test_epv_total_kwh_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("epv_total_kwh")] == 125_000.0

    def test_line_to_line_voltages(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vac_rs_v")] == 480.5
        assert result[self._col_idx("vac_st_v")] == 481.1
        assert result[self._col_idx("vac_tr_v")] == 480.8


class TestBuildPlantRowMPPT:
    """Per-MPPT voltage + day energy now populated from Huawei rich response."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vpv1_to_vpv8_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vpv1_v")] == 720.1
        assert result[self._col_idx("vpv2_v")] == 718.4
        assert result[self._col_idx("vpv8_v")] == 720.8

    def test_vpv9_to_vpv16_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(9, 17):
            assert result[self._col_idx(f"vpv{i}_v")] == ""

    def test_epv_today_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("epv1_today_kwh")] == 125.4
        assert result[self._col_idx("epv8_today_kwh")] == 125.5


class TestBuildPlantRowBlankColumns:
    """Some Growatt-specific cols remain blank in Huawei rows by design."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_iac_a_blank(self, rich_online):
        # Huawei reports per-phase only — single iac_a stays blank
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("iac_a")] == ""

    def test_per_string_voltages_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(1, 33):
            assert result[self._col_idx(f"vstring{i}_v")] == ""

    def test_per_mppt_power_blank(self, rich_online):
        # ppv1..ppv9 — Huawei doesn't expose per-MPPT power directly
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(1, 10):
            assert result[self._col_idx(f"ppv{i}_w")] == ""

    def test_growatt_fault_cols_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("fault_code_1")] == ""
        assert result[self._col_idx("fault_code_2")] == ""


class TestBuildPlantRowWeather:
    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_weather_populated(self, rich_online, weather):
        result = build_plant_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("irradiance_wm2")] == 583.0
        assert result[self._col_idx("cloud_cover_pct")] == 54.7

    def test_weather_blank_default(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1", EMPTY_WEATHER)
        assert result[self._col_idx("irradiance_wm2")] == ""


# ============================================================
# build_common_row — narrow cross-vendor row
# ============================================================


class TestBuildCommonRow:
    def _col_idx(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length_matches_argia_schema(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count
        assert len(result) == 15

    def test_vendor_column_is_huawei(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("vendor")] == VENDOR_HUAWEI

    def test_plant_key(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("plant_key")] == "MEX1"

    def test_inverter_sn(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("inverter_sn")] == "ES2470051825"

    def test_temperature_c_populated(self, rich_online, weather):
        """Stage 4 had this blank — Stage 4.1 fills it."""
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("temperature_c")] == 48.5

    def test_temperature_blank_when_missing(self, sparse_response, weather):
        result = build_common_row(sparse_response, "Inverter 1", weather)
        assert result[self._col_idx("temperature_c")] == ""

    def test_fault_code_healthy(self, rich_online, weather):
        # raw_status="1" (online), inverter_state=512, run_state=1
        # Expected fault_code: "IS=512,RS=1" (inverter_state and run_state
        # are non-zero, raw_status="1" is online so it's NOT prepended)
        result = build_common_row(rich_online, "Inverter 1", weather)
        fc = result[self._col_idx("fault_code")]
        assert "IS=512" in fc
        assert "RS=1" in fc

    def test_fault_code_offline(self, offline_row, weather):
        # raw_status="3" (not "1"), so DS=3 should appear
        result = build_common_row(offline_row, "Inverter 3", weather)
        fc = result[self._col_idx("fault_code")]
        assert "DS=3" in fc

    def test_fault_code_zero_when_all_clean(self, weather):
        clean = HuaweiTelemetryRow(
            plant_key="MEX1",
            inverter_sn="X",
            timestamp_utc=_ts(),
            status=1,
            raw_status="1",
            inverter_state=0,
            run_state=0,
            power_w=10_000.0,
            etoday_kwh=50.0,
        )
        result = build_common_row(clean, "X", weather)
        assert result[self._col_idx("fault_code")] == "0"


# ============================================================
# Cross-vendor invariants
# ============================================================


class TestCrossVendorInvariants:
    """Huawei and Growatt common rows must have the same shape."""

    def test_same_column_count(self, rich_online, weather):
        hw = build_common_row(rich_online, "Inverter 1", weather)
        # Build a Growatt common row from minimal raw
        from argia.telemetry.growatt_row import build_common_row as build_gr
        from argia.vendors.growatt_web_parser import parse_max_history_row

        raw = {
            "calendar": {
                "year": 2026, "month": 4, "dayOfMonth": 13,
                "hourOfDay": 15, "minute": 35, "second": 0,
            },
            "pac": 100_000.0,
            "eacToday": 700.0,
            "pf": 1.0,
            "fac": 60.0,
            "temperature": 48.0,
            "faultCode1": 0, "faultCode2": 0, "faultType": 0,
        }
        gr_row = parse_max_history_row(raw)
        gr = build_gr(gr_row, "GTO1", "TESTSN", "Inv 1", weather)

        assert len(hw) == len(gr) == ARGIA_SCHEMA.column_count

    def test_vendor_column_differs(self, rich_online, weather):
        hw = build_common_row(rich_online, "Inverter 1", weather)
        vendor_idx = ARGIA_SCHEMA.columns.index("vendor")
        assert hw[vendor_idx] == "HUAWEI"
