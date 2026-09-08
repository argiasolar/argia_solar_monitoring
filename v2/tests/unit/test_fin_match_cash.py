"""Scenarios 15 (receipt), 18/19 (PO match, three-way), 29/30 (bank
reconciliation, cash balance), 67 (idempotent statement import)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from argia.fin import bank_csv, cash, match as M
from argia.fin.money import D

PO = M.PO("PO-2026-0007", "ELE010101AB1", "MXN", D("116000"))


def si(total="116000", rfc="ELE010101AB1", ccy="MXN", po="PO-2026-0007"):
    return M.SupplierInvoice("U1", rfc, ccy, D(total), po)


class TestThreeWay:
    def test_exact_match(self):
        r = M.three_way(si(), PO, M.Receipt(PO.number, D("116000")))
        assert r.verdict == "matched" and r.codes == []

    def test_within_tolerance(self):
        r = M.three_way(si("116400"), PO, M.Receipt(PO.number, D("116000")))   # +400 < 500 abs
        assert r.verdict == "matched"
        r = M.three_way(si("117100"), PO, M.Receipt(PO.number, D("116000")))   # +1100 < 1 % = 1160
        assert r.verdict == "matched"

    def test_over_po_and_over_received(self):
        r = M.three_way(si("120000"), PO, M.Receipt(PO.number, D("116000")))
        assert r.verdict == "exception" and set(r.codes) == {"OVER_PO", "OVER_RECEIVED"}

    def test_partial_receipt_limits_the_invoice(self):
        r = M.three_way(si("58000"), PO, M.Receipt(PO.number, D("58000")))
        assert r.verdict == "matched"
        r = M.three_way(si("70000"), PO, M.Receipt(PO.number, D("58000")))
        assert r.codes == ["OVER_RECEIVED"]

    def test_second_invoice_on_a_po_counts_what_was_billed(self):
        po2 = M.PO(PO.number, PO.supplier_rfc, "MXN", D("116000"), invoiced_so_far=D("58000"))
        r = M.three_way(si("58000"), po2, M.Receipt(PO.number, D("116000")))
        assert r.verdict == "matched"
        r = M.three_way(si("60000"), po2, M.Receipt(PO.number, D("116000")))
        assert "OVER_PO" in r.codes                       # 60000 > 58000 remaining beyond tolerance

    def test_wrong_supplier_currency_and_unapproved(self):
        r = M.three_way(si(rfc="XXX010101AAA", ccy="USD"), M.PO(PO.number, PO.supplier_rfc, "MXN", PO.total, status="draft"),
                        M.Receipt(PO.number, D("116000")))
        assert set(r.codes) == {"WRONG_SUPPLIER", "WRONG_CURRENCY", "PO_NOT_APPROVED"}

    def test_missing_po_and_not_received(self):
        assert M.three_way(si(po=None), None, None).verdict == "no_po"
        assert M.three_way(si(), PO, None).codes == ["NOT_RECEIVED"]

    def test_receipt_guard(self):
        assert M.receive(D("100"), D("40"), D("60")) == Decimal("100.00")
        with pytest.raises(ValueError, match="over-receipt"):
            M.receive(D("100"), D("40"), D("61"))
        with pytest.raises(ValueError, match="positive"):
            M.receive(D("100"), D("40"), D("0"))


def line(date, amount, desc="x", cp="", bid="", account="BBVA-MXN"):
    return cash.BankLine(account, dt.date.fromisoformat(date), D(amount), desc, cp, bid)


class TestStatement:
    def test_balances_must_close(self):
        st = cash.Statement("BBVA-MXN", dt.date(2026, 8, 1), dt.date(2026, 8, 31), D("1000"), D("1500"),
                            (line("2026-08-03", "800", "cliente"), line("2026-08-10", "-300", "proveedor")))
        assert cash.check_statement(st) == []
        bad = cash.Statement("BBVA-MXN", st.period_start, st.period_end, D("1000"), D("1400"), st.lines)
        assert "!= closing 1400.00" in cash.check_statement(bad)[0]

    def test_lines_outside_period_or_account_or_duplicated(self):
        st = cash.Statement("BBVA-MXN", dt.date(2026, 8, 1), dt.date(2026, 8, 31), D("0"), D("20"),
                            (line("2026-09-01", "10"), line("2026-08-02", "10", account="OTHER")))
        errs = cash.check_statement(st)
        assert any("outside" in e for e in errs) and any("belongs to account" in e for e in errs)
        st2 = cash.Statement("BBVA-MXN", dt.date(2026, 8, 1), dt.date(2026, 8, 31), D("0"), D("20"),
                             (line("2026-08-02", "10", "same"), line("2026-08-02", "10", "same")))
        assert any("duplicate lines" in e for e in cash.check_statement(st2))

    def test_natural_key_prefers_bank_id_and_dedupes(self):
        a = line("2026-08-03", "800", "SPEI recibido", "012345678901234567", "TX9")
        b = line("2026-08-03", "800", "SPEI RECIBIDO ", "012345678901234567", "TX9")
        assert a.key == b.key == "BBVA-MXN:TX9"
        c = line("2026-08-03", "800", "spei recibido", "012345678901234567")
        d = line("2026-08-03", "800", "SPEI recibido", "012345678901234567")
        assert c.key == d.key and c.key != a.key
        st = cash.Statement("BBVA-MXN", dt.date(2026, 8, 1), dt.date(2026, 8, 31), D("0"), D("1600"), (a, c))
        assert [l.key for l in cash.new_lines(st, [a.key])] == [c.key]
        assert cash.new_lines(st, [a.key, c.key]) == []

    def test_own_transfer(self):
        l = line("2026-08-03", "-5000", "traspaso", cp="012 180 0011 2233 4455 66")
        assert cash.is_own_transfer(l, ["012180001122334455 66"]) is True
        assert cash.is_own_transfer(l, ["999999999999999999"]) is False

    def test_company_cash_needs_fx(self):
        assert cash.company_cash({"BBVA|MXN": D("1000"), "CHASE|USD": D("100")}, {"USD/MXN": D("18.5")}) == Decimal("2850.00")
        with pytest.raises(cash.CashError, match="no FX rate"):
            cash.company_cash({"CHASE|USD": D("100")}, {})


class TestReconcile:
    def test_once_unless_split(self):
        l = line("2026-08-03", "800")
        m1 = cash.reconcile(l, [], [cash.Match(l.key, "customer_payment", "SAVIO-P1", D("800"))])
        assert cash.reconciled_state(l, m1) == "reconciled"
        with pytest.raises(cash.CashError, match="exceed the line"):
            cash.reconcile(l, m1, [cash.Match(l.key, "customer_payment", "SAVIO-P2", D("1"))])

    def test_split_must_sum_exactly(self):
        l = line("2026-08-03", "800")
        parts = [cash.Match(l.key, "customer_payment", "I1", D("500")), cash.Match(l.key, "customer_payment", "I2", D("300"))]
        assert cash.reconciled_state(l, cash.reconcile(l, [], parts)) == "reconciled"
        assert cash.reconciled_state(l, cash.reconcile(l, [], parts[:1])) == "partial"

    def test_sign_and_zero(self):
        l = line("2026-08-03", "-300")
        with pytest.raises(cash.CashError, match="sign"):
            cash.reconcile(l, [], [cash.Match(l.key, "payment", "P1", D("300"))])
        with pytest.raises(cash.CashError, match="zero"):
            cash.reconcile(l, [], [cash.Match(l.key, "payment", "P1", D("0"))])

    def test_unreconcile_returns_the_removed_match(self):
        l = line("2026-08-03", "800")
        m = cash.reconcile(l, [], [cash.Match(l.key, "customer_payment", "P1", D("800"))])
        keep, gone = cash.unreconcile(l, m, "P1")
        assert keep == [] and gone.target_ref == "P1"
        assert cash.reconciled_state(l, keep) == "open"


GENERIC = """opening,1000.00
closing,1500.00
period,2026-08-01,2026-08-31
date,description,debit,credit,counterpart,bank_id
2026-08-03,SPEI RECIBIDO TAIGENE,,800.00,012180001122334455,TX1
2026-08-10,PAGO ELECTRO,300.00,,,TX2
"""


class TestBankCsv:
    def test_generic_format_round_trip(self):
        st = bank_csv.parse("argia_generic", GENERIC, "BBVA-MXN")
        assert cash.check_statement(st) == []
        assert [l.amount for l in st.lines] == [Decimal("800.00"), Decimal("-300.00")]
        assert st.lines[0].key == "BBVA-MXN:TX1" and st.lines[0].counterpart == "012180001122334455"

    def test_unknown_format_refused(self):
        with pytest.raises(bank_csv.BankFormatError, match="unknown bank format"):
            bank_csv.parse("banorte_xlsx", GENERIC, "X")

    def test_bad_rows_refused(self):
        with pytest.raises(bank_csv.BankFormatError, match="both debit and credit"):
            bank_csv.parse("argia_generic", GENERIC.replace("2026-08-10,PAGO ELECTRO,300.00,,", "2026-08-10,PAGO ELECTRO,300.00,5.00,"), "X")
        with pytest.raises(bank_csv.BankFormatError, match="missing opening"):
            bank_csv.parse("argia_generic", GENERIC.replace("opening,1000.00\n", ""), "X")

    def test_dmy_dates_and_signed_columns_refused(self):
        txt = GENERIC.replace("2026-08-10,PAGO ELECTRO,300.00,,", "10/08/2026,PAGO ELECTRO,300.00,,")
        st = bank_csv.parse("argia_generic", txt, "BBVA-MXN")
        assert st.lines[1].date == dt.date(2026, 8, 10)
        # (300.00) in the debit column is a bank that prints debits in
        # parentheses — it must get its own registered format, never a guess
        with pytest.raises(bank_csv.BankFormatError, match="unsigned"):
            bank_csv.parse("argia_generic", GENERIC.replace("300.00,,", "(300.00),,"), "X")
