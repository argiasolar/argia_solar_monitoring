"""Soiling analysis — Stage 7.3.

Decides "is this plant due for cleaning?" by comparing the rolling-median
PR over the last N days against a stored baseline PR (from when the plant
was known clean), then translating the deficit into pesos and comparing
to the plant's cleaning cost.

Honest limitations
==================

1. **Baseline PR (``pr_baseline`` on Plants tab) must be set by hand**
   after the plant has been monitored for ~30 days following a known-
   clean event. There is no auto-baseline in 7.3. Missing baseline →
   plant is skipped with a logged warning.

2. **Soiling is conflated with everything else that lowers PR**: panel
   aging (~0.5% per year, slow), inverter degradation, sensor drift,
   shading changes from new construction, irrigation overspray. We're
   computing "PR loss vs baseline", not "soiling specifically." The
   notification language must reflect that.

3. **The dollar math is a projection, not a measurement**. We project
   "what would the next 30 days cost at current loss rate" using the
   plant's typical daily kWh and tariff. Actual recovery after cleaning
   may differ — sometimes the panels just keep getting dirty.

4. **Tariff in MXN per kWh must be set on Plants tab**. Missing tariff
   → we skip the dollar comparison and report only the loss %.

5. **First-day-of-history values are noisy.** We require at least 7 days
   of post-baseline data before computing anything. Fewer days → skip.

Stage 7.3 ships the framework. Tuning the alert threshold ("alert when
projected loss > X × cleaning cost") happens in 7.5 once real data exists.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from argia.archive.kpi_daily import KpiDailyRow, rows_for_window
from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient

LOG = logging.getLogger("argia.analytics.soiling")


# ---------- enums ----------


class SoilingDecision(str, Enum):
    NOT_DUE = "NOT_DUE"
    """Loss is small or recoverable cost-benefit is unfavorable."""

    APPROACHING = "APPROACHING"
    """Projected monthly loss is between 50%–100% of cleaning cost.
    Mention in next report, don't alert yet."""

    DUE = "DUE"
    """Projected monthly loss > cleaning cost. Cleaning will pay back
    in <1 month. Alert ops."""

    OVERDUE = "OVERDUE"
    """Projected monthly loss > 2× cleaning cost. Long overdue."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    """Not enough days of KPI history yet, or baseline missing, or
    confidence too low. No decision possible."""


# ---------- Cleaning_Costs schema ----------


CLEANING_COSTS_TAB = "Cleaning_Costs"

CLEANING_COSTS_HEADER = [
    "plant_key", "cost_mxn", "last_cleaned_date", "notes",
]


@dataclass(frozen=True)
class CleaningCost:
    plant_key: str
    cost_mxn: Optional[float]
    """Cost of one full cleaning, MXN. Includes labor, water truck, access."""
    last_cleaned_date: str
    """ISO date. Empty if never cleaned / unknown."""
    notes: str = ""


def load_cleaning_costs(sheets: SheetsClient) -> Dict[str, CleaningCost]:
    """Read Cleaning_Costs tab. Returns dict plant_key → CleaningCost.

    Missing tab is non-fatal — returns empty dict and logs."""
    try:
        rows = sheets.read_table(CLEANING_COSTS_TAB, "A1:D")
    except Exception as e:
        LOG.warning("Could not read %s: %s — returning empty", CLEANING_COSTS_TAB, e)
        return {}

    out: Dict[str, CleaningCost] = {}
    for r in rows:
        plant_key = normalize_text(r.get("plant_key"))
        if not plant_key:
            continue
        out[plant_key] = CleaningCost(
            plant_key=plant_key,
            cost_mxn=safe_float(r.get("cost_mxn")),
            last_cleaned_date=normalize_text(r.get("last_cleaned_date")),
            notes=normalize_text(r.get("notes")),
        )
    LOG.info("Loaded %d cleaning cost entries", len(out))
    return out


def create_cleaning_costs_tab_if_missing(
    sheets: SheetsClient,
    plant_keys: Optional[List[str]] = None,
) -> bool:
    """Bootstrap Cleaning_Costs tab. If plant_keys given, pre-populates
    rows with empty values so ops just fills in the numbers."""
    sheets.ensure_tab(CLEANING_COSTS_TAB)
    existing = sheets.read_range(CLEANING_COSTS_TAB, "A1:D1")
    if existing and any(str(c).strip() for c in (existing[0] if existing else [])):
        LOG.info("%s already has header — leaving alone", CLEANING_COSTS_TAB)
        return False
    sheets.ensure_header(CLEANING_COSTS_TAB, CLEANING_COSTS_HEADER)
    if plant_keys:
        rows = [[pk, "", "", ""] for pk in plant_keys]
        sheets.append_rows(CLEANING_COSTS_TAB, rows, value_input_option="RAW")
        LOG.info("Bootstrapped %s with %d empty plant rows",
                 CLEANING_COSTS_TAB, len(rows))
    return True


# ---------- soiling analysis ----------


MIN_DAYS_FOR_SOILING = 7
"""Don't compute soiling unless we have at least this many days of
post-baseline KPI history."""

APPROACHING_RATIO = 0.5
"""projected_monthly_loss_mxn >= APPROACHING_RATIO × cleaning_cost_mxn → APPROACHING"""

DUE_RATIO = 1.0
"""projected_monthly_loss_mxn >= DUE_RATIO × cleaning_cost_mxn → DUE"""

OVERDUE_RATIO = 2.0
"""projected_monthly_loss_mxn >= OVERDUE_RATIO × cleaning_cost_mxn → OVERDUE"""


@dataclass(frozen=True)
class SoilingAssessment:
    """Soiling analysis for one plant on one day."""

    plant_key: str
    as_of_date: str
    decision: SoilingDecision

    # Inputs / observations
    pr_baseline: Optional[float]
    pr_rolling_median: Optional[float]
    rolling_window_days: int
    days_used: int

    # Derived
    pr_loss_pct: Optional[float]
    """How much below baseline, as a fraction. e.g. 0.07 = 7% loss."""

    avg_daily_kwh: Optional[float]
    """Mean daily energy over the rolling window (used for projection)."""

    projected_monthly_loss_kwh: Optional[float]
    """pr_loss × avg_daily_kwh × 30."""

    projected_monthly_loss_mxn: Optional[float]
    """projected_monthly_loss_kwh × tariff_mxn_per_kwh. None when tariff missing."""

    cleaning_cost_mxn: Optional[float]
    days_since_last_cleaning: Optional[int]

    notes: str = ""


def _rolling_median_pr(
    rows: List[KpiDailyRow],
    min_confidence: str = "MEDIUM",
) -> Optional[float]:
    """Median PR across the given rows. Only include rows with
    ``pr_confidence`` >= min_confidence. Returns None if too few qualify.

    min_confidence rank: HIGH=3, MEDIUM=2, LOW=1, NONE=0."""
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}
    min_rank = rank.get(min_confidence.upper(), 2)
    valid = [
        r.pr for r in rows
        if r.pr is not None
        and rank.get(r.pr_confidence.upper(), 0) >= min_rank
    ]
    if len(valid) < MIN_DAYS_FOR_SOILING:
        return None
    return statistics.median(valid)


def _avg_daily_kwh(rows: List[KpiDailyRow]) -> Optional[float]:
    """Mean of energy_kwh over the rolling window. None if too few."""
    valid = [r.energy_kwh for r in rows if r.energy_kwh is not None]
    if len(valid) < MIN_DAYS_FOR_SOILING:
        return None
    return sum(valid) / len(valid)


def _decide(
    projected_loss_mxn: Optional[float],
    cleaning_cost_mxn: Optional[float],
) -> SoilingDecision:
    """Apply ratio thresholds.

    Returns NOT_DUE if projected loss is negative (rolling PR ABOVE
    baseline — typical after a recent cleaning). That's good news but
    not actionable."""
    if projected_loss_mxn is None or cleaning_cost_mxn is None:
        return SoilingDecision.INSUFFICIENT_DATA
    if projected_loss_mxn <= 0 or cleaning_cost_mxn <= 0:
        return SoilingDecision.NOT_DUE
    ratio = projected_loss_mxn / cleaning_cost_mxn
    if ratio >= OVERDUE_RATIO:
        return SoilingDecision.OVERDUE
    if ratio >= DUE_RATIO:
        return SoilingDecision.DUE
    if ratio >= APPROACHING_RATIO:
        return SoilingDecision.APPROACHING
    return SoilingDecision.NOT_DUE


def _days_since(date_iso: str, as_of: str) -> Optional[int]:
    try:
        a = dt.date.fromisoformat(date_iso)
        b = dt.date.fromisoformat(as_of)
        return (b - a).days
    except (ValueError, TypeError):
        return None


def assess_plant_soiling(
    plant_key: str,
    as_of_date: str,
    kpi_history: List[KpiDailyRow],
    pr_baseline: Optional[float],
    tariff_mxn_per_kwh: Optional[float],
    cleaning_cost: Optional[CleaningCost],
    window_days: int = 14,
) -> SoilingAssessment:
    """The main analysis function. Pure — caller passes pre-loaded inputs.

    Args:
        plant_key: identifier for the result
        as_of_date: end date of the rolling window (inclusive), 'YYYY-MM-DD'
        kpi_history: ALL available KpiDailyRows (caller doesn't need to
            pre-filter; this function filters to plant + window internally)
        pr_baseline: clean-state PR for this plant, from Plants tab. None
            → INSUFFICIENT_DATA decision
        tariff_mxn_per_kwh: energy price for dollar projection. None →
            decision falls back to PR-loss-only (still INSUFFICIENT_DATA
            because we can't compare to cost without dollars)
        cleaning_cost: this plant's CleaningCost row. None → cost-benefit
            comparison skipped, decision falls back to INSUFFICIENT_DATA
        window_days: how far back to roll. Default 14.
    """
    plant_rows = rows_for_window(
        kpi_history, as_of_date, window_days, plant_key=plant_key,
    )

    notes: List[str] = []

    # Early exits — assemble result with as much detail as we have
    if pr_baseline is None:
        notes.append(
            "pr_baseline missing on Plants tab. Set it to a known-clean "
            "rolling-median PR (run 30 days post-cleaning to derive)."
        )
    if cleaning_cost is None or cleaning_cost.cost_mxn is None:
        notes.append(
            "cleaning cost not configured on Cleaning_Costs tab."
        )
    if tariff_mxn_per_kwh is None:
        notes.append(
            "tariff_mxn_per_kwh missing on Plants tab."
        )

    rolling_pr = _rolling_median_pr(plant_rows)
    avg_kwh = _avg_daily_kwh(plant_rows)

    if rolling_pr is None or avg_kwh is None:
        notes.append(
            f"Only {len(plant_rows)} day(s) of usable history in last "
            f"{window_days}; need >= {MIN_DAYS_FOR_SOILING}."
        )
        return SoilingAssessment(
            plant_key=plant_key,
            as_of_date=as_of_date,
            decision=SoilingDecision.INSUFFICIENT_DATA,
            pr_baseline=pr_baseline,
            pr_rolling_median=rolling_pr,
            rolling_window_days=window_days,
            days_used=len(plant_rows),
            pr_loss_pct=None,
            avg_daily_kwh=avg_kwh,
            projected_monthly_loss_kwh=None,
            projected_monthly_loss_mxn=None,
            cleaning_cost_mxn=cleaning_cost.cost_mxn if cleaning_cost else None,
            days_since_last_cleaning=_days_since(
                cleaning_cost.last_cleaned_date, as_of_date,
            ) if cleaning_cost and cleaning_cost.last_cleaned_date else None,
            notes="; ".join(notes),
        )

    # Compute loss (only meaningful if we have a baseline)
    pr_loss_pct: Optional[float] = None
    projected_monthly_loss_kwh: Optional[float] = None
    projected_monthly_loss_mxn: Optional[float] = None

    if pr_baseline is not None and pr_baseline > 0:
        pr_loss_pct = (pr_baseline - rolling_pr) / pr_baseline
        # 30-day projection
        # If pr_loss_pct is positive: panels are dirty, we're losing avg × loss × 30 kWh
        # If negative: panels are cleaner than baseline (rare; reset baseline)
        projected_monthly_loss_kwh = avg_kwh * pr_loss_pct * 30.0
        if tariff_mxn_per_kwh is not None:
            projected_monthly_loss_mxn = projected_monthly_loss_kwh * tariff_mxn_per_kwh

    # Decision
    cost_mxn = cleaning_cost.cost_mxn if cleaning_cost else None
    decision = _decide(projected_monthly_loss_mxn, cost_mxn)
    if pr_baseline is None or cost_mxn is None or tariff_mxn_per_kwh is None:
        decision = SoilingDecision.INSUFFICIENT_DATA

    if pr_loss_pct is not None and pr_loss_pct < 0:
        notes.append(
            f"Rolling PR ({rolling_pr:.3f}) is ABOVE baseline ({pr_baseline:.3f}). "
            f"Consider updating the baseline."
        )

    return SoilingAssessment(
        plant_key=plant_key,
        as_of_date=as_of_date,
        decision=decision,
        pr_baseline=pr_baseline,
        pr_rolling_median=rolling_pr,
        rolling_window_days=window_days,
        days_used=len(plant_rows),
        pr_loss_pct=pr_loss_pct,
        avg_daily_kwh=avg_kwh,
        projected_monthly_loss_kwh=projected_monthly_loss_kwh,
        projected_monthly_loss_mxn=projected_monthly_loss_mxn,
        cleaning_cost_mxn=cost_mxn,
        days_since_last_cleaning=_days_since(
            cleaning_cost.last_cleaned_date, as_of_date,
        ) if cleaning_cost and cleaning_cost.last_cleaned_date else None,
        notes="; ".join(notes),
    )
