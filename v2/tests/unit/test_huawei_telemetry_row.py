"""Tests for argia.telemetry.huawei_row (Stage 4.2).

Stage 4.2 changes:
- Phase voltages (a_u/b_u/c_u) now populate vacr_v/vacs_v/vact_v columns
- Per-MPPT lifetime energy goes to epv{i}_total_kwh, not epv{i}_today_kwh
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pytest

from argia.telemetry.growatt_row import EMPTY_WEATHER, WeatherSnapshot
from argia.telemetry.huawei_row import build_common_row, build_plant_row
from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA, VENDOR_HUAWEI
from argia.vendors.huawei_telemetry import HuaweiTelemetryRow


def _ts() -> dt.datetime:
    return dt.datetime(2026, 5, 14, 15, 30, 25, tzinfo=dt.timezone.utc)


@pytest.fixture
def rich_online() -> HuaweiTelemetryRow:
    """Richly-populated telemetry row reflecting Stage 4.2 changes:
    - Has line-to-neutral voltages (a_u_v, b_u_v, c_u_v)
    - pv_etotal_kwh values are already converted to kWh
    """
    return HuaweiTelemetryRow(
        plant_key="MEX1",
        inverter_sn="ES2470051825",
        timestamp_utc=_ts(),
        status=1,
        raw_status="1",
        inverter_state=512,
        run_state=1,
        power_w=99166.0,
        reactive_power_var=1500.0,
        power_factor=0.997,
        efficiency_pct=98.5,
        elec_freq_hz=60.02,
        # Line-to-line
        ab_u_v=480.5,
        bc_u_v=481.1,
        ca_u_v=480.8,
        # Line-to-neutral (NEW Stage 4.2)
        a_u_v=277.4,
        b_u_v=277.8,
        c_u_v=277.5,
        # Currents
        a_i_a=119.2,
        b_i_a=120.4,
        c_i_a=119.8,
        # Energy
        etoday_kwh=1022.61,
        etotal_kwh=125_000.0,
        mppt_total_kwh=125_500.0,
        temperature_c=48.5,
        mppt_power_w=100_200.0,
        pv_voltages_v=(720.1, 718.4, 721.0, 719.2, 720.5, 718.9, 722.1, 720.8,
                       None, None, None, None, None, None, None, None),
        pv_currents_a=(13.8, 14.1, 13.9, 14.0, 14.2, 13.7, 14.1, 13.9,
                       None, None, None, None, None, None, None, None),
        # Per-MPPT LIFETIME energy in kWh (parser converted from Wh)
        pv_etotal_kwh=(56.82, 56.29, 57.14, 55.85, 56.14, 11.10, 32.14, 18.39,
                       None, None, None, None, None, None, None, None),
        raw_data_item_map={"active_power": 99.166},
    )


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=661.0,
        irradiance_kwh_m2_5m=0.055083,
        cloud_cover_pct=2.5,
        ambient_temp_c=None,
    )


# ============================================================
# STAGE 4.2 fixes: phase voltage population
# ============================================================


class TestPhaseVoltagesPopulated:
    """Stage 4.1 left these blank. Stage 4.2 populates them."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vacr_v_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vacr_v")] == 277.4

    def test_vacs_v_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vacs_v")] == 277.8

    def test_vact_v_populated(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vact_v")] == 277.5

    def test_line_to_line_still_works(self, rich_online):
        """Stage 4.1 line-to-line population must still work."""
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vac_rs_v")] == 480.5
        assert result[self._col_idx("vac_st_v")] == 481.1
        assert result[self._col_idx("vac_tr_v")] == 480.8


# ============================================================
# STAGE 4.2 fixes: per-MPPT lifetime energy routing
# ============================================================


class TestPerMpptEnergyRouting:
    """Stage 4.1 wrote lifetime Wh into epv_today_kwh columns (misleading).
    Stage 4.2 writes lifetime kWh into epv_total_kwh columns (correct)."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_epv_today_kwh_columns_blank(self, rich_online):
        """The daily-energy columns must be blank for Huawei."""
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(1, 16):
            cell = result[self._col_idx(f"epv{i}_today_kwh")]
            assert cell == "", (
                f"epv{i}_today_kwh should be blank for Huawei, got {cell!r}"
            )

    def test_epv_total_kwh_columns_populated(self, rich_online):
        """The lifetime-energy columns must hold the converted-from-Wh values."""
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("epv1_total_kwh")] == 56.82
        assert result[self._col_idx("epv2_total_kwh")] == 56.29
        assert result[self._col_idx("epv3_total_kwh")] == 57.14
        assert result[self._col_idx("epv4_total_kwh")] == 55.85
        assert result[self._col_idx("epv8_total_kwh")] == 18.39

    def test_epv_total_kwh_blank_beyond_available(self, rich_online):
        """MPPTs 9-15 have None in the fixture → blank in the row."""
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(9, 16):
            assert result[self._col_idx(f"epv{i}_total_kwh")] == ""


# ============================================================
# Regression tests — Stage 4.1 behaviors that must keep working
# ============================================================


class TestStage41RegressionShape:
    def test_length_matches_plant_schema(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count
        assert len(result) == 142

    def test_no_none_values(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert all(c is not None for c in result)


class TestStage41RegressionFields:
    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_identity_unchanged(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[2] == "ES2470051825"
        assert result[3] == "Inverter 1"

    def test_status_power_etoday(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("status")] == 1
        assert result[self._col_idx("power_w")] == 99166
        assert result[self._col_idx("etoday_kwh")] == 1022.61

    def test_temperature(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("temperature_c")] == 48.5

    def test_pf_fac_hz(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("pf")] == 0.997
        assert result[self._col_idx("fac_hz")] == 60.02

    def test_ppv_etotal(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("ppv_w")] == 100_200.0
        assert result[self._col_idx("epv_total_kwh")] == 125_000.0

    def test_per_mppt_voltages(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("vpv1_v")] == 720.1
        assert result[self._col_idx("vpv8_v")] == 720.8
        assert result[self._col_idx("vpv9_v")] == ""


class TestStage41RegressionBlanks:
    """Columns Huawei never populates — must STILL be blank in Stage 4.2."""

    def _col_idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_iac_a_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("iac_a")] == ""

    def test_per_string_voltages_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(1, 33):
            assert result[self._col_idx(f"vstring{i}_v")] == ""

    def test_per_mppt_power_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        for i in range(1, 10):
            assert result[self._col_idx(f"ppv{i}_w")] == ""

    def test_growatt_fault_cols_blank(self, rich_online):
        result = build_plant_row(rich_online, "Inverter 1")
        assert result[self._col_idx("fault_code_1")] == ""
        assert result[self._col_idx("fault_code_2")] == ""


# ============================================================
# Common row (narrow Argia tab) — unchanged in Stage 4.2
# ============================================================


class TestCommonRowUnchanged:
    def _col_idx(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count

    def test_vendor_huawei(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("vendor")] == VENDOR_HUAWEI

    def test_temperature_populated(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[self._col_idx("temperature_c")] == 48.5

    def test_fault_code(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        fc = result[self._col_idx("fault_code")]
        assert "IS=512" in fc
        assert "RS=1" in fc

    def test_weather_at_end(self, rich_online, weather):
        result = build_common_row(rich_online, "Inverter 1", weather)
        assert result[-4:] == [661.0, 0.055083, 2.5, ""]
