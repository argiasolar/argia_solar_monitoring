"""Tests for argia.telemetry.solaredge_row (Stage 5.1).

Stage 5.1 changes vs 5.0:
- vacr/vacs/vact populated from L1/L2/L3.acVoltage
- vac_rs/vac_st/vac_tr populated from vL1To2/vL2To3/vL3To1
- pacr/pacs/pact populated from L1/L2/L3.activePower
- iac_a, pf, fac_hz populated from phase means
"""

from __future__ import annotations

import datetime as dt

import pytest

from argia.telemetry.growatt_row import EMPTY_WEATHER, WeatherSnapshot
from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA, VENDOR_SOLAREDGE
from argia.telemetry.solaredge_row import build_common_row, build_plant_row
from argia.vendors.solaredge_telemetry import PhaseData, SolarEdgeTelemetryRow


def _ts() -> dt.datetime:
    return dt.datetime(2026, 5, 14, 18, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def rich_row() -> SolarEdgeTelemetryRow:
    """Realistic QRO1-shape row with full per-phase data."""
    return SolarEdgeTelemetryRow(
        plant_key="QRO1",
        inverter_sn="INV001",
        timestamp_utc=_ts(),
        status=1,
        raw_mode="MPPT",
        operation_mode=0,
        power_w=80990.64,
        v_l1_to_l2_v=435.14,
        v_l2_to_l3_v=435.44,
        v_l3_to_l1_v=435.33,
        l1=PhaseData(
            ac_voltage_v=251.07,
            ac_current_a=109.79,
            ac_frequency_hz=60.025,
            active_power_w=27064.04,
            apparent_power_va=27681.03,
            reactive_power_var=-5775.17,
            cos_phi=1.0,
        ),
        l2=PhaseData(
            ac_voltage_v=251.82,
            ac_current_a=109.46,
            ac_frequency_hz=60.025,
            active_power_w=26997.76,
            apparent_power_va=27608.95,
            reactive_power_var=-5742.50,
            cos_phi=1.0,
        ),
        l3=PhaseData(
            ac_voltage_v=251.08,
            ac_current_a=109.42,
            ac_frequency_hz=60.025,
            active_power_w=26928.83,
            apparent_power_va=27550.34,
            reactive_power_var=-5784.77,
            cos_phi=1.0,
        ),
        etoday_kwh=470.88,
        etotal_kwh=421970.88,
        temperature_c=53.11,
        dc_voltage_v=893.17,
        power_limit_pct=100.0,
        ground_fault_resistance=466.51,
    )


@pytest.fixture
def weather() -> WeatherSnapshot:
    return WeatherSnapshot(
        irradiance_wm2=850.0,
        irradiance_kwh_m2_5m=0.0708,
        cloud_cover_pct=5.0,
        ambient_temp_c=None,
    )


# ============================================================
# STAGE 5.1: per-phase voltages now populated
# ============================================================


class TestPhaseToNeutralVoltages:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vacr_v_from_l1(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vacr_v")] == 251.07

    def test_vacs_v_from_l2(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vacs_v")] == 251.82

    def test_vact_v_from_l3(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vact_v")] == 251.08


class TestLineToLineVoltages:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_vac_rs_v(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vac_rs_v")] == 435.14

    def test_vac_st_v(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vac_st_v")] == 435.44

    def test_vac_tr_v(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vac_tr_v")] == 435.33


# ============================================================
# STAGE 5.1: per-phase active power now populated
# ============================================================


class TestPerPhasePower:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_pacr_from_l1(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("pacr_w")] == 27064.04

    def test_pacs_from_l2(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("pacs_w")] == 26997.76

    def test_pact_from_l3(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("pact_w")] == 26928.83

    def test_phase_powers_sum_close_to_total(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        pacr = result[self._idx("pacr_w")]
        pacs = result[self._idx("pacs_w")]
        pact = result[self._idx("pact_w")]
        power_w = result[self._idx("power_w")]
        # Sum within 1% of total
        assert abs((pacr + pacs + pact) - power_w) / power_w < 0.01


# ============================================================
# STAGE 5.1: phase-mean derived fields
# ============================================================


class TestPhaseMeanDerived:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_iac_a_is_mean_of_phases(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        iac = result[self._idx("iac_a")]
        # Mean of 109.79, 109.46, 109.42 = 109.5566...
        assert 109.5 < iac < 109.6

    def test_pf_is_mean(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        # All three phases have cos_phi=1.0
        assert result[self._idx("pf")] == 1.0

    def test_fac_hz_is_mean(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        # All three phases at 60.025
        assert result[self._idx("fac_hz")] == 60.025


class TestPhaseMeanWithMissingPhase:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_iac_skips_none_phases(self):
        """If L2 is empty, iac_a is the mean of L1 and L3 only."""
        row = SolarEdgeTelemetryRow(
            plant_key="QRO1",
            inverter_sn="INV001",
            timestamp_utc=_ts(),
            status=1,
            l1=PhaseData(ac_current_a=100.0),
            l2=PhaseData(),  # all None
            l3=PhaseData(ac_current_a=120.0),
        )
        result = build_plant_row(row, "Inverter 1")
        # Mean of 100 and 120 = 110
        assert result[self._idx("iac_a")] == 110.0

    def test_iac_blank_when_all_phases_empty(self):
        row = SolarEdgeTelemetryRow(
            plant_key="QRO1",
            inverter_sn="INV001",
            timestamp_utc=_ts(),
            status=1,
        )
        result = build_plant_row(row, "Inverter 1")
        assert result[self._idx("iac_a")] == ""


# ============================================================
# STAGE 5 REGRESSION: existing field population
# ============================================================


class TestStage5Regression:
    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

    def test_identity(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[2] == "INV001"
        assert result[3] == "Inverter 1"

    def test_status(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("status")] == 1

    def test_power_w(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("power_w")] == 80991  # int(round(80990.64))

    def test_etoday(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("etoday_kwh")] == 470.88

    def test_temperature(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("temperature_c")] == 53.11

    def test_etotal(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("epv_total_kwh")] == 421970.88

    def test_vpv1_is_dc_voltage(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert result[self._idx("vpv1_v")] == 893.17


class TestStillBlankByDesign:
    """Fields SolarEdge doesn't expose stay blank."""

    def _idx(self, name: str) -> int:
        return PLANT_SCHEMA.columns.index(name)

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

    def test_epv_today_total_blank(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        for i in range(1, 16):
            assert result[self._idx(f"epv{i}_today_kwh")] == ""
            assert result[self._idx(f"epv{i}_total_kwh")] == ""


# ============================================================
# Narrow common row — unchanged in 5.1
# ============================================================


class TestCommonRow:
    def _idx(self, name: str) -> int:
        return ARGIA_SCHEMA.columns.index(name)

    def test_length(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert len(result) == ARGIA_SCHEMA.column_count

    def test_vendor_solaredge(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("vendor")] == VENDOR_SOLAREDGE

    def test_temperature_real(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("temperature_c")] == 53.11

    def test_fault_code_healthy(self, rich_row, weather):
        result = build_common_row(rich_row, "Inverter 1", weather)
        assert result[self._idx("fault_code")] == "0"


# ============================================================
# Shape invariants
# ============================================================


class TestShapeInvariants:
    def test_plant_row_length(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert len(result) == PLANT_SCHEMA.column_count
        assert len(result) == 142

    def test_no_none_values_in_plant_row(self, rich_row):
        result = build_plant_row(rich_row, "Inverter 1")
        assert all(c is not None for c in result)
