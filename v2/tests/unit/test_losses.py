"""v264 - the loss method: expected from weather and peers, split by cause."""
from __future__ import annotations

import pytest

from argia.analytics import losses as L

S = L.Slot


class TestPeers:
    def test_the_cdmx_plants_are_each_others_peers_and_leon_has_none(self):
        g = L.peer_groups({"MEX1": (19.5759, -99.1996), "MEX2": (19.5967, -99.2066),
                           "MEX3": (19.4326, -99.1332), "GTO1": (21.1042, -101.7553)})
        assert g["MEX1"] == ["MEX2", "MEX3"] and g["GTO1"] == []

    def test_a_plant_without_coordinates_has_no_peers(self):
        assert L.peer_groups({"A": (None, None), "B": (19.4, -99.1)}) == {"A": [], "B": []}

    def test_healthy_means_85_percent_of_its_own_weather_expectation(self):
        assert L.healthy(850, 1000) and not L.healthy(840, 1000)
        assert not L.healthy(None, 1000) and not L.healthy(900, None)

    def test_calibration_is_the_median_ratio_and_needs_enough_days(self):
        assert L.calibration([(1.03, 1.0)] * 4) is None
        assert L.calibration([(1.03, 1.0)] * 3 + [(1.05, 1.0)] * 2 + [(0.9, 1.0)]) == pytest.approx(1.03)
        assert L.calibration([(2.0, 1.0)] * 10) is None          # implausible roof: a data problem

    def test_peer_expected_uses_the_median_peer_and_the_plants_own_ratio(self):
        assert L.peer_expected(600, [4.0, 5.0, 9.0], 1.03) == pytest.approx(600 * 5.0 * 1.03)
        assert L.peer_expected(600, [], 1.0) is None
        assert L.peer_expected(600, [4.0], None) is None


class TestExpected:
    def test_peers_win_then_calibrated_weather_then_raw_model(self):
        assert L.choose_expected(1000, 1100, 1.1) == (1100, "peers")
        assert L.choose_expected(1000, None, 1.1) == (pytest.approx(1100), "weather")
        assert L.choose_expected(1000, None, None) == (1000, "weather-model")
        assert L.choose_expected(None, None, None) == (None, "none")


class TestSlots:
    DAY = {f"{h:02d}:00": irr for h, irr in ((8, 200.0), (10, 600.0), (12, 1000.0), (14, 600.0), (16, 200.0))}

    def own(self, **over):
        o = {ts: S(irr, 0, 3, 100.0) for ts, irr in self.DAY.items()}
        o.update(over)
        return o

    def test_a_healthy_day_has_no_unavailability(self):
        assert L.slot_causes(2600, 2600, self.own(), {}, 3) == (0.0, 0.0)

    def test_inverters_at_zero_watts_cost_their_share_of_the_day(self):
        # the 12:00 slot carries 1000/2600 of the day; 2 of 3 inverters at 0 W
        u, _ = L.slot_causes(2600, 2000, self.own(**{"12:00": S(1000.0, 2, 3, 30.0)}), {}, 3)
        assert u == pytest.approx(2600 * 1000 / 2600 * 2 / 3)

    def test_a_datalogger_gap_with_the_inverters_producing_costs_nothing(self):
        # SAG, 21 Sep: telemetry for 43 of 204 slots, yet the counter shows a full day
        own = {k: v for k, v in self.own().items() if k in ("08:00", "16:00")}
        u, _ = L.slot_causes(2600, 2550, own, dict(self.DAY), 3)
        assert u == 0.0

    def test_a_silent_plant_that_really_stopped_is_unavailability(self):
        # SAG, 19 Sep: data for the morning only, the counter made little all day
        own = {"08:00": S(200.0, 0, 3, 100.0)}
        u, _ = L.slot_causes(2600, 300, own, dict(self.DAY), 3)
        silent_expected = 2600 * (2600 - 200) / 2600
        reported = 100.0 * 5 / 60
        assert u == pytest.approx(silent_expected - (300 - reported))

    def test_a_plant_with_no_telemetry_at_all_is_judged_on_the_peers_daylight(self):
        u, _ = L.slot_causes(2600, 0, {}, dict(self.DAY), 3)
        assert u == pytest.approx(2600)

    def test_approved_customer_maintenance_is_excused_not_lost(self):
        own = self.own(**{"12:00": S(1000.0, 3, 3, 0.0, excused=True)})
        u, x = L.slot_causes(2600, 1600, own, {}, 3)
        assert u == 0.0 and x == pytest.approx(1000)

    def test_night_and_missing_irradiance_do_not_count(self):
        assert L.slot_causes(2600, 0, {"03:00": S(0.0, 3, 3, 0.0)}, {}, 3) == (0.0, 0.0)
        assert L.slot_causes(None, 0, self.own(), {}, 3) == (0.0, 0.0)


