"""Performance KPIs — Stage 7.2.

Pure math. Inputs are typed objects from energy.py / irradiance.py /
reader.py + plant/inverter configs. No I/O.

Three KPIs:
1. Performance Ratio (PR) — the gold standard for plant performance
2. Capacity factor — simpler, doesn't need irradiance
3. Inverter peer ranking — within a plant, which inverter is underperforming

PR formulas
-----------

**PR (standard, IEC 61724)**:
    PR = E_actual / (P_dc × H)
    where:
        E_actual = end-of-day energy, kWh
        P_dc = installed DC capacity, kWp
        H = plane-of-array irradiation, kWh/m²/day at 1000 W/m² STC

PR = 1.0 means: a perfectly clean panel facing the sun directly, with
zero inverter/cable/temperature losses. Real plants land between 0.70
(hot+dirty+old) and 0.88 (cool+clean+new). Mexico typically 0.75-0.82.

**Capacity factor** is unrelated to weather. It's the fraction of nameplate
AC output you achieved averaged over the whole 24h:
    CF = E_actual / (P_ac × 24h)

Useful when irradiance data is missing entirely. Mexican plants typically
CF = 0.18-0.25.

Both metrics get a "confidence" flag based on data quality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from argia.kpi.energy import EnergyDay, sum_inverter_energies
from argia.kpi.irradiance import IrradianceDay, IrradianceSource

LOG = logging.getLogger("argia.kpi.performance")

# Temperature coefficient of Pmax, per degC. Negative: power falls as the
# module heats up. -0.0035/degC (-0.35%/degC) is a standard crystalline-
# silicon default. It is a PLACEHOLDER until per-plant module-datasheet
# coefficients are available (see PlantConfig — no gamma field yet).
GAMMA_PMAX_DEFAULT = -0.0035

# Standard Test Conditions reference (cell) temperature.
T_STC_C = 25.0


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


@dataclass(frozen=True)
class PlantPerformanceDay:
    """End-of-day performance summary for one plant."""

    plant_key: str
    date_iso: str

    # Inputs
    kwp_dc: float
    kwp_ac: float

    # Energy
    energy_kwh: Optional[float]
    energy_per_inverter: Dict[str, EnergyDay]

    # Irradiance
    irradiance_kwh_m2: Optional[float]
    irradiance_source: IrradianceSource

    # KPIs
    pr: Optional[float]
    """Performance Ratio, [0, 1+]. Can exceed 1 with sensor errors;
    callers should sanity-cap when displaying."""

    capacity_factor: Optional[float]
    """Daily capacity factor = E_actual / (kwp_ac × 24h), [0, 1]."""

    # Confidence flag — tells alert engine / report whether to trust the PR
    pr_confidence: Confidence
    capacity_factor_confidence: Confidence

    # Data-quality counters
    inverters_with_data: int
    inverters_with_reboot: int

    # Diagnostic note when something interesting happened
    notes: str = ""

    # Temperature-corrected PR (to 25 degC cell). None when no module temp.
    pr_stc: Optional[float] = None
    # Daily irradiance-weighted module temperature used for the correction.
    module_temp_c: Optional[float] = None


def _confidence_from_irradiance(ir: IrradianceDay, energy_present: bool) -> Confidence:
    """Map an IrradianceDay + energy presence into a PR confidence flag."""
    if ir.kwh_m2 is None or not energy_present:
        return Confidence.NONE
    measured = (IrradianceSource.SHINEMASTER,
                IrradianceSource.SHINEMASTER_HISTORY)
    if ir.source in measured and ir.samples_used >= 60:
        return Confidence.HIGH
    if ir.source in measured:
        return Confidence.MEDIUM
    if ir.source == IrradianceSource.CLOUD_COVER_MODEL:
        return Confidence.LOW
    return Confidence.NONE


def _confidence_from_energy(
    energy: Optional[float],
    per_inverter: Dict[str, EnergyDay],
    inverter_count_expected: int,
) -> Confidence:
    """Capacity-factor confidence comes from how many inverters reported
    AND whether any of them had reboots."""
    if energy is None or energy <= 0:
        return Confidence.NONE
    reported = sum(1 for e in per_inverter.values() if e.energy_kwh is not None)
    if inverter_count_expected and reported < inverter_count_expected:
        return Confidence.LOW
    if any(e.detected_reboot for e in per_inverter.values()):
        return Confidence.MEDIUM
    return Confidence.HIGH


def irradiance_weighted_module_temp(
    samples: Iterable[Tuple[Optional[float], Optional[float]]],
) -> Optional[float]:
    """Irradiance-weighted mean module temperature for a day.

    Each sample is ``(module_temp_c, irradiance_wm2)``. Samples missing the
    temperature are skipped. Weighting by irradiance lets midday (high-output)
    temperatures dominate — matching where the energy is actually produced,
    which is what the PR_STC correction should reflect.

    Falls back to a simple mean when no positive irradiance weights are
    present (e.g. temps logged but irradiance missing). Returns None when
    there are no usable module-temp samples at all.
    """
    temps: List[float] = []
    weight_sum = 0.0
    weighted_sum = 0.0
    for temp_c, irr in samples:
        if temp_c is None:
            continue
        temps.append(temp_c)
        if irr is not None and irr > 0:
            weight_sum += irr
            weighted_sum += temp_c * irr
    if not temps:
        return None
    if weight_sum > 0:
        return round(weighted_sum / weight_sum, 2)
    return round(sum(temps) / len(temps), 2)


def temp_corrected_pr(
    pr: Optional[float],
    module_temp_c: Optional[float],
    gamma_pmax: float = GAMMA_PMAX_DEFAULT,
    t_ref_c: float = T_STC_C,
    cell_temp_offset_c: float = 0.0,
) -> Optional[float]:
    """Temperature-correct a Performance Ratio to STC (25 degC).

        PR_STC = PR / (1 + gamma_pmax * (T - t_ref_c))
        T = module_temp_c + cell_temp_offset_c

    With the default ``cell_temp_offset_c=0`` this is the DIRECT method: the
    measured back-of-module temperature is used as-is, the standard choice when
    a BOM sensor is present. To switch to the Sandia cell-temp variant later,
    pass a small offset (a few degC, optionally irradiance-scaled by the
    caller) — no other code changes needed.

    Since gamma is negative, a hot module (T > 25) yields PR_STC > PR: the
    correction reports what the plant *would* do at 25 degC, stripping the
    thermal penalty. Returns None if ``pr`` or ``module_temp_c`` is missing,
    and guards the (physically impossible) non-positive denominator.
    """
    if pr is None or module_temp_c is None:
        return None
    t = module_temp_c + cell_temp_offset_c
    denom = 1.0 + gamma_pmax * (t - t_ref_c)
    if denom <= 0:
        return None
    return round(pr / denom, 4)


def compute_plant_pr(
    plant_key: str,
    date_iso: str,
    kwp_dc: float,
    kwp_ac: float,
    energy_per_inverter: Dict[str, EnergyDay],
    irradiance: IrradianceDay,
    inverter_count_expected: int = 0,
    module_temp_c: Optional[float] = None,
    gamma_pmax: float = GAMMA_PMAX_DEFAULT,
    cell_temp_offset_c: float = 0.0,
) -> PlantPerformanceDay:
    """Combine energy + irradiance into a Performance Ratio + capacity factor.

    Args:
        plant_key: identifier for logs and the returned object
        date_iso: 'YYYY-MM-DD' (local plant date)
        kwp_dc: installed DC capacity, kWp — REQUIRED for PR
        kwp_ac: installed AC capacity, kWp — REQUIRED for capacity factor
        energy_per_inverter: output of energy.compute_plant_energy()
        irradiance: output of irradiance.daily_irradiance_for_plant()
        inverter_count_expected: from the Inverters tab. Used to gauge
            data completeness. Pass 0 if unknown (confidence becomes LOW)

    Returns:
        PlantPerformanceDay with all fields populated (None where data is
        missing). Never raises.
    """
    total_energy = sum_inverter_energies(energy_per_inverter)
    inverters_with_data = sum(
        1 for e in energy_per_inverter.values() if e.energy_kwh is not None
    )
    inverters_with_reboot = sum(
        1 for e in energy_per_inverter.values() if e.detected_reboot
    )

    notes: List[str] = []

    # PR: needs energy AND irradiance AND kwp_dc
    pr: Optional[float] = None
    if total_energy is not None and irradiance.kwh_m2 is not None and kwp_dc > 0:
        denom = kwp_dc * irradiance.kwh_m2
        if denom > 0:
            pr = round(total_energy / denom, 4)
            if pr > 1.2:
                # Almost certainly wrong: kwp_dc too low, irradiance reading
                # broken, or a data import duplicated rows. Flag but still
                # return the value so analysts can investigate.
                notes.append(
                    f"PR={pr:.2f} > 1.2 is implausible; check kwp_dc and "
                    f"irradiance source"
                )
            elif pr < 0:
                notes.append(f"PR={pr:.2f} negative; energy reading suspect")
    elif kwp_dc <= 0:
        notes.append("PR=None: kwp_dc is 0 or missing in Plants tab")
    elif irradiance.kwh_m2 is None:
        notes.append("PR=None: no usable irradiance data for the day")
    elif total_energy is None:
        notes.append("PR=None: no inverter energy data reported")

    # PR_STC: temperature-correct the PR to 25 degC using the day's module temp.
    pr_stc: Optional[float] = temp_corrected_pr(
        pr, module_temp_c, gamma_pmax=gamma_pmax,
        cell_temp_offset_c=cell_temp_offset_c,
    )
    if pr is not None and pr_stc is None and module_temp_c is None:
        notes.append("PR_STC=None: no module temperature for the day")

    # Capacity factor: needs energy AND kwp_ac
    capacity_factor: Optional[float] = None
    if total_energy is not None and kwp_ac > 0:
        capacity_factor = round(total_energy / (kwp_ac * 24.0), 4)
        if capacity_factor > 1.0:
            notes.append(
                f"CF={capacity_factor:.2f} > 1.0 is implausible; check kwp_ac"
            )

    if inverters_with_reboot > 0:
        notes.append(
            f"{inverters_with_reboot} inverter(s) detected mid-day reboots; "
            f"energy total uses max() instead of last()"
        )

    pr_conf = _confidence_from_irradiance(
        irradiance, energy_present=(total_energy is not None and total_energy > 0),
    )
    cf_conf = _confidence_from_energy(
        total_energy, energy_per_inverter, inverter_count_expected,
    )

    return PlantPerformanceDay(
        plant_key=plant_key,
        date_iso=date_iso,
        kwp_dc=kwp_dc,
        kwp_ac=kwp_ac,
        energy_kwh=total_energy,
        energy_per_inverter=energy_per_inverter,
        irradiance_kwh_m2=irradiance.kwh_m2,
        irradiance_source=irradiance.source,
        pr=pr,
        capacity_factor=capacity_factor,
        pr_confidence=pr_conf,
        capacity_factor_confidence=cf_conf,
        inverters_with_data=inverters_with_data,
        inverters_with_reboot=inverters_with_reboot,
        notes="; ".join(notes),
        pr_stc=pr_stc,
        module_temp_c=module_temp_c,
    )


# ============================================================
# Per-inverter peer ranking
# ============================================================


@dataclass(frozen=True)
class InverterPeerRank:
    """How one inverter performed RELATIVE TO ITS PEERS at the same plant."""

    plant_key: str
    inverter_sn: str
    inverter_label: str
    energy_kwh: Optional[float]
    rated_kw: Optional[float]
    """The Inverters tab's rated_kw. Used to normalize when inverters differ
    in size — a 100kW unit producing 600 kWh isn't comparable to a 50kW
    unit producing 500 kWh; specific yield (kWh/kWp) is."""

    specific_yield_kwh_per_kwp: Optional[float]
    """energy_kwh / rated_kw. The actual peer-comparable metric."""

    peer_mean_yield: Optional[float]
    """Mean specific yield across this inverter's peers in the same plant."""

    relative_to_peer: Optional[float]
    """specific_yield / peer_mean_yield. 1.0 = at the mean, <1.0 = lagging.
    None when peer_mean_yield is missing or 0."""


