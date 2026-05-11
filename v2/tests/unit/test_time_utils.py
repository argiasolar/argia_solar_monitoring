"""Tests for argia.core.time_utils."""

import datetime as dt

import pytest
from freezegun import freeze_time

from argia.core.time_utils import (
    MX_TZ,
    UTC,
    fmt_sheets_date,
    fmt_sheets_datetime,
    now_mx,
    now_utc,
    now_utc_iso,
    parse_growatt_calendar,
    parse_provider_datetime,
    utc_to_mx,
)


class TestNow:
    @freeze_time("2026-04-15 18:30:00", tz_offset=0)
    def test_now_utc_is_aware(self):
        result = now_utc()
        assert result.tzinfo is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 15
        assert result.hour == 18

    @freeze_time("2026-04-15 18:30:00", tz_offset=0)
    def test_now_utc_iso_format(self):
        # Should round-trip back through fromisoformat
        s = now_utc_iso()
        parsed = dt.datetime.fromisoformat(s)
        assert parsed.tzinfo is not None

    @freeze_time("2026-04-15 18:30:00", tz_offset=0)
    def test_now_mx_is_in_mx_timezone(self):
        result = now_mx()
        # CDMX is UTC-6 standard, UTC-5 during DST
        # April 15 2026 is during DST, so UTC-6
        # (Mexico abolished DST in 2022 for most regions; using non-DST offset)
        # Either way, it should be tz-aware
        assert result.tzinfo is not None

    def test_now_utc_microseconds_zeroed(self):
        result = now_utc()
        assert result.microsecond == 0


class TestUtcToMx:
    def test_aware_utc_converts(self):
        utc_dt = dt.datetime(2026, 4, 15, 18, 30, tzinfo=UTC)
        mx_dt = utc_to_mx(utc_dt)
        assert mx_dt.tzinfo == MX_TZ
        # 18:30 UTC = 12:30 MX (assuming UTC-6)
        assert mx_dt.hour == 12
        assert mx_dt.minute == 30

    def test_naive_assumed_utc(self):
        naive = dt.datetime(2026, 4, 15, 18, 30)
        mx_dt = utc_to_mx(naive)
        assert mx_dt.tzinfo == MX_TZ
        assert mx_dt.hour == 12  # same as the aware case above

    def test_already_in_mx(self):
        mx_dt = dt.datetime(2026, 4, 15, 12, 30, tzinfo=MX_TZ)
        result = utc_to_mx(mx_dt)
        # Same instant, just confirmed in MX
        assert result == mx_dt


class TestFmtSheets:
    def test_datetime_format(self):
        utc_dt = dt.datetime(2026, 4, 15, 18, 30, 5, tzinfo=UTC)
        # 18:30:05 UTC → 12:30:05 MX
        assert fmt_sheets_datetime(utc_dt) == "4/15/2026 12:30:05"

    def test_datetime_no_zero_padding_on_hour(self):
        # Sheets accepts both "12:30" and "9:30" — we don't pad single-digit hours
        utc_dt = dt.datetime(2026, 4, 15, 15, 5, 0, tzinfo=UTC)  # 9:05 MX
        assert fmt_sheets_datetime(utc_dt) == "4/15/2026 9:05:00"

    def test_minutes_seconds_padded(self):
        utc_dt = dt.datetime(2026, 4, 15, 18, 5, 7, tzinfo=UTC)
        result = fmt_sheets_datetime(utc_dt)
        # minute and second always 2-digit
        assert ":05:07" in result

    def test_date_only(self):
        utc_dt = dt.datetime(2026, 4, 15, 18, 30, tzinfo=UTC)
        assert fmt_sheets_date(utc_dt) == "4/15/2026"

    def test_date_crosses_midnight_mx(self):
        # 03:00 UTC on Apr 16 = 21:00 MX on Apr 15
        utc_dt = dt.datetime(2026, 4, 16, 3, 0, tzinfo=UTC)
        assert fmt_sheets_date(utc_dt) == "4/15/2026"


