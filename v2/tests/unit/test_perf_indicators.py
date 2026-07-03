"""Tests for daily performance indicators (plan #4).

Real-world seeds come from the 2026-06-28 ARGIA Daily Report:
- GTO1 (Taigene): inv3 dead all day (0 kWh), inv2 died mid-day (462 vs ~800 peers)
- MEX1 (SAG): inv2+inv3 produced 0 kWh while "ONLINE"
- NL1 (Plastic Omnium): four healthy inverters within ~1% of each other
"""

from __future__ import annotations

from argia.analytics.inverter_health import (
    InverterReading,
    Severity,
    evaluate_inverter_relative,
)
from argia.analytics.perf_indicators import (
    EXPECTED_CRIT_BELOW,
    EXPECTED_WARN_BELOW,
    TWIN_PAIRS,
    evaluate_energy_vs_expected,
    evaluate_plant_twins,
)


# --------------------------------------------------------------------------
class TestLayer1DailyEnergyReuse:
    """Layer 1 = existing detector fed DAILY ENERGY instead of power."""

    def _r(self, sn, kwh, rated=150.0):
        return InverterReading(plant_key="GTO1", inverter_sn=sn,
                               value=kwh, rated_kw=rated)

    def test_gto1_june28_dead_and_halfday_flag(self):
        # Real day: 829 / 462 / 0 / 773 kWh, equal-size inverters.
        readings = [self._r("INV1", 829), self._r("INV2", 462),
                    self._r("INV3", 0), self._r("INV4", 773)]
        breaches = evaluate_inverter_relative(readings)
        by_sn = {b.inverter_sn: b for b in breaches}
        assert by_sn["INV3"].severity is Severity.CRITICAL   # dead
        assert "INV2" in by_sn                               # half-day lag
        assert "INV1" not in by_sn and "INV4" not in by_sn   # healthy

    def test_nl1_june28_healthy_plant_is_silent(self):
        vals = {"I1": 996, "I2": 918, "I3": 915, "I4": 903}
        readings = [InverterReading("NL1", sn, v, 150.0)
                    for sn, v in vals.items()]
        assert evaluate_inverter_relative(readings) == []

    def test_mex1_june28_two_dead_online_inverters_flag(self):
        # 963 / 0 / 0 — "ONLINE" but producing nothing. Peer mean for a dead
        # unit is (963+0)/2, ratio 0 -> CRITICAL for both dead ones.
        readings = [InverterReading("MEX1", "I1", 963, 196.0),
                    InverterReading("MEX1", "I2", 0, 196.0),
                    InverterReading("MEX1", "I3", 0, 196.0)]
        breaches = evaluate_inverter_relative(readings)
        crit = {b.inverter_sn for b in breaches
                if b.severity is Severity.CRITICAL}
        assert {"I2", "I3"} <= crit


# --------------------------------------------------------------------------
class TestPlantTwins:
    def test_default_pairs(self):
        assert ("SLP1", "SLP2") in TWIN_PAIRS
        assert ("MEX1", "MEX2") in TWIN_PAIRS

    def test_lagging_twin_flags_one_direction_only(self):
        sy = {"SLP1": 2.0, "SLP2": 4.0}
        b = evaluate_plant_twins(sy)
        assert len(b) == 1
        assert b[0].plant_key == "SLP1" and b[0].twin_key == "SLP2"
        assert b[0].ratio == 0.5
        assert b[0].severity is Severity.CRITICAL     # < 0.70

    def test_warning_band(self):
        sy = {"SLP1": 3.0, "SLP2": 4.0}               # ratio 0.75
        b = evaluate_plant_twins(sy)
        assert len(b) == 1 and b[0].severity is Severity.WARNING

    def test_healthy_twins_silent(self):
        # Real 2026-06-28: SLP1 3.61 kWh/kWp (653/181), SLP2 3.82 (1066/279).
        sy = {"SLP1": 653 / 181, "SLP2": 1066 / 279}
        assert evaluate_plant_twins(sy) == []

    def test_missing_plant_skipped(self):
        assert evaluate_plant_twins({"SLP1": 3.0}) == []
        assert evaluate_plant_twins({"SLP1": 3.0, "SLP2": None}) == []

    def test_near_zero_reference_carries_no_signal(self):
        # Deep-overcast/fault day on the reference: ratio is noise -> skip.
        sy = {"MEX1": 0.05, "MEX2": 0.3}
        assert evaluate_plant_twins(sy) == []

    def test_plants_outside_pairs_ignored(self):
        sy = {"GTO1": 0.1, "NL1": 5.0}
        assert evaluate_plant_twins(sy) == []


# --------------------------------------------------------------------------
class TestEnergyVsExpected:
    def test_underperforming_plant_flags(self):
        b = evaluate_energy_vs_expected({"GTO1": 963.0}, {"GTO1": 2478.0})
        assert len(b) == 1
        assert b[0].severity is Severity.CRITICAL     # 39%
        assert b[0].ratio == 0.389

    def test_warning_band(self):
        b = evaluate_energy_vs_expected({"SLP1": 800.0}, {"SLP1": 1000.0})
        assert len(b) == 1 and b[0].severity is Severity.WARNING
        assert b[0].threshold == EXPECTED_WARN_BELOW

    def test_at_or_above_warn_is_silent(self):
        assert evaluate_energy_vs_expected(
            {"SLP1": 850.0}, {"SLP1": 1000.0}) == []
        assert evaluate_energy_vs_expected(
            {"SLP1": 1100.0}, {"SLP1": 1000.0}) == []

    def test_missing_or_zero_expected_skipped(self):
        assert evaluate_energy_vs_expected({"A": 100.0}, {}) == []
        assert evaluate_energy_vs_expected({"A": 100.0}, {"A": None}) == []
        assert evaluate_energy_vs_expected({"A": 100.0}, {"A": 0.0}) == []
        assert evaluate_energy_vs_expected({"A": None}, {"A": 500.0}) == []

    def test_thresholds_sane(self):
        assert 0 < EXPECTED_CRIT_BELOW < EXPECTED_WARN_BELOW < 1
