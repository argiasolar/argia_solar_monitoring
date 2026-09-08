"""Scenarios 21 (AP), 22/24 (payments), 27 (AR), 28 (incoming
reconciliation), 45 (credit notes), 46 (cancelled CFDI), 62 (duplicates)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from argia.fin import ledger as L
from argia.fin.money import D, same, pct

T = dt.date(2026, 9, 8)


def inv(ref="A", total="116000", issued="2026-07-20", terms=30, status="approved", ccy="MXN", due=None):
    return L.Invoice(ref, D(total), dt.date.fromisoformat(issued), terms, ccy, status, due)


class TestMoney:
    def test_decimal_not_float(self):
        assert D(0.1) + D(0.2) == Decimal("0.30")
        assert D("1.005") == Decimal("1.01")          # half-up, not banker's
        assert same("100.004", "100.00") and not same("100.02", "100.00")
        assert pct("25", "200") == Decimal("12.50") and pct(1, 0) is None
        with pytest.raises(ValueError):
            D("abc")


class TestDueAndAging:
    def test_due_date_follows_terms_unless_explicit(self):
        assert L.due_date(inv(issued="2026-07-20", terms=30)) == dt.date(2026, 8, 19)
        assert L.due_date(inv(due=dt.date(2026, 9, 30))) == dt.date(2026, 9, 30)

    def test_buckets(self):
        assert L.bucket_of(-3) == "current" and L.bucket_of(0) == "current"
        assert L.bucket_of(1) == "0-30" and L.bucket_of(30) == "0-30"
        assert L.bucket_of(31) == "31-60" and L.bucket_of(90) == "61-90" and L.bucket_of(91) == "90+"

    def test_aging_only_counts_open_balances(self):
        a = inv("A", "116000", "2026-07-20")           # due 08-19 -> 20 days -> 0-30
        b = inv("B", "50000", "2026-05-01")            # due 05-31 -> 100 days -> 90+
        c = inv("C", "10000", "2026-09-01")            # due 10-01 -> current
        paid = inv("D", "7000", "2026-06-01")          # fully paid
        cancelled = inv("E", "9000", "2026-06-01", status="cancelled")
        allocs = [L.Allocation("A", D("58000")), L.Allocation("D", D("7000"))]
        ag = L.aging([a, b, c, paid, cancelled], allocs, T)
        assert ag["0-30"] == Decimal("58000.00") and ag["90+"] == Decimal("50000.00")
        assert ag["current"] == Decimal("10000.00") and ag["total"] == Decimal("118000.00")

    def test_aging_is_per_currency(self):
        usd = inv("U", "1000", "2026-05-01", ccy="USD")
        mxn = inv("M", "1000", "2026-05-01")
        assert L.aging([usd, mxn], [], T, currency="MXN")["total"] == Decimal("1000.00")

    def test_overdue_list_oldest_first(self):
        rows = L.overdue([inv("A", "1", "2026-07-20"), inv("B", "1", "2026-05-01"), inv("C", "1", "2026-09-01")], [], T)
        assert [r[0].ref for r in rows] == ["B", "A"]


class TestAllocation:
    def test_partial_then_full(self):
        i = inv()
        a1 = L.allocate(i, [], L.Allocation("A", D("58000"), ref="P1"))
        assert L.outstanding(i, a1) == Decimal("58000.00") and L.status_after(i, a1) == "partially_paid"
        a2 = L.allocate(i, a1, L.Allocation("A", D("58000"), ref="P2"))
        assert L.outstanding(i, a2) == Decimal("0.00") and L.status_after(i, a2) == "paid"

    def test_cannot_pay_twice(self):
        i = inv()
        a = L.allocate(i, [], L.Allocation("A", D("116000")))
        with pytest.raises(L.LedgerError, match="exceeds outstanding"):
            L.allocate(i, a, L.Allocation("A", D("0.01")))

    def test_cannot_exceed_or_be_zero(self):
        i = inv()
        with pytest.raises(L.LedgerError, match="exceeds"):
            L.allocate(i, [], L.Allocation("A", D("116000.01")))
        with pytest.raises(L.LedgerError, match="positive"):
            L.allocate(i, [], L.Allocation("A", D("0")))

    def test_only_approved_invoices_are_payable(self):
        for st in ("exception", "rejected", "cancelled", "draft"):
            with pytest.raises(L.LedgerError, match="not payable"):
                L.allocate(inv(status=st), [], L.Allocation("A", D("1")))

    def test_wrong_invoice(self):
        with pytest.raises(L.LedgerError):
            L.allocate(inv("A"), [], L.Allocation("B", D("1")))

    def test_one_payment_many_invoices_is_many_allocations(self):
        a, b = inv("A", "100"), inv("B", "50")
        allocs = [L.Allocation("A", D("100"), ref="P9"), L.Allocation("B", D("50"), ref="P9")]
        assert L.outstanding(a, allocs) == 0 and L.outstanding(b, allocs) == 0

    def test_credit_note_reduces_balance_and_is_tagged(self):
        i = inv()
        allocs = L.apply_credit_note(i, [], "CN-1", D("16000"))
        assert allocs[-1].kind == "credit_note" and L.outstanding(i, allocs) == Decimal("100000.00")

    def test_cancelled_invoice_owes_nothing(self):
        i = inv(status="cancelled")
        assert L.outstanding(i, []) == 0 and L.status_after(i, []) == "cancelled"

    def test_cancel_refuses_when_money_is_attached(self):
        i = inv()
        allocs = L.allocate(i, [], L.Allocation("A", D("10")))
        with pytest.raises(L.LedgerError, match="reverse or reassign"):
            L.cancel(i, allocs)
        assert L.cancel(i, []).status == "cancelled"


class TestDuplicatePayment:
    def test_same_amount_counterpart_within_24h(self):
        ts = dt.datetime(2026, 9, 8, 10, 0)
        recent = [{"account": "BBVA", "counterpart": "ele010101ab1", "amount": "58000", "ts": ts, "id": 1}]
        cand = {"account": "BBVA", "counterpart": "ELE010101AB1", "amount": "58000.00", "ts": ts + dt.timedelta(hours=5)}
        assert L.duplicate_payment(cand, recent)["id"] == 1
        assert L.duplicate_payment({**cand, "ts": ts + dt.timedelta(hours=30)}, recent) is None
        assert L.duplicate_payment({**cand, "amount": "58000.01"}, recent) is None
        assert L.duplicate_payment({**cand, "account": "BANORTE"}, recent) is None
