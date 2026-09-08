"""v244 — the demo world, the Savio client and the ingest transforms
(scenarios 16, 17, 26, 62, 66, 67)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
from decimal import Decimal

import pytest

from argia.fin import bank_csv, cash, cfdi as CF, ingest as I, savio as S
from argia.fin.money import D

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "fin"
sys.path.insert(0, str(V2 / "scripts"))


class TestFixturesAreTheGenerator:
    def test_committed_files_match_the_generator(self):
        import fin_fixtures as G
        assert G.check(G.build()) == []

    def test_world_is_coherent(self):
        w = json.loads((FIX / "world.json").read_text(encoding="utf-8"))
        codes = {c["code"] for c in w["cost_codes"]}
        rfcs = {s["rfc"] for s in w["suppliers"]}
        pids = {p["project_id"] for p in w["projects"]}
        for po in w["purchase_orders"]:
            assert po["cost_code"] in codes and po["supplier_rfc"] in rfcs and po["project_id"] in pids
        for pid, versions in w["budgets"].items():
            for v in versions:
                assert set(v["lines"]) <= codes, (pid, v["version"])
        assert len(w["entities"]) == 2 and len(w["accounts"]) == 3 and len(w["projects"]) == 6

    def test_every_cfdi_parses_and_says_what_it_is_for(self):
        w = json.loads((FIX / "world.json").read_text(encoding="utf-8"))
        seen = {}
        for f in sorted((FIX / "cfdi").glob("*.xml")):
            c = CF.parse(f.read_text(encoding="utf-8"))
            verdict, reasons = I.classify_cfdi(c, w["our_rfcs"], seen)
            seen.setdefault(c.uuid, f.name)
            if f.name.startswith("08_"):
                assert verdict == "skip"
            elif f.name.startswith("09_"):
                assert verdict == "reject" and reasons[0].startswith("TOTALS:")
            elif f.name.startswith("10_"):
                assert verdict == "reject" and "FOREIGN_CFDI" in reasons[0]
            else:
                assert verdict == "ingest", (f.name, reasons)

    def test_statements_balance_and_chain(self):
        closing = json.loads((FIX / "bank" / "closing_balances.json").read_text(encoding="utf-8"))
        for acc in ("DEMO-BBVA-MXN", "DEMO-BANORTE-USD", "DEMO-CZ-EUR"):
            prev = None
            for f in sorted((FIX / "bank" / acc).glob("*.csv")):
                st = bank_csv.parse("argia_generic", f.read_text(encoding="utf-8"), acc)
                assert cash.check_statement(st) == []
                assert f"{st.closing:.2f}" == closing[acc][f.stem]
                if prev is not None:
                    assert st.opening == prev
                prev = st.closing

    def test_snapshot_and_drive_tree_cover_every_project(self):
        w = json.loads((FIX / "world.json").read_text(encoding="utf-8"))
        snap = json.loads((FIX / "pmo" / "portfolio_snapshot.json").read_text(encoding="utf-8"))
        tree = json.loads((FIX / "drive" / "project_tree.json").read_text(encoding="utf-8"))
        pids = {p["project_id"] for p in w["projects"]}
        assert {p["project_id"] for p in snap["projects"]} == pids
        assert {p["project_id"] for p in tree["projects"]} == pids
        assert all(len(p["subfolders"]) == 7 for p in tree["projects"])


class TestSavioClient:
    def test_cursor_pagination_follows_next_cursor(self):
        t = S.FakeTransport(FIX / "savio")
        rows = list(S.SavioClient(t).invoices())
        assert len(rows) == 7 and [c[2].get("cursor") for c in t.calls] == [None, "cur_demo_page2"]
        assert all(c[2]["include"] == "cfdis,items" for c in t.calls)
        assert [r["invoice_id"] for r in rows] == sorted((r["invoice_id"] for r in rows), key=lambda x: x)  # fixture sorted by updated_at then id

    def test_resume_from_a_cursor(self):
        t = S.FakeTransport(FIX / "savio")
        rows = list(S.SavioClient(t).invoices(cursor="cur_demo_page2"))
        assert len(rows) == 3

    def test_bad_cursor_is_an_error_not_a_restart(self):
        with pytest.raises(S.SavioError, match="unknown cursor"):
            list(S.SavioClient(S.FakeTransport(FIX / "savio")).invoices(cursor="nope"))

    def test_http_transport_retries_429_then_gives_up(self, monkeypatch):
        import io
        import urllib.error
        calls = {"n": 0}
        waits = []

        def fake_open(req, timeout=0):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {"Retry-After": "2"}, io.BytesIO(b"{}"))
        monkeypatch.setattr(S.urllib.request, "urlopen", fake_open)
        t = S.HttpTransport("http://127.0.0.1:1/api/v1", key="k", sleep=waits.append)
        with pytest.raises(S.SavioError, match="gave up"):
            t.request("GET", "invoice")
        assert calls["n"] == S.MAX_RETRY_429 + 1 and waits == [2.0] * S.MAX_RETRY_429

    def test_key_is_read_from_a_file_never_env(self, tmp_path):
        p = tmp_path / "k"
        p.write_text("sk_demo_123\n", encoding="utf-8")
        assert S.read_key(str(p)) == "sk_demo_123"
        assert S.read_key(str(tmp_path / "missing")) == ""

    def test_client_from_env_defaults_to_fake(self, monkeypatch):
        monkeypatch.delenv("ARGIA_SAVIO_BASE", raising=False)
        assert isinstance(S.client_from_env().t, S.FakeTransport)
        monkeypatch.setenv("ARGIA_SAVIO_BASE", "http://127.0.0.1:8530/api/v1")
        assert isinstance(S.client_from_env().t, S.HttpTransport)


class TestTransforms:
    def test_savio_invoice_to_row(self):
        inv = json.loads((FIX / "savio" / "invoices_p1.json").read_text(encoding="utf-8"))["data"][0]
        row = I.customer_invoice_row(inv, "DEMO-MX", {"cus_demo_002": 7, "cus_demo_001": 8, "cus_demo_003": 9, "cus_demo_004": 10})
        assert row["savio_invoice_id"] == inv["invoice_id"] and row["cfdi_uuid"] == inv["cfdis"][0]["uuid"].upper()
        assert row["total"] == D(inv["total"]) and row["customer_id"] in (7, 8, 9, 10)
        assert row["project_id"] == (inv["custom_fields"]["argia_project_id"] or None)

    def test_cancelled_cfdi_forces_cancelled_status(self):
        pages = [json.loads((FIX / "savio" / f).read_text(encoding="utf-8"))["data"] for f in ("invoices_p1.json", "invoices_p2.json")]
        canc = next(i for p in pages for i in p if i["invoice_id"] == "inv_demo_0106")
        assert I.customer_invoice_row(canc, "DEMO-MX", {})["status"] == "cancelled"

    def test_payment_rows(self):
        pay = json.loads((FIX / "savio" / "payments.json").read_text(encoding="utf-8"))["data"][0]
        p, a = I.customer_payment_rows(pay, "DEMO-MX", "DEMO-BBVA-MXN")
        assert p["direction"] == "in" and p["savio_payment_id"] == pay["payment_id"] and a["invoice_side"] == "ar"
        assert a["amount"] == p["amount"] == D(pay["amount"])

    def test_supplier_invoice_row_and_hint(self):
        c = CF.parse((FIX / "cfdi" / "01_electro_A1021_inverters_mxn.xml").read_text(encoding="utf-8"))
        hint = I.po_hint_from_concepto("3.2 · ARG9001 · PO-2026-0007", ["ARG9001", "ARG9002"], ["PO-2026-0007"])
        assert hint == {"project_id": "ARG9001", "po_number": "PO-2026-0007"}
        row = I.supplier_invoice_row(c, "DEMO-MX", {"ELE010101AB1": 3}, hint={"project_id": "ARG9001", "po_id": 11, "cost_code": "3.2", "terms_days": 30})
        assert row["cfdi_uuid"] == c.uuid and row["supplier_id"] == 3 and row["po_id"] == 11
        assert row["issue_date"] == dt.date(2026, 8, 14) and row["due_date"] == dt.date(2026, 9, 13)
        assert row["total"] == Decimal("116000.00") and row["status"] == "received" and row["fx_rate"] is None

    def test_usd_invoice_keeps_its_fx(self):
        c = CF.parse((FIX / "cfdi" / "03_modulos_M310_modules_usd.xml").read_text(encoding="utf-8"))
        row = I.supplier_invoice_row(c, "DEMO-MX", {})
        assert row["currency"] == "USD" and row["fx_rate"] == Decimal("18.42")

    def test_statement_rows_and_own_transfer(self):
        txt = (FIX / "bank" / "DEMO-BBVA-MXN" / "2026-07.csv").read_text(encoding="utf-8")
        st = bank_csv.parse("argia_generic", txt, "DEMO-BBVA-MXN")
        head, lines, problems = I.statement_rows(st, ["012180001122334455", "072180009988776677"], hashlib.sha256(txt.encode()).hexdigest())
        assert problems == [] and head["closing"] == Decimal("1643350.00") and len(lines) == 5
        own = [l for l in lines if l["own_transfer"]]
        assert len(own) == 1 and own[0]["description"].startswith("TRASPASO")
        bad = cash.Statement(st.account, st.period_start, st.period_end, st.opening, D("1"), st.lines)
        assert I.statement_rows(bad, [], "x")[2]

    def test_snapshot_rows_keep_money_out(self):
        snap = json.loads((FIX / "pmo" / "portfolio_snapshot.json").read_text(encoding="utf-8"))
        projects, ms, tasks = I.snapshot_rows(snap, {"ARG9006": "DEMO-CZ"}, "2026-09-08T06:00:00-06:00")
        assert len(projects) == 6 and len(ms) == 20 and len(tasks) == 40
        assert "contract_value" not in projects[0] and "status" not in projects[0]
        assert "billed_invoice" not in ms[0]
        assert "baseline_date" not in I.SNAPSHOT_MILESTONE_UPDATE and "amount" not in I.SNAPSHOT_MILESTONE_UPDATE


class TestUpsertSql:
    def test_natural_key_conflict_updates_only_source_columns(self):
        sql = I.upsert("customer_invoice", [{"savio_invoice_id": "inv_1", "entity_id": "DEMO-MX", "total": D("10"), "project_id": None}],
                       key=("savio_invoice_id",), never_update=("entity_id",))
        assert sql.startswith("INSERT INTO customer_invoice (savio_invoice_id, entity_id, total, project_id) VALUES ('inv_1', 'DEMO-MX', 10.00, NULL)")
        assert "ON CONFLICT (savio_invoice_id) DO UPDATE SET total = EXCLUDED.total, project_id = EXCLUDED.project_id;" in sql
        assert "entity_id = EXCLUDED" not in sql

    def test_do_nothing_when_nothing_to_update(self):
        sql = I.upsert("savio_cursor", [{"resource": "invoice"}], key=("resource",))
        assert sql.endswith("DO NOTHING;")

    def test_literals(self):
        assert I._lit("O'Neil") == "'O''Neil'" and I._lit(True) == "true" and I._lit(None) == "NULL"
        assert I._lit(dt.date(2026, 9, 8)) == "'2026-09-08'" and I._lit(["M1", "M2"]) == "ARRAY['M1', 'M2']::text[]"
        assert I._lit([]) == "ARRAY[]::text[]" and I._lit({"a": 1}) == "'{\"a\": 1}'::jsonb"
        assert I._lit(Decimal("12.50")) == "12.50"


class TestSavioMock:
    def test_mock_serves_the_fixtures_with_the_api_shapes(self, monkeypatch):
        pytest.importorskip("flask")          # the laptop venv has no Flask; CI and the server do
        monkeypatch.setenv("ARGIA_SAVIO_FIXTURES", str(FIX / "savio"))
        sys.path.insert(0, str(V2 / "server" / "bundle"))
        import importlib
        m = importlib.import_module("savio_mock")
        importlib.reload(m)
        c = m.app.test_client()
        p1 = c.get("/api/v1/invoice?limit=100&include=cfdis,items").get_json()
        assert p1["nextCursor"] == "cur_demo_page2" and len(p1["data"]) == 4
        p2 = c.get("/api/v1/invoice?cursor=cur_demo_page2").get_json()
        assert p2["nextCursor"] is None and len(p2["data"]) == 3
        assert c.get("/api/v1/invoice?cursor=zzz").status_code == 400
        assert c.get("/api/v1/customer").get_json()["data"][0]["customer_id"] == "cus_demo_001"
        assert c.get("/health").get_json()["ok"] is True
        c.get("/api/v1/_ratelimit/2")
        r = c.get("/api/v1/payment")
        assert r.status_code == 429 and r.headers["Retry-After"] == "1"
        c.get("/api/v1/payment")
        assert c.get("/api/v1/payment").status_code == 200

    def test_mock_binds_loopback_only(self):
        src = (V2 / "server" / "bundle" / "savio_mock.py").read_text(encoding="utf-8")
        assert 'app.run(host="127.0.0.1"' in src
