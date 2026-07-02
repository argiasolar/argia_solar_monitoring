"""Tests for argia.analytics.inverter_health (inverter_relative detection).

The headline test drives the detector with REAL data: MEX1 Inverter 2 dead
(online, 0 W) while its siblings produced ~79 kW on 2026-07-02. Expected
result: inv2 -> CRITICAL, siblings -> nothing.
"""

from __future__ import annotations

import json
import pathlib

from argia.analytics.inverter_health import (
    DEFAULT_CRIT_BELOW,
    DEFAULT_WARN_BELOW,
    InverterReading,
    RelativeBreach,
    evaluate_inverter_relative,
)
from argia.core.thresholds import Severity

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "health"


def _r(plant, sn, value, rated_kw=None):
    return InverterReading(plant_key=plant, inverter_sn=sn, value=value, rated_kw=rated_kw)


# --------------------------------------------------------------------------
class TestMex1RealFixture:
    """The reason this module exists."""

    def _load(self):
        data = json.loads((FIXTURES / "mex1_inv2_dead_20260702.json").read_text("utf-8"))
        readings = [_r(x["plant_key"], x["inverter_sn"], x["power_w"], x.get("rated_kw"))
                    for x in data["readings"]]
        return data, readings

    def test_dead_online_inverter_flagged_critical(self):
        data, readings = self._load()
        # A production floor well below the ~79 kW siblings, so the plant IS judged.
        breaches = evaluate_inverter_relative(readings, min_peer_floor=1000.0)
        by_sn = {b.inverter_sn: b for b in breaches}

        # Exactly the expected inverter breaches, at the expected severity.
        assert set(by_sn) == set(data["expected_breaches"])
        for sn, sev_name in data["expected_breaches"].items():
            assert by_sn[sn].severity == Severity(sev_name)

    def test_dead_inverter_ratio_is_zero(self):
        _, readings = self._load()
        b = evaluate_inverter_relative(readings, min_peer_floor=1000.0)[0]
        assert b.inverter_sn == "ES2470051826"
        assert b.ratio == 0.0
        assert b.severity == Severity.CRITICAL
        # peer mean = (79099 + 78306) / 2
        assert b.peer_mean == (79099.0 + 78306.0) / 2

    def test_healthy_siblings_not_flagged(self):
        _, readings = self._load()
        flagged = {b.inverter_sn for b in evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert "ES2470051825" not in flagged
        assert "GR2489022511" not in flagged


# --------------------------------------------------------------------------
class TestSeverityBands:
    PLANT = "TEST"

    def _run(self, victim_value, peers, **kw):
        readings = [_r(self.PLANT, "V", victim_value)]
        readings += [_r(self.PLANT, f"P{i}", v) for i, v in enumerate(peers)]
        return evaluate_inverter_relative(readings, min_peer_floor=1.0, **kw)

    def test_warning_band(self):
        # victim at 80% of a uniform peer mean -> between 0.70 and 0.85 -> WARNING
        b = self._run(80.0, [100.0, 100.0])
        assert len(b) == 1 and b[0].inverter_sn == "V"
        assert b[0].severity == Severity.WARNING

    def test_healthy_not_flagged(self):
        # victim at 90% of peers -> above 0.85 -> no breach
        assert self._run(90.0, [100.0, 100.0]) == []

    def test_critical_band(self):
        b = self._run(50.0, [100.0, 100.0])
        assert b[0].severity == Severity.CRITICAL

    def test_critical_takes_precedence_over_warning(self):
        b = self._run(10.0, [100.0, 100.0])
        assert b[0].severity == Severity.CRITICAL

    def test_boundary_exactly_at_warn_is_not_a_breach(self):
        # ratio == 0.85 exactly -> strict '<' means NOT below -> no breach
        assert self._run(85.0, [100.0, 100.0]) == []

    def test_boundary_exactly_at_crit_is_warning_not_critical(self):
        # ratio == 0.70 exactly -> not below crit, but below warn -> WARNING
        b = self._run(70.0, [100.0, 100.0])
        assert b[0].severity == Severity.WARNING


# --------------------------------------------------------------------------
class TestPeerLogic:
    def test_single_inverter_plant_skipped(self):
        # No peers -> cannot judge -> no breach ever.
        assert evaluate_inverter_relative([_r("SOLO", "X", 0.0)], min_peer_floor=1.0) == []

    def test_leave_one_out_mean_healthy_survivors(self):
        # One dead of three: the dead one flags, the two producers do NOT,
        # even though the dead unit drags down their leave-one-out peer mean.
        readings = [_r("P", "a", 79000.0), _r("P", "b", 0.0), _r("P", "c", 78000.0)]
        flagged = {b.inverter_sn: b.severity for b in
                   evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert flagged == {"b": Severity.CRITICAL}

    def test_two_dead_of_three_flags_both_dead_skips_survivor(self):
        # a alive (79k), b & c dead (0). Survivor a's peer mean = mean(0,0) = 0
        # -> below floor -> skipped (not mis-flagged). b and c each have peer
        # mean = mean(79k, 0) = 39.5k -> above floor -> CRITICAL.
        readings = [_r("P", "a", 79000.0), _r("P", "b", 0.0), _r("P", "c", 0.0)]
        flagged = {b.inverter_sn: b.severity for b in
                   evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert flagged == {"b": Severity.CRITICAL, "c": Severity.CRITICAL}


# --------------------------------------------------------------------------
class TestFloorGating:
    def test_night_all_low_no_false_positive(self):
        # Dawn/night: everyone near zero. Peer means fall below the floor, so no
        # inverter is judged -> no false CRITICALs.
        readings = [_r("P", "a", 5.0), _r("P", "b", 0.0), _r("P", "c", 8.0)]
        assert evaluate_inverter_relative(readings, min_peer_floor=1000.0) == []

    def test_floor_zero_default_is_permissive(self):
        # With the default floor 0.0, a near-zero peer mean is still judged —
        # this documents WHY the engine must pass a real floor.
        readings = [_r("P", "a", 5.0), _r("P", "b", 0.0), _r("P", "c", 8.0)]
        breaches = evaluate_inverter_relative(readings)  # floor defaults to 0.0
        # b at 0 vs peer mean 6.5 -> ratio 0 -> CRITICAL (a false positive at night)
        assert any(b.inverter_sn == "b" and b.severity == Severity.CRITICAL
                   for b in breaches)


# --------------------------------------------------------------------------
class TestNameplateNormalization:
    """The real GTO1 case: a 60 kW inverter among 124 kW peers must NOT be
    flagged just for being smaller, once nameplates are supplied."""

    # All three at the SAME specific output (~155 W/kW): a healthy plant that
    # happens to mix a 60 kW unit with two 124 kW units.
    _EQUAL_SPEC = [
        ("small", 9300.0, 60.0),    # 155 W/kW
        ("big1", 19220.0, 124.0),   # 155 W/kW
        ("big2", 19220.0, 124.0),   # 155 W/kW
    ]

    def test_small_inverter_not_flagged_when_specific_output_is_fine(self):
        # With nameplates, equal per-kW output -> every ratio 1.0 -> no breach.
        readings = [_r("GTO1", sn, v, rated_kw=k) for sn, v, k in self._EQUAL_SPEC]
        assert evaluate_inverter_relative(readings, min_peer_floor=1000.0) == []

    def test_same_case_raw_comparison_false_positives(self):
        # Drop the nameplates -> raw comparison unfairly flags the small unit
        # (9300 vs ~19220 peer mean = 0.48 -> CRITICAL).
        readings = [_r("GTO1", sn, v) for sn, v, _ in self._EQUAL_SPEC]
        flagged = {b.inverter_sn for b in evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert "small" in flagged  # documents WHY normalization matters

    def test_dead_inverter_still_flagged_with_nameplates(self):
        # Normalization must not rescue a genuinely dead unit.
        readings = [
            _r("MEX1", "dead", 0.0, rated_kw=150.0),
            _r("MEX1", "ok1", 79000.0, rated_kw=150.0),
            _r("MEX1", "ok2", 78000.0, rated_kw=150.0),
        ]
        flagged = {b.inverter_sn: b.severity for b in
                   evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert flagged == {"dead": Severity.CRITICAL}

    def test_partial_missing_nameplate_falls_back_to_raw(self):
        # If ANY inverter lacks a nameplate, the plant uses raw comparison,
        # so the small unit is flagged again despite being fine per-kW.
        readings = [
            _r("GTO1", "small", 9300.0, rated_kw=60.0),
            _r("GTO1", "big1", 19220.0, rated_kw=None),  # missing
            _r("GTO1", "big2", 19220.0, rated_kw=124.0),
        ]
        flagged = {b.inverter_sn for b in evaluate_inverter_relative(readings, min_peer_floor=1000.0)}
        assert "small" in flagged  # raw fallback -> small flagged again


# --------------------------------------------------------------------------
class TestMultiPlantAndShape:
    def test_multiple_plants_grouped_independently(self):
        readings = [
            _r("MEX1", "m1", 79000.0), _r("MEX1", "m2", 0.0), _r("MEX1", "m3", 78000.0),
            _r("SLP1", "s1", 100.0), _r("SLP1", "s2", 95.0),  # both healthy
        ]
        flagged = {b.inverter_sn for b in
                   evaluate_inverter_relative(readings, min_peer_floor=50.0)}
        assert flagged == {"m2"}

    def test_empty_input(self):
        assert evaluate_inverter_relative([]) == []

    def test_deterministic_sort(self):
        readings = [
            _r("PB", "z", 0.0), _r("PB", "y", 100.0), _r("PB", "x", 100.0),
            _r("PA", "b", 0.0), _r("PA", "a", 100.0), _r("PA", "c", 100.0),
        ]
        out = evaluate_inverter_relative(readings, min_peer_floor=10.0)
        keys = [(b.plant_key, b.inverter_sn) for b in out]
        assert keys == sorted(keys)

    def test_default_thresholds_match_sheet_values(self):
        # Guard: the module defaults track the Thresholds tab (0.85 / 0.70).
        assert DEFAULT_WARN_BELOW == 0.85
        assert DEFAULT_CRIT_BELOW == 0.70

    def test_returns_relativebreach_instances(self):
        readings = [_r("P", "a", 0.0), _r("P", "b", 100.0), _r("P", "c", 100.0)]
        out = evaluate_inverter_relative(readings, min_peer_floor=10.0)
        assert all(isinstance(b, RelativeBreach) for b in out)
        assert out[0].message  # human-readable message populated
