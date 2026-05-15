"""End-of-day energy KPI — Stage 7.2.

Pure math, no I/O. Operates on lists of ``InverterRow`` from the reader.

The etoday-monotonic problem
============================

``etoday_kwh`` is supposed to be cumulative-from-midnight. In practice it
mostly is, but:

1. **Inverter reboots reset it to 0.** A reboot at 14:00 gives a row
   sequence like ``[1.2, 3.8, 7.5, 0.0, 1.1, 2.3, ...]``. ``max()`` gives
   you 7.5, ``last()`` gives you 2.3 — neither is the true daily total
   (which is closer to 7.5 + 2.3 = 9.8).

2. **SolarEdge derives etoday via cumulative-energy diff.** Tiny rounding
   noise can make the sequence non-monotonic by ±0.001 kWh. Harmless.

3. **Some Growatt inverters reset etoday a few minutes after midnight,**
   so the first row of a day can be the previous day's final value.

We use the following heuristic, in order of robustness:

- ``max(etoday_kwh)`` — robust to small noise, **wrong on reboot**.
- ``last_at_or_before_sunset(etoday_kwh)`` — robust to nighttime resets,
  **wrong on reboot AND** requires knowing sunset time.
- ``sum of reboot-aware segments`` — correct on reboot, but more code.

Stage 7.2 ships the first two. We compute BOTH and warn when they
disagree by >10%. Stage 7.3 will switch the default to segmented if real
data shows frequent reboots.

We do NOT integrate ``power_w`` over time as a fallback — it requires
trapezoidal integration over 5-minute samples which compounds drift; if
``etoday_kwh`` is missing for an inverter, we report None.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from argia.kpi.reader import InverterRow

LOG = logging.getLogger("argia.kpi.energy")


@dataclass(frozen=True)
class EnergyDay:
    """End-of-day energy for one inverter or plant."""

    energy_kwh: Optional[float]
    """Best-effort total for the day. None if there's not enough data."""

    energy_kwh_max: Optional[float]
    """max(etoday_kwh) interpretation."""

    energy_kwh_last: Optional[float]
    """last value of etoday_kwh, ignoring trailing zeros after sunset."""

    rows_seen: int
    """How many rows fed this calculation."""

    rows_online: int
    """How many of those rows had status=1 (online)."""

    detected_reboot: bool
    """True if etoday_kwh decreased between consecutive rows by more than
    a small noise threshold. When True, the max-vs-last methods will
    disagree significantly and the true daily total is harder to pin down."""

    discrepancy_pct: Optional[float]
    """abs(max - last) / max × 100, or None if either is missing."""


# Tolerable downward drift in etoday_kwh between rows. Noise above this
# we treat as a real reset, not measurement noise.
REBOOT_THRESHOLD_KWH = 0.5

# If max and last differ by more than this, we flag a reliability issue.
DISCREPANCY_WARN_PCT = 10.0


def _max_etoday(rows: List[InverterRow]) -> Optional[float]:
    values = [r.etoday_kwh for r in rows if r.etoday_kwh is not None]
    if not values:
        return None
    return max(values)


def _last_etoday(rows: List[InverterRow]) -> Optional[float]:
    """Last non-None etoday_kwh in time order, ignoring trailing zeros.

    Why ignore trailing zeros: after sunset the inverter idles and some
    vendors (Growatt, Huawei in particular) start reporting 0.0 for
    etoday_kwh once it resets just past midnight. We don't want those
    midnight-rollover zeros to clobber the day's real total."""
    # Rows are not guaranteed sorted by caller; sort defensively
    sorted_rows = sorted(rows, key=lambda r: r.timestamp_utc)
    last_nonzero: Optional[float] = None
    for r in sorted_rows:
        if r.etoday_kwh is None:
            continue
        if r.etoday_kwh > 0:
            last_nonzero = r.etoday_kwh
        # If r.etoday_kwh == 0 and we already have a non-zero last,
        # we keep the non-zero (treats trailing zeros as rollover noise).
    return last_nonzero


def _detect_reboot(rows: List[InverterRow]) -> bool:
    """True if etoday_kwh dropped by more than REBOOT_THRESHOLD_KWH between
    consecutive rows (in time order)."""
    sorted_rows = sorted(rows, key=lambda r: r.timestamp_utc)
    prev: Optional[float] = None
    for r in sorted_rows:
        if r.etoday_kwh is None:
            continue
        if prev is not None and prev - r.etoday_kwh > REBOOT_THRESHOLD_KWH:
            return True
        prev = r.etoday_kwh
    return False


def compute_inverter_energy(rows: List[InverterRow]) -> EnergyDay:
    """Compute end-of-day energy for ONE inverter's rows.

    Caller should pre-filter to a single (plant_key, inverter_sn). Pass
    an empty list to get an EnergyDay with all metrics None.
    """
    if not rows:
        return EnergyDay(
            energy_kwh=None, energy_kwh_max=None, energy_kwh_last=None,
            rows_seen=0, rows_online=0,
            detected_reboot=False, discrepancy_pct=None,
        )

    max_e = _max_etoday(rows)
    last_e = _last_etoday(rows)
    reboot = _detect_reboot(rows)

    # Best-effort: prefer last_e when no reboot detected; otherwise prefer max_e
    # (which is at least the peak before reboot). Tag as None when both missing.
    if reboot and max_e is not None:
        best = max_e  # last_e is unreliable after reboot
    elif last_e is not None:
        best = last_e
    else:
        best = max_e

    # Discrepancy: how much max and last disagree, as a pct of max
    discrepancy_pct: Optional[float] = None
    if max_e is not None and last_e is not None and max_e > 0:
        discrepancy_pct = abs(max_e - last_e) / max_e * 100.0

    online = sum(1 for r in rows if r.status == 1)

    if (
        discrepancy_pct is not None
        and discrepancy_pct > DISCREPANCY_WARN_PCT
        and not reboot
    ):
        # Reboot path already explains the gap; warn only on unexplained gaps
        sample = rows[0]
        LOG.warning(
            "[%s/%s] energy discrepancy %.1f%% (max=%.2f last=%.2f) — "
            "no reboot detected; check parser",
            sample.plant_key, sample.inverter_sn,
            discrepancy_pct, max_e, last_e,
        )

    return EnergyDay(
        energy_kwh=best,
        energy_kwh_max=max_e,
        energy_kwh_last=last_e,
        rows_seen=len(rows),
        rows_online=online,
        detected_reboot=reboot,
        discrepancy_pct=discrepancy_pct,
    )


def compute_plant_energy(
    rows: List[InverterRow],
) -> Dict[str, EnergyDay]:
    """Compute end-of-day energy for every inverter in one plant.

    Returns dict: inverter_sn → EnergyDay. The plant total is NOT computed
    here — caller sums it (or skips missing inverters). Keeping it as a
    per-inverter map lets the caller decide policy for missing data.
    """
    by_sn: Dict[str, List[InverterRow]] = {}
    for r in rows:
        by_sn.setdefault(r.inverter_sn, []).append(r)
    return {sn: compute_inverter_energy(rs) for sn, rs in by_sn.items()}


def sum_inverter_energies(per_inverter: Dict[str, EnergyDay]) -> Optional[float]:
    """Sum end-of-day kWh across all inverters in a plant.

    Returns None if NO inverter had a value (full-day blackout). Returns
    a partial sum if SOME inverters had values — callers should look at
    the dict directly to know which inverters contributed."""
    values = [e.energy_kwh for e in per_inverter.values() if e.energy_kwh is not None]
    if not values:
        return None
    return sum(values)
