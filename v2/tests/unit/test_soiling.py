"""Tests for argia.analytics.soiling."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.analytics.soiling import (
    APPROACHING_RATIO,
    CleaningCost,
    DUE_RATIO,
    MIN_DAYS_FOR_SOILING,
    OVERDUE_RATIO,
    SoilingDecision,
    _decide,
    assess_plant_soiling,
    load_cleaning_costs,
)
from argia.archive.kpi_daily import KpiDailyRow


# ============================================================
# _decide pure function
# ============================================================


class TestDecide:
    def test_insufficient_when_loss_none(self):
        assert _decide(None, 5000.0) == SoilingDecision.INSUFFICIENT_DATA

    def test_insufficient_when_cost_none(self):
        assert _decide(1000.0, None) == SoilingDecision.INSUFFICIENT_DATA

    def test_not_due_when_negative_loss(self):
        """Rolling PR > baseline (recently cleaned) → no action."""
        assert _decide(-500.0, 5000.0) == SoilingDecision.NOT_DUE

    def test_not_due_when_well_below_approaching(self):
        # 5000 × 0.3 = 1500 < APPROACHING_RATIO (0.5) × 5000 = 2500
        assert _decide(1500.0, 5000.0) == SoilingDecision.NOT_DUE

    def test_approaching_at_boundary(self):
        # = APPROACHING_RATIO × cost
        assert _decide(APPROACHING_RATIO * 5000.0, 5000.0) == SoilingDecision.APPROACHING

    def test_due_at_boundary(self):
        assert _decide(DUE_RATIO * 5000.0, 5000.0) == SoilingDecision.DUE

    def test_overdue_at_2x(self):
        assert _decide(OVERDUE_RATIO * 5000.0, 5000.0) == SoilingDecision.OVERDUE

    def test_far_overdue(self):
        assert _decide(20000.0, 5000.0) == SoilingDecision.OVERDUE


# ============================================================
# Fixtures
# ============================================================


def _kpi(date, pr=0.80, energy=2400.0, conf="HIGH", plant_key="P1"):
    return KpiDailyRow(
        date_iso=date, plant_key=plant_key,
        energy_kwh=energy, irradiance_kwh_m2=6.0,
        irradiance_source="shinemaster",
        pr=pr, pr_confidence=conf,
        capacity_factor=0.25, capacity_factor_confidence=conf,
        inverters_reporting=4, inverters_with_reboot=0,
        notes="", written_at_utc="",
    )


def _history(days=14, pr_pattern=None, plant_key="P1"):
    """Build a history ending at 2026-05-14 with N days back."""
    import datetime as dt
    end = dt.date(2026, 5, 14)
    out = []
    for i in range(days):
        d = (end - dt.timedelta(days=i)).isoformat()
        pr = pr_pattern[i] if pr_pattern else 0.80
        out.append(_kpi(d, pr=pr, plant_key=plant_key))
    return out


# ============================================================
# assess_plant_soiling — happy paths
# ============================================================


class TestAssessSoiling:
    def test_clean_plant_not_due(self):
        """Rolling PR matches baseline → no soiling, no action."""
        history = _history(days=14, pr_pattern=[0.80] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 8000.0, "2026-04-01"),
        )
        assert result.decision == SoilingDecision.NOT_DUE
        assert result.pr_loss_pct == pytest.approx(0.0)

    def test_dirty_plant_due(self):
        """7% PR loss × 2400 kWh × 30 days × 2.5 MXN/kWh = 12,600 MXN.
        Cleaning cost 5000 MXN → ratio 2.52 → OVERDUE."""
        history = _history(days=14, pr_pattern=[0.744] * 14)  # 7% below
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, "2026-04-01"),
        )
        assert result.pr_loss_pct == pytest.approx(0.07, abs=0.001)
        assert result.projected_monthly_loss_mxn == pytest.approx(12600.0, abs=10)
        assert result.decision == SoilingDecision.OVERDUE

    def test_moderate_dirty_approaching(self):
        """2% PR loss × 2400 × 30 × 2.5 = 3600 MXN. Cleaning cost 5000 MXN
        → ratio 0.72 → APPROACHING."""
        history = _history(days=14, pr_pattern=[0.784] * 14)  # 2% below
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, "2026-04-01"),
        )
        assert result.decision == SoilingDecision.APPROACHING


# ============================================================
# Missing inputs
# ============================================================


class TestMissingInputs:
    def test_missing_baseline_insufficient(self):
        history = _history(days=14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=None,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.decision == SoilingDecision.INSUFFICIENT_DATA
        assert "pr_baseline missing" in result.notes

    def test_missing_tariff_insufficient(self):
        history = _history(days=14, pr_pattern=[0.70] * 14)  # clearly dirty
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=None,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.decision == SoilingDecision.INSUFFICIENT_DATA
        # PR loss is still computed for informational purposes
        assert result.pr_loss_pct is not None

    def test_missing_cleaning_cost_insufficient(self):
        history = _history(days=14, pr_pattern=[0.70] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=None,
        )
        assert result.decision == SoilingDecision.INSUFFICIENT_DATA

    def test_zero_cost_not_due(self):
        """A zero cleaning cost shouldn't make every plant DUE."""
        history = _history(days=14, pr_pattern=[0.70] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 0.0, ""),
        )
        assert result.decision == SoilingDecision.NOT_DUE


