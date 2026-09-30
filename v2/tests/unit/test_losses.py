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
        SAG counter 427 kWh, telemetry only until mid-morning - judged on the day
        counter alone (no later night to prove otherwise, see TestCatchUp)."""
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


class TestCatchUp:
    """v276, Tomasz 2026-09-30: 'usually it looks that we are losing connections
    not the production'. SAG's September nightly counters (vendor_counter_snapshot)."""

    SAG = [("2026-09-18", 2066.49, 1278697.04), ("2026-09-19", 427.11, 1279124.16),
           ("2026-09-20", 3080.39, 1284646.29), ("2026-09-24", 2518.80, 1294921.46),
           ("2026-09-25", 263.56, 1295185.02), ("2026-09-26", 2518.25, 1299452.49),
           ("2026-09-27", 2141.07, 1301593.56), ("2026-09-28", 1203.03, 1302796.59),
           ("2026-09-29", 1930.31, 1306055.70)]

    def test_the_nights_after_a_frozen_day_counter_carry_the_missing_energy(self):
        ex = L.catch_up(self.SAG)
        assert ex["2026-09-20"] == pytest.approx(2441.74, abs=0.01)
        assert ex["2026-09-26"] == pytest.approx(1749.22, abs=0.01)
        assert ex["2026-09-29"] == pytest.approx(1328.80, abs=0.01)
        assert "2026-09-19" not in ex and "2026-09-28" not in ex      # normal nights: step == day counter
        assert "2026-09-24" not in ex                                   # 20 -> 24 is not one night

    def test_rounding_and_missing_counters_are_not_catch_up(self):
        assert L.catch_up([("2026-09-01", 100.0, 1000.0), ("2026-09-02", 100.0, 1110.0)]) == {}   # 10 kWh
        assert L.catch_up([("2026-09-01", 100.0, None), ("2026-09-02", 100.0, 2000.0)]) == {}

    def test_credit_goes_to_the_day_before_then_further_back_never_beyond_its_shortfall(self):
        short = {"2026-09-19": 2662.0, "2026-09-25": 1967.0, "2026-09-28": 1378.0}
        c = L.allocate_catch_up({"2026-09-20": 2441.74, "2026-09-26": 1749.22, "2026-09-29": 1328.80}, short)
        assert c == pytest.approx({"2026-09-19": 2441.74, "2026-09-25": 1749.22, "2026-09-28": 1328.80})
        # a two-day gap: the excess fills the day before, the rest the day before that
        c = L.allocate_catch_up({"2026-09-10": 3000.0}, {"2026-09-09": 2000.0, "2026-09-08": 1500.0})
        assert c == pytest.approx({"2026-09-09": 2000.0, "2026-09-08": 1000.0})
        # energy that fits no shortfall invents nothing
        assert L.allocate_catch_up({"2026-09-10": 500.0}, {}) == {}

    def test_19_sep_with_the_catch_up_is_not_an_outage(self):
        day = {f"{h:02d}:00": irr for h, irr in ((8, 150.0), (10, 600.0), (12, 950.0), (14, 700.0), (16, 250.0))}
        d = L.compute_day("MEX1", "2026-09-19", 597.78, 427.0, 3191.0, {"MEX2": 5.16, "MEX3": 5.11}, 1.0, 0.95,
                          {"08:00": S(150.0, 0, 3, 90.0)}, day, 3, 0.0, 2.508, catchup=2441.74)
        assert d.counter_kwh == 427.0 and d.catchup_kwh == pytest.approx(2441.7, abs=0.1)
        assert d.actual_kwh == pytest.approx(2868.7, abs=0.1)
        assert d.lost_kwh == 0                                         # 2,662 kWh / $6,676 'lost' before v276
        assert 0 < d.tolerance_kwh < 0.10 * d.expected_kwh              # the few % left: scatter of the estimate
        assert d.peer_ratio == 1.0 and d.weather_ratio is None


class TestTolerance:
    def test_a_small_unexplained_shortfall_is_normal_variation(self):
        d = L.compute_day("MEX2", "d", 100, 460, 500, {}, None, 1.0, {}, {}, 1, 0.0, 2.5)   # 8% short
        assert d.lost_kwh == 0 and d.tolerance_kwh == pytest.approx(40)
        d = L.compute_day("MEX2", "d", 100, 480, 500, {}, None, 1.0, {}, {}, 1, 0.0, 2.5)   # 4% short
        assert d.lost_kwh == 0 and d.underperformance_kwh == 0 and d.tolerance_kwh == pytest.approx(20)
        assert d.lost_mxn == 0

    def test_a_real_shortfall_counts_in_full(self):
        d = L.compute_day("MEX2", "d", 100, 450, 500, {}, None, 1.0, {}, {}, 1, 0.0, 2.5)   # 10% short
        assert d.lost_kwh == pytest.approx(50) and d.underperformance_kwh == pytest.approx(50) and d.tolerance_kwh == 0

    def test_the_tolerance_is_two_sigma_of_the_measured_scatter(self):
        assert L.UNDERPERF_TOL == 0.10          # 2 x 4.8% (143 normal PPA plant-days, Sep 2026)

    def test_inverters_at_zero_watts_are_measured_and_count_even_when_small(self):
        day = {f"{h:02d}:00": 500.0 for h in (8, 10, 12, 14, 16)}
        own = {ts: S(500.0, 0, 4, 100.0) for ts in day}
        own["12:00"] = S(500.0, 1, 4, 75.0)                            # one of 4 inverters at 0 W for one slot
        d = L.compute_day("MEX2", "d", 100, 475, 500, {}, None, 1.0, own, day, 4, 0.0, 2.5)
        assert d.unavailability_kwh == pytest.approx(25.0) and d.lost_kwh == pytest.approx(25.0)

    def test_silent_slots_left_after_the_catch_up_are_estimated_and_tolerated(self):
        day = {f"{h:02d}:00": 500.0 for h in (8, 10, 12, 14, 16)}
        own = {"08:00": S(500.0, 0, 4, 100.0)}                          # data only in the morning
        d = L.compute_day("MEX2", "d", 100, 470, 500, {}, None, 1.0, own, day, 4, 0.0, 2.5)
        assert d.lost_kwh == 0 and d.unavailability_kwh == 0 and d.tolerance_kwh == pytest.approx(30)

    def test_measured_causes_are_never_tolerated(self):
        s, tol = L.tolerate(L.split_loss(500, 480, unavailability=15.0, overheating=3.0), 500)
        assert s.unavailability == 15.0 and s.overheating == 3.0 and s.underperformance == 0 and tol == pytest.approx(2.0)
        assert s.lost == pytest.approx(18.0)


class TestExplain:
    """v276: every figure can say how it was made (portal mouse-over, app tap)."""

    ROW = {"expected_kwh": 2581.4, "expected_basis": "peers", "peers": "MEX2,MEX3", "kwp_dc": 597.78,
           "expected_peers_kwh": 2581.4, "peer_ratio": 0.995, "actual_kwh": 2531.8, "counter_kwh": 1203.0,
           "catchup_kwh": 1328.8, "lost_kwh": 0.0, "unavailability_kwh": 0.0, "overheating_kwh": 0.0,
           "underperformance_kwh": 0.0, "tolerance_kwh": 49.6, "tariff_mxn": 2.508, "lost_mxn": 0.0}
    NAMES = {"MEX2": "Vitalmex", "MEX3": "SMS"}

    def test_the_28_sep_data_gap_reads_as_a_data_gap(self):
        en, es = L.explain_day(self.ROW, lambda k: self.NAMES.get(k, k))
        assert "nearby healthy plants (Vitalmex, SMS)" in en and "0.995" in en and "597.8 kWp" in en
        assert "vendor day counter 1,203 + 1,329 kWh proved by the next night's lifetime counter" in en
        assert "not a loss" in en and "Nothing lost." in en and "50 kWh short" in en
        assert "MEX2" not in en and "MEX2" not in es                  # names, never codes
        assert "contador diario del fabricante 1,203" in es

    def test_underperformance_is_explained_with_its_definition(self):
        r = dict(self.ROW, catchup_kwh=0.0, counter_kwh=2141.0, actual_kwh=2141.0, lost_kwh=311.0,
                 unavailability_kwh=149.0, underperformance_kwh=162.0, tolerance_kwh=0.0, lost_mxn=780.0)
        en, _ = L.explain_day(r)
        assert "Lost 311 kWh: unavailability 149" in en and "underperformance 162" in en
        assert "not explained by 0 W or heat" in en and "MXN = 311 kWh x PPA tariff 2.5080" in en

    def test_weather_basis_and_no_expectation(self):
        en, _ = L.explain_day({"expected_kwh": 3452.0, "expected_basis": "weather", "expected_weather_kwh": 3193.0,
                               "weather_ratio": 1.081, "actual_kwh": 2892.0, "lost_kwh": 561.0})
        assert "weather model 3,193 kWh" in en and "1.081" in en and "no healthy neighbour" in en
        assert L.explain_day({"expected_kwh": None})[0].startswith("No expectation")

    def test_no_em_dash_in_the_explanations(self):
        for txt in L.explain_day(self.ROW) + L.explain_period(5520, 2097, 29):
            assert chr(0x2014) not in txt


class TestUnreliableModel:
    """v277: GTO2 and NL2 (CAPEX) booked ~21,000 kWh each in Sep 2026 against the
    raw weather model - a plant that never meets its model has a sensor or design
    data problem, not a monthly loss of a third of its energy."""

    def test_no_loss_is_booked_against_the_raw_model(self):
        d = L.compute_day("GTO2", "2026-09-10", 500, 1800, 2500, {}, None, None, {}, {}, 2, 0.0, None)
        assert d.expected_basis == "weather-model" and d.expected_kwh == 2500
        assert d.lost_kwh is None and d.unavailability_kwh is None and d.underperformance_kwh is None
        assert d.lost_mxn is None
        en, _ = L.explain_day({"expected_kwh": 2500.0, "expected_basis": "weather-model", "lost_kwh": None})
        assert "No loss is booked against it" in en

    def test_a_calibrated_weather_basis_still_counts(self):
        d = L.compute_day("GTO1", "2026-09-10", 500, 1800, 2500, {}, None, 1.0, {}, {}, 2, 0.0, 2.0)
        assert d.expected_basis == "weather" and d.lost_kwh == pytest.approx(700)
