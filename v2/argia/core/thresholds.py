"""
Thresholds — Stage 7.1.

Reads the ``Thresholds`` tab from the sheet. One row per
(plant_key, metric, severity). Returns a queryable structure.

Sheet schema:
    plant_key  metric                severity  condition  value  duration_min  enabled  channels   notes
    ALL        inverter_offline      CRITICAL  duration   30     30            TRUE     email      Inverter dark >30min
    ALL        inverter_offline      WARNING   duration   10     10            TRUE     sheet      Brief outage
    ALL        pr_daily              WARNING   below      0.75   -             TRUE     sheet,email Daily PR below target
    ALL        pr_daily              CRITICAL  below      0.50   -             TRUE     email      Severe underperformance
    ALL        inverter_relative     WARNING   below      0.85   -             TRUE     sheet      Inverter <85% of peer mean
    QRO1       pr_daily              WARNING   below      0.70   -             TRUE     sheet      Per-plant override
    ALL        plant_offline_5m      CRITICAL  duration   30     30            TRUE     email      Whole plant dark

Columns:
    plant_key    -- "ALL" for portfolio-wide, or a specific key like "QRO1"
    metric       -- machine name of the metric; must be in KNOWN_METRICS
    severity     -- INFO | WARNING | CRITICAL
    condition    -- below | above | equals | duration
    value        -- numeric threshold (interpretation depends on metric+condition)
    duration_min -- minutes a 'duration' condition must persist (ignored otherwise)
    enabled      -- TRUE/FALSE; lets ops disable a check without deleting the row
    channels     -- comma-separated: sheet,email,slack (Stage 7.4 honors these)
    notes        -- free text

Plant-specific overrides: when looking up "what threshold applies to plant
QRO1 for metric pr_daily severity WARNING", we prefer a QRO1-specific row
over an "ALL" row. If neither exists, that check is silently skipped for
that plant.

Stage 7.1 ships the LOADER ONLY. No engine that USES the thresholds yet —
that's Stage 7.4. We deliberately separate these so you can populate the
sheet, run unit tests against the loader, and validate your data is clean
BEFORE any production code starts firing alerts based on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple

from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient

LOG = logging.getLogger("argia.core.thresholds")


# ---------- enums ----------


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Condition(str, Enum):
    BELOW = "below"
    ABOVE = "above"
    EQUALS = "equals"
    DURATION = "duration"  # for "X for at least N minutes" checks


# ---------- known metrics ----------
#
# We keep this list small and intentional. Adding a metric here is a
# code-and-tests change; you can't conjure new metrics from the sheet alone.
# That's a feature: the alert engine in Stage 7.4 must KNOW how to compute
# each metric, so unknown names are an error, not a no-op.

KNOWN_METRICS: FrozenSet[str] = frozenset({
    # Inverter-level
    "inverter_offline",       # individual inverter dark
    "inverter_relative",      # inverter producing < X% of peer mean
    "inverter_fault",         # vendor fault codes reported (FT/FC non-zero)
    "string_fault",           # NEW string-diagnostic bits vs trailing baseline
    "inverter_temp_high",     # inverter internal temperature
    # Plant-level
    "plant_offline",          # whole plant dark
    "pr_daily",               # end-of-day Performance Ratio
    "energy_daily_pct",       # end-of-day kWh vs expected (0-1)
    "plant_twin_yield",       # specific yield vs regional twin (0-1 ratio)
    # Data-quality / pipeline health
    "data_stale",             # no rows arrived for X minutes during daylight
})


# ---------- channels ----------

VALID_CHANNELS: FrozenSet[str] = frozenset({"sheet", "email", "slack"})


# ---------- data structures ----------


@dataclass(frozen=True)
class Threshold:
    """One alert threshold."""

    plant_key: str        # "ALL" or specific plant_key
    metric: str           # must be in KNOWN_METRICS
    severity: Severity
    condition: Condition
    value: float          # numeric threshold value
    duration_min: int     # only meaningful when condition == DURATION
    enabled: bool
    channels: FrozenSet[str]  # subset of VALID_CHANNELS
    notes: str = ""

    @property
    def applies_globally(self) -> bool:
        return self.plant_key.upper() == "ALL"


@dataclass(frozen=True)
class ThresholdSet:
    """The full collection of thresholds, indexed for fast lookup.

    Don't construct this directly; use ``load_thresholds()``.
    """

    # All thresholds, in the order they appeared in the sheet
    all_thresholds: Tuple[Threshold, ...] = ()

    # Index: (plant_key_or_ALL, metric, severity) → Threshold
    # Populated by __post_init__
    _index: Dict[Tuple[str, str, Severity], Threshold] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Build the lookup index. Because ThresholdSet is frozen, we have
        to use object.__setattr__ to assign to _index."""
        idx: Dict[Tuple[str, str, Severity], Threshold] = {}
        for t in self.all_thresholds:
            key = (t.plant_key.upper(), t.metric, t.severity)
            if key in idx:
                LOG.warning(
                    "Duplicate threshold (plant=%s metric=%s severity=%s) "
                    "— keeping first, ignoring rest",
                    t.plant_key, t.metric, t.severity.value,
                )
                continue
            idx[key] = t
        object.__setattr__(self, "_index", idx)

    def get(
        self,
        plant_key: str,
        metric: str,
        severity: Severity,
    ) -> Optional[Threshold]:
        """Return the threshold for (plant, metric, severity) with proper
        fallback to ALL. Returns None if neither plant-specific nor ALL
        exists. Disabled thresholds also return None (caller doesn't need
        to filter)."""
        pk = plant_key.upper()
        specific = self._index.get((pk, metric, severity))
        if specific is not None and specific.enabled:
            return specific
        global_t = self._index.get(("ALL", metric, severity))
        if global_t is not None and global_t.enabled:
            return global_t
        return None

    def all_metrics_configured(self) -> FrozenSet[str]:
        """Which metrics have at least one enabled threshold somewhere?"""
        return frozenset(
            t.metric for t in self.all_thresholds if t.enabled
        )

    def thresholds_for_plant(self, plant_key: str) -> List[Threshold]:
        """All enabled thresholds (specific + ALL) that apply to one plant.

        If both a plant-specific and an ALL row exist for the same
        (metric, severity), the plant-specific one wins.
        """
        pk = plant_key.upper()
        out: Dict[Tuple[str, Severity], Threshold] = {}
        for t in self.all_thresholds:
            if not t.enabled:
                continue
            if t.plant_key.upper() not in ("ALL", pk):
                continue
            sub_key = (t.metric, t.severity)
            existing = out.get(sub_key)
            if existing is None:
                out[sub_key] = t
            elif existing.applies_globally and not t.applies_globally:
                # plant-specific overrides global
                out[sub_key] = t
            # else: keep existing
        return list(out.values())


