"""Inverter health detection — ``inverter_relative``.

Catches an inverter under-producing relative to its plant peers — including
the nasty case that a plain offline check MISSES: an inverter reporting
status = ONLINE while putting out ~0, its siblings producing normally. (Real
example: MEX1 Inverter 2 on 2026-07-02 — online, 0 W, while Inverters 1 and 3
did ~79 kW each. See tests/fixtures/health/mex1_inv2_dead_20260702.json.)

This is CONFIG-INDEPENDENT: it compares each inverter to the mean of its plant
peers, so it needs no plant kwp, no expected factor, no tariff. That's why it's
safe to build before the feed's config truths (e.g. GTO1 605.9 vs 818.33) are
settled — unlike ``pr_daily`` / ``energy_daily_pct``, which divide by that
config and must wait.

WHAT THIS MODULE IS
    A PURE detector. Given a set of contemporaneous inverter production values
    (5-min power, or a daily energy total — the function is unit-agnostic) and
    two thresholds, it returns which inverters breach and at what severity.
    It reads nothing, writes nothing, and holds no state.

WHAT THIS MODULE IS NOT (later increments, Phase 2)
    - the open/resolve alert lifecycle (that's alerts_state.py + the engine)
    - debounce/persistence ("breaching for N minutes")
    - notification (email/sheet channels)
    - the other metrics (inverter_offline, plant_offline, data_stale)

Honest limitations
==================
1. **Needs a production floor.** At night / dawn / dusk every inverter is near
   zero, so a naive ratio (5 W vs a 50 W peer mean) would fire false CRITICALs.
   The caller MUST pass ``min_peer_floor`` — a production level the peer mean
   must clear before any judgement is made. The engine will derive it from
   nameplate (e.g. peers must be above ~5% of rated). This pure function
   defaults it to 0.0 for testability, but 0.0 in production would be noisy.

2. **Needs at least two inverters.** A single-inverter plant has no peers to
   compare against, so it's skipped (no judgement). Those plants need
   ``inverter_offline`` / ``plant_offline`` instead — a later increment.

3. **Peer mean is leave-one-out.** Each inverter is compared to the mean of the
   OTHERS, so a dead unit doesn't dilute its own ratio. If enough peers die that
   the remaining peer mean falls below the floor, the survivor is skipped rather
   than mis-flagged.

4. **Single-snapshot, no persistence here.** One reading in, one verdict out.
   Whether a breach must persist before it becomes an alert is the engine's
   call, not this function's.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional

from argia.core.thresholds import Severity

LOG = logging.getLogger("argia.analytics.inverter_health")

# The configured Thresholds-tab values for inverter_relative, kept here as the
# documented defaults. The engine resolves the live values from the sheet and
# passes them in; these are what the sheet ships with today.
DEFAULT_WARN_BELOW = 0.85
DEFAULT_CRIT_BELOW = 0.70


@dataclass(frozen=True)
class InverterReading:
    """One inverter's production at one moment (or over one day).

    ``value`` is a production magnitude — instantaneous power in W, or daily
    energy in kWh. The detector doesn't care which, as long as every reading in
    a single call uses the same unit.

    ``rated_kw`` is the inverter's nameplate. When every inverter in a plant has
    a positive nameplate, the detector compares *specific* output (value per kW)
    so a physically smaller inverter isn't falsely flagged against larger peers.
    Leave it None to fall back to a raw-value comparison (only fair when all the
    plant's inverters are the same size).
    """

    plant_key: str
    inverter_sn: str
    value: float
    rated_kw: Optional[float] = None


@dataclass(frozen=True)
class RelativeBreach:
    """An inverter judged to be under-producing vs its peers."""

    plant_key: str
    inverter_sn: str
    value: float
    peer_mean: float
    ratio: float
    severity: Severity
    threshold: float      # the breached threshold (crit if CRITICAL, else warn)
    message: str


def _severity_for(ratio: float, warn_below: float, crit_below: float) -> Optional[Severity]:
    """Most-severe band the ratio breaches, or None.

    Strict ``<``: a ratio exactly equal to a threshold does NOT breach it.
    CRITICAL wins over WARNING.
    """
    if ratio < crit_below:
        return Severity.CRITICAL
    if ratio < warn_below:
        return Severity.WARNING
    return None


def evaluate_inverter_relative(
    readings: List[InverterReading],
    warn_below: float = DEFAULT_WARN_BELOW,
    crit_below: float = DEFAULT_CRIT_BELOW,
    min_peer_floor: float = 0.0,
) -> List[RelativeBreach]:
    """Flag inverters producing below a fraction of their plant-peer mean.

    Args:
        readings: contemporaneous inverter readings, any number of plants mixed.
        warn_below: ratio under which a WARNING is raised (default 0.85).
        crit_below: ratio under which a CRITICAL is raised (default 0.70).
        min_peer_floor: the peer mean must exceed this (same unit as ``value``)
            before an inverter is judged — guards against night/low-light false
            positives. See "Honest limitations" #1.

    Returns:
        Breaches sorted by (plant_key, inverter_sn). Plants with fewer than two
        inverters, and inverters whose peer mean is below the floor, are skipped.
    """
    if crit_below > warn_below:
        # Misconfiguration guard: CRITICAL must be the tighter (lower) bound.
        LOG.warning(
            "crit_below (%.3f) > warn_below (%.3f) — thresholds look swapped",
            crit_below, warn_below,
        )

    # Group by plant.
    by_plant: Dict[str, List[InverterReading]] = {}
    for r in readings:
        by_plant.setdefault(r.plant_key, []).append(r)

    breaches: List[RelativeBreach] = []

    for plant_key, plant_readings in by_plant.items():
        n = len(plant_readings)
        if n < 2:
            continue  # no peers to compare against

        # Use per-kW (specific) output when the WHOLE plant has nameplates, so a
        # smaller inverter isn't unfairly flagged against larger peers. If any
        # nameplate is missing/non-positive, fall back to raw values (only fair
        # for same-size plants).
        normalized = all(r.rated_kw and r.rated_kw > 0 for r in plant_readings)
        if normalized:
            spec = {r.inverter_sn: r.value / r.rated_kw for r in plant_readings}
        else:
            spec = {r.inverter_sn: r.value for r in plant_readings}

        raw_total = sum(r.value for r in plant_readings)     # floor gate (raw)
        spec_total = sum(spec.values())                      # ratio (specific)

        for r in plant_readings:
            # Floor gate on RAW peer production — "are the peers actually
            # producing?" — kept in the caller's raw unit regardless of scaling.
            raw_peer_mean = (raw_total - r.value) / (n - 1)
            if raw_peer_mean <= min_peer_floor:
                continue  # peers not producing enough to judge fairly

            # Ratio in specific (per-kW) space when normalized, else raw.
            spec_peer_mean = (spec_total - spec[r.inverter_sn]) / (n - 1)
            if spec_peer_mean <= 0:
                continue
            ratio = spec[r.inverter_sn] / spec_peer_mean
            peer_mean = raw_peer_mean  # reported in raw units for readability

            sev = _severity_for(ratio, warn_below, crit_below)
            if sev is None:
                continue

            threshold = crit_below if sev is Severity.CRITICAL else warn_below
            pct = ratio * 100.0
            breaches.append(RelativeBreach(
                plant_key=plant_key,
                inverter_sn=r.inverter_sn,
                value=r.value,
                peer_mean=peer_mean,
                ratio=ratio,
                severity=sev,
                threshold=threshold,
                message=(
                    f"{plant_key} {r.inverter_sn}: {pct:.0f}% of peer mean "
                    f"({r.value:.0f} vs {peer_mean:.0f}) — below "
                    f"{threshold:.0%} [{sev.value}]"
                ),
            ))

    breaches.sort(key=lambda b: (b.plant_key, b.inverter_sn))
    return breaches
