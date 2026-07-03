"""Tests for vendor-flag detectors (inverter_fault)."""

from __future__ import annotations

import datetime as dt

from argia.alerts.engine import candidate_from_fault_breach
from argia.analytics.inverter_health import Severity
from argia.analytics.vendor_flags import (
    MIN_FAULT_SAMPLES,
    evaluate_inverter_faults,
)
from argia.core.time_utils import MX_TZ, UTC


def _mx(h, m=0):
    return dt.datetime(2026, 7, 2, h, m, tzinfo=MX_TZ).astimezone(UTC)


def _s(h, code, sn="JFM5D8900B", plant="GTO1", m=0):
    return (_mx(h, m), plant, sn, code)


class TestEvaluateInverterFaults:
    def test_real_ft302_case_fires_critical(self):
        # Real GTO1 2026-07-02 pattern: repeated FT=302 during daylight.
        samples = [_s(10, "FT=302"), _s(11, "FT=302"), _s(12, "FT=302"),
                   _s(13, "0")]
        b = evaluate_inverter_faults(samples)
        assert len(b) == 1
        assert b[0].severity is Severity.CRITICAL
        assert b[0].plant_key == "GTO1" and b[0].inverter_sn == "JFM5D8900B"
        assert "FT=302 (x3)" in b[0].codes
        assert b[0].samples_faulted == 3 and b[0].samples_total == 4
        assert "FT=302" in b[0].message

    def test_single_glitch_row_is_debounced(self):
        samples = [_s(10, "FT=302"), _s(11, "0"), _s(12, "0")]
        assert evaluate_inverter_faults(samples) == []
        assert MIN_FAULT_SAMPLES == 2

    def test_healthy_codes_never_fire(self):
        samples = [_s(10, "0"), _s(11, "0.0"), _s(12, ""), _s(13, None)]
        assert evaluate_inverter_faults(samples) == []

    def test_huawei_normal_state_never_fires(self):
        # Regression for the 2026-07-03 real-data finding: IS=512,RS=1 is
        # Huawei's NORMAL on-grid running state, present in every healthy
        # sample. Treating it as a fault flagged all six MEX inverters.
        samples = [_s(h, "IS=512,RS=1", sn="GR2499018245", plant="MEX2")
                   for h in range(8, 18)]
        assert evaluate_inverter_faults(samples) == []

    def test_huawei_unknown_state_values_do_not_fire(self):
        # IS=768 (seen on a weak unit) is undecoded STATE — a lead to
        # investigate, not a fault to alert on.
        samples = [_s(10, "IS=768,RS=1"), _s(11, "IS=768,RS=1")]
        assert evaluate_inverter_faults(samples) == []

    def test_huawei_devstatus_abnormal_fires(self):
        samples = [_s(10, "DS=3,IS=512,RS=1"), _s(11, "DS=3,IS=512,RS=1")]
        b = evaluate_inverter_faults(samples)
        assert len(b) == 1 and "DS=3" in b[0].codes
        assert "IS=512" not in b[0].codes          # state stripped from summary

    def test_night_faults_ignored(self):
        # Standby/after-sunset codes must not count toward the threshold.
        samples = [_s(3, "FT=302"), _s(4, "FT=302"), _s(12, "FT=302")]
        assert evaluate_inverter_faults(samples) == []       # only 1 daylight

    def test_multiple_codes_summarized_most_common_first(self):
        samples = [_s(9, "FT=302"), _s(10, "FT=302"),
                   _s(11, "FC1=1,FT=203")]
        b = evaluate_inverter_faults(samples)[0]
        assert b.codes.startswith("FT=302 (x2)")
        assert "FC1=1,FT=203 (x1)" in b.codes

    def test_per_inverter_isolation(self):
        samples = [_s(10, "FT=302", sn="A"), _s(11, "FT=302", sn="A"),
                   _s(10, "0", sn="B"), _s(11, "0", sn="B")]
        b = evaluate_inverter_faults(samples)
        assert len(b) == 1 and b[0].inverter_sn == "A"

    def test_mapper_produces_engine_candidate(self):
        samples = [_s(10, "FT=302"), _s(11, "FT=302")]
        c = candidate_from_fault_breach(evaluate_inverter_faults(samples)[0])
        assert c.metric == "inverter_fault"
        assert c.alert_key == "gto1:inv:jfm5d8900b:inverter_fault"
        assert c.severity == "CRITICAL"
        assert c.value == 2.0
