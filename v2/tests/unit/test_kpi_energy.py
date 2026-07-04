"""Tests for argia.kpi.energy."""

from __future__ import annotations

import datetime as dt

import pytest

from argia.kpi.energy import (
    DISCREPANCY_WARN_PCT,
    REBOOT_THRESHOLD_KWH,
    EnergyDay,
    compute_inverter_energy,
    compute_plant_energy,
    sum_inverter_energies,
)
from argia.kpi.reader import InverterRow
from argia.core.time_utils import UTC


def _row(hour, etoday_kwh, status=1, sn="SN1", plant="P"):
    return InverterRow(
        timestamp_utc=dt.datetime(2026, 5, 14, hour, 0, tzinfo=UTC),
        plant_key=plant, inverter_sn=sn, inverter_label="",
        vendor="", status=status,
        power_w=None, etoday_kwh=etoday_kwh, temperature_c=None,
        fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
        cloud_cover_pct=None, ambient_temp_c=None,
    )


# ============================================================
# Happy path
# ============================================================


class TestSunnyDay:
    """Monotonically increasing etoday — the easy case."""

    def test_simple_increasing_series(self):
        rows = [_row(7, 1.0), _row(10, 50.0), _row(13, 150.0),
                _row(16, 280.0), _row(18, 320.0)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == 320.0
        assert e.energy_kwh_max == 320.0
        assert e.energy_kwh_last == 320.0
        assert not e.detected_reboot
        assert e.discrepancy_pct == 0.0
        assert e.rows_seen == 5
        assert e.rows_online == 5

    def test_offline_status_counted(self):
        rows = [_row(7, 1.0, status=1), _row(10, 50.0, status=3),
                _row(13, 150.0, status=1)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == 150.0
        assert e.rows_online == 2  # one was offline


# ============================================================
# Empty / sparse
# ============================================================


class TestEmpty:
    def test_no_rows(self):
        e = compute_inverter_energy([])
        assert e.energy_kwh is None
        assert e.rows_seen == 0
        assert not e.detected_reboot

    def test_all_etoday_none(self):
        rows = [_row(7, None), _row(10, None)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh is None
        assert e.rows_seen == 2

    def test_single_row(self):
        rows = [_row(13, 50.0)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == 50.0
        assert not e.detected_reboot


# ============================================================
# Midnight rollover (Growatt/Huawei reset etoday just past midnight,
# which can appear as the LAST row's value being 0 on the previous day)
# ============================================================


class TestMidnightRollover:
    def test_trailing_zero_ignored_by_last(self):
        """Trailing 0.0 after a real day total should not clobber the result."""
        rows = [_row(7, 1.0), _row(13, 100.0), _row(18, 280.0),
                _row(20, 0.0)]  # rollover noise
        e = compute_inverter_energy(rows)
        # last_e should be 280, not 0
        assert e.energy_kwh_last == 280.0
        assert e.energy_kwh == 280.0
        # Crossing from 280 → 0 IS a reboot-like drop, so detected_reboot=True
        # That's expected; the metric stays robust either way
        assert e.detected_reboot is True

    def test_all_zeros(self):
        rows = [_row(7, 0.0), _row(10, 0.0), _row(18, 0.0)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh_last is None  # no non-zero observation
        assert e.energy_kwh_max == 0.0
        # energy_kwh fallback to max → 0.0
        assert e.energy_kwh == 0.0
        assert not e.detected_reboot


# ============================================================
# Reboot in the middle of the day (etoday resets to 0)
# ============================================================


class TestReboot:
    """Inverter reboots at 14:00 — etoday: 1, 50, 150, 0, 30, 80, 120"""

    def _rows(self):
        return [_row(7, 1.0), _row(10, 50.0), _row(13, 150.0),
                _row(14, 0.0), _row(15, 30.0), _row(16, 80.0),
                _row(17, 120.0)]

    def test_reboot_detected(self):
        e = compute_inverter_energy(self._rows())
        assert e.detected_reboot is True

    def test_energy_uses_max_when_reboot(self):
        """When reboot detected, prefer max(150) over last(120) since
        last is post-reboot only."""
        e = compute_inverter_energy(self._rows())
        # We use max as the best-effort because last() would lie
        assert e.energy_kwh == 150.0
        assert e.energy_kwh_max == 150.0
        assert e.energy_kwh_last == 120.0

    def test_small_dip_not_a_reboot(self):
        """A 0.3 kWh dip (below REBOOT_THRESHOLD_KWH) is sensor noise, not
        a reboot — common with SolarEdge derived etoday."""
        rows = [_row(10, 50.0), _row(13, 50.3),
                _row(14, 50.0), _row(18, 280.0)]  # 0.3 dip
        e = compute_inverter_energy(rows)
        assert e.detected_reboot is False
        assert e.energy_kwh == 280.0


# ============================================================
# Discrepancy flagging
# ============================================================


class TestDiscrepancy:
    def test_no_discrepancy_when_clean(self):
        rows = [_row(7, 1.0), _row(13, 100.0), _row(18, 200.0)]
        e = compute_inverter_energy(rows)
        assert e.discrepancy_pct == 0.0

    def test_discrepancy_computed_on_reboot(self):
        """After reboot at 14:00 MX with max=150, last=120, discrepancy is 20%.

        Hours are UTC; 16/20/23 UTC = 10:00/14:00/17:00 MX — mid-day, so the
        pre-dawn carryover strip (see test_kpi_energy_carryover) stays out of
        the way and this exercises the genuine-reboot path."""
        rows = [_row(16, 150.0), _row(20, 0.0), _row(23, 120.0)]
        e = compute_inverter_energy(rows)
        assert e.discrepancy_pct is not None
        assert 19.5 < e.discrepancy_pct < 20.5

    def test_discrepancy_none_when_max_missing(self):
        e = compute_inverter_energy([])
        assert e.discrepancy_pct is None


# ============================================================
# Plant aggregation
# ============================================================


class TestPlantAggregation:
    def test_groups_by_inverter(self):
        rows = [
            _row(10, 50.0, sn="A"), _row(18, 280.0, sn="A"),
            _row(10, 60.0, sn="B"), _row(18, 320.0, sn="B"),
        ]
        result = compute_plant_energy(rows)
        assert set(result.keys()) == {"A", "B"}
        assert result["A"].energy_kwh == 280.0
        assert result["B"].energy_kwh == 320.0

    def test_sum_inverter_energies_ignores_none(self):
        per_inv = {
            "A": EnergyDay(280.0, 280.0, 280.0, 5, 5, False, 0.0),
            "B": EnergyDay(None, None, None, 0, 0, False, None),
            "C": EnergyDay(320.0, 320.0, 320.0, 5, 5, False, 0.0),
        }
        total = sum_inverter_energies(per_inv)
        assert total == 600.0

    def test_sum_returns_none_when_all_missing(self):
        per_inv = {
            "A": EnergyDay(None, None, None, 0, 0, False, None),
            "B": EnergyDay(None, None, None, 0, 0, False, None),
        }
        assert sum_inverter_energies(per_inv) is None

    def test_empty_plant_returns_empty_dict(self):
        assert compute_plant_energy([]) == {}


# ============================================================
# Edge: out-of-order input
# ============================================================


class TestRowOrdering:
    def test_handles_unsorted_input(self):
        """Caller should pass sorted rows, but we must handle scrambled too."""
        rows = [_row(18, 280.0), _row(7, 1.0), _row(13, 100.0)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == 280.0
        assert not e.detected_reboot  # reboot detection sorts internally
