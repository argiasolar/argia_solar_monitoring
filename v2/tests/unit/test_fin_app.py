"""v244 — the /finance/ and /projects/ app against the demo world
(fake row source keyed by query name; the SQL text is still built)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys

import pytest

pytest.importorskip("flask")          # the laptop venv has no Flask; CI and the server do

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "fin"
sys.path.insert(0, str(V2 / "server" / "bundle"))
import fin_app as F   # noqa: E402

_I18N = re.compile(r'<span data-en="[^"]*" data-es="[^"]*">([^<]*)</span>')


def en(h):
    return _I18N.sub(r"\1", h)


def es(h):
    return re.sub(r'<span data-en="[^"]*" data-es="([^"]*)">[^<]*</span>', r"\1", h)


def demo_rows():
    """The demo world as the queries would return it (strings, like psql --csv)."""
    w = json.loads((FIX / "world.json").read_text(encoding="utf-8"))
    snap = json.loads((FIX / "pmo" / "portfolio_snapshot.json").read_text(encoding="utf-8"))
    cust = {c["rfc"]: c["name"] for c in w["customers"]}
    projects = [{"project_id": p["project_id"], "name": p["name"], "site": p["site"], "project_type": p["project_type"], "status": p["status"],
                 "pm_user": p["pm_user"], "contract_value": str(p["contract_value"]), "contract_ccy": p["contract_ccy"], "kwp_dc": str(p["kwp_dc"] or ""),
                 "customer": cust.get(p["customer_rfc"] or "", ""), "plant_key": "", "pmo_sheet_id": f"1DEMO_{p['project_id']}"} for p in w["projects"] if p["entity_id"] == "DEMO-MX"]
    milestones = [{"project_id": p["project_id"], "ref": m["ref"], "name": m["name"], "kind": m["kind"], "baseline_date": m["baseline"], "planned_date": m["planned"],
                   "actual_date": m["actual"] or "", "billable": "t" if m["billable"] else "f", "amount": str(m["amount"]), "billed_invoice": "", "depends_on": ""}
                  for p in snap["projects"] for m in p["milestones"]]
    budget = [{"project_id": pid, "version": str(v["version"]), "status": v["status"], "cost_code": c, "amount": str(a)}
              for pid, vs in w["budgets"].items() for v in vs for c, a in v["lines"].items()]
    cos = [{"project_id": c["project_id"], "ref": c["ref"], "status": c["status"], "revenue_impact": str(c["revenue_impact"]), "cost_impact": str(c["cost_impact"])} for c in w["change_orders"]]
    sup = {s["rfc"]: s["name"] for s in w["suppliers"]}
    pos = [{"po_id": str(i + 1), "po_number": po["po_number"], "project_id": po["project_id"], "status": po["status"], "currency": po["currency"], "total": po["total"],
            "expected_delivery": po["expected_delivery"], "supplier": sup[po["supplier_rfc"]], "cost_code": po["cost_code"],
            "invoiced": {"PO-2026-0007": "116000.00", "PO-2026-0009": "556800.00", "PO-2026-0011": "243600.00", "PO-2026-0012": "63200.00"}.get(po["po_number"], "0"),
            "invoiced_net": {"PO-2026-0007": "100000.00", "PO-2026-0009": "480000.00", "PO-2026-0011": "210000.00", "PO-2026-0012": "60000.00"}.get(po["po_number"], "0"),
            "subtotal": po["subtotal"], "received": po["received"]} for i, po in enumerate(w["purchase_orders"])]
    u = w["uuids"]
    ap = [{"ref": u["ELE-A-1021"], "who": sup["ELE010101AB1"], "project_id": "ARG9001", "cost_code": "3.2", "po_number": "PO-2026-0007", "issue_date": "2026-08-14", "due_date": "2026-09-13",
           "currency": "MXN", "total": "116000.00", "subtotal": "100000.00", "tipo": "I", "status": "partially_paid", "related_uuid": "", "applied": "58000.00"},
          {"ref": u["EST-B-77"], "who": sup["EST020202CD2"], "project_id": "ARG9001", "cost_code": "3.3", "po_number": "PO-2026-0009", "issue_date": "2026-08-20", "due_date": "2026-10-04",
           "currency": "MXN", "total": "556800.00", "subtotal": "480000.00", "tipo": "I", "status": "paid", "related_uuid": "", "applied": "556800.00"},
          {"ref": u["MOD-M-310"], "who": sup["MOD030303EF3"], "project_id": "ARG9003", "cost_code": "3.1", "po_number": "PO-2026-0011", "issue_date": "2026-08-05", "due_date": "2026-10-04",
           "currency": "USD", "total": "243600.00", "subtotal": "210000.00", "tipo": "I", "status": "approved", "related_uuid": "", "applied": "0"},
          {"ref": u["ING-C-5"], "who": sup["ING040404GH4"], "project_id": "ARG9002", "cost_code": "2.1", "po_number": "PO-2026-0012", "issue_date": "2026-08-25", "due_date": "2026-09-09",
           "currency": "MXN", "total": "63200.00", "subtotal": "60000.00", "tipo": "I", "status": "approved", "related_uuid": "", "applied": "0"},
          {"ref": u["ELE-NC-12"], "who": sup["ELE010101AB1"], "project_id": "ARG9001", "cost_code": "3.2", "po_number": "PO-2026-0007", "issue_date": "2026-08-28", "due_date": "2026-09-27",
           "currency": "MXN", "total": "11600.00", "subtotal": "10000.00", "tipo": "E", "status": "approved", "related_uuid": u["ELE-A-1021"], "applied": "0"},
          {"ref": u["EST-B-90"], "who": sup["EST020202CD2"], "project_id": "", "cost_code": "", "po_number": "", "issue_date": "2026-09-03", "due_date": "2026-10-18",
           "currency": "MXN", "total": "29000.00", "subtotal": "25000.00", "tipo": "I", "status": "received", "related_uuid": "", "applied": "0"}]
    pages = [json.loads((FIX / "savio" / f).read_text(encoding="utf-8"))["data"] for f in ("invoices_p1.json", "invoices_p2.json")]
    cname = {c["savio_customer_id"]: c["name"] for c in w["customers"]}
    ar = [{"ref": i["invoice_id"], "cfdi_uuid": i["cfdis"][0]["uuid"], "who": cname[i["customer_id"]], "project_id": i["custom_fields"]["argia_project_id"],
           "plant_key": i["custom_fields"]["argia_plant_key"], "issue_date": i["issue_date"], "due_date": i["due_date"], "currency": i["currency"], "total": i["total"],
           "status": "cancelled" if i["cfdis"][0]["status"] == "cancelled" else i["status"], "applied": i["paid_amount"]} for p in pages for i in p]
    closing = json.loads((FIX / "bank" / "closing_balances.json").read_text(encoding="utf-8"))
    accounts = [{"account_id": a["account_id"], "bank": a["bank"], "currency": a["currency"], "purpose": a["purpose"],
                 "closing": closing[a["account_id"]]["2026-08"], "as_of": "2026-08-31", "unreconciled": {"DEMO-BBVA-MXN": "6", "DEMO-BANORTE-USD": "2"}.get(a["account_id"], "0")}
                for a in w["accounts"] if a["entity_id"] == "DEMO-MX"]
    exceptions = [{"exception_id": "1", "kind": "MISSING_PO", "ref": u["EST-B-90"], "project_id": "", "owner": "", "status": "open", "detail": "07_estructuras_B90_no_po.xml: no purchase order", "opened": "2026-09-08"},
                  {"exception_id": "2", "kind": "CFDI_TOTALS", "ref": u["TAM-X-1"], "project_id": "", "owner": "", "status": "open", "detail": "09_tampered_total.xml: TOTALS", "opened": "2026-09-08"},
                  {"exception_id": "3", "kind": "FOREIGN_CFDI", "ref": u["FOR-Z-9"], "project_id": "", "owner": "", "status": "open", "detail": "10_foreign_not_ours.xml", "opened": "2026-09-08"}]
    tasks = {p["project_id"]: [{"task_id": t["task_id"], "name": t["name"], "phase": t["phase"], "start_date": t["start"], "end_date": t["end"], "progress_pct": str(t["progress_pct"]), "resource": t["resource"]}
                               for t in p["tasks"]] for p in snap["projects"]}
    bank_lines = [{"line_key": "DEMO-BBVA-MXN:DEMO-TX-0828A", "account_id": "DEMO-BBVA-MXN", "tx_date": "2026-08-28", "amount": "2737600.00", "description": "SPEI RECIBIDO LOGISTICA", "counterpart": "LOG070707MN7", "own_transfer": "f", "matched": "customer_payment:pay_demo_9002"},
                  {"line_key": "DEMO-BBVA-MXN:DEMO-TX-0722A", "account_id": "DEMO-BBVA-MXN", "tx_date": "2026-07-22", "amount": "-184200.00", "description": "TRASPASO A BANORTE USD", "counterpart": "072180009988776677", "own_transfer": "t", "matched": ""},
                  {"line_key": "DEMO-BBVA-MXN:DEMO-TX-0831A", "account_id": "DEMO-BBVA-MXN", "tx_date": "2026-08-31", "amount": "12500.00", "description": "DEPOSITO NO IDENTIFICADO", "counterpart": "", "own_transfer": "f", "matched": ""}]
    return {"accounts": accounts, "ar": ar, "ap": ap, "exceptions": exceptions, "projects": projects, "milestones": milestones, "budget_lines": budget,
            "change_orders": cos, "pos": pos, "tasks": tasks, "bank_lines": bank_lines}


class FakeRows:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, name, sql):
        self.calls.append((name, sql))
        assert sql.strip().upper().startswith("SELECT"), name
        if name == "tasks":
            pid = re.search(r"project_id = '([^']+)'", sql).group(1)
            return self.data["tasks"].get(pid, [])
        return self.data[name]


@pytest.fixture
def client(monkeypatch):
    fake = FakeRows(demo_rows())
    F.app.config["ROWS"] = fake
    F.app.config["TODAY"] = lambda: dt.date(2026, 9, 8)
    monkeypatch.setattr(F, "ALLOWED_USERS", {"tomasz"})
    monkeypatch.setattr(F, "ALLOWED_EMAILS", {"tomasz.zemelka@argia.com.mx"})
    monkeypatch.setattr(F, "ALLOW_FILE", "/nonexistent/fin_allow.txt")
    monkeypatch.setattr(F, "email_of", lambda u: {"tz": "tomasz.zemelka@argia.com.mx", "juan": "juan@argia.com.mx"}.get(u, ""))
    c = F.app.test_client()
    c.fake = fake
    yield c
    F.app.config.pop("ROWS", None)
    F.app.config.pop("TODAY", None)


def get(c, path, user="tomasz"):
    return c.get(path, headers={"X-Remote-User": user} if user else {})


class TestSecret:
    """Only Tomasz sees it — by username, by e-mail, never anyone else."""

    @pytest.mark.parametrize("path", ["/finance/", "/finance/ar/", "/finance/ap/", "/finance/bank/", "/finance/exceptions/", "/projects/", "/projects/ARG9001/"])
    def test_other_users_get_403(self, client, path):
        for u in ("juan", "arturo", "", "cust"):
            r = get(client, path, u)
            assert r.status_code == 403, (path, u)
            assert "No access" in r.get_data(as_text=True) and "DEMO" not in r.get_data(as_text=True)

    def test_tomasz_by_username_and_by_email(self, client):
        assert get(client, "/finance/", "tomasz").status_code == 200
        assert get(client, "/finance/", "tz").status_code == 200          # e-mail lookup
        assert get(client, "/finance/", "TOMASZ").status_code == 200

    def test_me_endpoint_drives_the_hidden_card(self, client):
        assert get(client, "/finance/me", "tomasz").get_json() == {"user": "tomasz", "allowed": True}
        assert get(client, "/finance/me", "juan").get_json()["allowed"] is False
        assert get(client, "/finance/me", "").get_json()["allowed"] is False

    def test_allow_file_adds_people_live(self, client, tmp_path, monkeypatch):
        f = tmp_path / "fin_allow.txt"
        f.write_text("# accountants\narturo.gonzalez@argia.solar\nvit\n", encoding="utf-8")
        monkeypatch.setattr(F, "ALLOW_FILE", str(f))
        monkeypatch.setattr(F, "email_of", lambda u: {"arturo": "arturo.gonzalez@argia.solar"}.get(u, ""))
        assert get(client, "/finance/me", "arturo").get_json()["allowed"] is True
        assert get(client, "/finance/me", "vit").get_json()["allowed"] is True
        assert get(client, "/finance/me", "juan").get_json()["allowed"] is False

    def test_landing_card_hidden_by_default_and_prefixes_gated(self):
        pg = (V2 / "server/bundle/portal_gen.py").read_text(encoding="utf-8")
        ch = (V2 / "server/bundle/portal_chrome.py").read_text(encoding="utf-8")
        assert "class=\"card dest{' finonly' if key in ('finance', 'projects') else ''}\"" in pg
        assert ".adminonly,.askonly,.finonly{display:none}" in ch and "fetch('/finance/me'" in ch
        ac = (V2 / "server/bundle/auth_core.py").read_text(encoding="utf-8")
        assert "'/finance/': ALL" in ac and "'/projects/': ALL" in ac
        ng = (V2 / "server/bundle/nginx-argia_session.conf").read_text(encoding="utf-8")
        assert ng.count("proxy_pass http://127.0.0.1:8515;") == 2
        unit = (V2 / "server/bundle/argia-fin.service").read_text(encoding="utf-8")
        assert "ARGIA_FIN_EMAILS=tomasz.zemelka@argia.com.mx" in unit and "ARGIA_FIN_PORT=8515" in unit and "fin_app.py" in unit


class TestToday:
    def test_cash_ar_ap_and_exceptions(self, client):
        h = en(get(client, "/finance/").get_data(as_text=True))
        assert "3,468,200" in h and "28,165" in h                          # BBVA MXN, Banorte USD closings
        assert "6 unreconciled line(s)" in h
        # AR: open 0102 (5,475,200 − 2,737,600 = 2,737,600, current), 0103 (951,200; due 08-01 -> 38 d -> 31-60),
        # 0104 (649,600 current), 0105 (311,872.03 current), 0107 (174,000 due 05-20 -> 111 d -> 90+); 0101 paid, 0106 cancelled
        assert "Receivables · MXN" in h and "4,824,272" in h and "90+ 174,000" in h and "31-60 951,200" in h
        # AP: approved/paid/partially paid only: 58,000 open on A-1021 (due 09-13 current) + 63,200 (due 09-09 current) MXN; USD 243,600 current
        assert "Payables · MXN" in h and "121,200" in h and "Payables · USD" in h and "243,600" in h
        assert "Receivables · USD" not in h                                   # only the cancelled USD invoice: no tile
        assert "Supplier invoices awaiting approval" in h and "29,000" in h
        assert "Exceptions" in h and "CFDI_TOTALS 1" in h and "FOREIGN_CFDI 1" in h and "MISSING_PO 1" in h

    def test_project_cost_table_is_the_cost_model(self, client):
        h = en(get(client, "/finance/").get_data(as_text=True))
        # ARG9001: contract 7,240,000 (+0 approved CO revenue), actual net = 100,000 + 480,000 − 10,000 = 570,000;
        # committed = PO-0009 600,000 − 480,000 invoiced = 120,000 (PO-0007 fully invoiced)
        assert "Hotel SLP 417 kWp + BESS" in h and "7,240,000" in h and "570,000" in h and "120,000" in h
        assert "Projects — cost and margin" in h and "How the numbers are calculated" in h
        assert "DEMO-MX" in h and "demo data" in h

    def test_bilingual(self, client):
        h = get(client, "/finance/").get_data(as_text=True)
        assert "Dónde está el dinero" in es(h) and "Por cobrar" in es(h) and "Excepciones" in es(h)


class TestLedgers:
    def test_ar_rows_and_buckets(self, client):
        h = en(get(client, "/finance/ar/").get_data(as_text=True))
        assert "inv_demo_0107" in h and "90+" in h and "cancelled" in h and "paid" in h
        assert "HOTELES DEMO SLP" in h and "Outstanding" in h

    def test_ap_rows_show_type_and_po(self, client):
        h = en(get(client, "/finance/ap/").get_data(as_text=True))
        assert "PO-2026-0007" in h and "partially_paid" in h and ">E<" in h and "received" in h
        assert "Payables" in h and "credit note (E) reduces its original" in h

    def test_bank_page(self, client):
        h = en(get(client, "/finance/bank/").get_data(as_text=True))
        assert "customer_payment:pay_demo_9002" in h and "reconciled" in h and "own transfer" in h and "DEPOSITO NO IDENTIFICADO" in h

    def test_exceptions_page(self, client):
        h = en(get(client, "/finance/exceptions/").get_data(as_text=True))
        assert "Exceptions · 3" in h and "MISSING_PO" in h and "07_estructuras_B90_no_po.xml" in h


class TestProjects:
    def test_portfolio_cards(self, client):
        h = en(get(client, "/projects/").get_data(as_text=True))
        for pid in ("ARG9001", "ARG9002", "ARG9003", "ARG9004", "ARG9005"):
            assert f'href="/projects/{pid}/"' in h
        assert "Active projects" in h and ">3<" in h                       # 9001, 9002, 9003
        assert "health" in h and "next" in h

    def test_project_page(self, client):
        h = en(get(client, "/projects/ARG9001/").get_data(as_text=True))
        assert "Hotel SLP 417 kWp + BESS" in h and "Milestones" in h and "Mechanical complete" in h and "+21d" in h
        assert "UNBILLED" in h                                             # M2 completed, billable, not billed in the fake
        assert "Change orders" in h and "CO-1" in h and "pending" in h and "unapproved exposure" in h
        assert "PO-2026-0007" in h and "PO-2026-0009" in h and "Purchase orders" in h
        assert "Budget vs actual by cost code" in h and "3.2" in h
        assert "Tasks (PMO snapshot)" in h and "prepare" in h
        assert "Health" in h and "AGS-903" in h

    def test_unknown_project_404_and_input_bounded(self, client):
        assert get(client, "/projects/NOPE/").status_code == 404
        r = get(client, "/projects/" + "A" * 200 + "/")
        assert r.status_code == 404

    def test_queries_are_selects_with_entity_filter(self, client):
        get(client, "/finance/")
        get(client, "/projects/ARG9001/")
        names = {n for n, _ in client.fake.calls}
        assert {"accounts", "ar", "ap", "exceptions", "projects", "milestones", "budget_lines", "change_orders", "pos", "tasks"} <= names
        for n, sql in client.fake.calls:
            if n in ("accounts", "ar", "ap", "projects", "pos"):
                assert "entity_id = 'DEMO-MX'" in sql, n
