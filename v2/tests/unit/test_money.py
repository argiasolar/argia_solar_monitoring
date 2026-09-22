"""v257 - what an outage costs, in pesos (Tomasz 2026-09-16: "show the
bleeding in MXN"). The numbers below are SAG's real shape - 597.78 kWp,
PR baseline 0.86, tariff 2.508 MXN/kWh, and the 2026-09-16 close of
1730.2 kWh actual against 3169.9 expected - so the tests fail if the
arithmetic ever stops matching the plant that prompted the feature."""
from __future__ import annotations

import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from argia.analytics import money as M   # noqa: E402

SAG_KWP, SAG_PR, SAG_TARIFF = 597.78, 0.86, 2.508


class TestExpectedPower:
    def test_full_sun_is_nameplate_times_pr(self):
        assert M.expected_kw(SAG_KWP, 1000.0, SAG_PR) == pytest.approx(514.09, abs=0.01)
        assert M.expected_kw(SAG_KWP, 500.0, SAG_PR) == pytest.approx(257.04, abs=0.01)

    def test_night_and_near_night_expect_nothing(self):
        assert M.expected_kw(SAG_KWP, 0.0, SAG_PR) == 0.0
        assert M.expected_kw(SAG_KWP, 19.9, SAG_PR) == 0.0, "dusk must not count as a loss"
        assert M.expected_kw(SAG_KWP, 20.0, SAG_PR) > 0

    def test_a_missing_input_is_unknown_not_zero(self):
        assert M.expected_kw(None, 800.0) is None
        assert M.expected_kw(SAG_KWP, None) is None
        assert M.expected_kw(0, 800.0) is None

    def test_a_missing_pr_falls_back_rather_than_crashing(self):
        assert M.expected_kw(100.0, 1000.0, None) == pytest.approx(100 * M.DEFAULT_PR)
        assert M.expected_kw(100.0, 1000.0, 0) == pytest.approx(100 * M.DEFAULT_PR)


class TestIntradayLoss:
    def test_a_dead_plant_in_full_sun_loses_its_whole_output(self):
        # 12 samples x 5 min = 1 h at 1000 W/m2
        samples = [(1000.0, 0.0)] * 12
        assert M.lost_kwh_intraday(samples, SAG_KWP, SAG_PR) == pytest.approx(514.1, abs=0.2)

    def test_no_reading_at_all_counts_as_zero_production(self):
        """SAG went from 0 W to no value once the datalogger dropped - the
        loss must not disappear with the reading."""
        blind = [(1000.0, None)] * 12
        dark = [(1000.0, 0.0)] * 12
        assert M.lost_kwh_intraday(blind, SAG_KWP, SAG_PR) == M.lost_kwh_intraday(dark, SAG_KWP, SAG_PR)

    def test_a_healthy_plant_loses_nothing_and_never_goes_negative(self):
        good = [(1000.0, 520.0)] * 12          # beating the model
        assert M.lost_kwh_intraday(good, SAG_KWP, SAG_PR) == 0.0

    def test_partial_output_loses_only_the_shortfall(self):
        half = [(1000.0, 257.0)] * 12
        assert M.lost_kwh_intraday(half, SAG_KWP, SAG_PR) == pytest.approx(257.1, abs=0.2)

    def test_night_samples_and_empty_input_cost_nothing(self):
        assert M.lost_kwh_intraday([(0.0, 0.0)] * 12, SAG_KWP, SAG_PR) == 0.0
        assert M.lost_kwh_intraday([], SAG_KWP, SAG_PR) == 0.0
        assert M.lost_kwh_intraday([(1000.0, 0.0)], None, SAG_PR) == 0.0


class TestClosedDayLoss:
    def test_the_real_sag_day(self):
        """2026-09-16: expected 3169.9, actual 1730.2."""
        lost = M.lost_kwh_day(3169.9, 1730.2)
        assert lost == pytest.approx(1439.7, abs=0.1)
        assert M.cost_mxn(lost, SAG_TARIFF) == pytest.approx(3610.76, abs=0.5)
        assert M.loss_phrase(lost, SAG_TARIFF, estimated=False) == "1,440 kWh lost - $3,611 MXN"

    def test_a_good_day_costs_nothing_even_when_it_beat_expectation(self):
        assert M.lost_kwh_day(2660.5, 2776.5) == 0.0

    def test_no_expectation_is_unknown_not_zero(self):
        assert M.lost_kwh_day(None, 1730.2) is None
        assert M.lost_kwh_day(3169.9, None) == pytest.approx(3169.9)


class TestTariffAndHonesty:
    def test_the_contract_month_wins_over_the_standing_tariff(self):
        assert M.tariff_mxn(2.75, SAG_TARIFF) == 2.75
        assert M.tariff_mxn(None, SAG_TARIFF) == SAG_TARIFF
        assert M.tariff_mxn(0, SAG_TARIFF) == SAG_TARIFF, "a zero tariff is not a price"
        assert M.tariff_mxn("nonsense", SAG_TARIFF) == SAG_TARIFF

    def test_a_capex_plant_has_no_peso_figure_and_says_so(self):
        """The five CAPEX sites are net-metering; ARGIA bills no kWh there.
        Inventing a peso number for them would be a lie in a report."""
        assert M.tariff_mxn(None, None) is None
        assert M.cost_mxn(1439.7, None) is None
        assert M.loss_phrase(1439.7, None) == "≈ 1,440 kWh lost (no per-kWh tariff for this site)"

    def test_nothing_lost_means_no_clause_at_all(self):
        assert M.loss_phrase(None, SAG_TARIFF) == ""

    def test_the_estimate_is_marked_as_one(self):
        assert M.loss_phrase(100.0, SAG_TARIFF).startswith("≈ ")
        assert not M.loss_phrase(100.0, SAG_TARIFF, estimated=False).startswith("≈")


class TestFleetTotal:
    def test_a_fleet_total_prices_only_what_has_a_tariff(self):
        kwh, mxn = M.total_cost([(1439.7, SAG_TARIFF), (200.0, 1.975), (500.0, None)])
        assert kwh == pytest.approx(2139.7)
        assert mxn == pytest.approx(3610.76 + 395.0, abs=1.0), "the CAPEX row adds kWh, not pesos"

    def test_all_capex_gives_kwh_and_no_money_rather_than_zero(self):
        kwh, mxn = M.total_cost([(500.0, None), (300.0, None)])
        assert kwh == 800.0 and mxn is None

    def test_empty_and_unknown_rows(self):
        assert M.total_cost([]) == (0.0, None)
        assert M.total_cost([(None, SAG_TARIFF)]) == (0.0, None)

    def test_formatting(self):
        assert M.fmt_mxn(3610.76) == "$3,611 MXN" and M.fmt_mxn(None) == " - "
        assert M.fmt_kwh(1439.7) == "1,440 kWh" and M.fmt_kwh(None) == " - "
