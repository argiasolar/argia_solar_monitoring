"""Tests for the alert engine (plan #5) — pure reconcile logic."""

from __future__ import annotations

import datetime as dt

from argia.alerts.engine import (
    Candidate,
    candidate_from_expected_breach,
    candidate_from_relative_breach,
    candidate_from_twin_breach,
    reconcile_alerts,
)
from argia.analytics.inverter_health import (
    InverterReading,
    Severity,
    evaluate_inverter_relative,
)
from argia.analytics.perf_indicators import (
    evaluate_energy_vs_expected,
    evaluate_plant_twins,
)
from argia.core.alerts_state import AlertsLedger, AlertState
from argia.core.time_utils import UTC

NOW = dt.datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
LATER = NOW + dt.timedelta(days=1)


def _cand(key="gto1:inv:inv3:inverter_relative", plant="GTO1", sn="INV3",
          metric="inverter_relative", sev="CRITICAL", value=0.0,
          threshold=0.7, msg="dead"):
    return Candidate(alert_key=key, plant_key=plant, inverter_sn=sn,
                     metric=metric, severity=sev, value=value,
                     threshold=threshold, message=msg)


def _ledger(records=()):
    return AlertsLedger(records=list(records))


class TestReconcileLifecycle:
    def test_new_candidate_opens(self):
        r = reconcile_alerts(_ledger(), [_cand()], NOW)
        assert len(r.opened) == 1 and not r.touched and not r.resolved
        rec = r.opened[0]
        assert rec.state is AlertState.OPEN
        assert rec.alert_key == "gto1:inv:inv3:inverter_relative"
        assert rec.severity == "CRITICAL"
        assert rec.opened_utc == rec.last_seen_utc
        assert rec.alert_id.startswith("ALT-20260703-")

    def test_still_true_touches_not_duplicates(self):
        day1 = reconcile_alerts(_ledger(), [_cand()], NOW)
        day2 = reconcile_alerts(_ledger(day1.records), [_cand(value=0.1)], LATER)
        assert not day2.opened and len(day2.touched) == 1
        assert len(day2.records) == 1                      # no second row
        rec = day2.records[0]
        assert rec.state is AlertState.OPEN
        assert rec.value == 0.1                            # refreshed
        assert rec.last_seen_utc != rec.opened_utc

    def test_condition_clears_resolves(self):
        day1 = reconcile_alerts(_ledger(), [_cand()], NOW)
        day2 = reconcile_alerts(_ledger(day1.records), [], LATER)
        assert len(day2.resolved) == 1 and not day2.opened
        rec = day2.records[0]
        assert rec.state is AlertState.RESOLVED
        assert rec.resolved_utc != ""

    def test_refire_after_resolve_creates_new_row(self):
        day1 = reconcile_alerts(_ledger(), [_cand()], NOW)
        day2 = reconcile_alerts(_ledger(day1.records), [], LATER)
        day3 = reconcile_alerts(_ledger(day2.records), [_cand()],
                                LATER + dt.timedelta(days=1))
        assert len(day3.opened) == 1
        assert len(day3.records) == 2                      # history preserved
        states = [r.state for r in day3.records]
        assert states == [AlertState.RESOLVED, AlertState.OPEN]

    def test_escalation_updates_severity_in_place(self):
        day1 = reconcile_alerts(_ledger(), [_cand(sev="WARNING",
                                                  threshold=0.85)], NOW)
        day2 = reconcile_alerts(_ledger(day1.records),
                                [_cand(sev="CRITICAL", threshold=0.70)], LATER)
        assert len(day2.records) == 1                      # same row
        assert day2.records[0].severity == "CRITICAL"
        assert day2.records[0].threshold == 0.70

    def test_foreign_metric_rows_left_alone(self):
        # A manual/other-engine OPEN row must never be auto-resolved.
        day1 = reconcile_alerts(
            _ledger(), [_cand(key="slp1:plant:data_stale", plant="SLP1",
                              sn="", metric="data_stale", sev="WARNING")], NOW)
        # data_stale is NOT in ENGINE_METRICS... but it was opened above by us.
        # Simulate it as pre-existing, then run with no candidates:
        day2 = reconcile_alerts(_ledger(day1.records), [], LATER)
        assert not day2.resolved                           # untouched
        assert day2.records[0].state is AlertState.OPEN

    def test_duplicate_candidates_keep_worst(self):
        c1 = _cand(sev="WARNING", threshold=0.85, value=0.8)
        c2 = _cand(sev="CRITICAL", threshold=0.70, value=0.5)
        r = reconcile_alerts(_ledger(), [c1, c2], NOW)
        assert len(r.opened) == 1
        assert r.opened[0].severity == "CRITICAL"

    def test_resolved_history_never_touched(self):
        day1 = reconcile_alerts(_ledger(), [_cand()], NOW)
        day2 = reconcile_alerts(_ledger(day1.records), [], LATER)
        old = day2.records[0]
        day3 = reconcile_alerts(_ledger(day2.records), [], LATER)
        assert day3.records[0] == old                      # bit-identical


