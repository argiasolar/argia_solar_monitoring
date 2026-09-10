"""v247 — the Savio plugin: invoices vs the AR tracker, payments vs the
booked deposits, receiving accounts vs the registered ones. Runs on the
demo Savio fixtures with a tracker built to disagree in known ways."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "fin" / "savio"
sys.path.insert(0, str(V2 / "scripts"))

from argia.fin import savio_recon as SR   # noqa: E402
from argia.fin.money import D             # noqa: E402


def savio():
    inv = [i for f in ("invoices_p1.json", "invoices_p2.json") for i in json.loads((FIX / f).read_text(encoding="utf-8"))["data"]]
    pay = json.loads((FIX / "payments.json").read_text(encoding="utf-8"))["data"]
    return inv, pay


def tracker_from(inv):
    """The tracker as it SHOULD look, then four deliberate disagreements."""
    rows = []
    for i in inv:
        rows.append({"invoice": i["folio"], "folio_fiscal": i["cfdis"][0]["uuid"], "total": i["total"], "currency": i["currency"],
                     "paid_on": "2026-06-15" if i["status"] == "paid" else "", "status": "Paid" if i["status"] == "paid" else "On time", "company": i["customer_id"]})
    live = [r for r in rows if r["status"] != "Paid"]
    rows.remove(live[0])                                  # 1. an open Savio invoice missing from the tracker → SAVIO_ONLY
    live[1]["total"] = str(D(live[1]["total"]) + 100)      # 2. amount differs → AMOUNT (crit)
    live[1]["folio_fiscal"] = ""                           #    (and only matchable by folio+total → no match → SAVIO_ONLY instead)
    live[2]["paid_on"] = "2026-07-01"                      # 3. tracker says paid, Savio says open → STATE
    rows.append({"invoice": "9999", "folio_fiscal": "", "total": "1000.00", "currency": "MXN", "paid_on": "", "status": "Delay", "company": "OUTSIDE SAVIO"})   # 4. TRACKER_ONLY
    return rows


class TestInvoices:
    def test_matches_by_uuid_then_folio_and_reports_the_four_disagreements(self):
        inv, pay = savio()
        tr = tracker_from(inv)
        rec = SR.reconcile(inv, pay, tr, [], source="mock", now=dt.datetime(2026, 9, 10, 12))
        kinds = sorted((f.kind, f.severity) for f in rec.findings if f.kind in ("SAVIO_ONLY", "TRACKER_ONLY", "AMOUNT", "STATE", "CURRENCY"))
        assert ("TRACKER_ONLY", "info") in kinds and ("STATE", "warn") in kinds
        assert kinds.count(("SAVIO_ONLY", "warn")) == 2          # the removed row and the one whose uuid + total both changed
        assert rec.invoices == 7 and rec.invoices_matched == 7 - 2       # 7 in Savio: the 2 SAVIO_ONLY are unmatched, the rest (incl. the cancelled one) match
        assert ("STATE", "crit") in kinds                                 # the cancelled CFDI is still 'On time' in this tracker
        assert all(f.detail for f in rec.findings)

    def test_amount_and_currency_are_critical(self):
        inv, pay = savio()
        tr = tracker_from(inv)
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        # same uuid, wrong total → AMOUNT; same uuid, wrong currency → CURRENCY
        i0 = next(i for i in inv if i["status"] != "paid" and not any(c["status"] == "cancelled" for c in i["cfdis"]))
        t = [{"invoice": i0["folio"], "folio_fiscal": i0["cfdis"][0]["uuid"], "total": str(D(i0["total"]) - 50), "currency": i0["currency"], "paid_on": "", "status": "On time", "company": "x"}]
        SR.match_invoices([i0], t, rec)
        assert [f.kind for f in rec.findings] == ["AMOUNT"] and rec.findings[0].severity == "crit" and rec.findings[0].amount == D(50)
        rec2 = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        t[0]["total"], t[0]["currency"] = i0["total"], "USD" if i0["currency"] == "MXN" else "MXN"
        SR.match_invoices([i0], t, rec2)
        assert [f.kind for f in rec2.findings] == ["CURRENCY"] and rec2.findings[0].severity == "crit"

    def test_a_cancelled_cfdi_still_open_in_the_tracker_is_critical(self):
        inv, _ = savio()
        c = next(i for i in inv if any(x["status"] == "cancelled" for x in i["cfdis"]))
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        SR.match_invoices([c], [{"invoice": c["folio"], "folio_fiscal": c["cfdis"][0]["uuid"], "total": c["total"], "currency": c["currency"], "paid_on": "", "status": "Delay", "company": "x"}], rec)
        assert [(f.kind, f.severity) for f in rec.findings] == [("STATE", "crit")]


class TestPayments:
    def test_payment_matches_a_deposit_by_amount_date_and_reference(self):
        _, pay = savio()
        p = pay[0]
        deposits = [{"date": "2026-06-14", "amount": p["amount"], "account": "102-01-001", "currency": p["currency"], "reference": "SPEI OTHER", "concept": "COBRO"},
                    {"date": "2026-06-16", "amount": p["amount"], "account": "102-01-007", "currency": p["currency"], "reference": p["reference"], "concept": "COBRO CLIENTE"},
                    {"date": "2026-06-16", "amount": "77.00", "account": "102-01-007", "currency": "MXN", "reference": "", "concept": "INTERESES"}]
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        m = SR.match_payments([p], deposits, rec)
        assert m[p["payment_id"]]["account"] == "102-01-007"          # the reference wins over the closer date
        assert rec.payments_matched == 1 and rec.deposits_matched == 1
        assert [f.kind for f in rec.findings] == ["DEPOSIT_UNMATCHED"] and rec.deposits_internal == 1   # SPEI OTHER listed, INTERESES internal
        assert all(f.severity == "info" for f in rec.findings)

    def test_payment_without_a_deposit_and_currency_must_agree(self):
        _, pay = savio()
        p = dict(pay[0], currency="USD")
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        SR.match_payments([p], [{"date": p["date"], "amount": p["amount"], "account": "102-01-001", "currency": "MXN", "reference": p["reference"], "concept": ""}], rec)
        assert [f.kind for f in rec.findings if f.kind == "NO_DEPOSIT"] and rec.payments_matched == 0

    def test_internal_deposits_stay_out_of_the_review_list(self):
        """Transfers between own accounts (both spellings in the books), refunds,
        interest, loans are not customer money: not listed, counted as internal."""
        for c in ("TRASPASO ENTRE CUENTAS", "TRAPASO ENTRE CUENTAS", "Traspaso de cuentas", "DEVOLUCION DE DEPOSITO", "INTERESES", "PRESTAMO", "CREDITO FINAMO", "COMISIONES BANCARIAS"):
            assert SR.is_internal(c), c
        for c in ("FIBRA PROLOGIS", "PIRELLI NEUMATICOS", "", None):
            assert not SR.is_internal(c), c
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        SR.match_payments([], [{"date": "2026-06-01", "amount": "5", "account": "a", "currency": "MXN", "reference": "", "concept": "TRASPASO ENTRE CUENTAS"},
                               {"date": "2026-06-01", "amount": "5", "account": "a", "currency": "MXN", "reference": "", "concept": "FIBRA PROLOGIS"}], rec)
        assert rec.deposits == 1 and rec.deposits_internal == 1 and [f.kind for f in rec.findings] == ["DEPOSIT_UNMATCHED"]

    def test_window_is_three_days(self):
        _, pay = savio()
        p = pay[0]
        far = {"date": (dt.date.fromisoformat(p["date"]) + dt.timedelta(days=4)).isoformat(), "amount": p["amount"], "account": "a", "currency": "MXN", "reference": "", "concept": ""}
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        assert SR.match_payments([p], [far], rec) == {} and rec.count("NO_DEPOSIT") == 1


class TestAccounts:
    def test_unknown_receiving_account_is_critical_and_known_is_silent(self):
        rec = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        SR.check_accounts([{"payment_id": "p1", "amount": "10", "currency": "MXN", "bank_account": "012180001234567890"},
                           {"payment_id": "p2", "amount": "10", "currency": "MXN", "clabe": "072180009988776677"},
                           {"payment_id": "p3", "amount": "10", "currency": "MXN"}], ["7890", "072180009988776677"], rec)
        assert rec.accounts_checked == 2 and rec.findings == []
        rec2 = SR.Recon(dt.datetime(2026, 9, 10), "mock")
        SR.check_accounts([{"payment_id": "p9", "amount": "500000", "currency": "MXN", "bank_account": "999999999999999999"}], ["7890"], rec2)
        assert [(f.kind, f.severity) for f in rec2.findings] == [("UNKNOWN_ACCOUNT", "crit")]


class TestRowsAndOrder:
    def test_findings_sorted_by_severity_and_rows_carry_a_summary(self):
        inv, pay = savio()
        rec = SR.reconcile(inv, pay, tracker_from(inv), [], ["1234"], source="mock", now=dt.datetime(2026, 9, 10, 12))
        sev = [f.severity for f in rec.findings]
        assert sev == sorted(sev, key={"crit": 0, "warn": 1, "info": 2}.get)
        rows = SR.rows_for(rec, "ARGIA-MX")
        assert rows[0]["kind"] == "SUMMARY" and "invoices" in rows[0]["detail"] and len(rows) == len(rec.findings) + 1
        assert rows[0]["checked_at"].startswith("2026-09-10T12:00")
        assert not rec.ok or not any(f.severity == "crit" for f in rec.findings)


flask = pytest.importorskip("flask")
sys.path.insert(0, str(V2 / "server" / "bundle"))
import fin_app as F   # noqa: E402


class TestPage:
    def test_savio_page_renders_findings_with_the_mock_banner(self):
        rows = [{"checked_at": "2026-09-10 12:00", "source": "mock", "kind": "SUMMARY", "severity": "info", "savio_ref": "", "our_ref": "", "amount": "0", "currency": "", "detail": "invoices 4/7 matched · payments 2/2 with a deposit · deposits 2/3 explained · accounts checked 0"},
                {"checked_at": "2026-09-10 12:00", "source": "mock", "kind": "AMOUNT", "severity": "crit", "savio_ref": "inv_demo_0102", "our_ref": "1001", "amount": "100.00", "currency": "MXN", "detail": "Savio total 951,300.00 vs tracker 951,200.00"},
                {"checked_at": "2026-09-10 12:00", "source": "mock", "kind": "DEPOSIT_UNMATCHED", "severity": "info", "savio_ref": "", "our_ref": "102-01-001 2026-06-16", "amount": "77.00", "currency": "MXN", "detail": "deposit INTERESES has no Savio payment"}]
        F.app.config["ROWS"] = lambda name, sql: rows if name == "savio_check" else ([{"period": "2026-07"}] if name == "period" else [])
        F.app.config["MODE"] = "books"
        F.ALLOWED_USERS.add("tomasz")
        try:
            h = F.app.test_client().get("/finance/savio/", headers={"X-Remote-User": "tomasz"}).data.decode()
        finally:
            F.app.config.pop("ROWS", None)
            F.app.config.pop("MODE", None)
        assert "Mock data." in h and "Savio check" in h and 'class="dtwrap"' in h and '<body class="wide">' in h
        assert "Amount differs" in h and "inv_demo_0102" in h and "1 critical" in h
        import portal_chrome as C
        assert ("savio", "Savio", "Savio") in C.SECTIONS["finance"][2]

    def test_timer_unit_and_runbook_know_the_check(self):
        bundle = V2 / "server" / "bundle"
        assert (bundle / "argia-fin-savio.timer").exists()
        unit = (bundle / "argia-fin-savio.service").read_text(encoding="utf-8")
        assert "fin_savio_recon.py --apply" in unit and "run_job.sh" in unit and "argia-savio-mock.service" in unit
        timer = (bundle / "argia-fin-savio.timer").read_text(encoding="utf-8")
        assert "12:40" in timer and "Persistent=true" in timer       # 20 min after the Drive ingest (12:20)
        ops = (V2 / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        assert "argia-fin-savio" in ops and "fin_savio_recon" in ops and "/root/.argia_savio" in ops
        from argia.fin import schema as S
        assert "savio_check" in S.TABLES and len(S.TABLES) >= 43
