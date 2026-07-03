"""Plain-language explanations for alerts — what it means, what to check.

The ``message`` column states the technical fact (numbers, thresholds);
the ``explanation`` column says what that fact MEANS and what to do about
it, for a reader who doesn't carry the detector design in their head.

One entry per engine metric. Adding a metric without an entry fails a
test on purpose — an alert nobody can interpret shouldn't ship.

The same catalog will feed the daily e-mail report later, so wording is
kept self-contained (no references to code or internal jargon).
"""

from __future__ import annotations

from typing import Dict, Optional

# metric -> (meaning, what_to_check)
_CATALOG: Dict[str, Dict[str, str]] = {
    "inverter_relative": {
        "meaning": (
            "This inverter produced much less energy than its sibling "
            "inverters at the same site, under the same sun — so weather "
            "is ruled out; the problem is this unit."),
        "check": (
            "Check this inverter in the vendor portal for faults or "
            "restarts, compare its per-string values against a healthy "
            "sibling, and inspect for shading or a tripped breaker on "
            "part of its array."),
    },
    "inverter_fault": {
        "meaning": (
            "The inverter ITSELF reported a fault code — this is the "
            "device's own diagnosis, not an inference from lost energy. "
            "It was likely offline or derated while the code was active."),
        "check": (
            "Look the code up in the vendor portal / manual for this "
            "model (e.g. FT=302 on Growatt). If it repeats across days, "
            "the unit needs a site visit or a warranty case."),
    },
    "string_fault": {
        "meaning": (
            "The inverter started reporting a string-diagnostic flag it "
            "had NEVER reported before (chronic, always-on flags are "
            "filtered out). Something changed on the DC side — possibly "
            "a broken or disconnected string, a blown string fuse, or a "
            "new mismatch."),
        "check": (
            "Compare per-string voltages/currents for this inverter in "
            "the vendor portal against last week. A string at ~0 A in "
            "good sun confirms a physical problem worth a site visit."),
    },
    "inverter_temp_high": {
        "meaning": (
            "The inverter's internal temperature is high. It will derate "
            "(produce less on purpose) to protect itself, and sustained "
            "heat shortens its lifetime. Warning from 65 degC, critical "
            "from 75 degC."),
        "check": (
            "Check ventilation: blocked or dirty fans/heatsink, direct "
            "sun on the enclosure, or dead cooling. If several units at "
            "the site run hot together, it is the installation "
            "environment, not one device."),
    },
    "plant_offline": {
        "meaning": (
            "EVERY reporting inverter at this plant was at 0 W in the "
            "middle of daylight. That is practically never coincidence — "
            "it points at something shared: grid outage, main breaker or "
            "transformer, or a site-wide shutdown."),
        "check": (
            "Confirm with the site contact whether the facility has "
            "power; check the main AC breaker and the vendor portal for "
            "grid-related fault codes across all units."),
    },
    "energy_daily_pct": {
        "meaning": (
            "The plant produced meaningfully less than expected for the "
            "day, where 'expected' already accounts for plant size and "
            "the day's measured sunlight — so ordinary clouds are NOT "
            "the explanation. Note: on plants with a sparse irradiance "
            "feed this can also reflect measurement quality."),
        "check": (
            "Look for a companion alert naming the cause (inverter fault "
            "/ underperformer / string). If none, review the day's "
            "per-hour production for a gap, and consider soiling or "
            "curtailment."),
    },
    "plant_twin_yield": {
        "meaning": (
            "This plant's yield per installed kW fell well below its "
            "regional twin, which shares its weather. The whole plant is "
            "underperforming even if its inverters agree with each other "
            "— typical of uniform soiling, curtailment, or a shared "
            "electrical issue."),
        "check": (
            "Compare the two plants' daily curves; if this plant's shape "
            "is normal but uniformly lower, suspect soiling or "
            "curtailment. Check the last cleaning date."),
    },
    "data_stale": {
        "meaning": (
            "Telemetry stopped arriving from this plant during daylight. "
            "The plant may be producing fine — this is about the DATA "
            "pipeline (datalogger, internet at the site, vendor cloud, "
            "or our collector). Daily figures for the gap are not "
            "trustworthy."),
        "check": (
            "Check whether the vendor portal itself shows fresh data. "
            "Portal fresh -> the problem is our collector; portal stale "
            "-> the site's datalogger or internet connection is down."),
    },
}


def explain(metric: str, severity: str = "",
            value: Optional[float] = None) -> str:
    """One self-contained paragraph for an alert: meaning + what to check.

    Unknown metrics return "" rather than raising — an alert must never
    fail to write because its explanation is missing; the coverage test
    keeps the catalog complete at development time instead.
    """
    entry = _CATALOG.get(str(metric).strip())
    if not entry:
        return ""
    sev = str(severity).strip().upper()
    prefix = ""
    if sev == "CRITICAL":
        prefix = "Needs attention now. "
    elif sev == "WARNING":
        prefix = "Worth watching; act if it persists. "
    return f"{prefix}{entry['meaning']} What to check: {entry['check']}"


def catalog_metrics() -> set:
    """Metrics the catalog covers (used by the completeness test)."""
    return set(_CATALOG)