# ============================================================
# Insufficient history
# ============================================================


class TestInsufficientHistory:
    def test_too_few_days(self):
        history = _history(days=MIN_DAYS_FOR_SOILING - 1, pr_pattern=[0.70] * 6)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.decision == SoilingDecision.INSUFFICIENT_DATA
        assert "Only" in result.notes

    def test_low_confidence_rows_dropped(self):
        """Default min_confidence=MEDIUM. Rows with confidence=LOW are
        dropped from the rolling median."""
        import datetime as dt
        rows = []
        end = dt.date(2026, 5, 14)
        for i in range(14):
            d = (end - dt.timedelta(days=i)).isoformat()
            # All LOW confidence
            rows.append(_kpi(d, pr=0.70, conf="LOW"))
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=rows,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.decision == SoilingDecision.INSUFFICIENT_DATA
        # Note: pr_rolling_median is None because none of the LOW rows count
        assert result.pr_rolling_median is None


# ============================================================
# Rolling PR uses median (robust to one bad day)
# ============================================================


class TestRollingMedian:
    def test_one_bad_day_doesnt_skew(self):
        """13 days at 0.80 + 1 day at 0.20 (cloud). Median is 0.80;
        mean would be 0.74. Soiling decision must use median."""
        pattern = [0.80] * 13 + [0.20]
        history = _history(days=14, pr_pattern=pattern)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        # Median is 0.80, no loss
        assert result.pr_rolling_median == 0.80
        assert result.decision == SoilingDecision.NOT_DUE


# ============================================================
# Rolling PR ABOVE baseline (suspicious)
# ============================================================


class TestRollingAboveBaseline:
    def test_negative_loss_logged_and_not_due(self):
        """Rolling PR is HIGHER than baseline — usually means recent cleaning
        or wrong baseline. Decision is NOT_DUE, notes warn."""
        history = _history(days=14, pr_pattern=[0.85] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.decision == SoilingDecision.NOT_DUE
        assert result.pr_loss_pct < 0
        assert "ABOVE baseline" in result.notes


# ============================================================
# load_cleaning_costs
# ============================================================


class TestLoadCleaningCosts:
    def test_empty(self):
        sheets = MagicMock()
        sheets.read_table.return_value = []
        result = load_cleaning_costs(sheets)
        assert result == {}

    def test_sheets_error_returns_empty(self):
        sheets = MagicMock()
        sheets.read_table.side_effect = Exception("not found")
        result = load_cleaning_costs(sheets)
        assert result == {}

    def test_parses_row(self):
        sheets = MagicMock()
        sheets.read_table.return_value = [{
            "plant_key": "QRO1", "cost_mxn": "8500",
            "last_cleaned_date": "2026-03-15", "notes": "water truck",
        }]
        result = load_cleaning_costs(sheets)
        assert "QRO1" in result
        assert result["QRO1"].cost_mxn == 8500.0
        assert result["QRO1"].last_cleaned_date == "2026-03-15"


# ============================================================
# days_since_last_cleaning
# ============================================================


class TestDaysSinceClean:
    def test_days_calc(self):
        history = _history(days=14, pr_pattern=[0.80] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, "2026-04-14"),
        )
        assert result.days_since_last_cleaning == 30

    def test_none_when_no_clean_date(self):
        history = _history(days=14, pr_pattern=[0.80] * 14)
        result = assess_plant_soiling(
            plant_key="P1", as_of_date="2026-05-14",
            kpi_history=history,
            pr_baseline=0.80,
            tariff_mxn_per_kwh=2.5,
            cleaning_cost=CleaningCost("P1", 5000.0, ""),
        )
        assert result.days_since_last_cleaning is None
