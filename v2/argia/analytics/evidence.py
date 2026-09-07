"""Production evidence behind an alert (v220).

Tomasz, 2026-09-07, on the open-issues list: "for the temperature send
CRITICAL only when the temperature is hot AND there is abnormal
production; if production stays the same as a cooler inverter keep it a
warning" and, on the string flag, "I expect data showing the performance
went down, or at least some data supporting this".

So a hot inverter is CRITICAL only when its own output is measurably
below its cooler peers (acute: kW per rated kW at the same minute; daily:
the thermal_daily derating evidence), and a new string-diagnostic bit
becomes a WARNING only when the day's data shows a loss — a string far
below its own usual current (its share of the MPPT pair over the previous
days), or the inverter below the plant peers. Without evidence the flag is INFO: recorded in the ledger and on
the portal, never mailed. Pure functions; the scripts feed them rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

THERMAL_LOSS_CRIT_PCT = 10.0
"""Acute rule: a hot unit is CRITICAL when it produces at least this much
less (kW per rated kW) than the median of its cooler peers right now."""
THERMAL_DAY_LOSS_PCT = 10.0
"""Daily rule: CRITICAL when thermal_daily's suspected derating loss is at
least this share of the unit's day energy …"""
THERMAL_DAY_DERATING_MIN = 60
"""… or the unit spent at least this many minutes in suspected derating."""

STRING_WEAK_RATIO = 0.5
"""A string at or below this fraction of ITS OWN trailing share of the
MPPT pair (median of the previous days) is a measurable string loss —
against its own history, not its siblings: half the string inputs of a
MAX inverter can be legitimately empty (verified 2026-09-07: sibling
comparison called every unused input "weak")."""
STRING_BASE_MIN_SHARE = 0.10
"""A string whose trailing share is below this never carried current —
an unused input, not a string that broke."""
STRING_BASE_MIN_DAYS = 3
"""Days of history a string needs before its drop counts as evidence."""
STRING_PEER_RATIO = 0.90
"""… or the whole inverter below this share of its plant peers' specific
production (kWh per rated kW)."""


def median(xs: Sequence[float]) -> Optional[float]:
    v = sorted(float(x) for x in xs)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


# ------------------------------------------------------------ thermal
def thermal_shortfall_pct(power_w: Optional[float], rated_kw: Optional[float],
                          cooler_specific: Sequence[float]) -> Optional[float]:
    """How much less (in %) this unit makes per rated kW than the median of
    its cooler peers; None when it cannot be measured (no rated kW, no
    power, no cooler peer producing)."""
    if not rated_kw or rated_kw <= 0 or power_w is None:
        return None
    ref = median([c for c in cooler_specific if c is not None and c > 0])
    if not ref:
        return None
    return (1.0 - (float(power_w) / float(rated_kw)) / ref) * 100.0


def thermal_severity(temp_c: float, warn_c: float, high_c: float, hotter_than_peers: Optional[bool],
                     shortfall_pct: Optional[float],
                     loss_crit_pct: float = THERMAL_LOSS_CRIT_PCT) -> Tuple[str, str, str]:
    """(severity, why, evidence) for the acute rule.

    CRITICAL needs three things: >= high_c, hotter than the peers (or no
    peer to compare — a lone unit is judged on itself) AND a measured
    shortfall >= loss_crit_pct against the cooler peers. Everything else
    hot is WARNING with the evidence stated either way."""
    if shortfall_pct is None:
        evidence = "no cooler peer to measure the loss against"
    elif shortfall_pct >= 3.0:
        evidence = f"producing {shortfall_pct:.0f}% below cooler peers"
    else:
        evidence = f"producing within {max(0.0, shortfall_pct):.0f}% of cooler peers"
    hot = temp_c >= high_c
    if hot and (hotter_than_peers is None or hotter_than_peers) and shortfall_pct is not None \
            and shortfall_pct >= loss_crit_pct:
        return "CRITICAL", f">= {high_c:.0f}, hotter than its peers and losing output", evidence
    if hot and hotter_than_peers is False:
        return "WARNING", f">= {high_c:.0f}, plant-wide heat", evidence
    if hot:
        return "WARNING", f">= {high_c:.0f}, no output loss measured", evidence
    return "WARNING", f">= {warn_c:.0f}", evidence


@dataclass(frozen=True)
class ThermalDay:
    derating_minutes: int
    lost_kwh: float
    energy_kwh: Optional[float]


