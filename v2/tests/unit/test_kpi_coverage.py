"""Tests for KPI_Daily coverage stamping (classify_coverage + stamp_data_class)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

from argia.archive.kpi_daily import (
    DATA_CLASS_FULL,
    DATA_CLASS_NO_DATA,
    DATA_CLASS_PARTIAL,
    DATA_COVERAGE_CUTOFF_HOUR,
    KPI_DAILY_TAB,
    classify_coverage,
    compute_availability,
    compute_expected_kwh,
    compute_production_pct,
    compute_soiling_loss_pct,
    compute_specific_yield,
    mean_cloud_cover,
    normalize_kpi_date_iso,
    stamp_column,
    stamp_data_class,
)
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ, UTC

SHEETS_EPOCH = dt.date(1899, 12, 30)


def _serial(d: dt.date) -> int:
    return (d - SHEETS_EPOCH).days


def _mx_to_utc(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=MX_TZ).astimezone(UTC)


# --------------------------------------------------------------------------
class TestClassifyCoverage:
    def test_none_is_no_data(self):
        assert classify_coverage(None) == DATA_CLASS_NO_DATA

    def test_late_sample_is_full(self):
        # Real July-1 case: last MX sample 20:42 -> full.
        assert classify_coverage(_mx_to_utc(2026, 7, 1, 20, 42)) == DATA_CLASS_FULL

    def test_early_sample_is_partial(self):
        # Real June-30 case: last MX sample 13:18 -> partial.
        assert classify_coverage(_mx_to_utc(2026, 6, 30, 13, 18)) == DATA_CLASS_PARTIAL

    def test_boundary_exactly_cutoff_is_full(self):
        h = DATA_COVERAGE_CUTOFF_HOUR
        assert classify_coverage(_mx_to_utc(2026, 7, 1, h, 0)) == DATA_CLASS_FULL

    def test_boundary_one_minute_before_cutoff_is_partial(self):
        h = DATA_COVERAGE_CUTOFF_HOUR
        assert classify_coverage(_mx_to_utc(2026, 7, 1, h - 1, 59)) == DATA_CLASS_PARTIAL

    def test_custom_cutoff(self):
        # With a 14:00 cutoff, the 13:18 sample is still partial but a 15:00 is full.
        assert classify_coverage(_mx_to_utc(2026, 6, 30, 13, 18), cutoff_hour=14) == DATA_CLASS_PARTIAL
        assert classify_coverage(_mx_to_utc(2026, 6, 30, 15, 0), cutoff_hour=14) == DATA_CLASS_FULL


# --------------------------------------------------------------------------
class TestStampDataClass:
    def _client(self, rows):
        c = MagicMock(spec=SheetsClient)
        c.read_range.return_value = rows
        return c

    # header with data_class NOT in the writer's prefix, date_iso as a SERIAL
    HEADER = ["date_iso", "plant_key", "energy_kwh", "data_class"]

    def _rows(self):
        return [
            self.HEADER,
            [_serial(dt.date(2026, 6, 30)), "SLP1", 565.2, ""],   # sheet row 2
            [_serial(dt.date(2026, 6, 30)), "GTO1", 1953.6, ""],  # sheet row 3
            [_serial(dt.date(2026, 7, 1)), "SLP1", 698.9, ""],    # sheet row 4
        ]

    def test_writes_correct_cell_with_serial_date_match(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {("2026-06-30", "SLP1"): DATA_CLASS_PARTIAL})
        assert n == 1
        # data_class is column index 3 -> 1-based col 4; SLP1/6-30 is sheet row 2
        c.write_cell.assert_called_once_with(KPI_DAILY_TAB, 2, 4, DATA_CLASS_PARTIAL)

    def test_matches_ignoring_plant_key_case_and_space(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {("2026-06-30", " slp1 "): DATA_CLASS_FULL})
        assert n == 1
        c.write_cell.assert_called_once_with(KPI_DAILY_TAB, 2, 4, DATA_CLASS_FULL)

    def test_multiple_stamps(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {
            ("2026-06-30", "SLP1"): DATA_CLASS_PARTIAL,
            ("2026-07-01", "SLP1"): DATA_CLASS_FULL,
        })
        assert n == 2
        assert c.write_cell.call_count == 2

    def test_missing_data_class_column_is_noop(self):
        c = self._client([["date_iso", "plant_key", "energy_kwh"],
                          [_serial(dt.date(2026, 6, 30)), "SLP1", 565.2]])
        n = stamp_data_class(c, {("2026-06-30", "SLP1"): DATA_CLASS_PARTIAL})
        assert n == 0
        c.write_cell.assert_not_called()

    def test_row_not_found_is_skipped(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {("2026-06-30", "NL1"): DATA_CLASS_PARTIAL})  # no NL1 row
        assert n == 0
        c.write_cell.assert_not_called()

    def test_dry_run_writes_nothing(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {("2026-06-30", "SLP1"): DATA_CLASS_PARTIAL}, dry_run=True)
        assert n == 1               # counted as "would write"
        c.write_cell.assert_not_called()

    def test_empty_stamps_does_not_even_read(self):
        c = self._client(self._rows())
        n = stamp_data_class(c, {})
        assert n == 0
        c.read_range.assert_not_called()


# --------------------------------------------------------------------------
class TestMeanCloudCover:
    def test_simple_daylight_mean(self):
        s = [(_mx_to_utc(2026, 7, 1, 10, 0), 0.4),
             (_mx_to_utc(2026, 7, 1, 14, 0), 0.6)]
        assert mean_cloud_cover(s) == 0.5

    def test_night_samples_excluded(self):
        # June-30 real case: stray 00:40 rows must not skew the mean.
        s = [(_mx_to_utc(2026, 6, 30, 0, 40), 1.0),
             (_mx_to_utc(2026, 6, 30, 12, 0), 0.3)]
        assert mean_cloud_cover(s) == 0.3

    def test_boundaries(self):
        # 06:00 counts, 20:00 does not.
        s = [(_mx_to_utc(2026, 7, 1, 6, 0), 0.2),
             (_mx_to_utc(2026, 7, 1, 20, 0), 1.0)]
        assert mean_cloud_cover(s) == 0.2

    def test_none_values_ignored(self):
        s = [(_mx_to_utc(2026, 7, 1, 10, 0), None),
             (_mx_to_utc(2026, 7, 1, 11, 0), 0.8)]
        assert mean_cloud_cover(s) == 0.8

    def test_no_usable_samples_returns_none(self):
        assert mean_cloud_cover([]) is None
        assert mean_cloud_cover([(_mx_to_utc(2026, 7, 1, 2, 0), 0.5)]) is None
        assert mean_cloud_cover([(_mx_to_utc(2026, 7, 1, 10, 0), None)]) is None

    def test_rounding(self):
        s = [(_mx_to_utc(2026, 7, 1, 10, 0), 1/3),
             (_mx_to_utc(2026, 7, 1, 11, 0), 1/3)]
        assert mean_cloud_cover(s) == 0.3333


# --------------------------------------------------------------------------
class TestStampColumnGeneric:
    HEADER = ["date_iso", "plant_key", "energy_kwh", "data_class",
              "cloud_coverage_pct"]

    def _client(self):
        c = MagicMock(spec=SheetsClient)
        c.read_range.return_value = [
            self.HEADER,
            [_serial(dt.date(2026, 7, 1)), "SLP1", 698.9, "full", ""],  # row 2
        ]
        return c

    def test_stamps_named_column_by_position(self):
        c = self._client()
        n = stamp_column(c, "cloud_coverage_pct",
                         {("2026-07-01", "SLP1"): 0.42})
        assert n == 1
        # cloud_coverage_pct is 0-based index 4 -> 1-based col 5
        c.write_cell.assert_called_once_with(KPI_DAILY_TAB, 2, 5, 0.42)

    def test_unknown_column_is_noop(self):
        c = self._client()
        n = stamp_column(c, "no_such_col", {("2026-07-01", "SLP1"): 1})
        assert n == 0
        c.write_cell.assert_not_called()

    def test_data_class_wrapper_still_works_via_generic(self):
        c = self._client()
        n = stamp_data_class(c, {("2026-07-01", "SLP1"): "partial"})
        assert n == 1
        c.write_cell.assert_called_once_with(KPI_DAILY_TAB, 2, 4, "partial")


# --------------------------------------------------------------------------
class TestComputeExpectedKwh:
    def test_matches_v1_theoretical_exactly(self):
        # v1 SLP1 2024-03-01: 189.2 kWp x 6.01 kWh/m2 x 0.73 = 830.08 (stored
        # Theoretical_kWh 830.0771...). Same formula, same result.
        assert compute_expected_kwh(189.2, 6.01, 0.73) == 830.08

    def test_nl1_uses_bifacial_factor(self):
        # NL1 expected_factor 0.78 (bifacial premium), 617.4 kWp, 7.0 kWh/m2.
        assert compute_expected_kwh(617.4, 7.0, 0.78) == 3371.0

    def test_missing_inputs_return_none(self):
        assert compute_expected_kwh(None, 6.0, 0.73) is None
        assert compute_expected_kwh(189.2, None, 0.73) is None
        assert compute_expected_kwh(189.2, 6.0, None) is None

    def test_nonpositive_inputs_return_none(self):
        assert compute_expected_kwh(0, 6.0, 0.73) is None
        assert compute_expected_kwh(189.2, 0, 0.73) is None
        assert compute_expected_kwh(189.2, -1.0, 0.73) is None
        assert compute_expected_kwh(189.2, 6.0, 0) is None

    def test_rounded_to_2dp(self):
        v = compute_expected_kwh(100.0, 3.333, 0.73)
        assert v == round(100.0 * 3.333 * 0.73, 2)


# --------------------------------------------------------------------------
class TestComputeAvailability:
    SNS = ["INV1", "INV2", "INV3"]

    def _s(self, h, m, sn, status=1):
        return (_mx_to_utc(2026, 7, 1, h, m), sn, status)

    def test_all_online_all_slots(self):
        s = [self._s(10, 0, sn) for sn in self.SNS] + \
            [self._s(11, 0, sn) for sn in self.SNS]
        assert compute_availability(s, self.SNS) == 1.0

    def test_dead_inverter_missing_rows_drags_average(self):
        # INV3 never reports at all -> 2 of 3 fully available = 0.6667.
        s = [self._s(10, 0, "INV1"), self._s(10, 0, "INV2"),
             self._s(11, 0, "INV1"), self._s(11, 0, "INV2")]
        assert compute_availability(s, self.SNS) == 0.6667

    def test_status_3_counts_unavailable(self):
        # GTO1-style FAULT: row present but status=3.
        s = [self._s(10, 0, "INV1", 1), self._s(10, 0, "INV2", 3)]
        assert compute_availability(s, ["INV1", "INV2"]) == 0.5

    def test_online_but_zero_power_is_still_available(self):
        # Semantics: availability is uptime; 0W-online is #4's problem.
        s = [self._s(10, 0, "INV1", 1)]  # status carries no power info here
        assert compute_availability(s, ["INV1"]) == 1.0

    def test_partial_day_recovery(self):
        # INV2 offline in the morning slot, online in the afternoon: 0.5;
        # INV1 online both: 1.0 -> plant 0.75.
        s = [self._s(9, 0, "INV1", 1), self._s(9, 0, "INV2", 3),
             self._s(15, 0, "INV1", 1), self._s(15, 0, "INV2", 1)]
        assert compute_availability(s, ["INV1", "INV2"]) == 0.75

    def test_night_slots_excluded(self):
        s = [(_mx_to_utc(2026, 6, 30, 0, 40), "INV1", 1),
             self._s(12, 0, "INV1", 1)]
        assert compute_availability(s, ["INV1"]) == 1.0

    def test_same_poll_minutes_apart_is_one_slot(self):
        # Regression for the real GTO1 artifact: one poll's device timestamps
        # spread 10:55:35 -> 10:59:35. Gap-clustering must keep them ONE slot,
        # so both inverters are fully available (minute-keying faked 0.5).
        a = (_mx_to_utc(2026, 7, 2, 10, 55).replace(second=35), "INV1", 1)
        b = (_mx_to_utc(2026, 7, 2, 10, 59).replace(second=35), "INV2", 1)
        assert compute_availability([a, b], ["INV1", "INV2"]) == 1.0

    def test_gap_larger_than_threshold_starts_new_slot(self):
        # Two polls 60 min apart: INV2 only in the first -> 0.5 for INV2.
        s = [self._s(10, 0, "INV1", 1), self._s(10, 0, "INV2", 1),
             self._s(11, 0, "INV1", 1)]
        assert compute_availability(s, ["INV1", "INV2"]) == 0.75

    def test_no_slots_or_no_expected_returns_none(self):
        assert compute_availability([], ["INV1"]) is None
        assert compute_availability([self._s(10, 0, "INV1")], []) is None
        night = [(_mx_to_utc(2026, 7, 1, 2, 0), "INV1", 1)]
        assert compute_availability(night, ["INV1"]) is None

    def test_sn_whitespace_normalized(self):
        s = [self._s(10, 0, "INV1 ", 1)]
        assert compute_availability(s, [" INV1"]) == 1.0


# --------------------------------------------------------------------------
class TestComputeSpecificYield:
    def test_real_slp1_day(self):
        # SLP1 2026-07-01: 698.9 kWh / 189.2 kWp = 3.6939 kWh/kWp.
        assert compute_specific_yield(698.9, 189.2) == 3.694

    def test_zero_energy_is_valid_zero_yield(self):
        assert compute_specific_yield(0.0, 189.2) == 0.0

    def test_missing_inputs_none(self):
        assert compute_specific_yield(None, 189.2) is None
        assert compute_specific_yield(100.0, None) is None
        assert compute_specific_yield(100.0, 0) is None
        assert compute_specific_yield(-1.0, 189.2) is None


# --------------------------------------------------------------------------
class TestComputeProductionPct:
    def test_real_july2_values_match_alert_ratios(self):
        # Must equal what energy_daily_pct alerts computed for the same day.
        assert compute_production_pct(3732.0, 4978.0) == 0.7497   # GTO1
        assert compute_production_pct(2964.0, 3809.0) == 0.7782   # NL1

    def test_over_100_pct_allowed(self):
        assert compute_production_pct(1100.0, 1000.0) == 1.1

    def test_missing_or_zero_inputs_none(self):
        assert compute_production_pct(None, 1000.0) is None
        assert compute_production_pct(500.0, None) is None
        assert compute_production_pct(500.0, 0) is None
        assert compute_production_pct(-1.0, 1000.0) is None


class TestComputeSoilingLossPct:
    def test_typical_drift(self):
        # SLP1 baseline 0.82, today 0.75 -> 8.5% soiling estimate.
        assert compute_soiling_loss_pct(0.75, 0.82) == 0.0854

    def test_cleaner_than_baseline_is_zero_not_negative(self):
        assert compute_soiling_loss_pct(0.85, 0.82) == 0.0

    def test_implausible_pr_none(self):
        # Sparse-irradiance artifact (PR 1.23 seen on real days) -> None.
        assert compute_soiling_loss_pct(1.23, 0.82) is None
        assert compute_soiling_loss_pct(0.0, 0.82) is None

    def test_missing_baseline_none(self):
        assert compute_soiling_loss_pct(0.75, None) is None
        assert compute_soiling_loss_pct(0.75, 0) is None


# --------------------------------------------------------------------------
class TestNormalizeDateIso:
    def _client(self, rows):
        c = MagicMock(spec=SheetsClient)
        c.read_range.return_value = rows
        return c

    # date_iso at col A; a real-date row reads back as a serial (number), a
    # text-date row reads back as a string.
    def _rows(self):
        return [
            ["date_iso", "plant_key", "energy_kwh"],
            [_serial(dt.date(2026, 6, 30)), "SLP1", 565.2],   # real date -> skip
            ["2026-07-02", "SLP1", 500.0],                    # TEXT -> fix (row 3)
            ["2026-07-02", "SLP2", 900.0],                    # TEXT -> fix (row 4)
        ]

    def test_converts_only_text_dates(self):
        c = self._client(self._rows())
        r = normalize_kpi_date_iso(c, dry_run=False)
        assert r == {"scanned": 3, "text_dates": 2, "fixed": 2}
        # two writes, both USER_ENTERED, into column A (col 1), rows 3 and 4
        assert c.write_cell.call_count == 2
        for call in c.write_cell.call_args_list:
            args, kwargs = call
            assert args[0] == KPI_DAILY_TAB
            assert args[2] == 1  # column A
            assert kwargs.get("value_input_option") == "USER_ENTERED"
        written_rows = {call.args[1] for call in c.write_cell.call_args_list}
        assert written_rows == {3, 4}

    def test_writes_canonical_iso_date(self):
        c = self._client([
            ["date_iso", "plant_key"],
            ["7/2/2026", "SLP1"],   # US-format text -> canonicalized
        ])
        normalize_kpi_date_iso(c, dry_run=False)
        args, _ = c.write_cell.call_args
        assert args[3] == "2026-07-02"

    def test_dry_run_writes_nothing(self):
        c = self._client(self._rows())
        r = normalize_kpi_date_iso(c, dry_run=True)
        assert r["fixed"] == 2          # counted as would-fix
        c.write_cell.assert_not_called()

    def test_all_real_dates_is_noop(self):
        c = self._client([
            ["date_iso", "plant_key"],
            [_serial(dt.date(2026, 6, 30)), "SLP1"],
            [_serial(dt.date(2026, 7, 1)), "SLP2"],
        ])
        r = normalize_kpi_date_iso(c, dry_run=False)
        assert r == {"scanned": 2, "text_dates": 0, "fixed": 2 - 2}
        c.write_cell.assert_not_called()

    def test_unparseable_text_is_skipped(self):
        c = self._client([
            ["date_iso", "plant_key"],
            ["not-a-date", "SLP1"],
        ])
        r = normalize_kpi_date_iso(c, dry_run=False)
        assert r["text_dates"] == 1 and r["fixed"] == 0
        c.write_cell.assert_not_called()

    def test_missing_column_is_noop(self):
        c = self._client([["plant_key", "energy_kwh"], ["SLP1", 1.0]])
        r = normalize_kpi_date_iso(c, dry_run=False)
        assert r["fixed"] == 0
        c.write_cell.assert_not_called()
