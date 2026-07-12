"""Maintenance alert suppression (v92).

The rule under test: when a plant is in a logged maintenance window, its
plant-level "down / underproducing" candidates are dropped before
reconcile, so no critical opens — while hardware faults on the SAME plant
and any alert on OTHER plants pass through untouched. The integration
test proves a suppressed candidate never reaches the ledger and a
non-suppressed one does.
"""

import datetime as dt

from argia.alerts.engine import (
    Candidate, MAINTENANCE_SUPPRESSED_METRICS,
    apply_maintenance_suppression, reconcile_alerts,
)
from argia.core.alerts_state import AlertsLedger
from argia.core.time_utils import UTC

NOW = dt.datetime(2026, 7, 14, 13, 0, tzinfo=UTC)


def _cand(plant="GTO1", metric="energy_daily_pct", sn="",
          sev="CRITICAL"):
    key = f"{plant.lower()}:plant:{metric}" if not sn \
        else f"{plant.lower()}:inv:{sn.lower()}:{metric}"
    return Candidate(alert_key=key, plant_key=plant, inverter_sn=sn,
                     metric=metric, severity=sev, value=0.0,
                     threshold=0.85, message=f"{plant} {metric}")


class TestSuppressionTruthTable:
    def test_suppressed_metrics_are_the_plant_down_family(self):
        assert MAINTENANCE_SUPPRESSED_METRICS == frozenset({
            "energy_daily_pct", "plant_offline", "data_stale",
            "plant_twin_yield"})

    def test_suppressible_metric_on_maint_plant_dropped(self):
        for metric in MAINTENANCE_SUPPRESSED_METRICS:
            kept, supp = apply_maintenance_suppression(
                [_cand(metric=metric)], {"GTO1"})
            assert kept == []
            assert len(supp) == 1

    def test_hardware_fault_on_maint_plant_kept(self):
        # a genuine fault during maintenance still surfaces
        c = _cand(metric="inverter_fault", sn="INV3")
        kept, supp = apply_maintenance_suppression([c], {"GTO1"})
        assert kept == [c]
        assert supp == []

    def test_other_plant_unaffected(self):
        c = _cand(plant="MEX2", metric="energy_daily_pct")
        kept, supp = apply_maintenance_suppression([c], {"GTO1"})
        assert kept == [c]
        assert supp == []

    def test_mixed_batch(self):
        cands = [
            _cand(plant="GTO1", metric="energy_daily_pct"),      # supp
            _cand(plant="GTO1", metric="plant_offline"),         # supp
            _cand(plant="GTO1", metric="inverter_fault", sn="X"),  # kept
            _cand(plant="MEX2", metric="energy_daily_pct"),      # kept
        ]
        kept, supp = apply_maintenance_suppression(cands, {"GTO1"})
        assert len(kept) == 2
        assert len(supp) == 2
        assert {c.metric for c in supp} == {"energy_daily_pct",
                                            "plant_offline"}

    def test_no_maintenance_plants_is_noop(self):
        cands = [_cand(), _cand(plant="MEX2")]
        kept, supp = apply_maintenance_suppression(cands, set())
        assert kept == cands
        assert supp == []


class TestSuppressionReconcileIntegration:
    def test_suppressed_candidate_never_opens_an_alert(self):
        # GTO1 underproducing (would open a CRITICAL) but it is under
        # maintenance; MEX2 underproducing is real and must open.
        cands = [
            _cand(plant="GTO1", metric="energy_daily_pct"),
            _cand(plant="MEX2", metric="energy_daily_pct"),
        ]
        kept, _ = apply_maintenance_suppression(cands, {"GTO1"})
        result = reconcile_alerts(AlertsLedger(records=[]), kept, NOW)
        opened_plants = {r.plant_key for r in result.opened}
        assert opened_plants == {"MEX2"}   # GTO1 suppressed, never opened

    def test_existing_alert_resolves_when_plant_enters_maintenance(self):
        # day 1: GTO1 underproduction opens. day 2: GTO1 goes into a
        # maintenance window, candidate suppressed → daily tier resolves
        # the open alert (acknowledged as maintenance).
        day1 = reconcile_alerts(
            AlertsLedger(records=[]),
            [_cand(plant="GTO1", metric="energy_daily_pct")], NOW)
        assert len(day1.opened) == 1
        later = NOW + dt.timedelta(days=1)
        kept, supp = apply_maintenance_suppression(
            [_cand(plant="GTO1", metric="energy_daily_pct")], {"GTO1"})
        assert kept == [] and len(supp) == 1
        day2 = reconcile_alerts(AlertsLedger(records=day1.records), kept,
                                later, resolve_missing=True)
        assert len(day2.resolved) == 1