class TestSplit:
    def test_causes_always_add_up_to_the_loss(self):
        s = L.split_loss(3000, 2000, unavailability=600, overheating=300)
        assert (s.lost, s.unavailability, s.overheating, s.underperformance) == (1000, 600, 300, 100)

    def test_each_cause_is_capped_by_what_is_left(self):
        s = L.split_loss(3000, 2500, unavailability=400, overheating=400)
        assert (s.unavailability, s.overheating, s.underperformance) == (400, 100, 0)
        s = L.split_loss(3000, 2500, unavailability=900)
        assert (s.lost, s.unavailability) == (500, 500)

    def test_more_than_expected_is_no_loss(self):
        s = L.split_loss(2000, 2200, unavailability=50)
        assert (s.lost, s.unavailability, s.underperformance) == (0, 0, 0)

    def test_a_dark_day_is_all_unavailability(self):
        s = L.split_loss(3000, 60)
        assert s.unavailability == s.lost == 2940

    def test_excused_energy_is_taken_out_before_the_loss(self):
        s = L.split_loss(3000, 1800, excused=1000)
        assert (s.excused, s.lost) == (1000, 200)

    def test_no_expectation_no_answer(self):
        assert L.split_loss(None, 100) is None and L.split_loss(100, None) is None


class TestMoney:
    def test_ppa_is_priced_capex_is_not(self):
        assert L.mxn(1000, 2.508) == 2508.0
        assert L.mxn(1000, None) is None

    def test_a_sag_outage_day_end_to_end(self):
        """19 Sep: peers MEX2 5.16 and MEX3 5.11 kWh/kWp, SAG's usual ratio 1.0,
        SAG counter 427 kWh, telemetry only until mid-morning."""
        day = {f"{h:02d}:00": irr for h, irr in ((8, 150.0), (10, 600.0), (12, 950.0), (14, 700.0), (16, 250.0))}
        d = L.compute_day("MEX1", "2026-09-19", 597.78, 427.0, 3191.0, {"MEX2": 5.16, "MEX3": 5.11}, 1.0, 0.95,
                          {"08:00": S(150.0, 0, 3, 90.0)}, day, 3, 0.0, 2.508)
        assert d.expected_basis == "peers" and d.peers == "MEX2,MEX3"
        assert d.expected_kwh == pytest.approx(597.78 * 5.135, abs=0.1)
        assert d.lost_kwh == pytest.approx(d.expected_kwh - 427.0, abs=0.1)
        assert d.unavailability_kwh > 0.9 * d.lost_kwh                 # it was the outage, not the panels
        assert d.lost_mxn == pytest.approx(d.lost_kwh * 2.508, abs=0.5)

    def test_totals_price_only_tariffed_days(self):
        a = L.compute_day("MEX1", "d1", 100, 300, 500, {}, None, 1.0, {}, {}, 1, 0.0, 2.0)
        b = L.compute_day("MEX3", "d1", 100, 300, 500, {}, None, 1.0, {}, {}, 1, 0.0, None)
        t = L.totals([a, b])
        assert t["lost"] == 400 and t["lost_mxn"] == 400 and t["priced"] == 1.0
