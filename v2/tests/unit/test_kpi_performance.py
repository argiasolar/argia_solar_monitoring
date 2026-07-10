"""Tests for argia.kpi.performance."""

from __future__ import annotations

import pytest

from argia.kpi.energy import EnergyDay
from argia.kpi.irradiance import IrradianceDay, IrradianceSource
from argia.kpi.performance import (
    Confidence,
    compute_inverter_peer_ranking,
    compute_plant_pr,
)


# ============================================================
# Helpers
# ============================================================


def _energy(kwh, reboot=False):
    return EnergyDay(
        energy_kwh=kwh, energy_kwh_max=kwh, energy_kwh_last=kwh,
        rows_seen=60, rows_online=60, detected_reboot=reboot,
        discrepancy_pct=0.0,
    )


def _irradiance(kwh_m2, source=IrradianceSource.SHINEMASTER, samples=120):
    return IrradianceDay(kwh_m2=kwh_m2, source=source, samples_used=samples)


# ============================================================
# PR computation
# ============================================================


class TestPrSunny:
    """A 500 kWp plant on a 6 kWh/m² day producing 2400 kWh:
    PR = 2400 / (500 × 6) = 0.80 (excellent for Mexico)"""

    def test_basic_pr_calculation(self):
        per_inv = {
            "A": _energy(1200.0), "B": _energy(1200.0),
        }  # total 2400
        result = compute_plant_pr(
            plant_key="QRO1", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=2,
        )
        assert result.pr == 0.80
        assert result.energy_kwh == 2400.0
        assert result.pr_confidence == Confidence.HIGH

    def test_capacity_factor(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        # CF = 2400 / (400 × 24) = 0.25
        assert result.capacity_factor == 0.25
        assert result.capacity_factor_confidence == Confidence.HIGH


class TestPrMissingData:
    def test_pr_none_when_no_irradiance(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=IrradianceDay(None, IrradianceSource.NONE, 0),
            inverter_count_expected=1,
        )
        assert result.pr is None
        assert result.pr_confidence == Confidence.NONE
        # Capacity factor still works without irradiance
        assert result.capacity_factor is not None
        assert "no usable irradiance" in result.notes

    def test_pr_none_when_no_energy(self):
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter={},
            irradiance=_irradiance(6.0),
            inverter_count_expected=2,
        )
        assert result.pr is None
        assert result.energy_kwh is None

    def test_pr_none_when_kwp_dc_zero(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=0.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        assert result.pr is None
        assert "kwp_dc is 0" in result.notes

    def test_capacity_factor_none_when_kwp_ac_zero(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=0.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        assert result.capacity_factor is None
        assert result.pr is not None  # PR still works


# ============================================================
# Confidence flags
# ============================================================


class TestConfidence:
    def test_high_confidence_with_full_shinemaster(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0, samples=120),
            inverter_count_expected=1,
        )
        assert result.pr_confidence == Confidence.HIGH
        assert result.capacity_factor_confidence == Confidence.HIGH

    def test_medium_confidence_with_sparse_shinemaster(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0, samples=20),  # < 60 = medium
            inverter_count_expected=1,
        )
        assert result.pr_confidence == Confidence.MEDIUM

    def test_low_confidence_with_cloud_model(self):
        per_inv = {"A": _energy(2400.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0, IrradianceSource.CLOUD_COVER_MODEL, 1),
            inverter_count_expected=1,
        )
        assert result.pr_confidence == Confidence.LOW

    def test_cf_confidence_medium_with_reboot(self):
        per_inv = {"A": _energy(2400.0, reboot=True)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        assert result.capacity_factor_confidence == Confidence.MEDIUM
        assert result.inverters_with_reboot == 1
        assert "reboot" in result.notes

    def test_cf_confidence_low_when_partial_inverters(self):
        """Plant has 4 expected but only 2 reported."""
        per_inv = {"A": _energy(1200.0), "B": _energy(1200.0)}
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=4,
        )
        assert result.capacity_factor_confidence == Confidence.LOW


# ============================================================
# Implausible values
# ============================================================


class TestImplausible:
    def test_pr_over_1_2_flagged(self):
        """PR > 1.2 is almost always a config error."""
        per_inv = {"A": _energy(5000.0)}  # way too much for 500kW × 6kWh/m²
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        assert result.pr > 1.2
        assert "implausible" in result.notes.lower()

    def test_cf_over_1_flagged(self):
        per_inv = {"A": _energy(20000.0)}  # 400kW × 24h × 1 = 9600 max
        result = compute_plant_pr(
            plant_key="X", date_iso="2026-05-14",
            kwp_dc=500.0, kwp_ac=400.0,
            energy_per_inverter=per_inv,
            irradiance=_irradiance(6.0),
            inverter_count_expected=1,
        )
        assert result.capacity_factor > 1.0
        assert "CF=" in result.notes


# ============================================================
# Peer ranking
# ============================================================


