"""v267 - what a maintenance ticket costs while it stays open."""
from __future__ import annotations

import datetime as dt

import pytest

from argia.maintenance import ticket_cost as TC

TODAY = dt.date(2026, 9, 28)
PD = TC.PlantDay


class TestWindow:
    def test_open_ticket_runs_from_its_mx_day_to_yesterday(self):
        # 03:00 UTC on the 25th is still the 24th in Mexico
        days = TC.window("2026-09-25 03:00:00+00", "", "", TODAY)
        assert days[0] == dt.date(2026, 9, 24) and days[-1] == dt.date(2026, 9, 27)

    def test_resolved_ticket_stops_on_its_resolution_day(self):
        days = TC.window("2026-09-20 15:00:00+00", "2026-09-22 20:00:00+00", "", TODAY)
        assert days == [dt.date(2026, 9, 20), dt.date(2026, 9, 21), dt.date(2026, 9, 22)]

    def test_a_ticket_opened_today_has_no_closed_day_yet(self):
        assert TC.window("2026-09-28 15:00:00+00", "", "", TODAY) == []

    def test_unparseable_date_is_empty_not_a_crash(self):
        assert TC.window("", "", "", TODAY) == []


class TestInverter:
    def test_shortfall_against_the_peer_median_per_rated_kw(self):
        assert TC.inverter_shortfall(100, 300, [5.0, 6.0, 7.0]) == pytest.approx(300)
        assert TC.inverter_shortfall(100, 700, [5.0]) == 0.0           # better than its peers
        assert TC.inverter_shortfall(100, 300, []) is None             # nothing to compare with
        assert TC.inverter_shortfall(None, 300, [5.0]) is None


class TestDays:
    PLANT = {"2026-09-25": PD(1967.2, 2.508, 1967.2, 0, 0), "2026-09-26": PD(172.0, 2.508, 15, 0, 157),
             "2026-09-27": PD(0.0, 2.508)}

    def test_plant_ticket_carries_the_plant_loss_and_its_cause(self):
        days = TC.ticket_days([dt.date(2026, 9, d) for d in (25, 26, 27)], self.PLANT)
        assert [(d.day, d.lost_kwh, d.mxn, d.cause) for d in days] == [
            ("2026-09-25", 1967.2, round(1967.2 * 2.508, 2), "unavailability"),
            ("2026-09-26", 172.0, round(172 * 2.508, 2), "underperformance"),
            ("2026-09-27", 0.0, 0.0, "none")]
        kwh, mxn = TC.total(days)
        assert kwh == 2139.2 and mxn == pytest.approx(2139.2 * 2.508, abs=0.02)

    def test_inverter_ticket_counts_only_its_shortfall_capped_by_the_plant(self):
        inv = {"2026-09-25": (125.0, 100.0, [8.0]), "2026-09-26": (125.0, 900.0, [8.0])}
        days = TC.ticket_days([dt.date(2026, 9, 25), dt.date(2026, 9, 26)], self.PLANT, inv)
        assert days[0].lost_kwh == 900.0 and days[0].basis == "inverter" and days[0].cause == "inverter"
        assert days[1].lost_kwh == 100.0                               # 125 x 8 - 900

    def test_inverter_ticket_never_exceeds_the_plant_loss(self):
        days = TC.ticket_days([dt.date(2026, 9, 26)], self.PLANT, {"2026-09-26": (125.0, 0.0, [8.0])})
        assert days[0].lost_kwh == 172.0

    def test_single_inverter_plant_falls_back_to_the_plant_and_says_so(self):
        days = TC.ticket_days([dt.date(2026, 9, 25)], self.PLANT, {"2026-09-25": (125.0, 100.0, [])})
        assert days[0].lost_kwh == 1967.2 and days[0].basis == "plant-no-peers"

    def test_capex_is_kwh_only(self):
        days = TC.ticket_days([dt.date(2026, 9, 25)], {"2026-09-25": PD(500.0, None, 500, 0, 0)})
        assert days[0].mxn is None and TC.total(days) == (500.0, None)

    def test_a_day_without_a_loss_row_is_skipped(self):
        assert TC.ticket_days([dt.date(2026, 9, 1)], self.PLANT) == []


class TestCombined:
    def test_two_tickets_on_the_same_plant_day_count_once(self):
        a = TC.ticket_days([dt.date(2026, 9, 25)], TestDays.PLANT)
        b = TC.ticket_days([dt.date(2026, 9, 25)], TestDays.PLANT, {"2026-09-25": (125.0, 100.0, [8.0])})
        kwh, mxn = TC.combined([("MEX1", a), ("MEX1", b)], {("MEX1", "2026-09-25"): 2.508})
        assert kwh == 1967.2 and mxn == pytest.approx(1967.2 * 2.508, abs=0.01)

    def test_inverter_tickets_add_up_but_not_beyond_the_plant(self):
        x = TC.ticket_days([dt.date(2026, 9, 26)], TestDays.PLANT, {"2026-09-26": (125.0, 900.0, [8.0])})   # 100
        y = TC.ticket_days([dt.date(2026, 9, 26)], TestDays.PLANT, {"2026-09-26": (125.0, 950.0, [8.0])})   # 50
        z = TC.ticket_days([dt.date(2026, 9, 26)], TestDays.PLANT, {"2026-09-26": (125.0, 960.0, [8.0])})   # 40
        kwh, _ = TC.combined([("MEX1", x), ("MEX1", y), ("MEX1", z)], {})
        assert kwh == 172.0                                            # 190 capped at the plant's 172

    def test_capex_totals_have_no_pesos(self):
        d = TC.ticket_days([dt.date(2026, 9, 25)], {"2026-09-25": PD(500.0, None, 500, 0, 0)})
        assert TC.combined([("MEX3", d)], {}) == (500.0, None)
