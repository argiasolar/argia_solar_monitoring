"""Daily performance indicators — plan #4.

Three layered detectors, all pure (no I/O), each answering a different
"is something underperforming?" question. Layer 1 reuses the existing
inverter-relative detector; this module adds layers 2 and 3.

Layer 1 — inverter vs same-plant peers
    :func:`argia.analytics.inverter_health.evaluate_inverter_relative`,
    fed DAILY ENERGY per inverter (kWh). Catches a lagging/dead unit that
    its siblings expose. Blind to whole-plant problems.

Layer 2 — plant vs regional twin (this module)
    SLP1<->SLP2 and MEX1<->MEX2 sit close enough to share weather, so their
    SPECIFIC YIELD (kWh/kWp) should track. A plant far below its twin is
    underperforming as a whole even if its inverters agree with each other
    (uniform soiling, grid curtailment, string outages spread evenly).
    GTO1 and NL1 have no twin — layers 1 and 3 cover them.

Layer 3 — energy vs expected (this module)
    energy_kwh / expected_kwh, where expected already folds in kwp,
    the day's irradiance, and expected_factor. The catch-all: it sees
    problems the peer comparisons can't, but inherits irradiance quality
    (sparse ShineMaster days until Stage 4 — treat with data_class in mind).

Severity uses the same two-level scheme as inverter_health: WARNING below
``warn_below``, CRITICAL below ``crit_below``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.analytics.inverter_health import Severity

LOG = logging.getLogger("argia.analytics.perf_indicators")

# Regional twin pairs: physically close plants that share weather, so their
# specific yields should track. Order inside a pair doesn't matter — both
# directions are checked. GTO1/NL1 have no twin by geography.
TWIN_PAIRS: List[Tuple[str, str]] = [
    ("SLP1", "SLP2"),
    ("MEX1", "MEX2"),
]

TWIN_WARN_BELOW = 0.85
TWIN_CRIT_BELOW = 0.70

EXPECTED_WARN_BELOW = 0.85
EXPECTED_CRIT_BELOW = 0.70

# Below this specific yield (kWh/kWp) the "better" twin produced so little
# that a ratio against it is noise, not signal (deep overcast, fault days).
MIN_TWIN_YIELD = 0.5


@dataclass(frozen=True)
class TwinBreach:
    """A plant far below its regional twin's specific yield."""

    plant_key: str          # the lagging plant
    twin_key: str           # the reference plant
    specific_yield: float
    twin_yield: float
    ratio: float
    severity: Severity
    threshold: float
    message: str


@dataclass(frozen=True)
class ExpectedBreach:
    """A plant far below its own expected energy for the day."""

    plant_key: str
    energy_kwh: float
    expected_kwh: float
    ratio: float
    severity: Severity
    threshold: float
    message: str


def evaluate_plant_twins(
    specific_yield_by_plant: Dict[str, Optional[float]],
    twin_pairs: List[Tuple[str, str]] = None,
    warn_below: float = TWIN_WARN_BELOW,
    crit_below: float = TWIN_CRIT_BELOW,
    min_twin_yield: float = MIN_TWIN_YIELD,
) -> List[TwinBreach]:
    """Flag plants whose specific yield lags their regional twin.

    ``specific_yield_by_plant`` maps plant_key -> kWh/kWp for one day
    (None / missing plants are skipped). Both directions of each pair are
    checked; only the lagging side is flagged. If the reference twin itself
    produced under ``min_twin_yield``, the day carries no signal and the
    pair is skipped — a ratio against near-zero flags nothing but noise.
    """
    pairs = TWIN_PAIRS if twin_pairs is None else twin_pairs
    breaches: List[TwinBreach] = []
    for a, b in pairs:
        for lag, ref in ((a, b), (b, a)):
            sy = specific_yield_by_plant.get(lag)
            ref_sy = specific_yield_by_plant.get(ref)
            if sy is None or ref_sy is None:
                continue
            if ref_sy < min_twin_yield:
                continue
            ratio = sy / ref_sy
            if ratio >= warn_below:
                continue
            severity = Severity.CRITICAL if ratio < crit_below else Severity.WARNING
            threshold = crit_below if severity is Severity.CRITICAL else warn_below
            breaches.append(TwinBreach(
                plant_key=lag,
                twin_key=ref,
                specific_yield=round(sy, 3),
                twin_yield=round(ref_sy, 3),
                ratio=round(ratio, 3),
                severity=severity,
                threshold=threshold,
                message=(
                    f"[{lag}] specific yield {sy:.2f} kWh/kWp is "
                    f"{ratio:.0%} of twin {ref} ({ref_sy:.2f}) — below "
                    f"{threshold:.0%}"
                ),
            ))
    return breaches


def evaluate_energy_vs_expected(
    energy_by_plant: Dict[str, Optional[float]],
    expected_by_plant: Dict[str, Optional[float]],
    warn_below: float = EXPECTED_WARN_BELOW,
    crit_below: float = EXPECTED_CRIT_BELOW,
) -> List[ExpectedBreach]:
    """Flag plants whose actual energy lags their expected energy.

    ``expected`` already folds in kwp, the day's irradiance, and
    expected_factor, so the ratio is weather-adjusted by construction.
    Plants with missing/non-positive energy or expected are skipped —
    absence of data is data_class's problem, not a performance breach.
    """
    breaches: List[ExpectedBreach] = []
    for pk, energy in energy_by_plant.items():
        expected = expected_by_plant.get(pk)
        if energy is None or expected is None or expected <= 0 or energy < 0:
            continue
        ratio = energy / expected
        if ratio >= warn_below:
            continue
        severity = Severity.CRITICAL if ratio < crit_below else Severity.WARNING
        threshold = crit_below if severity is Severity.CRITICAL else warn_below
        breaches.append(ExpectedBreach(
            plant_key=pk,
            energy_kwh=round(energy, 1),
            expected_kwh=round(expected, 1),
            ratio=round(ratio, 3),
            severity=severity,
            threshold=threshold,
            message=(
                f"[{pk}] produced {energy:.0f} kWh vs {expected:.0f} expected "
                f"({ratio:.0%}) — below {threshold:.0%}"
            ),
        ))
    return breaches
