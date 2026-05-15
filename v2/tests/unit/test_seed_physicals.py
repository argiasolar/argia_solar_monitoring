"""Tests for scripts/seed_plant_physicals.py — pure inference logic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from seed_plant_physicals import (  # noqa: E402
    DEFAULT_AZIMUTH_DEG,
    DEFAULT_MODULE_WP_MODERN,
    DEFAULT_MODULE_WP_OLDER,
    DEFAULT_SYSTEM_LOSSES_PCT,
    DEFAULT_TILT_DEG,
    MODERN_PANEL_CUTOFF_YEAR,
    guess_module_wp,
    guess_mppt_count,
    infer_inverter_physicals,
    infer_plant_physicals,
)

from argia.core.config import InverterConfig, PlantConfig


# ============================================================
# guess_module_wp
# ============================================================


class TestGuessModuleWp:
    def test_no_date_returns_modern(self):
        assert guess_module_wp("") == DEFAULT_MODULE_WP_MODERN

    def test_garbage_date_returns_modern(self):
        assert guess_module_wp("not-a-date") == DEFAULT_MODULE_WP_MODERN

    def test_recent_year_returns_modern(self):
        assert guess_module_wp("2024-03-15") == DEFAULT_MODULE_WP_MODERN
        assert guess_module_wp("2022-01-01") == DEFAULT_MODULE_WP_MODERN

    def test_pre_cutoff_returns_older(self):
        assert guess_module_wp("2019-06-01") == DEFAULT_MODULE_WP_OLDER
        assert guess_module_wp("2018-12-31") == DEFAULT_MODULE_WP_OLDER

    def test_cutoff_boundary_returns_modern(self):
        """Plants from the cutoff year itself default to modern."""
        date = f"{MODERN_PANEL_CUTOFF_YEAR}-01-01"
        assert guess_module_wp(date) == DEFAULT_MODULE_WP_MODERN


# ============================================================
# guess_mppt_count
# ============================================================


class TestGuessMpptCount:
    def test_residential(self):
        assert guess_mppt_count(5) == 2
        assert guess_mppt_count(8) == 2

    def test_small_commercial(self):
        assert guess_mppt_count(15) == 4
        assert guess_mppt_count(25) == 4
        assert guess_mppt_count(40) == 4

    def test_mid_commercial(self):
        assert guess_mppt_count(50) == 6
        assert guess_mppt_count(75) == 6

    def test_large(self):
        assert guess_mppt_count(100) == 12
        assert guess_mppt_count(150) == 12

    def test_extra_large(self):
        assert guess_mppt_count(200) == 16
        assert guess_mppt_count(250) == 16

    def test_zero_returns_safe_default(self):
        """rated_kw=0 returns a small MPPT count rather than crashing."""
        assert guess_mppt_count(0) == 2


# ============================================================
# infer_plant_physicals
# ============================================================


def _plant(
    plant_key="P1",
    kwp_dc=500.0,
    installation_date="2023-01-01",
    module_wp=None,
    module_count=None,
    tilt_deg=None,
    azimuth_deg=None,
    system_losses_pct=None,
):
    return PlantConfig(
        plant_key=plant_key, customer="", brand="GROWATT", site_id="",
        kwp_dc=kwp_dc, kwp_ac=400.0, lat=None, lon=None,
        expected_factor=0.0, pr_target=0.0, installation_date=installation_date,
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True,
        module_wp=module_wp,
        module_count=module_count,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        system_losses_pct=system_losses_pct,
    )


class TestPlantInference:
    def test_all_empty_fields_get_inferences(self):
        plant = _plant()
        inf = infer_plant_physicals(plant)
        assert inf.will_update_module_wp
        assert inf.will_update_module_count
        assert inf.will_update_tilt_deg
        assert inf.will_update_azimuth_deg
        assert inf.will_update_losses_pct
        assert inf.inferred_module_wp == 540
        assert inf.inferred_tilt_deg == DEFAULT_TILT_DEG
        assert inf.inferred_azimuth_deg == DEFAULT_AZIMUTH_DEG
        assert inf.inferred_losses_pct == DEFAULT_SYSTEM_LOSSES_PCT

    def test_module_count_derived_from_kwp_dc(self):
        """500 kWp / 540 Wp = 925.9 → rounds to 926."""
        plant = _plant(kwp_dc=500.0)
        inf = infer_plant_physicals(plant)
        # 500 * 1000 / 540 = 925.9259 → 926
        assert inf.inferred_module_count == 926

    def test_module_count_uses_existing_module_wp_if_set(self):
        """When module_wp is already set, derive module_count using that
        value, not the inferred default."""
        plant = _plant(kwp_dc=500.0, module_wp=400.0)
        inf = infer_plant_physicals(plant)
        # 500 * 1000 / 400 = 1250
        assert inf.inferred_module_count == 1250

    def test_module_count_zero_when_kwp_dc_zero(self):
        plant = _plant(kwp_dc=0.0)
        inf = infer_plant_physicals(plant)
        assert inf.inferred_module_count == 0
        assert inf.will_update_module_count is False  # can't write 0


class TestPlantInferenceIdempotency:
    """The critical invariant: never overwrite a non-zero existing value."""

    def test_existing_module_wp_preserved(self):
        plant = _plant(module_wp=410.0)
        inf = infer_plant_physicals(plant)
        assert inf.will_update_module_wp is False

    def test_existing_module_count_preserved(self):
        plant = _plant(module_count=1200)
        inf = infer_plant_physicals(plant)
        assert inf.will_update_module_count is False

    def test_existing_tilt_preserved(self):
        plant = _plant(tilt_deg=20.0)
        inf = infer_plant_physicals(plant)
        assert inf.will_update_tilt_deg is False

    def test_explicit_zero_azimuth_preserved(self):
        """Azimuth=0 (north) is valid, must not be overwritten."""
        plant = _plant(azimuth_deg=0.0)
        inf = infer_plant_physicals(plant)
        assert inf.will_update_azimuth_deg is False

    def test_existing_losses_preserved(self):
        plant = _plant(system_losses_pct=18.0)
        inf = infer_plant_physicals(plant)
        assert inf.will_update_losses_pct is False

    def test_mixed_some_set_some_not(self):
        """Partial pre-population is the common case."""
        plant = _plant(module_wp=540.0, tilt_deg=20.0)
        inf = infer_plant_physicals(plant)
        # These two are set → preserve
        assert inf.will_update_module_wp is False
        assert inf.will_update_tilt_deg is False
        # The others remain to be inferred
        assert inf.will_update_azimuth_deg is True
        assert inf.will_update_losses_pct is True
        assert inf.will_update_module_count is True


class TestPlantInferenceInstallDate:
    def test_old_install_uses_older_panel_wp(self):
        plant = _plant(installation_date="2019-06-01", kwp_dc=100.0)
        inf = infer_plant_physicals(plant)
        assert inf.inferred_module_wp == DEFAULT_MODULE_WP_OLDER
        # 100 * 1000 / 330 = 303.03 → 303
        assert inf.inferred_module_count == 303


# ============================================================
# Inverter inference
# ============================================================


def _inverter(plant_key="P1", sn="SN1", rated_kw=50.0, mppt_count=None):
    return InverterConfig(
        plant_key=plant_key, inverter_sn=sn, inverter_label="",
        rated_kw=rated_kw, active=True, mppt_count=mppt_count,
    )


class TestInverterInference:
    def test_infers_mppt_for_50kw(self):
        inv = _inverter(rated_kw=50.0)
        inf = infer_inverter_physicals("P1", inv)
        assert inf.will_update_mppt_count is True
        assert inf.inferred_mppt_count == 6

    def test_skips_when_mppt_already_set(self):
        inv = _inverter(mppt_count=8)
        inf = infer_inverter_physicals("P1", inv)
        assert inf.will_update_mppt_count is False
        assert inf.existing_mppt_count == 8

    def test_zero_rated_kw_still_returns_guess(self):
        """rated_kw=0 doesn't tell us inverter size, but we still emit a
        safe placeholder rather than failing."""
        inv = _inverter(rated_kw=0.0)
        inf = infer_inverter_physicals("P1", inv)
        assert inf.will_update_mppt_count is True
        assert inf.inferred_mppt_count == 2  # default for tiny units
        assert "rated_kw=0" in inf.note
