"""SLP1 debt switch — Oliva Hermanos replaces the test loan (2026-09-01).

The embedded schedule is data transcribed from a bank report; these
tests are the transcription's safety net: bank checksums, contiguous
numbering, one row per month, a balance that amortizes to exactly zero,
and SQL that deletes only the artificial loan. Plus the report_gen
position rule that makes a mid-life takeover read 16/84 instead of
3/84.
"""
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import switch_slp1_oliva as S                              # noqa: E402


class TestEmbeddedSchedule:
    def test_bank_checksums_hold(self):
        assert S.integrity_ok()

    def test_71_rows_numbered_14_to_84(self):
        assert [r[0] for r in S.SCHEDULE] == list(range(14, 85))

    def test_open_debt_matches_the_bank_report_total(self):
        open_sum = sum(r[2] for r in S.SCHEDULE if r[0] >= 17)
        assert open_sum == pytest.approx(5_906_286.64, abs=0.005)

    def test_balance_amortizes_to_exactly_zero(self):
        assert S.SCHEDULE[-1][3] == 0.00

    def test_outstanding_after_16_matches_the_summary(self):
        by_no = {r[0]: r[3] for r in S.SCHEDULE}
        assert by_no[16] == pytest.approx(4_365_432.11, abs=0.005)

    def test_due_after_decreases_by_the_flat_principal(self):
        """5,200,000 amortizes at 64,197.53/month from installment 4 —
        each stored row's balance must drop by exactly that (final row
        clears the rounding tail)."""
        for prev, cur in zip(S.SCHEDULE, S.SCHEDULE[1:]):
            drop = round(prev[3] - cur[3], 2)
            want = 64197.60 if cur[0] == 84 else 64197.53
            assert drop == pytest.approx(want, abs=0.005), cur[0]

    def test_first_carried_month_is_when_quimica_took_over(self):
        assert S.SCHEDULE[0][1] == "2026-06"     # SLP1-L1 ended 2026-05

    def test_september_payment_is_the_banks(self):
        by_no = {r[0]: r[2] for r in S.SCHEDULE}
        assert by_no[17] == 109665.62


class TestSql:
    def test_delete_targets_only_the_test_loan(self):
        for sql in S.delete_sqls():
            assert "'SLP1-L2'" in sql
            assert "SLP1-L1" not in sql and "SLP1-L3" not in sql

    def test_loan_row_carries_the_bank_facts(self):
        sql = S.insert_loan_sql()
        assert "5200000.00" in sql and ",84," in sql
        assert "'BanBajio'" in sql and "'MXN'" in sql
        assert "DATE '2026-06-01'" in sql and "DATE '2032-04-01'" in sql
        assert "ON CONFLICT (loan_id) DO NOTHING" in sql

    def test_schedule_insert_is_conflict_safe_and_mxn_only(self):
        sql = S.insert_schedule_sql()
        assert sql.count("(") >= 71
        assert "payment_ccy" not in sql        # MXN loan: no invented FX
        assert "ON CONFLICT DO NOTHING" in sql
        assert "DATE'2026-06-01',14,121614.68" in sql.replace(" ", "")
        assert "DATE'2032-04-01',84,64887.69" in sql.replace(" ", "")


class TestLoanPositionRule:
    def test_report_gen_uses_bank_installment_numbers(self):
        src = (V2 / "server" / "bundle" / "report_gen.py").read_text(
            encoding="utf-8")
        assert "coalesce(max(installment_no),0)" in src
        assert "SELECT 1 FROM loan_schedule" not in src
