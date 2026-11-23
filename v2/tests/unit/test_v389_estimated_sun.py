"""v389 - a day whose sun is only ESTIMATED (no usable weather-station reading) is not an
energy loss and not an energy alarm on its own.

Regression case, numbers from production (pio06, 22 Nov 2026): the SAG datalogger was
offline 06:00-12:00, the station behind it (shared by Vitalmex and SMS) read 0 W/m2 while
the plants produced, the day's irradiance fell back to the cloud-cover estimate and all
three plants looked ~50 % short: CRITICAL mails and $5,596 MXN "lost" for SAG + Vitalmex.
The three plants made the same kWh per kWp as usual relative to each other."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from argia.analytics import losses as L

V2 = pathlib.Path(__file__).resolve().parents[2]

# 22 Nov 2026 (daily_production + loss_daily on pio06)
KWP = {"MEX1": 597.78, "MEX2": 609.84, "MEX3": 155.0}
ACTUAL = {"MEX1": 1236.55, "MEX2": 1182.53, "MEX3": 308.1}
WEATHER = {"MEX1": 2158.7, "MEX2": 2336.4, "MEX3": 643.6}       # from the cloud-cover estimate
PEER_RATIO = {"MEX1": 0.962, "MEX2": 1.014, "MEX3": 1.015}
WEATHER_RATIO = {"MEX1": 1.070, "MEX2": 1.032, "MEX3": 1.024}
TARIFF = 2.508


def sy(k):
    return ACTUAL[k] / KWP[k]


def day(k, peers, measured=False, actual=None, slots=None):
    return L.compute_day(k, "2026-11-22", KWP[k], ACTUAL[k] if actual is None else actual, WEATHER[k],
                         {p: sy(p) for p in peers}, PEER_RATIO[k], WEATHER_RATIO[k],
                         slots or {}, {}, 3, 0.0, TARIFF, weather_is_measured=measured)


# ------------------------------------------------------------ which sun is measured
@pytest.mark.parametrize("src, measured", [("shinemaster", True), ("shinemaster_history", True), (None, True),
                                           ("", True), ("cloud_cover_model", False), ("proxy:GTO1", True),
                                           ("none", False)])
def test_weather_measured(src, measured):
    assert L.weather_measured(src) is measured


def test_a_peer_on_an_estimated_day_is_judged_by_availability_not_by_the_estimate():
    # SAG made 57 % of the estimate: not "healthy", but it was 100 % available
    assert not L.peer_counts(ACTUAL["MEX1"], WEATHER["MEX1"], True, 1.0)
    assert L.peer_counts(ACTUAL["MEX1"], WEATHER["MEX1"], False, 1.0)
    assert not L.peer_counts(ACTUAL["MEX1"], WEATHER["MEX1"], False, 0.80)       # partly down: no witness
    assert not L.peer_counts(0.0, WEATHER["MEX1"], False, 1.0)
    assert not L.peer_counts(ACTUAL["MEX1"], WEATHER["MEX1"], False, None)


# ------------------------------------------------------------ the 22 Nov case
@pytest.mark.parametrize("k", ["MEX1", "MEX2", "MEX3"])
def test_22_nov_neighbours_explain_the_day_nothing_lost(k):
    d = day(k, [p for p in KWP if p != k])
    assert d.expected_basis == "peers"
    assert d.lost_kwh == 0.0 and (d.lost_mxn or 0.0) == 0.0
    assert abs(d.expected_kwh / ACTUAL[k] - 1) < 0.10           # within the 10 % day scatter


def test_22_nov_without_neighbours_the_estimate_books_no_underperformance():
    d = day("MEX2", [])
    assert d.expected_basis == "weather-estimated"
    assert d.underperformance_kwh == 0.0 and d.lost_kwh == 0.0 and d.lost_mxn == 0.0
    ex = L.explain_day(vars(d))
    assert "ESTIMATE" in ex[0] and "ESTIMACIÓN" in ex[1]


def test_before_v389_the_same_day_was_booked_as_lost():
    # the old rule: no peer counts (each missed the inflated estimate) -> weather basis, measured
    d = day("MEX2", [], measured=True)
    assert d.expected_basis == "weather" and d.underperformance_kwh > 1000          # the false alarm


def test_measured_outage_still_counts_on_an_estimated_day():
    # a plant at 0 kWh all day (NL1 11 Oct 2026 pattern) is down, whatever the sun estimate says
    d = day("MEX2", [], actual=0.0)
    assert d.expected_basis == "weather-estimated"
    assert d.unavailability_kwh == pytest.approx(d.expected_kwh, abs=0.2) and d.lost_kwh > 0


def test_measured_days_are_unchanged():
    a = L.compute_day("MEX2", "2026-11-21", 609.84, 1914.76, 1925.8, {"MEX1": 3.23, "MEX3": 3.21}, 1.010, 1.03,
                      {}, {}, 3, 0.0, TARIFF)
    b = L.compute_day("MEX2", "2026-11-21", 609.84, 1914.76, 1925.8, {"MEX1": 3.23, "MEX3": 3.21}, 1.010, 1.03,
                      {}, {}, 3, 0.0, TARIFF, weather_is_measured=True)
    assert vars(a) == vars(b) and a.expected_basis == "peers"


# ------------------------------------------------------------ what alerts and the report judge by
def test_judged_expected():
    assert L.judged_expected(2000.0, "shinemaster_history", "peers", 1900.0) == (2000.0, "weather")
    assert L.judged_expected(2000.0, None, None, None) == (2000.0, "weather")
    assert L.judged_expected(2412.0, "cloud_cover_model", "peers", 1129.0) == (1129.0, "peers")
    assert L.judged_expected(2412.0, "cloud_cover_model", "weather-estimated", 2309.0) == (None, "estimated")
    assert L.judged_expected(2412.0, "cloud_cover_model", None, None) == (None, "estimated")


@pytest.fixture(scope="module")
def alerts_daily():
    sys.path.insert(0, str(V2 / "scripts"))
    spec = importlib.util.spec_from_file_location("alerts_daily_v389", V2 / "scripts" / "alerts_daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_energy_alarm_on_the_22_nov_pattern(alerts_daily):
    kpi = {k: {"energy": ACTUAL[k], "sy": sy(k), "expected": WEATHER[k], "data_class": "full",
               "irradiance_source": "cloud_cover_model"} for k in KWP}
    before = alerts_daily.build_candidates([], kpi)
    assert {c.plant_key for c in before if c.metric == "energy_daily_pct"} >= {"MEX1", "MEX2"}   # the false alarm
    verdicts = {k: ("peers", day(k, [p for p in KWP if p != k]).expected_kwh) for k in KWP}
    after = alerts_daily.build_candidates([], alerts_daily.judge_with_peers(kpi, verdicts))
    assert not [c for c in after if c.metric == "energy_daily_pct"]
    # no neighbour verdict: not judged at all, rather than judged against the guess
    alone = alerts_daily.build_candidates([], alerts_daily.judge_with_peers(kpi, {}))
    assert not [c for c in alone if c.metric == "energy_daily_pct"]


def test_a_real_shortfall_on_a_measured_day_still_alarms(alerts_daily):
    kpi = {"MEX2": {"energy": 1182.53, "sy": 1.94, "expected": 2336.4, "data_class": "full",
                    "irradiance_source": "shinemaster_history"}}
    out = alerts_daily.build_candidates([], alerts_daily.judge_with_peers(kpi, {"MEX2": ("peers", 1254.0)}))
    assert [c for c in out if c.metric == "energy_daily_pct"]
