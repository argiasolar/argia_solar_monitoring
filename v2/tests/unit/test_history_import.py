"""v285 - vendor history import (argia.recon.history_import), pure parts.

Tomasz 2026-10-01: load the SMS plant's Growatt history so it "matches the
total we have currently in Growatt". The numbers below are synthetic (public
repo); the shapes copy what Growatt returned for that plant: a gap the logger
missed spread as equal days that the month total does not contain, and a month
whose days add up to less than the month total.
"""
from __future__ import annotations

import datetime as dt

import pytest

from argia.recon import history_import as H
from argia.vendors.growatt_web_parser import parse_energy_series


class TestFillRuns:
    def test_equal_neighbours_are_a_fill(self):
        assert H.fill_run_indices([500, 77.0, 77.0, 77.1, 610]) == {1, 2, 3}

    def test_two_alike_days_or_smooth_real_days_are_not(self):
        assert H.fill_run_indices([500, 656.4, 656.5, 610]) == set()
        assert H.fill_run_indices([413.7, 415.8, 416.0, 415.9, 415.0]) == set()

    def test_zeros_and_lone_days_are_not(self):
        assert H.fill_run_indices([0, 0, 0, 500, 600]) == set()
        assert H.fill_run_indices([500]) == set()
        assert H.fill_run_indices([]) == set()


class TestAllocateMonth:
    def test_gap_fill_days_take_the_difference(self):
        # like April 2025: 9 days of 77.0 that the month total does not contain
        days = [0, 0, 600, 700] + [77.0] * 9 + [650, 640]
        total = 600 + 700 + 650 + 640 + 2.1
        out = H.allocate_month(days, total)
        assert sum(out) == pytest.approx(total, abs=0.0011)
        assert out[2:4] == [600, 700] and out[-2:] == [650, 640]     # measured days untouched
        assert all(o == pytest.approx(2.1 / 9, abs=0.005) for o in out[4:13])

    def test_no_fill_days_scale_in_proportion(self):
        days = [400.0, 500.0, 100.0]
        out = H.allocate_month(days, 1100.0)
        assert sum(out) == pytest.approx(1100.0, abs=0.0011)
        assert out[0] / out[1] == pytest.approx(0.8, rel=1e-3)

    def test_month_adds_up_exactly_after_rounding(self):
        out = H.allocate_month([1 / 3] * 30, 12.3456)
        assert sum(out) == pytest.approx(12.3456, abs=0.0011)

    def test_no_total_leaves_days_alone(self):
        assert H.allocate_month([1.23456, 2.0], None) == [1.235, 2.0]

    def test_never_negative(self):
        # the difference is bigger than the fill days hold - scale everything instead
        out = H.allocate_month([300.0, 10.0, 10.0, 300.0], 400.0)
        assert min(out) >= 0 and sum(out) == pytest.approx(400.0, abs=0.0011)

    def test_empty_month_with_a_total_spreads_evenly(self):
        assert H.allocate_month([0, 0], 10.0) == [5.0, 5.0]


class TestTargets:
    MD = {"2025-04": [0, 0, 0, 0, 68.4] + [500.0] * 25, "2025-05": [600.0] * 31}
    MT = {"2025-04": 12568.4, "2025-05": 18000.0}

    def test_range_starting_after_zero_days_still_reconciles(self):
        t = H.targets_for_range(self.MD, self.MT, dt.date(2025, 4, 5), dt.date(2025, 5, 31))
        assert min(t) == "2025-04-05" and max(t) == "2025-05-31" and len(t) == 57
        apr = sum(v for d, v in t.items() if d.startswith("2025-04"))
        assert apr == pytest.approx(12568.4, abs=0.002)
        assert sum(v for d, v in t.items() if d.startswith("2025-05")) == pytest.approx(18000.0, abs=0.002)

    def test_partial_month_with_production_outside_is_not_reconciled(self):
        t = H.targets_for_range(self.MD, self.MT, dt.date(2025, 5, 10), dt.date(2025, 5, 31))
        assert len(t) == 22 and all(v == 600.0 for v in t.values())


