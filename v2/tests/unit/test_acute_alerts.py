"""Tests for acute (per-snapshot) detectors and two-tier resolution."""

from __future__ import annotations

import datetime as dt

from argia.alerts.engine import (
    Candidate,
    candidate_from_acute_breach,
    reconcile_alerts,
)
from argia.analytics.acute import (
    ACUTE_STALE_MIN,
    TEMP_CRIT_C,
    TEMP_WARN_C,
    evaluate_acute,
)
from argia.analytics.inverter_health import Severity
from argia.core.alerts_state import AlertsLedger, AlertState
from argia.core.time_utils import MX_TZ, UTC

# "now" = 2026-07-02 12:00 MX (mid-daylight, inside the dark-check window)
NOW_MX = dt.datetime(2026, 7, 2, 12, 0, tzinfo=MX_TZ)
NOW = NOW_MX.astimezone(UTC)
PLANTS = ["GTO1", "MEX1"]


def _s(minutes_ago, sn, plant="GTO1", power=50000.0, temp=45.0,
       status=1, fault="0"):
    ts = NOW - dt.timedelta(minutes=minutes_ago)
    return (ts, plant, sn, power, temp, status, fault)


class TestEvaluateAcute:
    def test_quiet_fleet_is_silent(self):
        samples = [_s(10, "A"), _s(10, "B"), _s(10, "C", plant="MEX1")]
        assert evaluate_acute(samples, PLANTS, NOW) == []

    def test_fault_token_in_latest_sample_fires(self):
        samples = [_s(10, "A", fault="FT=302"), _s(10, "B")]
        b = evaluate_acute(samples, PLANTS, NOW)
        assert len(b) == 1
        assert b[0].metric == "inverter_fault"
        assert b[0].severity is Severity.CRITICAL and b[0].inverter_sn == "A"

    def test_huawei_state_tokens_do_not_fire(self):
        samples = [_s(10, "A", plant="MEX1", fault="IS=512,RS=1")]
        assert evaluate_acute(samples, ["MEX1"], NOW) == []

    def test_stale_sample_with_fault_does_not_fire(self):
        # A fault in a 2h-old sample says nothing about NOW.
        samples = [_s(120, "A", fault="FT=302"), _s(10, "B")]
        b = evaluate_acute(samples, ["GTO1"], NOW)
        assert all(x.metric != "inverter_fault" for x in b)

    def test_temperature_bands(self):
        samples = [_s(5, "A", temp=66.0), _s(5, "B", temp=80.0),
                   _s(5, "C", temp=60.0)]
        b = {x.inverter_sn: x for x in evaluate_acute(samples, ["GTO1"], NOW)}
        assert b["A"].metric == "inverter_temp_high"
        assert b["A"].severity is Severity.WARNING and b["A"].value == 66.0
        assert b["B"].severity is Severity.CRITICAL
        assert "C" not in b
        assert TEMP_WARN_C < TEMP_CRIT_C

    def test_whole_plant_dark_fires_critical(self):
        samples = [_s(10, "A", power=0.0), _s(10, "B", power=0.0),
                   _s(10, "C", plant="MEX1", power=40000.0)]
        b = [x for x in evaluate_acute(samples, PLANTS, NOW)
             if x.metric == "plant_offline"]
        assert len(b) == 1 and b[0].plant_key == "GTO1"
        assert b[0].severity is Severity.CRITICAL

    def test_single_inverter_zero_does_not_fire_plant_offline(self):
        # Proven-transient case: one inverter at 0 stays daily-only.
        samples = [_s(10, "A", power=0.0), _s(10, "B", power=60000.0)]
        assert all(x.metric != "plant_offline"
                   for x in evaluate_acute(samples, ["GTO1"], NOW))

    def test_plant_dark_outside_midday_window_silent(self):
        evening = dt.datetime(2026, 7, 2, 18, 30, tzinfo=MX_TZ).astimezone(UTC)
        samples = [(evening - dt.timedelta(minutes=5), "GTO1", "A",
                    0.0, 40.0, 1, "0")]
        assert all(x.metric != "plant_offline"
                   for x in evaluate_acute(samples, ["GTO1"], evening))

    def test_acute_data_gap_fires_warning(self):
        old = ACUTE_STALE_MIN + 30
        samples = [_s(old, "A"), _s(10, "C", plant="MEX1")]
        b = [x for x in evaluate_acute(samples, PLANTS, NOW)
             if x.metric == "data_stale"]
        assert len(b) == 1 and b[0].plant_key == "GTO1"
        assert b[0].value == round(old / 60.0, 1)

    def test_night_is_total_noop(self):
        night = dt.datetime(2026, 7, 2, 3, 0, tzinfo=MX_TZ).astimezone(UTC)
        samples = [_s(10, "A", fault="FT=302"), _s(10, "B", temp=90.0)]
        assert evaluate_acute(samples, PLANTS, night) == []

    def test_mapper_keys_match_daily_tier(self):
        samples = [_s(10, "A", fault="FT=302"),
                   _s(10, "A", plant="MEX1", power=0.0),
                   _s(10, "B", plant="MEX1", power=0.0)]
        cands = {c.metric: c for c in
                 (candidate_from_acute_breach(b)
                  for b in evaluate_acute(samples, PLANTS, NOW))}
        assert cands["inverter_fault"].alert_key == \
            "gto1:inv:a:inverter_fault"
        assert cands["plant_offline"].alert_key == \
            "mex1:plant:plant_offline"


class TestTwoTierResolution:
    def _acute_cand(self):
        return Candidate(alert_key="gto1:inv:a:inverter_fault",
                         plant_key="GTO1", inverter_sn="A",
                         metric="inverter_fault", severity="CRITICAL",
                         value=None, threshold=None, message="fault now")

    def test_acute_never_resolves(self):
        # Snapshot 1 opens; snapshot 2 (condition gone) must NOT resolve.
        s1 = reconcile_alerts(AlertsLedger(records=[]),
                              [self._acute_cand()], NOW,
                              resolve_missing=False)
        s2 = reconcile_alerts(AlertsLedger(records=s1.records), [],
                              NOW + dt.timedelta(minutes=30),
                              resolve_missing=False)
        assert not s2.resolved
        assert s2.records[0].state is AlertState.OPEN

    def test_daily_resolves_what_acute_opened(self):
        s1 = reconcile_alerts(AlertsLedger(records=[]),
                              [self._acute_cand()], NOW,
                              resolve_missing=False)
        daily = reconcile_alerts(AlertsLedger(records=s1.records), [],
                                 NOW + dt.timedelta(hours=18),
                                 resolve_missing=True)
        assert len(daily.resolved) == 1
        assert daily.records[0].state is AlertState.RESOLVED