class TestCandidateMappers:
    def test_relative_breach_maps(self):
        b = evaluate_inverter_relative([
            InverterReading("GTO1", "INV1", 829, 150.0),
            InverterReading("GTO1", "INV2", 800, 150.0),
            InverterReading("GTO1", "INV3", 0, 150.0),
        ])
        crit = [x for x in b if x.severity is Severity.CRITICAL][0]
        c = candidate_from_relative_breach(crit)
        assert c.metric == "inverter_relative"
        assert c.alert_key == "gto1:inv:inv3:inverter_relative"
        assert c.severity == "CRITICAL" and c.inverter_sn == "INV3"

    def test_twin_breach_maps(self):
        b = evaluate_plant_twins({"SLP1": 2.0, "SLP2": 4.0})[0]
        c = candidate_from_twin_breach(b)
        assert c.metric == "plant_twin_yield"
        assert c.alert_key == "slp1:plant:plant_twin_yield"
        assert c.inverter_sn == ""

    def test_expected_breach_maps(self):
        b = evaluate_energy_vs_expected({"GTO1": 963.0}, {"GTO1": 2478.0})[0]
        c = candidate_from_expected_breach(b)
        assert c.metric == "energy_daily_pct"
        assert c.alert_key == "gto1:plant:energy_daily_pct"
        assert c.value == 0.389


class TestScriptLoadingPath:
    """Regression for the 2026-07-03 dry-run crash: compute_plant_energy
    returns sn -> EnergyDay OBJECTS, not floats. This test drives the
    script's actual telemetry->readings->candidates path end to end so a
    type mismatch there can never ship silently again."""

    @staticmethod
    def _row(hour, etoday_kwh, sn, plant="GTO1", status=1):
        from argia.kpi.reader import InverterRow
        return InverterRow(
            timestamp_utc=dt.datetime(2026, 7, 2, hour, 0, tzinfo=UTC),
            plant_key=plant, inverter_sn=sn, inverter_label="",
            vendor="", status=status,
            power_w=None, etoday_kwh=etoday_kwh, temperature_c=None,
            fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
            cloud_cover_pct=None, ambient_temp_c=None,
        )

    def test_energyday_objects_flow_into_candidates(self):
        from argia.kpi.energy import compute_plant_energy
        from scripts.alerts_daily import build_candidates
        from argia.analytics.inverter_health import InverterReading

        # Healthy INV1/INV2, dead INV3 — through the REAL energy pipeline.
        rows = []
        for sn, final in (("INV1", 800.0), ("INV2", 780.0), ("INV3", 0.0)):
            rows += [self._row(8, final * 0.2, sn),
                     self._row(12, final * 0.7, sn),
                     self._row(18, final, sn)]
        readings = []
        for sn, eday in compute_plant_energy(rows).items():
            assert not isinstance(eday, float)          # it IS an object
            if eday.energy_kwh is None:
                continue
            readings.append(InverterReading("GTO1", sn,
                                            eday.energy_kwh, 150.0))
        cands = build_candidates(readings, {})          # no plant rows needed
        crit = [c for c in cands if c.severity == "CRITICAL"]
        assert len(crit) == 1 and crit[0].inverter_sn == "INV3"