class TestPlanAndSql:
    def test_actions(self):
        ch = H.plan_changes({"2026-07-01": 10.0, "2026-07-02": 20.0, "2026-07-03": 30.0, "2026-07-04": 5.0},
                            {"2026-07-02": 20.0004, "2026-07-03": 0.0, "2026-07-04": None})
        assert [c.action for c in ch] == ["NEW", "SAME", "UPDATE", "UPDATE"]

    def test_a_day_a_few_wh_off_is_rewritten_so_the_month_adds_up(self):
        # v286: with 0.05 kWh slack 33 SMS days stayed a little off and September
        # missed Growatt's month by 0.043 kWh
        ch = H.plan_changes({"2026-09-01": 656.478}, {"2026-09-01": 656.44})
        assert ch[0].action == "UPDATE"

    def test_sql_inserts_updates_and_skips(self):
        ch = H.plan_changes({"2026-07-01": 10.0, "2026-07-02": 20.0, "2026-07-03": 30.0, "2026-07-05": 8.0},
                            {"2026-07-02": 20.0, "2026-07-03": 15.0, "2026-07-05": 0.0})
        sql = H.build_sql("mex3", ch, "Growatt history import; was it's")
        assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;")
        assert "INSERT INTO daily_production" in sql and "DATE '2026-07-01', 10.000, 'v2'" in sql
        assert "2026-07-02" not in sql                                   # SAME is never touched
        assert "energy_kwh = 30.000" in sql and "pr = round((pr * 2.000000000)::numeric, 4)" in sql
        assert "energy_kwh = 8.000" in sql and "pr = NULL" in sql        # was 0: ratios unknown
        assert sql.count(H.NOTE_MARK) == 3 and "it''s" in sql            # protected note, quoted
        assert "'MEX3'" in sql

    def test_note_mark_is_the_one_the_kpi_mirror_protects(self):
        from argia.store.kpi_mirror import VENDOR_NOTE_MARK
        assert VENDOR_NOTE_MARK in H.NOTE_MARK

    def test_month_summary(self):
        t = {"2026-07-01": 10.0, "2026-07-02": 20.0, "2026-08-01": 5.0}
        s = H.month_summary(t, {"2026-07-02": 18.0}, {"2026-07": 30.0}, {"2026-07": 30.0})
        assert s[0] == {"month": "2026-07", "vendor_month": 30.0, "vendor_days": 30.0, "stored": 18.0,
                        "after": 30.0, "new": 1, "update": 1}
        assert s[1]["month"] == "2026-08" and s[1]["vendor_month"] is None


class TestGrowattCharts:
    def test_parse_energy_series(self):
        env = {"_meta": {}, "response": {"_raw_text": '{"result":1,"obj":{"energy":[0.0,"12.5",null,3]}}'}}
        assert parse_energy_series(env) == [0.0, 12.5, 0.0, 3.0]

    def test_parse_failure_is_none(self):
        assert parse_energy_series({"_meta": {}, "response": {"_raw_text": '{"result":0,"msg":"x"}'}}) is None
        assert parse_energy_series({"_meta": {}, "response": {"_raw_text": '{"result":1,"obj":{}}'}}) is None

    def test_client_bodies(self, monkeypatch):
        from argia.vendors.growatt_web import GrowattWebClient
        c = GrowattWebClient(username="u", password="p")
        calls = []
        monkeypatch.setattr(c, "_post", lambda path, body=None: calls.append((path, body)) or {})
        c.get_max_month_chart("123", "2025-04")
        c.get_max_year_chart("123", 2025)
        assert calls == [("/panel/max/getMAXMonthChart", {"plantId": "123", "maxSn": "", "date": "2025-04"}),
                         ("/panel/max/getMAXYearChart", {"plantId": "123", "maxSn": "", "year": "2025"})]


class TestScriptHelpers:
    def test_capture_split_and_months(self):
        import importlib
        import sys
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
        S = importlib.import_module("vendor_history_import")
        md, mt, ds = S.split_capture({"2025-04": [1.0, 2.0], "year_2025": [0, 0, 0, 3.5] + [0] * 8, "captured_utc": "x"})
        assert md == {"2025-04": [1.0, 2.0]} and mt["2025-04"] == 3.5 and ds == {"2025-04": 3.0}
        assert list(S.months_between(dt.date(2025, 11, 5), dt.date(2026, 2, 1))) == ["2025-11", "2025-12", "2026-01", "2026-02"]