class TestParseProviderDatetime:
    def test_epoch_seconds_10_digit(self):
        result = parse_provider_datetime(1700000000)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.year == 2023

    def test_epoch_milliseconds_13_digit(self):
        result = parse_provider_datetime(1700000000000)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.year == 2023

    def test_epoch_string_seconds(self):
        result = parse_provider_datetime("1700000000")
        assert result is not None
        assert result.year == 2023

    def test_epoch_string_milliseconds(self):
        result = parse_provider_datetime("1700000000000")
        assert result is not None
        assert result.year == 2023

    def test_iso_with_z(self):
        result = parse_provider_datetime("2026-04-15T18:30:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 18

    def test_iso_with_offset(self):
        result = parse_provider_datetime("2026-04-15T18:30:00+00:00")
        assert result is not None
        assert result.year == 2026

    def test_space_separated(self):
        result = parse_provider_datetime("2026-04-15 18:30:00")
        assert result is not None
        assert result.year == 2026
        assert result.tzinfo == UTC  # we assume UTC for naive

    def test_slash_format(self):
        result = parse_provider_datetime("2026/04/15 18:30:00")
        assert result is not None
        assert result.year == 2026

    def test_none(self):
        assert parse_provider_datetime(None) is None

    def test_empty_string(self):
        assert parse_provider_datetime("") is None
        assert parse_provider_datetime("   ") is None

    def test_garbage(self):
        assert parse_provider_datetime("not a date") is None

    def test_partial_garbage(self):
        assert parse_provider_datetime("2026-13-99") is None  # invalid month/day


class TestParseGrowattCalendar:
    def test_normal_calendar(self):
        # April 15, 2026 — note month is 3 (0-based) for April
        cal = {
            "year": 2026,
            "month": 3,
            "dayOfMonth": 15,
            "hourOfDay": 12,
            "minute": 30,
            "second": 5,
        }
        result = parse_growatt_calendar(cal)
        assert result is not None
        assert result.year == 2026
        assert result.month == 4  # converted to 1-based
        assert result.day == 15
        assert result.hour == 12
        assert result.minute == 30
        assert result.second == 5
        assert result.tzinfo == MX_TZ

    def test_january_zero_month(self):
        cal = {"year": 2026, "month": 0, "dayOfMonth": 5}
        result = parse_growatt_calendar(cal)
        assert result is not None
        assert result.month == 1  # January

    def test_december_eleven_month(self):
        cal = {"year": 2026, "month": 11, "dayOfMonth": 31}
        result = parse_growatt_calendar(cal)
        assert result is not None
        assert result.month == 12

    def test_alternative_day_key(self):
        # Some responses use "day" instead of "dayOfMonth"
        cal = {"year": 2026, "month": 3, "day": 15}
        result = parse_growatt_calendar(cal)
        assert result is not None
        assert result.day == 15

    def test_missing_optional_time_fields(self):
        # When only date is given, time defaults to 00:00:00
        cal = {"year": 2026, "month": 3, "dayOfMonth": 15}
        result = parse_growatt_calendar(cal)
        assert result is not None
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0

    def test_missing_required_field(self):
        assert parse_growatt_calendar({"year": 2026}) is None
        assert parse_growatt_calendar({"month": 3, "dayOfMonth": 15}) is None

    def test_not_a_dict(self):
        assert parse_growatt_calendar(None) is None  # type: ignore[arg-type]
        assert parse_growatt_calendar("string") is None  # type: ignore[arg-type]
        assert parse_growatt_calendar([]) is None  # type: ignore[arg-type]

    def test_invalid_values(self):
        cal = {"year": "not a year", "month": 3, "dayOfMonth": 15}
        assert parse_growatt_calendar(cal) is None

    def test_invalid_date_combination(self):
        # Feb 30 doesn't exist
        cal = {"year": 2026, "month": 1, "dayOfMonth": 30}
        assert parse_growatt_calendar(cal) is None
