"""Tests for scripts/infer_plant_specs.py — pure inference logic only."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from infer_plant_specs import (
    MIN_DAYS_FOR_INFERENCE,
    MIN_OBSERVABLE_KW,
    STANDARD_INVERTER_SIZES_KW,
    infer_inverter_specs,
    infer_plant_specs,
    snap_to_standard_size,
)

from argia.core.config import PlantConfig
from argia.core.time_utils import UTC
from argia.kpi.reader import InverterRow


# ============================================================
# Snap function
# ============================================================


class TestSnap:
    def test_exact_match(self):
        assert snap_to_standard_size(50.0) == 50

    def test_rounds_up_slightly_over(self):
        assert snap_to_standard_size(47.3) == 50
        assert snap_to_standard_size(8.2) == 10
        assert snap_to_standard_size(102.0) == 110

    def test_rounds_up_just_below(self):
        assert snap_to_standard_size(49.9) == 50

    def test_just_above_largest_returns_largest(self):
        assert snap_to_standard_size(260) == 250

    def test_below_min_returns_zero(self):
        assert snap_to_standard_size(0.5) == 0
        assert snap_to_standard_size(0) == 0

    def test_three_kw_residential(self):
        assert snap_to_standard_size(2.5) == 3
        assert snap_to_standard_size(2.9) == 3

    def test_typical_string_inverter(self):
        # SolarEdge SE100K — typical observation might peak around 85-95 kW
        assert snap_to_standard_size(85.0) == 100
        # Huawei SUN2000-100KTL — same
        assert snap_to_standard_size(96.0) == 100

    def test_growatt_max_ranges(self):
        # Growatt MAX 50-100 family
        assert snap_to_standard_size(48.0) == 50
        assert snap_to_standard_size(73.0) == 75


# ============================================================
# Inverter inference
# ============================================================


def _row(plant_key, sn, hour, power_w, day=14):
    return InverterRow(
        timestamp_utc=dt.datetime(2026, 5, day, hour, 0, tzinfo=UTC),
        plant_key=plant_key, inverter_sn=sn, inverter_label="",
        vendor="", status=1,
        power_w=power_w, etoday_kwh=None, temperature_c=None,
        fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
        cloud_cover_pct=None, ambient_temp_c=None,
    )


class TestInferInverterBasic:
    def test_skips_inverter_already_filled(self):
        """Existing non-zero rated_kw must NEVER be overwritten."""
        rows = [_row("P1", "SN1", 13, 50000.0, day=14),
                _row("P1", "SN1", 14, 48000.0, day=15)]
        portfolio = {("P1", "SN1"): 100.0}  # already filled
        out = infer_inverter_specs(rows, portfolio)
        assert len(out) == 1
        assert out[0].will_update is False
        assert "not overwriting" in out[0].note

    def test_infers_from_peak_across_days(self):
        """Peak across multiple days should be used."""
        rows = [
            _row("P1", "SN1", 13, 40000.0, day=14),  # day 1: 40 kW peak
            _row("P1", "SN1", 13, 48000.0, day=15),  # day 2: 48 kW peak
        ]
        portfolio = {("P1", "SN1"): 0.0}
        out = infer_inverter_specs(rows, portfolio)
        assert len(out) == 1
        assert out[0].will_update is True
        assert out[0].observed_peak_kw == 48.0
        assert out[0].inferred_rated_kw == 50  # snapped up

    def test_skips_when_single_day_only(self):
        rows = [_row("P1", "SN1", 13, 50000.0, day=14)]  # only 1 day
        portfolio = {("P1", "SN1"): 0.0}
        out = infer_inverter_specs(rows, portfolio)
        assert out[0].will_update is False
        assert "1 day" in out[0].note

    def test_skips_when_peak_below_threshold(self):
        rows = [
            _row("P1", "SN1", 12, 500.0, day=14),  # 0.5 kW peak
            _row("P1", "SN1", 13, 300.0, day=15),
        ]
        portfolio = {("P1", "SN1"): 0.0}
        out = infer_inverter_specs(rows, portfolio)
        assert out[0].will_update is False
        assert "below threshold" in out[0].note

    def test_skips_inverter_with_no_data(self):
        """Inverter in portfolio but not in telemetry — must appear in output
        with skip note, not crash."""
        rows = []
        portfolio = {("P1", "SN1"): 0.0}
        out = infer_inverter_specs(rows, portfolio)
        assert len(out) == 1
        assert out[0].will_update is False
        assert "no telemetry" in out[0].note


class TestInferInverterNote:
    def test_warns_when_peak_is_unusually_low_pct(self):
        """If observed peak is <60% of snapped size, we might be under-rating."""
        # Snap of 6 kW with peak only 3.5 kW → ratio 0.58 → warning
        rows = [
            _row("P1", "SN1", 13, 3500.0, day=14),  # 3.5 kW
            _row("P1", "SN1", 13, 3500.0, day=15),
        ]
        portfolio = {("P1", "SN1"): 0.0}
        out = infer_inverter_specs(rows, portfolio)
        # 3.5 → snap to 4 kW. Ratio 3.5/4 = 0.875 → no warning
        # Let's check a case that WOULD warn
        rows = [
            _row("P1", "SN1", 13, 2500.0, day=14),  # 2.5 kW
            _row("P1", "SN1", 13, 2500.0, day=15),
        ]
        out = infer_inverter_specs(rows, portfolio)
        # snap(2.5) = 3, ratio = 0.83 → still no warning
        # Let's get a clear case: 1.5 kW peak → snaps to 3 → ratio 0.5
        rows = [
            _row("P1", "SN1", 13, 1500.0, day=14),
            _row("P1", "SN1", 13, 1500.0, day=15),
        ]
        out = infer_inverter_specs(rows, portfolio)
        assert out[0].inferred_rated_kw == 3
        assert "<60%" in out[0].note


class TestInferInverterIdempotency:
    def test_re_running_with_existing_value_preserves_it(self):
        """The whole point of this design: re-running as you backfill real
        installer data is safe."""
        rows = [
            _row("P1", "SN1", 13, 48000.0, day=14),
            _row("P1", "SN1", 13, 48000.0, day=15),
        ]
        # Hand-entered real value
        portfolio = {("P1", "SN1"): 60.0}
        out = infer_inverter_specs(rows, portfolio)
        assert out[0].will_update is False
        assert out[0].existing_rated_kw == 60.0


# ============================================================
# Plant inference
# ============================================================


def _plant(plant_key, kwp_ac=0.0, kwp_dc=0.0):
    return PlantConfig(
        plant_key=plant_key, customer="", brand="GROWATT", site_id="",
        kwp_dc=kwp_dc, kwp_ac=kwp_ac, lat=None, lon=None,
        expected_factor=0.0, pr_target=0.0, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True,
    )


def _inv_inf(plant_key, sn, inferred=0, existing=0.0, will_update=False):
    from infer_plant_specs import InverterInference
    return InverterInference(
        plant_key=plant_key, inverter_sn=sn,
        rows_seen=10, days_seen=5,
        observed_peak_kw=float(inferred) * 0.95 if inferred else None,
        inferred_rated_kw=inferred,
        existing_rated_kw=existing,
        will_update=will_update,
        note="",
    )


class TestInferPlant:
    def test_sums_inferred_values(self):
        inv = [
            _inv_inf("P1", "SN1", inferred=50, will_update=True),
            _inv_inf("P1", "SN2", inferred=50, will_update=True),
        ]
        out = infer_plant_specs(inv, {"P1": _plant("P1")}, dc_ac_ratio=1.20)
        assert len(out) == 1
        assert out[0].summed_rated_kw_ac == 100
        assert out[0].inferred_kwp_ac == 100.0
        assert out[0].inferred_kwp_dc == 120.0
        assert out[0].will_update_ac is True
        assert out[0].will_update_dc is True

    def test_uses_existing_values_when_set(self):
        """If an inverter already has rated_kw, use that in the sum
        (not the inferred value)."""
        inv = [
            _inv_inf("P1", "SN1", existing=60.0, will_update=False),
            _inv_inf("P1", "SN2", inferred=50, will_update=True),
        ]
        out = infer_plant_specs(inv, {"P1": _plant("P1")}, dc_ac_ratio=1.20)
        assert out[0].summed_rated_kw_ac == 110  # 60 + 50

    def test_does_not_overwrite_plant_kwp_ac(self):
        """If plant already has kwp_ac > 0, don't update."""
        inv = [
            _inv_inf("P1", "SN1", inferred=50, will_update=True),
            _inv_inf("P1", "SN2", inferred=50, will_update=True),
        ]
        plants = {"P1": _plant("P1", kwp_ac=95.0)}
        out = infer_plant_specs(inv, plants, dc_ac_ratio=1.20)
        assert out[0].will_update_ac is False
        assert out[0].existing_kwp_ac == 95.0

    def test_does_not_update_when_incomplete(self):
        """If any inverter has 0 rated_kw, plant total is incomplete →
        don't update."""
        inv = [
            _inv_inf("P1", "SN1", inferred=50, will_update=True),
            _inv_inf("P1", "SN2", inferred=0, will_update=False),  # no data
        ]
        out = infer_plant_specs(inv, {"P1": _plant("P1")}, dc_ac_ratio=1.20)
        assert out[0].will_update_ac is False
        assert "incomplete" in out[0].note

    def test_dc_ac_ratio_applied(self):
        inv = [_inv_inf("P1", "SN1", inferred=100, will_update=True)]
        out = infer_plant_specs(inv, {"P1": _plant("P1")}, dc_ac_ratio=1.25)
        assert out[0].inferred_kwp_dc == 125.0

    def test_keeps_existing_dc_when_set(self):
        """Plant has kwp_dc=130 already set, even if our inference would
        come up with 120. Keep the user's value."""
        inv = [
            _inv_inf("P1", "SN1", inferred=50, will_update=True),
            _inv_inf("P1", "SN2", inferred=50, will_update=True),
        ]
        plants = {"P1": _plant("P1", kwp_dc=130.0)}
        out = infer_plant_specs(inv, plants, dc_ac_ratio=1.20)
        assert out[0].will_update_dc is False
        assert out[0].existing_kwp_dc == 130.0


# ============================================================
# Boundary: snap covers the standard sizes
# ============================================================


class TestStandardSizesCoverage:
    def test_all_standard_sizes_snap_to_themselves(self):
        """Each standard size should snap to itself exactly."""
        for size in STANDARD_INVERTER_SIZES_KW:
            assert snap_to_standard_size(float(size)) == size

    def test_min_observable_threshold(self):
        """At MIN_OBSERVABLE_KW, snap returns a real size; below it
        returns 0."""
        assert snap_to_standard_size(MIN_OBSERVABLE_KW) > 0
        assert snap_to_standard_size(MIN_OBSERVABLE_KW - 0.01) == 0
