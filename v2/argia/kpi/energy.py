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

**Midnight carryover (case 3) is handled explicitly**: a leading FLAT
etoday segment that then resets to a lower value is yesterday's counter,
not production — those rows are stripped before any aggregation. The
discriminator vs. a real reboot: carryover is flat-then-reset (no growth
before the drop); a real reboot shows growth before the drop.

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

from argia.kpi.reader import MX_TZ, InverterRow

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

    carryover_rows_dropped: int = 0
    """Leading rows stripped because they still carried the PREVIOUS day's
    etoday counter (Growatt resets at wake, not midnight). Stripping them
    prevents max() from reporting yesterday's total as today's energy."""


# Tolerable downward drift in etoday_kwh between rows. Noise above this
# we treat as a real reset, not measurement noise.
REBOOT_THRESHOLD_KWH = 0.5

# If max and last differ by more than this, we flag a reliability issue.
DISCREPANCY_WARN_PCT = 10.0

# A leading flat segment must be at least this large (kWh) to be treated as
# midnight carryover — prevents stripping benign near-zero leading rows.
CARRYOVER_MIN_KWH = 1.0

# Carryover can only exist BEFORE the inverter wakes; production before this
# local hour is negligible (sunrise in central Mexico is 06:00-07:15). A
# flat-then-reset pattern later than this is treated as a real reboot.
CARRYOVER_DAWN_LOCAL_HOUR = 7


def find_carryover_cut(
    etoday_series: List[Optional[float]],
    local_hours: Optional[List[Optional[int]]] = None,
) -> int:
    """Index of the first row AFTER a leading midnight-carryover segment.

    Carryover signature — ALL must hold:
      1. the series starts with one or more FLAT rows (within
         REBOOT_THRESHOLD_KWH of the first value, i.e. no growth), and
      2. it then DROPS by more than REBOOT_THRESHOLD_KWH (the counter
         resetting when the inverter wakes), and
      3. when ``local_hours`` is given, every stale row sits before
         CARRYOVER_DAWN_LOCAL_HOUR — a stale counter cannot survive past
         wake, so a flat-then-reset later in the day is a real reboot
         under sparse polling and must NOT be stripped.

    A real mid-day reboot shows GROWTH before the drop and is never
    matched. Returns 0 when there is nothing to strip. Shared by kpi_eod
    and the dashboard builder so both consumers apply ONE rule.
    """
    idx_vals = [(i, v) for i, v in enumerate(etoday_series) if v is not None]
    if len(idx_vals) < 2:
        return 0
    first = idx_vals[0][1]
    if first < CARRYOVER_MIN_KWH:
        return 0
    j = 1
    while j < len(idx_vals) and abs(idx_vals[j][1] - first) <= REBOOT_THRESHOLD_KWH:
        j += 1
    if j >= len(idx_vals):
        return 0  # flat all day; nothing to decide
    if first - idx_vals[j][1] <= REBOOT_THRESHOLD_KWH:
        return 0  # no reset after the flat segment
    if local_hours is not None:
        for i, _ in idx_vals[:j]:
            h = local_hours[i] if i < len(local_hours) else None
            if h is None or h >= CARRYOVER_DAWN_LOCAL_HOUR:
                return 0  # stale segment not confined to pre-dawn
    return idx_vals[j][0]  # first row after the stale segment


def _strip_leading_carryover(
    sorted_rows: List[InverterRow],
) -> tuple[List[InverterRow], int]:
    """Drop leading rows that carry yesterday's etoday counter."""
    cut = find_carryover_cut(
        [r.etoday_kwh for r in sorted_rows],
        [r.timestamp_utc.astimezone(MX_TZ).hour for r in sorted_rows],
    )
    if cut == 0:
        return sorted_rows, 0
    kept = sorted_rows[cut:]
    if kept:
        LOG.info(
            "[%s/%s] stripped %d leading carryover row(s): etoday %.1f is "
            "yesterday's counter (resets to %.1f)",
            sorted_rows[0].plant_key, sorted_rows[0].inverter_sn, cut,
            sorted_rows[0].etoday_kwh or 0.0,
            next((r.etoday_kwh for r in kept if r.etoday_kwh is not None), 0.0),
        )
    return kept, cut


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

    sorted_rows = sorted(rows, key=lambda r: r.timestamp_utc)
    sorted_rows, carryover_dropped = _strip_leading_carryover(sorted_rows)
    if not sorted_rows:
        return EnergyDay(
            energy_kwh=None, energy_kwh_max=None, energy_kwh_last=None,
            rows_seen=len(rows), rows_online=0,
            detected_reboot=False, discrepancy_pct=None,
            carryover_rows_dropped=carryover_dropped,
        )
    rows = sorted_rows

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
        carryover_rows_dropped=carryover_dropped,
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
