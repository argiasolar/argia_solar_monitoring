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
    normalize_kpi_date_iso,
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
