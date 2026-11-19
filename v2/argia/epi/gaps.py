"""Communication gaps: counter-conserving reconstruction of 5-minute inverter energy.

Tomasz 19 Nov 2026: a lost connection must not look like "after hours"; the missing
energy is distributed "according to some known behaviours". The rules (review of
spec v0.7, IEA PVPS T13-28 section 3.3: impute, flag, never hide the uncertainty):

1. Energy is never invented. The inverter's day counter (kWh since local midnight)
   gives the exact energy inside a gap: E = C(after) - C(before). Before the first
   sample of the day the counter itself is the energy since midnight.
2. That energy is spread over the slots of the gap in proportion to a known shape:
   the model's expected power of that inverter (measured station irradiance), or,
   where the station is invalid, a clear-sky shape (Haurwitz). Sum is conserved
   exactly; slots with zero weight get zero.
3. After the last sample of the day nothing is known unless the vendor's daily total
   is given; without it those slots stay UNKNOWN (drawn as "no data", never zero).
4. Every slot carries a flag: measured (two consecutive samples), reconstructed
   (counter-proven, shape-distributed) or unknown.

Availability from counters (v310 evidence rule, made explicit): over a gap, if the
counter rose by at least 50 % of the expected energy the inverter is UP for the
gap's slots; if it rose by at most 5 % it is DOWN (proven); otherwise UNKNOWN.

Stale records (seen at SAG, October 2026: 12.8 % of producing rows): a vendor API
that stops updating keeps returning the last record. A record is stale when it
repeats the previous one exactly (same power and counter, power > 0), or when the
counter does not move while the inverter reports >= 2.4 kW in both samples
(2.4 kW x 5 min = 0.2 kWh, twice the 0.1 kWh counter resolution). Stale records are
treated as missing - otherwise a frozen API would read as zero production.

Pure functions; the acceptance test is scripts/epi600_gap_test.py (masks real hours
on complete October 2026 days and compares) plus tests/unit/test_v379_epi_gaps.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

MEASURED, RECONSTRUCTED, UNKNOWN = "measured", "reconstructed", "unknown"
UP_SHARE, DOWN_SHARE = 0.50, 0.05
COUNTER_TOL_KWH = 0.05          # a counter may not go backwards by more than this within a day (else glitch or reset)


@dataclass(frozen=True)
class Slot:
    bucket: int
    energy_kwh: Optional[float]
    flag: str


@dataclass(frozen=True)
class Gap:
    first: int                  # first slot of the gap (bucket)
    last: int                   # last slot (the slot of the closing sample)
    energy_kwh: float           # counter-proven energy in the gap
    expected_kwh: float         # model energy over the same slots (0 if no weights)
    availability: str           # "up" / "down" / "unknown"


STALE_MIN_W = 2400.0


def stale_buckets(rows: Sequence[Tuple[int, Optional[float], Optional[float]]]) -> Set[int]:
    """rows: (bucket, power W, day counter kWh) of one inverter, any order."""
    out: Set[int] = set()
    prev = None
    for b, p, c in sorted(rows):
        if prev is not None and p is not None and c is not None and prev[1] is not None and prev[2] is not None:
            same = p == prev[1] and c == prev[2] and p > 0
            stuck = abs(c - prev[2]) < 1e-9 and p >= STALE_MIN_W and prev[1] >= STALE_MIN_W
            if same or stuck:
                out.add(b)
        if p is not None or c is not None:
            prev = (b, p, c)
    return out


def _clean(counter: Sequence[Tuple[int, Optional[float]]]) -> List[Tuple[int, float]]:
    """Sorted samples with a counter made monotonic.

    A single sample below both neighbours is a glitch and is dropped. A drop that the
    following samples continue from (the counter restarted, e.g. an inverter reboot -
    Turistica Arizona, 10 Oct 2026, both inverters at 12:10) is a reset: the value
    before it is added to every later sample, so the day stays one continuous counter.
    The energy between the last sample before the reset and the reset itself is not
    in the counter; the slots there are closed by the daily total when one is given."""
    raw = [(b, c) for b, c in sorted(counter) if c is not None]
    out: List[Tuple[int, float]] = []
    offset = 0.0
    for i, (b, c) in enumerate(raw):
        v = c + offset
        if out and v < out[-1][1] - COUNTER_TOL_KWH:
            nxt = raw[i + 1][1] + offset if i + 1 < len(raw) else None
            if nxt is not None and nxt >= out[-1][1] - COUNTER_TOL_KWH:
                continue                                 # glitch: the next sample is back on track
            offset = out[-1][1]                          # reset: continue from the last good value
            v = c + offset
        out.append((b, max(v, out[-1][1]) if out else v))
    return out


def reconstruct_day(counter: Sequence[Tuple[int, Optional[float]]],
                    slots: Sequence[int],
                    weight_w: Mapping[int, float],
                    day_total_kwh: Optional[float] = None) -> Tuple[List[Slot], List[Gap]]:
    """One inverter, one local day.

    counter: (bucket, day counter kWh) of the samples received that day.
    slots: every 5-minute bucket of the day that can hold energy (sun up), sorted.
    weight_w: expected power per bucket (any scale) - the shape for distribution.
    day_total_kwh: vendor daily total, if known, closes the day after the last sample.
    """
    slots = sorted(slots)
    if not slots:
        return [], []
    anchors = _clean(counter)
    start = (slots[0] - 1, 0.0)                     # counter is 0 at local midnight
    if anchors and anchors[0][0] <= start[0]:
        start = (anchors[0][0], anchors[0][1])
        anchors = anchors[1:]
    pts = [start] + [a for a in anchors if a[0] > start[0]]
    if day_total_kwh is not None and day_total_kwh >= pts[-1][1] - COUNTER_TOL_KWH and slots[-1] > pts[-1][0]:
        pts.append((slots[-1], max(day_total_kwh, pts[-1][1])))
    energy: Dict[int, Tuple[float, str]] = {}
    gaps: List[Gap] = []
    slot_set = set(slots)
    for (ba, ca), (bb, cb) in zip(pts, pts[1:]):
        e = cb - ca
        span = [k for k in range(ba + 1, bb + 1)]
        inside = [k for k in span if k in slot_set]
        if bb - ba == 1:
            energy[bb] = (e, MEASURED)
            continue
        if not inside:
            continue
        w = [max(0.0, weight_w.get(k, 0.0)) for k in inside]
        tot = sum(w)
        if tot <= 0:
            w, tot = [1.0] * len(inside), float(len(inside))
        for k, wk in zip(inside, w):
            energy[k] = (e * wk / tot, RECONSTRUCTED)
        exp_kwh = sum(max(0.0, weight_w.get(k, 0.0)) for k in inside) * 5 / 60 / 1000.0
        if exp_kwh <= 0:
            av = "unknown"
        elif e >= UP_SHARE * exp_kwh:
            av = "up"
        elif e <= DOWN_SHARE * exp_kwh:
            av = "down"
        else:
            av = "unknown"
        gaps.append(Gap(inside[0], inside[-1], e, exp_kwh, av))
    out = [Slot(k, energy[k][0], energy[k][1]) if k in energy else Slot(k, None, UNKNOWN) for k in slots]
    return out, gaps


def day_energy_kwh(slots: Sequence[Slot]) -> Tuple[float, int]:
    """Known energy of the day and the number of unknown slots."""
    known = sum(s.energy_kwh for s in slots if s.energy_kwh is not None)
    return known, sum(1 for s in slots if s.energy_kwh is None)
