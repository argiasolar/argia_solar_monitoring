"""v220 — an alert's severity follows production evidence (Tomasz,
2026-09-07): hot inverters page only when they measurably lose output;
string-diagnostic flags are WARNING only with a measured loss, else INFO
and never mailed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from argia.analytics import evidence as EV

V2 = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class R:
    plant_key: str
    inverter_sn: str
    value: float
    rated_kw: float = 125.0


class TestThermal:
    def test_shortfall(self):
        assert EV.thermal_shortfall_pct(100000, 125.0, [944.0, 944.0]) == pytest_approx(15.25, 0.1)   # W per rated kW, as acute.py feeds it
        assert EV.thermal_shortfall_pct(None, 125.0, [0.9]) is None
        assert EV.thermal_shortfall_pct(100000, 0, [0.9]) is None
        assert EV.thermal_shortfall_pct(100000, 125.0, []) is None

    def test_severity_matrix(self):
        S = EV.thermal_severity
        assert S(71.0, 65, 70, True, 15.0)[0] == "CRITICAL"          # hot, hotter, losing
        assert S(71.0, 65, 70, True, 4.0)[0] == "WARNING"            # hot, hotter, not losing enough
        assert S(71.0, 65, 70, True, None)[0] == "WARNING"           # hot, hotter, nothing to measure
        assert S(71.0, 65, 70, None, 15.0)[0] == "CRITICAL"          # alone but measurably losing
        assert S(71.0, 65, 70, False, 30.0)[0] == "WARNING"          # plant-wide heat
        assert S(80.0, 65, 70, True, 0.0)[0] == "WARNING"            # 80 C with normal output
        assert S(66.0, 65, 70, True, 20.0)[0] == "WARNING"           # below 70 never critical
        sev, why, ev = S(71.0, 65, 70, True, 1.2)
        assert "no output loss measured" in why and ev == "producing within 1% of cooler peers"
        assert S(71.0, 65, 70, True, None)[2] == "no cooler peer to measure the loss against"

    def test_day_severity(self):
        D = EV.thermal_day_severity
        assert D(76.0, 70, None) == ("WARNING", "no thermal evaluation for the day")
        assert D(76.0, 70, EV.ThermalDay(0, 0.0, 300.0))[0] == "WARNING"
        assert D(76.0, 70, EV.ThermalDay(95, 41.0, 300.0))[0] == "CRITICAL"
        assert D(76.0, 70, EV.ThermalDay(20, 5.0, 300.0))[0] == "WARNING"       # 1.6 %, 20 min
        assert D(76.0, 70, EV.ThermalDay(70, 5.0, 300.0))[0] == "CRITICAL"      # an hour of derating
        assert D(68.0, 70, EV.ThermalDay(95, 41.0, 300.0))[0] == "WARNING"      # below 70 never critical

    def test_v222_vendor_word_in_the_acute_rule(self):
        S = EV.thermal_severity
        # the inverter itself reports Tinv: CRITICAL at >= 70 whatever the peers say
        sev, why, ev = S(71.0, 65, 70, True, 1.0, vendor_mode="Tinv", vendor_minutes=45)
        assert sev == "CRITICAL" and "the inverter itself reports thermal derating" in why
        assert ev == "Growatt reports Tinv derating (45 min); producing within 1% of cooler peers"
        assert S(71.0, 65, 70, False, None, vendor_mode="Tboost")[0] == "CRITICAL"       # plant-wide heat, still the device's word
        assert S(71.0, 65, 70, None, None, vendor_mode="Tinv")[2].startswith("Growatt reports Tinv derating; no cooler peer")
        assert S(66.0, 65, 70, True, 0.0, vendor_mode="Tinv")[0] == "WARNING"            # below 70 never critical
        assert S(71.0, 65, 70, True, 1.0, vendor_mode=None)[0] == "WARNING"              # no word -> as before
        assert EV.vendor_evidence(None) == "" and EV.vendor_evidence("Tinv") == "Growatt reports Tinv derating"

    def test_v222_vendor_minutes_in_the_daily_rule(self):
        D = EV.thermal_day_severity
        # no measured loss vs peers, but the device reported Tinv for 95 min (NL1 inverter 1, 2026-09-05)
        sev, ev = D(81.6, 70, EV.ThermalDay(0, 0.0, 700.0, vendor_derating_minutes=95))
        assert sev == "CRITICAL" and ev.startswith("Growatt reports thermal (Tinv/Tboost) derating (95 min); ")
        assert "no output loss vs cooler peers measured" in ev
        assert D(81.6, 70, EV.ThermalDay(0, 0.0, 700.0, vendor_derating_minutes=25))[0] == "WARNING"   # under an hour
        assert D(68.0, 70, EV.ThermalDay(0, 0.0, 700.0, vendor_derating_minutes=95))[0] == "WARNING"   # below 70
        assert EV.ThermalDay(0, 0.0, 700.0).vendor_derating_minutes == 0      # default keeps the v220 callers


class TestStrings:
    READINGS = [R("GTO1", "A", 500.0), R("GTO1", "B", 520.0), R("GTO1", "C", 510.0), R("MEX1", "Z", 1.0)]

    def test_peer_ratio(self):
        assert EV.peer_ratio(self.READINGS, "GTO1", "A") == pytest_approx(500 / 515, 0.001)
        assert EV.peer_ratio(self.READINGS, "MEX1", "Z") is None                 # no peers
        assert EV.peer_ratio(self.READINGS, "GTO1", "Q") is None                 # not in the day

    def test_weak_strings_against_their_own_history(self):
        base = {"s1": [0.5, 0.49, 0.51], "s2": [0.5, 0.51, 0.49], "s3": [0.0, 0.0, 0.0], "s4": [1.0, 1.0, 1.0]}
        today = [("s1", 0.02), ("s2", 0.98), ("s3", 0.0), ("s4", 1.0)]
        weak, judged = EV.weak_strings(today, base)
        assert weak == [("s1", pytest_approx(0.04, 0.001))] and judged == 3
        # s3 never carried current (an empty input) -> never "weak"; s4 alone in its pair stays 1.0
        assert EV.weak_strings([("s1", 0.5), ("s3", 0.0)], base) == ([], 1)      # s3 is not judged at all
        # too little history -> nothing judged
        assert EV.weak_strings([("s1", 0.0)], {"s1": [0.5, 0.5]}) == ([], None)
        # no current at all today (night / no data) -> nothing judged
        assert EV.weak_strings([("s1", 0.0), ("s2", None)], base) == ([], None)
        assert EV.weak_strings([], {}) == ([], None)

    def test_string_severity(self):
        S = EV.string_severity
        sev, ev = S(0.99, [("s3", 0.01)], 8)
        assert sev == "WARNING" and "string s3 at 1% of its own usual current" in ev and "inverter at 99% of plant peers" in ev
        sev, ev = S(0.80, [], 8)
        assert sev == "WARNING" and "inverter at 80% of plant peers" in ev
        sev, ev = S(0.99, [], 8)
        assert sev == "INFO" and ev.startswith("no measurable loss") and "all 8 strings within their usual current" in ev
        sev, ev = S(0.99, [], None)
        assert sev == "INFO" and "string currents not available" in ev
        assert S(None, [], None) == ("INFO", "no production data to confirm a loss")


class TestWiring:
    def test_daily_script_reads_evidence_and_builds_string_candidates(self):
        src = (V2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert "FROM thermal_daily WHERE prod_date = DATE" in src
        assert "FROM string_daily" in src and "kind = 'string'" in src and "STRING_BASELINE_DAYS" in src
        assert "string_candidate_with_evidence(b, per_inverter_kwh, string_rows or {})" in src

    def test_string_candidate_severity_and_message(self):
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        import alerts_daily as AD
        from argia.analytics.vendor_flags import StringBitBreach
        from argia.core.thresholds import Severity
        b = StringBitBreach("GTO1", "A", "break:15", Severity.WARNING,
                            "GTO1 A: NEW string-diagnostic bit(s) [break:15] not seen in prior 14 days [WARNING]")
        base = {"s1": [0.5] * 5, "s2": [0.5] * 5, "s3": [0.0] * 5}
        c = AD.string_candidate_with_evidence(b, self_readings(), {("GTO1", "A"): {"today": [("s1", 1.0), ("s2", 0.0), ("s3", 0.0)], "base": base}})
        assert c.severity == "WARNING" and "string s2 at 0%" in c.message and c.message.endswith("[WARNING]")
        c = AD.string_candidate_with_evidence(b, self_readings(), {("GTO1", "A"): {"today": [("s1", 0.5), ("s2", 0.5), ("s3", 0.0)], "base": base}})
        assert c.severity == "INFO" and "no measurable loss" in c.message and c.message.endswith("[INFO]")
        assert "NEW string-diagnostic bit(s) [break:15]" in c.message

    def test_info_alerts_are_never_mailed(self):
        from argia.alerts import ledger_mail as LM
        from argia.core.alerts_state import AlertRecord, AlertState

        def rec(sev):
            return AlertRecord("A1", "k", "GTO1", "A", "string_fault", sev, AlertState.OPEN,
                               "2026-09-06T12:00:00+00:00", "", "", None, None, "m", "", "")
        assert LM.unmailed([rec("INFO")]) == []
        assert [r.severity for r in LM.unmailed([rec("WARNING"), rec("CRITICAL")])] == ["WARNING", "CRITICAL"]
        src = (V2 / "scripts/daily_perf_mail.py").read_text(encoding="utf-8")
        assert "severity IN ('WARNING','CRITICAL')" in src


def self_readings():
    return [R("GTO1", "A", 500.0), R("GTO1", "B", 520.0), R("GTO1", "C", 510.0)]


def pytest_approx(v, tol):
    import pytest
    return pytest.approx(v, abs=tol)