def compute_inverter_peer_ranking(
    plant_key: str,
    energy_per_inverter: Dict[str, EnergyDay],
    inverter_meta: Dict[str, Dict],
) -> List[InverterPeerRank]:
    """Build peer-rank records for each inverter in a plant.

    Args:
        plant_key: identifier
        energy_per_inverter: from energy.compute_plant_energy()
        inverter_meta: dict sn → {"rated_kw": float, "inverter_label": str}.
            Caller builds this from the Inverters tab.

    Returns:
        list of InverterPeerRank, one per inverter that appears in either
        energy_per_inverter or inverter_meta. Order: descending by
        specific yield (best performers first).
    """
    all_sns = set(energy_per_inverter.keys()) | set(inverter_meta.keys())

    # First pass: compute specific yield per inverter
    yields: Dict[str, Optional[float]] = {}
    for sn in all_sns:
        e = energy_per_inverter.get(sn)
        meta = inverter_meta.get(sn) or {}
        rated = meta.get("rated_kw")
        energy = e.energy_kwh if e is not None else None
        if energy is None or rated is None or rated <= 0:
            yields[sn] = None
        else:
            yields[sn] = energy / rated

    # Peer mean: average of all non-None yields in this plant
    valid_yields = [y for y in yields.values() if y is not None]
    peer_mean: Optional[float] = (
        sum(valid_yields) / len(valid_yields) if valid_yields else None
    )

    records: List[InverterPeerRank] = []
    for sn in all_sns:
        e = energy_per_inverter.get(sn)
        meta = inverter_meta.get(sn) or {}
        energy_kwh = e.energy_kwh if e else None
        rated_kw = meta.get("rated_kw")
        sy = yields[sn]
        rel = (
            sy / peer_mean if (sy is not None and peer_mean and peer_mean > 0) else None
        )
        records.append(InverterPeerRank(
            plant_key=plant_key,
            inverter_sn=sn,
            inverter_label=meta.get("inverter_label", "") or sn,
            energy_kwh=energy_kwh,
            rated_kw=rated_kw,
            specific_yield_kwh_per_kwp=sy,
            peer_mean_yield=peer_mean,
            relative_to_peer=rel,
        ))

    # Sort: best yield first; Nones at the end
    # Tuple sort: first key is 0 for non-None, 1 for None (Nones last);
    # second key is negative yield so largest yields come first
    records.sort(
        key=lambda r: (
            1 if r.specific_yield_kwh_per_kwp is None else 0,
            -(r.specific_yield_kwh_per_kwp or 0),
        ),
    )
    return records
