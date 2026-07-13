"""Telemetry retention pure-logic tests (v95).

The two invariants under test: the prune set is a contiguous oldest-first
prefix, and it never crosses a day that KPI_Daily hasn't fully stamped
(the interlock that keeps past-month reports reproducible).
"""

import datetime as dt

import pytest

from argia.telemetry.retention import (
    keep_from_date, mx_date_of, plan_prune, rows_to_csv,
    stamped_dates_from_kpi,
)


class TestKeepFromDate:
    def test_ten_day_window_inclusive_of_today(self):
        # window=10 on the 12th keeps the 3rd..12th
        assert keep_from_date(dt.date(2026, 7, 12), 10) == dt.date(2026, 7, 3)

    def test_window_of_one_keeps_only_today(self):
        assert keep_from_date(dt.date(2026, 7, 12), 1) == dt.date(2026, 7, 12)

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            keep_from_date(dt.date(2026, 7, 12), 0)


class TestMxDateOf:
    def test_utc_maps_to_mx_calendar_day(self):
        # 04:00Z = 22:00 previous day in MX (UTC-6)
        assert mx_date_of("2026-07-03T04:00:00+00:00") == dt.date(2026, 7, 2)

    def test_daytime_same_day(self):
        assert mx_date_of("2026-07-03T18:00:00+00:00") == dt.date(2026, 7, 3)

    def test_z_suffix(self):
        assert mx_date_of("2026-07-03T18:00:00Z") == dt.date(2026, 7, 3)

    def test_naive_treated_as_utc(self):
        assert mx_date_of("2026-07-03 18:00:00") == dt.date(2026, 7, 3)

    def test_unparseable_is_none(self):
        assert mx_date_of("not-a-date") is None
        assert mx_date_of("") is None


class TestStampedDatesFromKpi:
    HEADER = ["date_iso", "plant_key", "energy_kwh", "data_class"]

    def test_only_full_days_counted(self):
        rows = [self.HEADER,
                ["2026-07-01", "MEX2", "100", "full"],
                ["2026-07-02", "MEX2", "50", "partial"],   # excluded
                ["2026-07-03", "MEX2", "0", "full"]]
        got = stamped_dates_from_kpi(rows)
        assert got["MEX2"] == {"2026-07-01", "2026-07-03"}

    def test_per_plant(self):
        rows = [self.HEADER,
                ["2026-07-01", "MEX2", "100", "full"],
                ["2026-07-01", "GTO1", "80", "full"]]
        got = stamped_dates_from_kpi(rows)
        assert got["MEX2"] == {"2026-07-01"}
        assert got["GTO1"] == {"2026-07-01"}

    def test_empty(self):
        assert stamped_dates_from_kpi([]) == {}

    def test_missing_data_class_defaults_full(self):
        rows = [["date_iso", "plant_key"], ["2026-07-01", "MEX2"]]
        assert stamped_dates_from_kpi(rows)["MEX2"] == {"2026-07-01"}


def _dr(*items):
    return [(dt.date.fromisoformat(d), row) for d, row in items]


class TestPlanPrune:
    KEEP = dt.date(2026, 7, 3)

    def test_prunes_old_stamped_stops_at_recent(self):
        rows = _dr(("2026-06-30", ["a"]), ("2026-07-01", ["b"]),
                   ("2026-07-03", ["c"]), ("2026-07-12", ["d"]))
        p = plan_prune(rows, self.KEEP, {"2026-06-30", "2026-07-01"})
        assert p.n_prune == 2
        assert p.stop_reason == "recent"
        assert set(p.rows_by_day) == {"2026-06-30", "2026-07-01"}

    def test_interlock_stops_at_unstamped_old_day(self):
        # Jul-1 is old but NOT stamped → stop there, keep it and after
        rows = _dr(("2026-06-30", ["a"]), ("2026-07-01", ["b"]),
                   ("2026-07-02", ["c"]))
        p = plan_prune(rows, self.KEEP, {"2026-06-30"})
        assert p.n_prune == 1
        assert p.stop_reason == "unstamped"
        assert set(p.rows_by_day) == {"2026-06-30"}

    def test_window_only_ignores_interlock(self):
        rows = _dr(("2026-06-30", ["a"]), ("2026-07-01", ["b"]),
                   ("2026-07-03", ["c"]))
        p = plan_prune(rows, self.KEEP, None)   # window-only
        assert p.n_prune == 2

    def test_all_recent_prunes_nothing(self):
        rows = _dr(("2026-07-05", ["a"]), ("2026-07-12", ["b"]))
        p = plan_prune(rows, self.KEEP, {"2026-07-05"})
        assert p.n_prune == 0
        assert p.stop_reason == "recent"

    def test_groups_multiple_rows_per_day(self):
        rows = _dr(("2026-06-30", ["a"]), ("2026-06-30", ["b"]),
                   ("2026-07-12", ["c"]))
        p = plan_prune(rows, self.KEEP, {"2026-06-30"})
        assert p.n_prune == 2
        assert len(p.rows_by_day["2026-06-30"]) == 2


class TestRowsToCsv:
    def test_header_and_rows(self):
        csv = rows_to_csv(["ts", "kw"], [["2026-07-01T00:00Z", 5],
                                         ["2026-07-01T00:05Z", None]])
        lines = csv.strip().split("\r\n")
        assert lines[0] == "ts,kw"
        assert lines[1] == "2026-07-01T00:00Z,5"
        assert lines[2] == "2026-07-01T00:05Z,"    # None → empty
