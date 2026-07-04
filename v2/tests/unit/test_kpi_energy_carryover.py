"""Regression tests: midnight etoday carryover (2026-07-03 incident).

Growatt resets etoday at inverter WAKE, not at midnight. A poll shortly
after midnight can still carry the PREVIOUS day's total; max()-based day
energy then reports yesterday's number as today's.

Real incident, 2026-07-03 (verified against v1 DailyData):
  SLP1 JNM7DY306G  00:04 etoday=502.4 (Jul 2 counter) ... day ends 434.9
                   -> KPI said 502.4, true 434.9  (+67.5 kWh inflated)
  GTO1 JFM7DXN00U  00:02 etoday=737.4 (Jul 2 counter) ... day ends 562.2
                   -> KPI said 737.4, true 562.2  (+175.2 kWh inflated)

The same stale rows made the dashboard's cumulative-diff baseline start at
yesterday's total, zeroing those inverters' whole day (opposite error).

Fix: find_carryover_cut() strips a leading FLAT etoday segment that then
resets. Real mid-day reboots (growth BEFORE the drop) must stay untouched.
"""

import datetime as dt

import pytest

from argia.kpi.energy import (
    CARRYOVER_MIN_KWH,
    compute_inverter_energy,
    find_carryover_cut,
)
from argia.kpi.reader import InverterRow

UTC = dt.timezone.utc


def _row(hh, mm, etoday, status=1, sn="SN1", plant="P"):
    """hh:mm are MX WALL-CLOCK times (as in the incident log); stored as the
    equivalent aware-UTC instant, exactly like the reader produces."""
    mx = dt.datetime(2026, 7, 3, hh, mm, tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    return InverterRow(
        timestamp_utc=mx.astimezone(UTC),
        plant_key=plant, inverter_sn=sn, inverter_label="",
        vendor="", status=status,
        power_w=None, etoday_kwh=etoday, temperature_c=None,
        fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
        cloud_cover_pct=None, ambient_temp_c=None,
    )


# --- find_carryover_cut unit behavior ---------------------------------------

class TestFindCarryoverCut:
    def test_no_carryover_clean_day(self):
        assert find_carryover_cut([0.0, 1.2, 50.0, 150.0, 320.0]) == 0

    def test_single_stale_leading_row(self):
        assert find_carryover_cut([502.4, 0.1, 11.8, 434.9]) == 1

    def test_multiple_flat_stale_rows(self):
        assert find_carryover_cut([737.4, 737.4, 737.4, 0.9, 59.9, 562.2]) == 3

    def test_hour_guard_blocks_midday_flat_then_reset(self):
        """Flat-then-reset at 10:00 local = sparse-poll REBOOT, not carryover."""
        assert find_carryover_cut([150.0, 0.0, 120.0], [10, 14, 17]) == 0

    def test_hour_guard_allows_predawn_strip(self):
        assert find_carryover_cut([502.4, 0.1, 434.9], [0, 6, 19]) == 1

    def test_hour_guard_unknown_hour_is_conservative(self):
        assert find_carryover_cut([502.4, 0.1, 434.9], [None, 6, 19]) == 0

    def test_real_midday_reboot_not_matched(self):
        # growth before the drop => reboot, NOT carryover
        assert find_carryover_cut([1.2, 3.8, 7.5, 0.0, 1.1, 2.3]) == 0

    def test_leading_zero_rows_not_stripped(self):
        assert find_carryover_cut([0.0, 0.0, 1.2, 50.0]) == 0

    def test_tiny_leading_value_below_min_not_stripped(self):
        assert find_carryover_cut([CARRYOVER_MIN_KWH / 2, 0.0, 5.0]) == 0

    def test_flat_all_day_untouched(self):
        assert find_carryover_cut([483.0, 483.0, 483.0]) == 0

    def test_none_values_ignored(self):
        assert find_carryover_cut([None, 502.4, None, 0.1, 434.9]) == 3

    def test_short_series(self):
        assert find_carryover_cut([]) == 0
        assert find_carryover_cut([502.4]) == 0


# --- regression: the real 2026-07-03 series ---------------------------------

# SLP1 JNM7DY306G — verbatim from Telemetry_Argia
SLP1_306G = [
    (0, 4, 502.4), (6, 48, 0.1), (8, 58, 11.8), (10, 36, 59.9),
    (11, 56, 146.0), (13, 40, 278.2), (14, 40, 335.6), (15, 41, 365.7),
    (16, 41, 403.4), (17, 41, 422.5), (19, 16, 434.8), (20, 20, 434.9),
    (23, 56, 434.9),
]

# GTO1 JFM7DXN00U — verbatim from Telemetry_Argia
GTO1_00U = [
    (0, 2, 737.4), (6, 50, 0.9), (8, 55, 59.9), (10, 36, 192.6),
    (11, 56, 309.2), (13, 41, 398.5), (14, 41, 466.3), (15, 41, 522.8),
    (16, 41, 547.7), (17, 41, 555.8), (19, 16, 562.0), (20, 21, 562.2),
    (23, 57, 562.2),
]


class TestJul3RegressionKPI:
    def test_slp1_306g_reports_true_total_not_yesterdays(self):
        rows = [_row(h, m, e, sn="JNM7DY306G", plant="SLP1")
                for h, m, e in SLP1_306G]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == pytest.approx(434.9)      # was 502.4 before fix
        assert e.carryover_rows_dropped == 1
        assert e.detected_reboot is False                # was misdiagnosed True

    def test_gto1_00u_reports_true_total_not_yesterdays(self):
        rows = [_row(h, m, e, sn="JFM7DXN00U", plant="GTO1")
                for h, m, e in GTO1_00U]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == pytest.approx(562.2)      # was 737.4 before fix
        assert e.carryover_rows_dropped == 1
        assert e.detected_reboot is False

    def test_unsorted_input_still_stripped(self):
        rows = [_row(h, m, e, sn="JNM7DY306G", plant="SLP1")
                for h, m, e in reversed(SLP1_306G)]
        e = compute_inverter_energy(rows)
        assert e.energy_kwh == pytest.approx(434.9)

    def test_real_reboot_still_uses_max(self):
        """The genuine-reboot path must be unchanged by the carryover fix."""
        rows = [_row(7, 0, 1.2), _row(9, 0, 3.8), _row(11, 0, 7.5),
                _row(13, 0, 0.0), _row(15, 0, 1.1), _row(17, 0, 2.3)]
        e = compute_inverter_energy(rows)
        assert e.detected_reboot is True
        assert e.energy_kwh == 7.5
        assert e.carryover_rows_dropped == 0
