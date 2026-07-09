"""Finance layer tests.

The reconciliation block is the migration's proof of faithfulness: the
derived July-2026 debt service must reproduce v1's stored Credit-tab
figures for every plant EXCEPT SLP1, where v1 is documented-wrong
(24,622.50 stored vs 12,500.00 actually owed — its Credit row was never
updated after the June-2026 refinance). If SLP1 ever "reconciles" to
v1, someone has broken the fix.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.finance.loans import (
    current_month, fx_exposure, load_loan_schedule, load_loans,
    monthly_debt_service, outstanding_balance, portfolio_debt_service,
)

DATA = Path(__file__).resolve().parents[2] / "data" / "finance"

# v1 ARGIA_Solar Credit tab, export 2026-07-08 — stored Monthly_Payment
V1_CREDIT_MONTHLY_PAYMENT = {
    "GTO1": 151558.14, "LGTO1": 214944.43, "LOAX1": 340224.75,
    "MEX1": 187052.01, "MEX2": 143449.87, "NL1": 151535.81,
    "SLP1": 24622.50,   # STALE — see module docstring
    "SLP2": 66223.21,
}
SLP1_TRUE_JULY_PAYMENT = 12500.00


def _seed_schedule():
    with open(DATA / "loan_schedule_seed.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def _seed_loans():
    with open(DATA / "loans_seed.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def _schedule_rows():
    """Seed CSV -> ScheduleRow list via the real loader (mock client)."""
    sheets = MagicMock(spec=SheetsClient)
    sheets.read_table.return_value = _seed_schedule()
    return load_loan_schedule(sheets)


# ---------------------------------------------------------------------------
# Seed integrity
# ---------------------------------------------------------------------------

class TestSeedIntegrity:
    def test_nine_loans(self):
        loans = _seed_loans()
        assert len(loans) == 9
        assert sorted(l["loan_id"] for l in loans) == [
            "GTO1-L1", "LGTO1-L1", "LOAX1-L1", "MEX1-L1", "MEX2-L1",
            "NL1-L1", "SLP1-L1", "SLP1-L2", "SLP2-L1",
        ]

    def test_589_schedule_rows_no_orphans(self):
        sched = _seed_schedule()
        assert len(sched) == 589
        lids = {l["loan_id"] for l in _seed_loans()}
        assert {r["loan_id"] for r in sched} <= lids

    def test_slp1_has_two_loans_back_to_back(self):
        loans = {l["loan_id"]: l for l in _seed_loans()}
        assert loans["SLP1-L1"]["last_month"] == "2026-05"
        assert loans["SLP1-L2"]["first_month"] == "2026-06"
        assert float(loans["SLP1-L2"]["principal_mxn"]) == 150000.00

    def test_loax1_is_one_loan_with_82_installments(self):
        # v1's month-1 row said "1/83"; installments run 1..82
        # continuously so it is a single 82-installment facility.
        rows = [r for r in _seed_schedule() if r["loan_id"] == "LOAX1-L1"]
        assert len(rows) == 82
        assert sorted(int(r["installment_no"]) for r in rows) == \
            list(range(1, 83))

    def test_usd_identity_every_row(self):
        # payment_mxn == payment_ccy * xr for all USD rows, to the cent
        checked = 0
        for r in _seed_schedule():
            if r["payment_ccy"] and r["xr"]:
                checked += 1
                assert abs(float(r["payment_ccy"]) * float(r["xr"]) -
                           float(r["payment_mxn"])) < 0.05, r
        assert checked == 154

    def test_usd_loans_are_the_laas_facilities(self):
        loans = {l["loan_id"]: l for l in _seed_loans()}
        usd = sorted(lid for lid, l in loans.items()
                     if l["currency"] == "USD")
        assert usd == ["LGTO1-L1", "LOAX1-L1"]


# ---------------------------------------------------------------------------
# Reconciliation against v1 — the migration's proof
# ---------------------------------------------------------------------------

class TestV1Reconciliation:
    def test_july_2026_debt_service_matches_v1_except_slp1(self):
        service = monthly_debt_service(_schedule_rows(), "2026-07")
        for plant, v1_value in V1_CREDIT_MONTHLY_PAYMENT.items():
            if plant == "SLP1":
                continue
            assert service[plant] == pytest.approx(v1_value, abs=0.01), plant

    def test_slp1_is_the_documented_v1_discrepancy(self):
        service = monthly_debt_service(_schedule_rows(), "2026-07")
        assert service["SLP1"] == pytest.approx(SLP1_TRUE_JULY_PAYMENT,
                                                abs=0.01)
        # guard the guard: v1's stale figure must NOT reconcile
        assert abs(service["SLP1"] - V1_CREDIT_MONTHLY_PAYMENT["SLP1"]) > 1

    def test_slp1_refinance_seam_may_to_june_2026(self):
        # L1's final installment (24/24) lands in May; L2's first (1/12)
        # in June — a clean handover with no overlap month. v1's Credit
        # row kept quoting L1's payment after the handover.
        rows = _schedule_rows()
        may = sorted((r.loan_id, r.installment_no) for r in rows
                     if r.plant_key == "SLP1" and r.ref_month == "2026-05")
        jun = sorted((r.loan_id, r.installment_no) for r in rows
                     if r.plant_key == "SLP1" and r.ref_month == "2026-06")
        assert may == [("SLP1-L1", 24)]
        assert jun == [("SLP1-L2", 1)]

    def test_portfolio_service_july(self):
        total = portfolio_debt_service(_schedule_rows(), "2026-07")
        expected = (sum(V1_CREDIT_MONTHLY_PAYMENT.values())
                    - V1_CREDIT_MONTHLY_PAYMENT["SLP1"]
                    + SLP1_TRUE_JULY_PAYMENT)
        assert total == pytest.approx(expected, abs=0.05)


# ---------------------------------------------------------------------------
# Derived queries
# ---------------------------------------------------------------------------

class TestQueries:
    def test_debt_service_absent_before_loan_starts(self):
        service = monthly_debt_service(_schedule_rows(), "2023-01")
        assert service == {}  # earliest loan (LOAX1) starts 2023-09

    def test_outstanding_balance_amortizes(self):
        rows = _schedule_rows()
        early = outstanding_balance(rows, "2025-01")
        late = outstanding_balance(rows, "2026-07")
        assert late["GTO1-L1"] < early["GTO1-L1"]

    def test_outstanding_reports_principal_for_unstarted_loan(self):
        # As of 2024-01, MEX2's loan (starts 2025-10) hasn't amortized:
        # its earliest known balance is reported, not silently dropped.
        bal = outstanding_balance(_schedule_rows(), "2024-01")
        assert "MEX2-L1" in bal

    def test_final_installment_clears_balance(self):
        # v1's amortization leaves a -0.46 MXN rounding residue on
        # SLP1-L1's last row; "cleared" means < 1 MXN, not exactly 0.
        rows = [r for r in _schedule_rows() if r.loan_id == "SLP1-L1"]
        assert rows[-1].installment_no == 24
        assert abs(rows[-1].due_after_mxn) < 1.0

    def test_fx_exposure_july(self):
        usd, total = fx_exposure(_schedule_rows(), "2026-07")
        assert usd == pytest.approx(214944.43 + 340224.75, abs=0.01)
        assert 0.43 < usd / total < 0.45  # ~43.8%

    def test_fx_projected_flag(self):
        rows = _schedule_rows()
        lg = [r for r in rows if r.loan_id == "LGTO1-L1"]
        past = next(r for r in lg if r.ref_month == "2025-08")
        future = next(r for r in lg if r.ref_month == "2027-01")
        assert not past.fx_projected("2026-07")
        assert future.fx_projected("2026-07")
        gto = next(r for r in rows if r.loan_id == "GTO1-L1"
                   and r.ref_month == "2027-01")
        assert not gto.fx_projected("2026-07")  # MXN never FX-projected


# ---------------------------------------------------------------------------
# Loaders degrade, never fail
# ---------------------------------------------------------------------------

class TestLoaders:
    def test_missing_tabs_degrade_to_empty(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.side_effect = RuntimeError("no tab")
        assert load_loans(sheets) == {}
        assert load_loan_schedule(sheets) == []

    def test_malformed_rows_skipped(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.return_value = [
            {"loan_id": "X-L1", "plant_key": "X", "principal_mxn": "nan?",
             "total_installments": ""},
            {"loan_id": "", "plant_key": "Y"},
        ]
        assert load_loans(sheets) == {}

    def test_load_loans_from_seed(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.return_value = _seed_loans()
        loans = load_loans(sheets)
        assert len(loans) == 9
        assert loans["LGTO1-L1"].currency == "USD"
        assert loans["GTO1-L1"].total_installments == 84

    def test_month_normalization(self):
        import datetime as dt
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.return_value = [{
            "loan_id": "Z-L1", "plant_key": "Z", "ref_month":
            dt.datetime(2026, 7, 1), "installment_no": "1",
            "total_installments": "10", "payment_mxn": "100.5",
            "payment_ccy": "", "xr": "", "due_after_mxn": "900",
        }]
        rows = load_loan_schedule(sheets)
        assert rows[0].ref_month == "2026-07"
        assert rows[0].payment_ccy is None

    def test_current_month(self):
        import datetime as dt
        assert current_month(dt.date(2026, 7, 9)) == "2026-07"