# ---------- header ----------

THRESHOLDS_HEADER = [
    "plant_key", "metric", "severity", "condition",
    "value", "duration_min", "enabled", "channels", "notes",
]

# Defaults written into a freshly-created Thresholds tab. Conservative —
# none of these will fire if you don't have telemetry yet. Tune later.
DEFAULT_THRESHOLDS: List[List[str]] = [
    # Inverter offline: WARNING after 15min, CRITICAL after 60min
    ["ALL", "inverter_offline", "WARNING",  "duration", "0", "15", "TRUE",
     "sheet", "Inverter dark for 15+ min"],
    ["ALL", "inverter_offline", "CRITICAL", "duration", "0", "60", "TRUE",
     "sheet,email", "Inverter dark for 60+ min"],
    # Inverter underperforming vs peer mean
    ["ALL", "inverter_relative", "WARNING", "below", "0.85", "-", "TRUE",
     "sheet",
     "Inverter <85% of peer mean (check shading, soiling)"],
    ["ALL", "inverter_relative", "CRITICAL", "below", "0.70", "-", "TRUE",
     "sheet,email",
     "Inverter <70% of peer mean (likely fault)"],
    # End-of-day PR
    ["ALL", "pr_daily", "WARNING", "below", "0.75", "-", "TRUE",
     "sheet", "Daily PR below 75%"],
    ["ALL", "pr_daily", "CRITICAL", "below", "0.50", "-", "TRUE",
     "sheet,email", "Daily PR below 50%"],
    # End-of-day energy vs expected
    ["ALL", "energy_daily_pct", "WARNING", "below", "0.85", "-", "TRUE",
     "sheet", "Daily kWh <85% of expected"],
    # Plant offline
    ["ALL", "plant_offline", "CRITICAL", "duration", "0", "30", "TRUE",
     "sheet,email", "Whole plant dark for 30+ min"],
    # Data-quality
    ["ALL", "data_stale", "WARNING", "duration", "0", "20", "TRUE",
     "sheet",
     "No telemetry rows arrived in 20+ min during daylight"],
]


# ---------- loader ----------


def _truthy(value) -> bool:
    s = normalize_text(value).lower()
    return s in ("true", "yes", "y", "1", "x")


def _parse_severity(raw) -> Optional[Severity]:
    s = normalize_text(raw).upper()
    try:
        return Severity(s)
    except ValueError:
        return None


