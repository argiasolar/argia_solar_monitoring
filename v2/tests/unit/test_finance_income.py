"""Income/DSCR tests.

Reconciliation targets are the July-2026 figures verified by hand
against the live sheet and v1 exports during the finance-layer build:
PPA expected incomes to the cent, LaaS fee × 17.98 conversions, the
7-of-31-day proration, and the actual Jul 1-7 revenue (93,328 kWh /
207,832.36 MXN across 6 plants).
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.finance.contract import CONTRACT_HEADER, load_contract_monthly
from argia.finance.income import (
    Period, actual_income, debt_service_for_period, dscr,
    expected_income_month, load_kpi_energy, om_cost_for_period,
    xr_for_month,
)
from argia.finance.loans import load_loan_schedule

DATA = Path(__file__).resolve().parents[2] / "data" / "finance"

# KPI_Daily Jul 1-7 per-plant energy (verified against live sheet)
JULY_MTD_ENERGY = {
    "SLP1": 5676.1, "SLP2": 9265.0, "GTO1": 25380.1,
    "MEX1": 14827.7, "NL1": 21441.5, "MEX2": 16737.8,
}
JULY_TARIFFS = {"SLP1": 2.596, "SLP2": 2.462, "GTO1": 1.975,
                "MEX1": 2.508, "NL1": 2.022, "MEX2": 2.367}


def _contracts():
    with open(DATA / "contract_monthly_seed.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    sheets = MagicMock(spec=SheetsClient)
    sheets.read_range.return_value = (
        [CONTRACT_HEADER] + [[r[c] for c in CONTRACT_HEADER] for r in rows])
    return load_contract_monthly(sheets)


def _schedule():
    with open(DATA / "loan_schedule_seed.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    sheets = MagicMock(spec=SheetsClient)
    sheets.read_table.return_value = rows
    return load_loan_schedule(sheets)


def _kpi_energy():
    """Synthetic KPI_Daily read for Jul 1-7 (energy spread arbitrarily
    across days; only the per-plant totals are pinned)."""
    header = ["date_iso", "plant_key", "energy_kwh"]
    rows = []
    for plant, total in JULY_MTD_ENERGY.items():
        per_day = total / 7.0
        for d in range(1, 8):
            rows.append(["2026-07-%02d" % d, plant, per_day])
    sheets = MagicMock(spec=SheetsClient)
    sheets.read_range.return_value = [header] + rows
    return load_kpi_energy(sheets, Period.from_iso("2026-07-01",
                                                   "2026-07-07"))


JULY_FULL = Period.from_iso("2026-07-01", "2026-07-31")
JULY_MTD = Period.from_iso("2026-07-01", "2026-07-07")


class TestPeriod:
    def test_single_month_overlap(self):
        assert JULY_MTD.month_overlaps() == [(2026, 7, 7, 31)]
        assert JULY_FULL.month_overlaps() == [(2026, 7, 31, 31)]

    def test_cross_month(self):
        p = Period.from_iso("2026-06-25", "2026-07-05")
        assert p.month_overlaps() == [(2026, 6, 6, 30), (2026, 7, 5, 31)]
        assert p.days == 11

    def test_cross_year(self):
        p = Period.from_iso("2026-12-30", "2027-01-02")
        assert p.month_overlaps() == [(2026, 12, 2, 31), (2027, 1, 2, 31)]

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError):
            Period.from_iso("2026-07-05", "2026-07-01")


class TestExpectedIncome:
    def test_ppa_july_to_the_cent(self):
        cm, sched = _contracts(), _schedule()
        expect = {"SLP1": 64572.90, "SLP2": 120468.12, "GTO1": 187028.55,
                  "MEX1": 219201.71, "NL1": 195539.53, "MEX2": 212743.59}
        for plant, val in expect.items():
            assert expected_income_month(cm, sched, plant, 2026, 7) == \
                pytest.approx(val, abs=0.02), plant

    def test_laas_uses_schedule_xr(self):
        cm, sched = _contracts(), _schedule()
        assert expected_income_month(cm, sched, "LOAX1", 2026, 7) == \
            pytest.approx(26750 * 17.98, abs=0.01)
        assert expected_income_month(cm, sched, "LGTO1", 2026, 7) == \
            pytest.approx(15233 * 17.98, abs=0.01)

    def test_unknown_month_is_none(self):
        cm, sched = _contracts(), _schedule()
        assert expected_income_month(cm, sched, "SLP1", 2050, 1) is None


class TestXr:
    def test_rate_resolution(self):
        sched = _schedule()
        assert xr_for_month(sched, "LOAX1", 2026, 7) == pytest.approx(17.98)
        assert xr_for_month(sched, "GTO1", 2026, 7) is None   # MXN loan

    def test_divergent_rates_warn_and_use_first(self, caplog):
        from argia.finance.loans import ScheduleRow

        def row(lid, xr):
            return ScheduleRow(loan_id=lid, plant_key="LGTO1",
                               ref_month="2027-01", installment_no=1,
                               total_installments=10, payment_mxn=1.0,
                               payment_ccy=1.0, xr=xr, due_after_mxn=0.0)
        with caplog.at_level("WARNING"):
            got = xr_for_month([row("LGTO1-L1", 18.0),
                                row("LGTO1-L2", 19.5)], "LGTO1", 2027, 1)
        assert got == 18.0
        assert "disagree" in caplog.text


class TestActualIncome:
    def test_ppa_july_mtd_totals(self):
        cm, sched, kpi = _contracts(), _schedule(), _kpi_energy()
        total = 0.0
        for plant, kwh in JULY_MTD_ENERGY.items():
            inc = actual_income(kpi, cm, sched, plant, JULY_MTD)
            assert inc == pytest.approx(kwh * JULY_TARIFFS[plant],
                                        abs=0.05), plant
            total += inc
        assert total == pytest.approx(207832.36, abs=0.5)

    def test_laas_accrues_prorated(self):
        cm, sched = _contracts(), _schedule()
        inc = actual_income({}, cm, sched, "LOAX1", JULY_MTD)
        assert inc == pytest.approx(26750 * 17.98 * 7 / 31, abs=0.01)

    def test_ppa_without_kpi_rows_is_none(self):
        cm, sched = _contracts(), _schedule()
        assert actual_income({}, cm, sched, "SLP1", JULY_MTD) is None

    def test_tariff_fallback_used_when_month_missing(self):
        cm, sched = {}, _schedule()
        kpi = {("SLP1", "2026-07-03"): 100.0}
        inc = actual_income(kpi, cm, sched, "SLP1", JULY_MTD,
                            tariff_fallback=2.596)
        assert inc == pytest.approx(259.60)


class TestBillablePreference:
    def test_billable_column_wins_over_energy(self):
        header = ["date_iso", "plant_key", "energy_kwh", "billable_kwh"]
        rows = [["2026-07-03", "MEX2", 284.59, 2972.43]]
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [header] + rows
        kpi = load_kpi_energy(sheets, JULY_MTD)
        assert kpi[("MEX2", "2026-07-03")] == pytest.approx(2972.43)

    def test_energy_fallback_when_no_billable(self):
        kpi = _kpi_energy()
        assert sum(v for (p, _), v in kpi.items() if p == "GTO1") == \
            pytest.approx(25380.1, abs=0.01)


class TestDebtServiceAndOm:
    def test_full_month_matches_loan_layer(self):
        sched = _schedule()
        assert debt_service_for_period(sched, "SLP1", JULY_FULL) == \
            pytest.approx(12500.00)
        assert debt_service_for_period(sched, "LOAX1", JULY_FULL) == \
            pytest.approx(340224.75)

    def test_mtd_prorates_by_days(self):
        sched = _schedule()
        assert debt_service_for_period(sched, "GTO1", JULY_MTD) == \
            pytest.approx(151558.14 * 7 / 31, abs=0.01)

    def test_prorate_off_for_whole_month_reporting(self):
        sched = _schedule()
        assert debt_service_for_period(sched, "GTO1", JULY_MTD,
                                       prorate=False) == \
            pytest.approx(151558.14)

    def test_om_proration_and_none(self):
        assert om_cost_for_period(8000.0, JULY_MTD) == \
            pytest.approx(8000 * 7 / 31)
        assert om_cost_for_period(None, JULY_MTD) == 0.0

    def test_month_with_no_installment_is_zero(self):
        sched = _schedule()
        p = Period.from_iso("2033-01-01", "2033-01-31")
        assert debt_service_for_period(sched, "MEX2", p) == 0.0


class TestDscr:
    def test_basic_and_edge(self):
        assert dscr(120.0, 100.0) == pytest.approx(1.2)
        assert dscr(None, 100.0) is None
        assert dscr(100.0, 0.0) is None   # no debt -> no DSCR

    def test_portfolio_expected_july(self):
        cm, sched = _contracts(), _schedule()
        plants = list(JULY_MTD_ENERGY) + ["LOAX1", "LGTO1"]
        inc = sum(expected_income_month(cm, sched, p, 2026, 7)
                  for p in plants)
        svc = sum(debt_service_for_period(sched, p, JULY_FULL)
                  for p in plants)
        assert inc == pytest.approx(1754409.06, abs=1.0)
        assert svc == pytest.approx(1267488.22, abs=0.05)
        assert dscr(inc, svc) == pytest.approx(1.384, abs=0.001)


class TestInstallmentLabel:
    """Loan position labels (user request 2026-07-10): paid/total per
    ACTIVE loan at a month, verified against the real schedule."""

    def test_july_positions_match_schedule(self):
        from argia.finance.loans import installment_label
        sched = _schedule()
        expect = {"GTO1": "22/84", "LGTO1": "13/72", "LOAX1": "35/82",
                  "MEX1": "20/84", "MEX2": "10/84", "NL1": "20/84",
                  "SLP2": "25/63"}
        for pk, label in expect.items():
            assert installment_label(sched, pk, "2026-07") == label, pk

    def test_slp1_handoff_between_loans(self):
        # L1 ends 2026-05 (24/24), L2 starts 2026-06 — completed loan
        # drops out, so July shows only the active refinance
        from argia.finance.loans import installment_label
        sched = _schedule()
        assert installment_label(sched, "SLP1", "2026-05") == "24/24"
        assert installment_label(sched, "SLP1", "2026-06") == "1/12"
        assert installment_label(sched, "SLP1", "2026-07") == "2/12"

    def test_boundaries(self):
        from argia.finance.loans import installment_label
        sched = _schedule()
        assert installment_label(sched, "SLP1", "2028-01") == "paid off"
        assert installment_label(sched, "SLP1",
                                 "2024-01") == "starts 2024-06"
        assert installment_label(sched, "NOLOAN", "2026-07") == ""

    def test_two_active_loans_show_separately(self):
        from argia.finance.loans import ScheduleRow, installment_label

        def row(lid, no, tot):
            return ScheduleRow(loan_id=lid, plant_key="LGTO1",
                               ref_month="2027-01", installment_no=no,
                               total_installments=tot, payment_mxn=1.0,
                               payment_ccy=1.0, xr=18.0,
                               due_after_mxn=0.0)
        sched = [row("LGTO1-L1", 19, 72), row("LGTO1-L2", 3, 48)]
        assert installment_label(sched, "LGTO1",
                                 "2027-01") == "19/72 · 3/48"
