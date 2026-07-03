"""Tests for alert explanations: catalog completeness + wiring."""

from __future__ import annotations

import datetime as dt

from argia.alerts.engine import (
    ENGINE_METRICS,
    Candidate,
    reconcile_alerts,
)
from argia.alerts.explanations import catalog_metrics, explain
from argia.core.alerts_state import AlertsLedger
from argia.core.time_utils import UTC

NOW = dt.datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class TestCatalogCompleteness:
    def test_every_engine_metric_has_an_explanation(self):
        # An alert nobody can interpret must not ship: adding a metric to
        # ENGINE_METRICS without a catalog entry fails HERE, at dev time.
        missing = ENGINE_METRICS - catalog_metrics()
        assert not missing, f"metrics without explanation: {sorted(missing)}"

    def test_every_explanation_names_meaning_and_action(self):
        for metric in ENGINE_METRICS:
            text = explain(metric, "WARNING")
            assert len(text) > 80, metric
            assert "What to check:" in text, metric


class TestExplainRendering:
    def test_critical_gets_urgency_prefix(self):
        assert explain("inverter_fault", "CRITICAL").startswith(
            "Needs attention now.")

    def test_warning_gets_watch_prefix(self):
        assert explain("energy_daily_pct", "WARNING").startswith(
            "Worth watching")

    def test_unknown_metric_is_empty_not_error(self):
        assert explain("no_such_metric", "CRITICAL") == ""


class TestEngineFillsExplanation:
    def _cand(self, sev="CRITICAL"):
        return Candidate(alert_key="gto1:inv:a:inverter_fault",
                         plant_key="GTO1", inverter_sn="A",
                         metric="inverter_fault", severity=sev,
                         value=2.0, threshold=None, message="m")

    def test_open_carries_explanation(self):
        r = reconcile_alerts(AlertsLedger(records=[]), [self._cand()], NOW)
        rec = r.opened[0]
        assert rec.explanation.startswith("Needs attention now.")
        assert "vendor portal" in rec.explanation

    def test_escalation_refreshes_urgency(self):
        d1 = reconcile_alerts(AlertsLedger(records=[]),
                              [self._cand(sev="WARNING")], NOW)
        assert d1.opened[0].explanation.startswith("Worth watching")
        d2 = reconcile_alerts(AlertsLedger(records=d1.records),
                              [self._cand(sev="CRITICAL")],
                              NOW + dt.timedelta(days=1))
        assert d2.records[0].explanation.startswith("Needs attention now.")

    def test_roundtrip_through_row(self):
        from argia.core.alerts_state import ALERTS_HEADER, record_to_row
        r = reconcile_alerts(AlertsLedger(records=[]), [self._cand()], NOW)
        row = record_to_row(r.opened[0])
        assert len(row) == len(ALERTS_HEADER) == 15
        assert row[-1] == r.opened[0].explanation
