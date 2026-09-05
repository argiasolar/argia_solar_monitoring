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

    def test_escalation_rearms_the_mail(self):
        """v202: a WARNING that was mailed and then becomes CRITICAL must be
        mailed again — channels_sent is cleared on escalation only."""
        from argia.core.alerts_state import mark_channels_sent
        day1 = reconcile_alerts(_ledger(), [_cand(sev="WARNING", threshold=0.85)], NOW)
        mailed = [mark_channels_sent(day1.records[0], ["email"])]
        same = reconcile_alerts(_ledger(mailed), [_cand(sev="WARNING", threshold=0.85)], LATER)
        assert same.records[0].channels_sent == "email"          # touched, not re-armed
        up = reconcile_alerts(_ledger(mailed), [_cand(sev="CRITICAL", threshold=0.70)], LATER)
        assert up.records[0].severity == "CRITICAL" and up.records[0].channels_sent == ""
        down = reconcile_alerts(_ledger(up.records), [_cand(sev="WARNING", threshold=0.85)],
                                LATER + dt.timedelta(days=1))
        assert down.records[0].severity == "WARNING" and down.records[0].channels_sent == ""

    def test_acute_tier_never_de_escalates(self):
        """v202: NL1 case — 81 degC at noon (CRITICAL), 66 degC at 18:00
        (WARNING candidate). The snapshot tier keeps CRITICAL and the peak
        message; only the daily tier (resolve_missing=True) lowers it."""
        crit = _cand(metric="inverter_temp_high", key="nl1:inv:i9:inverter_temp_high",
                     sev="CRITICAL", value=81.6, threshold=75.0, msg="81.6 degC")
        warn = _cand(metric="inverter_temp_high", key="nl1:inv:i9:inverter_temp_high",
                     sev="WARNING", value=66.0, threshold=65.0, msg="66.0 degC")
        noon = reconcile_alerts(_ledger(), [crit], NOW, resolve_missing=False)
        evening = reconcile_alerts(_ledger(noon.records), [warn],
                                   NOW + dt.timedelta(hours=6), resolve_missing=False)
        r = evening.records[0]
        assert r.severity == "CRITICAL" and r.value == 81.6 and r.message == "81.6 degC"
        assert r.last_seen_utc.startswith("2026-07-03T18:00")           # still touched
        assert evening.touched and not evening.opened
        # the acute tier still escalates upward
        up = reconcile_alerts(_ledger(reconcile_alerts(_ledger(), [warn], NOW,
                                                       resolve_missing=False).records),
                              [crit], LATER, resolve_missing=False)
        assert up.records[0].severity == "CRITICAL" and up.records[0].value == 81.6
        # the daily tier may lower it
        daily = reconcile_alerts(_ledger(evening.records), [warn], LATER, resolve_missing=True)
        assert daily.records[0].severity == "WARNING" and daily.records[0].value == 66.0

    def test_foreign_metric_rows_left_alone(self):
        # A manual/other-engine OPEN row (metric NOT in ENGINE_METRICS, e.g.
        # pr_daily) must never be auto-resolved when absent from candidates.
        day1 = reconcile_alerts(
            _ledger(), [_cand(key="slp1:plant:pr_daily", plant="SLP1",
                              sn="", metric="pr_daily", sev="WARNING")], NOW)
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


class TestDailyTemperatureHysteresis:
    """v202: an OPEN temperature alert resolves only after a full day
    below TEMP_CLEAR_C (60), not the moment the peak dips under WARN."""

    class _Bundle:
        def __init__(self, rows):
            self._rows = rows

        def rows_for_plant(self, pk):
            return [r for r in self._rows if r.plant_key == pk]

    class _Plant:
        plant_key = "MEX1"

    class _Portfolio:
        def active_plants(self):
            return [TestDailyTemperatureHysteresis._Plant()]

    def _cands(self, temps, open_keys=frozenset()):
        from scripts.alerts_daily import daily_temp_candidates
        rows = [_temp_row(t, sn) for sn, t in temps]
        return daily_temp_candidates(self._Bundle(rows), self._Portfolio(), open_keys)

    def test_thresholds_unchanged_for_new_alerts(self):
        assert self._cands([("ES1", 62.0)]) == []
        w = self._cands([("ES1", 66.5)])
        assert [c.severity for c in w] == ["WARNING"] and w[0].value == 66.5
        c = self._cands([("ES1", 76.2)])
        assert [x.severity for x in c] == ["CRITICAL"] and c[0].threshold == 75.0

    def test_open_alert_survives_a_62_degree_day_and_clears_at_59(self):
        key = "mex1:inv:es1:inverter_temp_high"
        held = self._cands([("ES1", 62.0)], frozenset({key}))
        assert len(held) == 1 and held[0].severity == "WARNING"
        assert held[0].alert_key == key and "clear level 60" in held[0].message
        assert self._cands([("ES1", 59.9)], frozenset({key})) == []
        # another inverter's key does not keep this one open
        assert self._cands([("ES1", 62.0)], frozenset({"mex1:inv:es2:inverter_temp_high"})) == []

    def test_script_passes_the_open_keys(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alerts_daily.py"
               ).read_text(encoding="utf-8")
        assert "daily_temp_candidates(bundle, portfolio, open_temp_keys)" in src
        assert src.index("ledger = load_alerts_ledger(sheets)") < src.index("candidates = build_candidates(")


def _temp_row(temp, sn):
    from argia.kpi.reader import InverterRow
    return InverterRow(
        timestamp_utc=dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
        plant_key="MEX1", inverter_sn=sn, inverter_label="", vendor="", status=1,
        power_w=1000.0, etoday_kwh=100.0, temperature_c=temp, fault_code="",
        irradiance_wm2=None, irradiance_kwh_m2_5m=None, cloud_cover_pct=None,
        ambient_temp_c=None)
