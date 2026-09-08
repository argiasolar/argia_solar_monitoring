"""v244 — the whole finance pipeline against a real PostgreSQL:
schema -> seed -> ingest (twice) -> decisions (twice) -> the app's own
queries. Runs only where a throw-away database is reachable through
the house psql wrapper (``ARGIA_FIN_TEST_DB`` names it); skipped on CI
and on the laptop. The sandbox that ships each version runs it.

    createdb argia_fin_test && ARGIA_FIN_TEST_DB=argia_fin_test pytest tests/unit/test_fin_pipeline_pg.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
DB = os.environ.get("ARGIA_FIN_TEST_DB", "")


def _psql(sql: str) -> list:
    r = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return [ln.split("|") for ln in r.stdout.strip().splitlines() if ln.strip()]


def _reachable() -> bool:
    if not DB:
        return False
    try:
        return _psql("SELECT 1;") == [["1"]]
    except Exception:            # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no throw-away PostgreSQL (set ARGIA_FIN_TEST_DB)")


def run(script: str, *args: str, env=None, ok=(0,)) -> str:
    e = dict(os.environ, ARGIA_PG_DB=DB, PYTHONPATH=str(V2))
    e.update(env or {})
    r = subprocess.run([sys.executable, str(V2 / "scripts" / script), *args], capture_output=True, text=True, timeout=300, env=e, cwd=str(V2))
    assert r.returncode in ok, r.stdout + r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def pipeline():
    _psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-d", DB, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(V2 / "server/bundle/schema.sql")], check=True, capture_output=True)
    assert "28 missing" in run("fin_schema.py", ok=(2,))          # exit 2 = missing tables, by contract
    assert "0 missing" in run("fin_schema.py", "--apply")
    assert "applied: project=6" in run("fin_seed.py", "--apply")
    run("fin_seed.py", "--apply")                                   # idempotent
    first = run("fin_ingest.py", "--all", "--apply")
    second = run("fin_ingest.py", "--all", "--apply")
    run("fin_seed.py", "--decisions")
    run("fin_seed.py", "--decisions")
    return first, second


def counts():
    q = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM {t}" for t in
                           ("supplier_invoice", "customer_invoice", "bank_statement", "bank_transaction", "payment", "allocation", "bank_match", "fin_exception", "project_milestone", "project_task", "purchase_order"))
    return {r[0]: int(r[1]) for r in _psql(q + ";")}


class TestPipeline:
    def test_ingest_is_idempotent(self, pipeline):
        first, second = pipeline
        assert "cfdi: 7 new, 1 duplicate(s) skipped, 2 rejected" in first
        assert "cfdi: 0 new, 8 duplicate(s) skipped, 2 rejected" in second
        assert "bank: 4 statement(s) loaded" in first and "bank: 0 statement(s) loaded, 4 already known" in second
        c = counts()
        assert c == {"supplier_invoice": 7, "customer_invoice": 7, "bank_statement": 4, "bank_transaction": 14, "payment": 6,
                     "allocation": 7, "bank_match": 10, "fin_exception": 4, "project_milestone": 20, "project_task": 40, "purchase_order": 6}

    def test_three_way_match_at_arrival(self, pipeline):
        st = {r[0][:8]: r[1] for r in _psql("SELECT cfdi_uuid, status FROM supplier_invoice;")}
        # four matched invoices were then approved/paid by the demo decisions; the no-PO one stays received
        assert sorted(st.values()) == ["approved", "paid", "paid", "partially_paid", "partially_paid", "received", "received"]
        kinds = sorted(r[0] for r in _psql("SELECT kind FROM fin_exception;"))
        assert kinds == ["CFDI_TOTALS", "FOREIGN_CFDI", "MISSING_PO", "UNKNOWN_PLANT"]

    def test_money_invariants_hold_in_the_database(self, pipeline):
        # no allocation exceeds its invoice
        over = _psql("SELECT i.cfdi_uuid FROM supplier_invoice i WHERE (SELECT coalesce(sum(amount),0) FROM allocation a WHERE a.invoice_ref = i.cfdi_uuid) > i.total;")
        assert over == []
        over = _psql("SELECT i.savio_invoice_id FROM customer_invoice i WHERE (SELECT coalesce(sum(amount),0) FROM allocation a WHERE a.invoice_ref = i.savio_invoice_id) > i.total;")
        assert over == []
        # every statement balances: opening + Σ lines = closing
        bad = _psql("SELECT s.statement_id FROM bank_statement s WHERE abs(s.opening + (SELECT coalesce(sum(amount),0) FROM bank_transaction t WHERE t.statement_id = s.statement_id) - s.closing) > 0.005;")
        assert bad == []
        # a line is matched at most once for its full amount (no double reconciliation)
        dup = _psql("SELECT line_key FROM bank_match WHERE reversed_at IS NULL GROUP BY line_key HAVING abs(sum(amount)) > (SELECT abs(amount) FROM bank_transaction t WHERE t.line_key = bank_match.line_key) + 0.005;")
        assert dup == []
        # own transfers are never income/expense: they carry a 'transfer' match
        assert _psql("SELECT count(*) FROM bank_transaction t WHERE own_transfer AND NOT EXISTS (SELECT 1 FROM bank_match m WHERE m.line_key = t.line_key AND m.target_kind = 'transfer');") == [["0"]]

    def test_the_app_queries_run(self, pipeline):
        sys.path.insert(0, str(V2 / "server" / "bundle"))
        os.environ["ARGIA_PG_DB"] = DB
        import importlib
        F = importlib.import_module("fin_app")
        F.app.config.pop("ROWS", None)
        acc = F.q_accounts()
        assert {a["account_id"] for a in acc} == {"DEMO-BBVA-MXN", "DEMO-BANORTE-USD"}
        bbva = next(a for a in acc if a["account_id"] == "DEMO-BBVA-MXN")
        assert bbva["closing"] == "3468200.00" and bbva["unreconciled"] == "4"
        ar = F.aging_by_ccy(F.q_ar())
        assert str(ar["MXN"]["total"]) == "4824272.03"
        ap_open = [r for r in F.q_ap() if r["status"] not in ("received", "exception")]
        ap = F.aging_by_ccy(ap_open)
        assert str(ap["MXN"]["total"]) == "46400.00" and str(ap["USD"]["total"]) == "121800.00"
        F.ALLOWED_USERS.add("tomasz")
        c = F.app.test_client()
        for path in ("/finance/", "/finance/ar/", "/finance/ap/", "/finance/bank/", "/finance/exceptions/", "/projects/", "/projects/ARG9001/"):
            assert c.get(path, headers={"X-Remote-User": "tomasz"}).status_code == 200, path
        assert c.get("/finance/", headers={"X-Remote-User": "nobody"}).status_code == 403
