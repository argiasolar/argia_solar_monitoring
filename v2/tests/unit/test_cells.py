"""Tests: shared sheet-cell coercion (argia.core.cells).

Named regression: the live Sheets API (UNFORMATTED_VALUE) returns datetime
cells as SERIAL floats; a private watchdog parser that only understood
strings/datetimes false-alarmed on a healthy sheet (2026-07-05)."""

import datetime as dt

from argia.core.cells import GOOGLE_EPOCH, coerce_date, coerce_ts


class TestCoerceTs:
    def test_serial_float_from_live_api(self):
        serial = (dt.datetime(2026, 7, 5, 13, 30) - GOOGLE_EPOCH) \
            / dt.timedelta(days=1)
        assert coerce_ts(serial) == dt.datetime(2026, 7, 5, 13, 30)

    def test_serial_int_date_only(self):
        serial = (dt.date(2026, 7, 4) - GOOGLE_EPOCH.date()).days
        assert coerce_ts(serial) == dt.datetime(2026, 7, 4)

    def test_datetime_passthrough(self):
        t = dt.datetime(2026, 7, 5, 7, 15)
        assert coerce_ts(t) is t

    def test_iso_strings(self):
        assert coerce_ts("2026-07-05 13:30:00") == dt.datetime(2026, 7, 5, 13, 30)
        assert coerce_ts("2026-07-05T13:30:00Z") == dt.datetime(2026, 7, 5, 13, 30)
        assert coerce_ts("2026-07-05") == dt.datetime(2026, 7, 5)

    def test_garbage_and_bool_rejected(self):
        assert coerce_ts("not a date") is None
        assert coerce_ts(None) is None
        assert coerce_ts(True) is None      # bool is an int subclass — guard


class TestCoerceDate:
    def test_serial_float(self):
        serial = (dt.date(2026, 7, 4) - GOOGLE_EPOCH.date()).days + 0.99
        assert coerce_date(serial) == dt.date(2026, 7, 4)

    def test_iso_string(self):
        assert coerce_date("2026-07-04") == dt.date(2026, 7, 4)