def _parse_condition(raw) -> Optional[Condition]:
    s = normalize_text(raw).lower()
    try:
        return Condition(s)
    except ValueError:
        return None


def _parse_channels(raw) -> FrozenSet[str]:
    """Parse comma-separated channels, drop unknowns."""
    if raw is None:
        return frozenset()
    s = str(raw).strip().lower()
    if not s:
        return frozenset()
    parts = {p.strip() for p in s.split(",") if p.strip()}
    valid = parts & VALID_CHANNELS
    invalid = parts - VALID_CHANNELS
    if invalid:
        LOG.warning(
            "Unknown channels %s — valid: %s", invalid, sorted(VALID_CHANNELS),
        )
    return frozenset(valid)


def _parse_duration_min(raw) -> int:
    """Parse duration_min. Empty / non-numeric → 0."""
    f = safe_float(raw, 0.0)
    if f is None:
        return 0
    try:
        return max(0, int(f))
    except (ValueError, TypeError):
        return 0


def load_thresholds(sheets: SheetsClient) -> ThresholdSet:
    """Read the Thresholds tab. Malformed rows are skipped (logged), not raised.

    The intent: a bad row in the sheet should never crash a telemetry run.
    Stage 7.4 will surface bad rows separately so ops sees them, but the
    main pipeline keeps going."""
    try:
        rows = sheets.read_table("Thresholds", "A1:I")
    except Exception as e:
        LOG.warning(
            "Could not read Thresholds tab (%s). Returning empty ThresholdSet. "
            "Run create_thresholds_tab() to bootstrap it.", e,
        )
        return ThresholdSet(all_thresholds=())

    thresholds: List[Threshold] = []
    for i, row in enumerate(rows, start=2):  # row 1 is header, data starts at 2
        plant_key = normalize_text(row.get("plant_key"))
        metric = normalize_text(row.get("metric"))
        if not plant_key or not metric:
            continue

        if metric not in KNOWN_METRICS:
            LOG.warning(
                "Thresholds row %d: unknown metric '%s' — skipping. "
                "Valid metrics: %s", i, metric, sorted(KNOWN_METRICS),
            )
            continue

        severity = _parse_severity(row.get("severity"))
        if severity is None:
            LOG.warning(
                "Thresholds row %d: invalid severity '%s' — skipping",
                i, row.get("severity"),
            )
            continue

        condition = _parse_condition(row.get("condition"))
        if condition is None:
            LOG.warning(
                "Thresholds row %d: invalid condition '%s' — skipping",
                i, row.get("condition"),
            )
            continue

        value = safe_float(row.get("value"), 0.0)
        if value is None:
            value = 0.0

        duration_min = _parse_duration_min(row.get("duration_min"))

        # Sanity check: duration condition requires duration_min > 0
        if condition == Condition.DURATION and duration_min <= 0:
            LOG.warning(
                "Thresholds row %d: condition='duration' but duration_min=%d. "
                "Skipping — this threshold would never fire.",
                i, duration_min,
            )
            continue

        thresholds.append(Threshold(
            plant_key=plant_key,
            metric=metric,
            severity=severity,
            condition=condition,
            value=value,
            duration_min=duration_min,
            enabled=_truthy(row.get("enabled")),
            channels=_parse_channels(row.get("channels")),
            notes=normalize_text(row.get("notes")),
        ))

    LOG.info(
        "Loaded %d thresholds (%d enabled)",
        len(thresholds),
        sum(1 for t in thresholds if t.enabled),
    )
    return ThresholdSet(all_thresholds=tuple(thresholds))


def create_thresholds_tab_if_missing(sheets: SheetsClient) -> bool:
    """Bootstrap the Thresholds tab with header + sensible defaults.

    Idempotent: if the tab already has content, this is a no-op. Returns
    True if it created/populated the tab, False if it was already populated.

    This is a CONVENIENCE for first-time setup. Production code should not
    call this — it's intended for the docs runbook to point at."""
    sheets.ensure_tab("Thresholds")
    existing = sheets.read_range("Thresholds", "A1:I1")
    if existing and any(str(c).strip() for c in (existing[0] if existing else [])):
        LOG.info("Thresholds tab already has a header — leaving alone")
        return False

    sheets.ensure_header("Thresholds", THRESHOLDS_HEADER)
    sheets.append_rows("Thresholds", DEFAULT_THRESHOLDS, value_input_option="RAW")
    LOG.info(
        "Bootstrapped Thresholds tab with %d default rows", len(DEFAULT_THRESHOLDS),
    )
    return True
