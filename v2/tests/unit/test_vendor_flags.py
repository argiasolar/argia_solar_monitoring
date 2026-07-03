"""Tests for vendor-flag detectors (inverter_fault)."""

from __future__ import annotations

import datetime as dt

from argia.alerts.engine import (
    candidate_from_fault_breach,
    candidate_from_stale_breach,
    candidate_from_string_breach,
)
from argia.analytics.data_health import (
    MAX_DAYLIGHT_GAP_HOURS,
    evaluate_data_stale,
)
from argia.analytics.vendor_flags import evaluate_string_new_bits
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


def _ss(h, cols, sn="JFM7DXN013", plant="GTO1", day=2):
    ts = dt.datetime(2026, 7, day, h, 0, tzinfo=MX_TZ).astimezone(UTC)
    return (ts, plant, sn, cols)


class TestStringNewBits:
    def test_chronic_bit_never_fires(self):
        # Real case: NL1 JGMAE65009 break bit 13 (=8192) every day since May.
        base = [_ss(12, {"str_break": 8192}, sn="JGMAE65009", plant="NL1", day=1)]
        day = [_ss(h, {"str_break": 8192}, sn="JGMAE65009", plant="NL1")
               for h in (10, 12, 14)]
        assert evaluate_string_new_bits(day, base) == []

    def test_new_bit_fires_warning(self):
        # Real case: JFM7DXN013 grew unmatch bits 10+11 (value 3072) on top
        # of chronic break bit 9 — early warning weeks before its fault.
        base = [_ss(12, {"str_break": 512}, day=1)]
        day = [_ss(10, {"str_break": 512, "str_unmatch": 3072}),
               _ss(12, {"str_break": 512, "str_unmatch": 3072})]
        b = evaluate_string_new_bits(day, base)
        assert len(b) == 1
        assert b[0].new_bits == "unmatch:10, unmatch:11"
        assert "break:9" not in b[0].new_bits
        assert b[0].severity.value == "WARNING"

    def test_single_sample_new_bit_debounced(self):
        base = [_ss(12, {"str_break": 512}, day=1)]
        day = [_ss(10, {"str_unmatch": 3}),          # once only
               _ss(12, {"str_break": 512})]
        assert evaluate_string_new_bits(day, base) == []

    def test_empty_baseline_everything_is_new(self):
        day = [_ss(10, {"str_break": 16}), _ss(12, {"str_break": 16})]
        b = evaluate_string_new_bits(day, [])
        assert len(b) == 1 and b[0].new_bits == "break:4"

    def test_night_day_samples_ignored(self):
        day = [_ss(2, {"str_break": 16}), _ss(3, {"str_break": 16})]
        assert evaluate_string_new_bits(day, []) == []

    def test_blank_columns_no_crash_no_fire(self):
        # Non-Growatt deep tabs leave string columns blank.
        day = [_ss(10, {"str_break": None, "str_unmatch": ""}),
               _ss(12, {"str_break": None})]
        assert evaluate_string_new_bits(day, []) == []

    def test_mapper(self):
        day = [_ss(10, {"str_break": 16}), _ss(12, {"str_break": 16})]
        c = candidate_from_string_breach(evaluate_string_new_bits(day, [])[0])
        assert c.metric == "string_fault"
        assert c.alert_key == "gto1:inv:jfm7dxn013:string_fault"


def _t(h, m=0, day=30):
    return dt.datetime(2026, 6, day, h, m, tzinfo=MX_TZ).astimezone(UTC)


class TestDataStale:
    def test_no_rows_is_critical(self):
        b = evaluate_data_stale({}, ["SLP1"], "2026-06-30")
        assert len(b) == 1
        assert b[0].severity.value == "CRITICAL" and b[0].gap_hours is None

    def test_june30_trailing_hole_fires_warning(self):
        # Real failure: last sample 13:18 -> 6.7 h hole to 20:00 daylight end.
        stamps = [_t(7), _t(9), _t(11), _t(13, 18)]
        b = evaluate_data_stale({"SLP1": stamps}, ["SLP1"], "2026-06-30")
        assert len(b) == 1
        assert b[0].severity.value == "WARNING"
        assert b[0].gap_hours == 6.7

    def test_github_cadence_stays_silent(self):
        # 2 h gaps and a 4 h late start are normal GitHub behaviour.
        stamps = [_t(10, day=1), _t(12, day=1), _t(14, day=1),
                  _t(16, day=1), _t(18, day=1), _t(19, 30, day=1)]
        assert evaluate_data_stale({"SLP1": stamps}, ["SLP1"],
                                   "2026-06-01") == []

    def test_threshold_constant_sane(self):
        assert 4.0 <= MAX_DAYLIGHT_GAP_HOURS <= 8.0

    def test_mapper(self):
        b = evaluate_data_stale({}, ["SLP1"], "2026-06-30")[0]
        c = candidate_from_stale_breach(b)
        assert c.metric == "data_stale"
        assert c.alert_key == "slp1:plant:data_stale"
        assert c.severity == "CRITICAL"