def thermal_day_severity(peak_c: float, crit_c: float, ev: Optional[ThermalDay],
                         loss_pct: float = THERMAL_DAY_LOSS_PCT,
                         derating_min: int = THERMAL_DAY_DERATING_MIN) -> Tuple[str, str]:
    """(severity, evidence) for the daily day-peak rule. CRITICAL only when
    the nightly thermal evaluation measured a loss; a hot day without a
    measured loss is WARNING."""
    if ev is None:
        return "WARNING", "no thermal evaluation for the day"
    total = (ev.energy_kwh or 0.0) + ev.lost_kwh
    share = (ev.lost_kwh / total * 100.0) if total > 0 else 0.0
    if ev.lost_kwh > 0 or ev.derating_minutes > 0:
        evidence = (f"suspected derating {ev.derating_minutes} min, {ev.lost_kwh:.1f} kWh lost"
                    f" ({share:.0f}% of the day) vs cooler peers")
    else:
        evidence = "no output loss vs cooler peers measured"
    if peak_c >= crit_c and (share >= loss_pct or ev.derating_minutes >= derating_min):
        return "CRITICAL", evidence
    return "WARNING", evidence


# ------------------------------------------------------------ strings
def peer_ratio(readings: Iterable, plant_key: str, sn: str) -> Optional[float]:
    """This inverter's day production per rated kW over the median of its
    plant peers (raw kWh when a nameplate is missing). ``readings`` are
    InverterReading-like (plant_key, inverter_sn, value, rated_kw)."""
    mine, peers = None, []
    rs = [r for r in readings if r.plant_key == plant_key]
    normalized = all(getattr(r, "rated_kw", None) and r.rated_kw > 0 for r in rs)
    for r in rs:
        v = r.value / r.rated_kw if normalized else r.value
        if r.inverter_sn == sn:
            mine = v
        else:
            peers.append(v)
    ref = median([p for p in peers if p is not None and p > 0])
    if mine is None or not ref:
        return None
    return float(mine) / float(ref)


def weak_strings(today: Sequence[Tuple[str, Optional[float]]],
                 baseline: Dict[str, Sequence[float]],
                 weak_ratio: float = STRING_WEAK_RATIO,
                 min_share: float = STRING_BASE_MIN_SHARE,
                 min_days: int = STRING_BASE_MIN_DAYS) -> Tuple[List[Tuple[str, float]], Optional[float]]:
    """``today``: (channel, share-of-MPPT-pair) rows of ONE inverter from
    string_daily (kind='string'); ``baseline``: channel -> that string's
    shares on the previous days. Returns the strings whose share today is
    at or below ``weak_ratio`` of their own trailing median (only strings
    that used to carry current, with enough history), as (channel,
    today/baseline) — and the number of strings judged (None = nothing
    to judge: no history, or no current at all today)."""
    judged = 0
    weak: List[Tuple[str, float]] = []
    any_current = any(s is not None and s > 0 for _, s in today)
    for c, share in today:
        hist = [float(x) for x in baseline.get(c, []) if x is not None]
        if len(hist) < min_days:
            continue
        base = median(hist)
        if base is None or base < min_share:
            continue                      # an input that never carried current
        judged += 1
        ratio = (float(share) if share is not None else 0.0) / base
        if ratio <= weak_ratio:
            weak.append((c, ratio))
    if not judged or not any_current:
        return [], None
    return sorted(weak), float(judged)


def string_severity(ratio: Optional[float], weak: Sequence[Tuple[str, float]], judged: Optional[float],
                    peer_ratio_min: float = STRING_PEER_RATIO) -> Tuple[str, str]:
    """(severity, evidence): WARNING when the day's data shows a loss,
    INFO when the flag is alone."""
    parts = []
    if weak:
        parts.append("string " + ", ".join(f"{c} at {s * 100:.0f}% of its own usual current" for c, s in weak))
    if ratio is not None:
        parts.append(f"inverter at {ratio * 100:.0f}% of plant peers")
    if weak or (ratio is not None and ratio < peer_ratio_min):
        return "WARNING", "; ".join(parts)
    if not parts:
        return "INFO", "no production data to confirm a loss"
    if judged is None:
        parts.append("string currents not available")
    else:
        parts.append(f"all {int(judged)} strings within their usual current")
    return "INFO", "no measurable loss — " + "; ".join(parts)
