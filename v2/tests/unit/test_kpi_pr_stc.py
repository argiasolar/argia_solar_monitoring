"""Tests for the PR_STC temperature correction (slice 4a).

Covers the two pure functions (temp_corrected_pr, irradiance_weighted_module_temp)
and their integration into compute_plant_pr. No I/O.
"""

from __future__ import annotations

import pytest

from argia.kpi.energy import EnergyDay
from argia.kpi.irradiance import IrradianceDay, IrradianceSource
from argia.kpi.performance import (
    GAMMA_PMAX_DEFAULT,
    T_STC_C,
    compute_plant_pr,
    irradiance_weighted_module_temp,
    temp_corrected_pr,
)


# ===================================================================
# temp_corrected_pr
# ===================================================================


class TestTempCorrectedPr:
    def test_hot_module_lifts_pr(self):
        # 45 degC, gamma -0.0035: denom = 1 + (-0.0035)(45-25) = 0.93
        # PR_STC = 0.80 / 0.93 = 0.8602
        assert temp_corrected_pr(0.80, 45.0) == pytest.approx(0.8602, abs=1e-4)

    def test_at_stc_pr_unchanged(self):
        assert temp_corrected_pr(0.80, T_STC_C) == pytest.approx(0.80)

    def test_cold_module_lowers_pr(self):
        # 15 degC: denom = 1 + (-0.0035)(15-25) = 1.035 -> PR_STC < PR
        assert temp_corrected_pr(0.80, 15.0) == pytest.approx(0.80 / 1.035, abs=1e-4)

    def test_none_pr_returns_none(self):
        assert temp_corrected_pr(None, 45.0) is None

    def test_none_temp_returns_none(self):
        assert temp_corrected_pr(0.80, None) is None

    def test_cell_temp_offset_direct_is_default(self):
        # Default offset 0 == direct method.
        direct = temp_corrected_pr(0.80, 45.0)
        explicit = temp_corrected_pr(0.80, 45.0, cell_temp_offset_c=0.0)
        assert direct == explicit

    def test_cell_temp_offset_shifts_result(self):
        # +3 degC offset -> effective T=48 -> lower denom -> higher PR_STC.
        base = temp_corrected_pr(0.80, 45.0)
        offset = temp_corrected_pr(0.80, 45.0, cell_temp_offset_c=3.0)
        assert offset > base

    def test_custom_gamma(self):
        # denom = 1 + (-0.0040)(20) = 0.92
        assert temp_corrected_pr(0.80, 45.0, gamma_pmax=-0.0040) == pytest.approx(
            0.80 / 0.92, abs=1e-4
        )

    def test_nonpositive_denominator_guarded(self):
        # Absurd gamma/temp that would zero the denominator -> None, no crash.
        assert temp_corrected_pr(0.80, 1000.0, gamma_pmax=-0.01) is None

    def test_default_gamma_is_negative(self):
        assert GAMMA_PMAX_DEFAULT < 0


# ===================================================================
# irradiance_weighted_module_temp
# ===================================================================


class TestIrradianceWeightedModuleTemp:
    def test_weights_toward_high_irradiance(self):
        # 30 degC at 1000 W/m^2, 50 degC at 100 W/m^2:
        # weighted = (30*1000 + 50*100) / 1100 = 31.82
        result = irradiance_weighted_module_temp([(30.0, 1000.0), (50.0, 100.0)])
        assert result == pytest.approx(31.82, abs=0.01)

    def test_falls_back_to_simple_mean_without_irradiance(self):
        result = irradiance_weighted_module_temp([(30.0, None), (40.0, 0.0)])
        assert result == pytest.approx(35.0)

    def test_skips_missing_temps(self):
        result = irradiance_weighted_module_temp([(None, 900.0), (40.0, 900.0)])
        assert result == pytest.approx(40.0)

    def test_none_when_no_usable_temps(self):
        assert irradiance_weighted_module_temp([(None, 900.0), (None, None)]) is None

    def test_empty(self):
        assert irradiance_weighted_module_temp([]) is None


# ===================================================================
# compute_plant_pr integration
# ===================================================================


def _energy(kwh):
    return {
        "SN1": EnergyDay(
            energy_kwh=kwh, energy_kwh_max=kwh, energy_kwh_last=kwh,
            rows_seen=80, rows_online=80, detected_reboot=False,
            discrepancy_pct=0.0,
        )
    }


def _irr(kwh_m2):
    return IrradianceDay(
        kwh_m2=kwh_m2, source=IrradianceSource.SHINEMASTER, samples_used=80
    )


class TestComputePlantPrStc:
    def test_pr_stc_populated_when_module_temp_given(self):
        perf = compute_plant_pr(
            plant_key="NL1", date_iso="2026-06-30",
            kwp_dc=100.0, kwp_ac=90.0,
            energy_per_inverter=_energy(500.0), irradiance=_irr(6.0),
            module_temp_c=45.0,
        )
        # PR = 500 / (100 * 6) = 0.8333; PR_STC = 0.8333 / 0.93 = 0.8960
        assert perf.pr == pytest.approx(0.8333, abs=1e-4)
        assert perf.pr_stc == pytest.approx(0.8960, abs=1e-3)
        assert perf.module_temp_c == pytest.approx(45.0)
        assert perf.pr_stc > perf.pr  # hot day

    def test_pr_stc_none_without_module_temp(self):
        perf = compute_plant_pr(
            plant_key="MEX1", date_iso="2026-06-30",
            kwp_dc=100.0, kwp_ac=90.0,
            energy_per_inverter=_energy(500.0), irradiance=_irr(6.0),
            module_temp_c=None,
        )
        assert perf.pr is not None
        assert perf.pr_stc is None
        assert "PR_STC=None" in perf.notes

    def test_pr_stc_none_when_pr_none(self):
        # No irradiance -> PR None -> PR_STC None even with a module temp.
        perf = compute_plant_pr(
            plant_key="GTO1", date_iso="2026-06-30",
            kwp_dc=100.0, kwp_ac=90.0,
            energy_per_inverter=_energy(500.0),
            irradiance=IrradianceDay(kwh_m2=None, source=IrradianceSource.NONE, samples_used=0),
            module_temp_c=45.0,
        )
        assert perf.pr is None
        assert perf.pr_stc is None

    def test_backwards_compatible_default(self):
        # Existing callers that don't pass module_temp_c still work; pr_stc None.
        perf = compute_plant_pr(
            plant_key="SLP1", date_iso="2026-06-30",
            kwp_dc=100.0, kwp_ac=90.0,
            energy_per_inverter=_energy(500.0), irradiance=_irr(6.0),
        )
        assert perf.pr is not None
        assert perf.pr_stc is None