class TestPeerRanking:
    def test_basic_ranking(self):
        """3 inverters at same plant, different sizes & outputs:
            A: 100 kWp, 500 kWh → 5.0 kWh/kWp (high)
            B: 100 kWp, 400 kWh → 4.0 kWh/kWp (mid)
            C: 100 kWp, 300 kWh → 3.0 kWh/kWp (low)
        Peer mean = 4.0; A is +25%, C is -25%"""
        per_inv = {
            "A": _energy(500.0), "B": _energy(400.0), "C": _energy(300.0),
        }
        meta = {
            "A": {"rated_kw": 100.0, "inverter_label": "Inv 1"},
            "B": {"rated_kw": 100.0, "inverter_label": "Inv 2"},
            "C": {"rated_kw": 100.0, "inverter_label": "Inv 3"},
        }
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        assert len(ranks) == 3
        # Best first
        assert ranks[0].inverter_sn == "A"
        assert ranks[0].specific_yield_kwh_per_kwp == 5.0
        assert ranks[0].relative_to_peer == pytest.approx(1.25)
        assert ranks[-1].inverter_sn == "C"
        assert ranks[-1].relative_to_peer == pytest.approx(0.75)

    def test_different_sized_inverters(self):
        """A 100kW unit producing 500 kWh is FASTER per kWp than a 200kW
        unit producing 600 kWh."""
        per_inv = {"A": _energy(500.0), "B": _energy(600.0)}
        meta = {
            "A": {"rated_kw": 100.0}, "B": {"rated_kw": 200.0},
        }
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        # A: 5.0/kWp, B: 3.0/kWp → A wins
        assert ranks[0].inverter_sn == "A"

    def test_missing_rated_kw_yields_none(self):
        per_inv = {"A": _energy(500.0)}
        meta = {"A": {"rated_kw": None}}
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        assert ranks[0].specific_yield_kwh_per_kwp is None
        assert ranks[0].relative_to_peer is None

    def test_missing_energy_yields_none(self):
        per_inv = {
            "A": _energy(500.0),
            "B": EnergyDay(None, None, None, 0, 0, False, None),
        }
        meta = {"A": {"rated_kw": 100.0}, "B": {"rated_kw": 100.0}}
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        # A's relative is comparison to peer mean which is A's own yield (5.0)
        # since B has no value → A relative = 1.0
        a = next(r for r in ranks if r.inverter_sn == "A")
        b = next(r for r in ranks if r.inverter_sn == "B")
        assert a.specific_yield_kwh_per_kwp == 5.0
        assert b.specific_yield_kwh_per_kwp is None

    def test_nones_sort_to_end(self):
        per_inv = {
            "A": _energy(500.0),
            "B": EnergyDay(None, None, None, 0, 0, False, None),
            "C": _energy(300.0),
        }
        meta = {sn: {"rated_kw": 100.0} for sn in ("A", "B", "C")}
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        # A and C have yields (sorted desc), B is None at end
        assert ranks[-1].inverter_sn == "B"
        assert ranks[0].inverter_sn == "A"

    def test_includes_inverters_without_telemetry(self):
        """Inverter exists in Inverters tab but reported no rows today.
        Must still appear in ranking, with None values."""
        per_inv = {"A": _energy(500.0)}
        meta = {"A": {"rated_kw": 100.0}, "B": {"rated_kw": 100.0}}
        ranks = compute_inverter_peer_ranking("P", per_inv, meta)
        sns = {r.inverter_sn for r in ranks}
        assert sns == {"A", "B"}

    def test_empty_returns_empty(self):
        assert compute_inverter_peer_ranking("P", {}, {}) == []


class TestHistorySourceConfidence:
    """Regression for the enum fall-through found 2026-07-10: every day
    since the dense pipeline became primary was stamped
    pr_confidence=NONE because _confidence_from_irradiance never
    learned SHINEMASTER_HISTORY — punishing the HIGHEST-quality
    irradiance source (stored minute-scale history, ~300 samples/day,
    validated <1% vs an independent model) and starving the soiling
    estimator of qualifying days."""

    def _ir(self, source, samples):
        from argia.kpi.irradiance import IrradianceDay
        return IrradianceDay(kwh_m2=5.0, source=source,
                             samples_used=samples)

    def test_dense_history_is_high(self):
        from argia.kpi.irradiance import IrradianceSource
        from argia.kpi.performance import (
            Confidence, _confidence_from_irradiance,
        )
        ir = self._ir(IrradianceSource.SHINEMASTER_HISTORY, 300)
        assert _confidence_from_irradiance(ir, True) is Confidence.HIGH

    def test_sparse_history_is_medium(self):
        from argia.kpi.irradiance import IrradianceSource
        from argia.kpi.performance import (
            Confidence, _confidence_from_irradiance,
        )
        ir = self._ir(IrradianceSource.SHINEMASTER_HISTORY, 12)
        assert _confidence_from_irradiance(ir, True) is Confidence.MEDIUM

    def test_live_shinemaster_grading_unchanged(self):
        from argia.kpi.irradiance import IrradianceSource
        from argia.kpi.performance import (
            Confidence, _confidence_from_irradiance,
        )
        assert _confidence_from_irradiance(
            self._ir(IrradianceSource.SHINEMASTER, 90), True) \
            is Confidence.HIGH
        assert _confidence_from_irradiance(
            self._ir(IrradianceSource.CLOUD_COVER_MODEL, 90), True) \
            is Confidence.LOW
        assert _confidence_from_irradiance(
            self._ir(IrradianceSource.SHINEMASTER_HISTORY, 300), False) \
            is Confidence.NONE   # no energy -> still NONE
