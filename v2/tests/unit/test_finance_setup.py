"""/setup/finance — the admin finance editor (2026-09-01).

Three layers:

  * finance_core — pure validation + SQL builders (imported directly,
    no flask needed);
  * setup_app wiring — every finance POST route must pass _fin_guard
    (admin + CSRF) before touching the database (checked at source
    level, the way the credential tests do);
  * pg_loans — the PG loaders that make PostgreSQL the single finance
    authority for the webreport once an admin can edit it.

The rule under test everywhere: paid history is immutable — every bulk
edit carries a ``ref_month >=`` clause and the from-month can never lie
in the past.
"""
import re
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2))

import finance_core as fin                                 # noqa: E402

SETUP_SRC = (V2 / "server" / "bundle" / "setup_app.py").read_text(
    encoding="utf-8")


class TestValidation:
    def test_month_shape(self):
        assert fin.parse_month("2026-09") == "2026-09"
        assert fin.parse_month("2026-13") is None
        assert fin.parse_month("09/2026") is None
        assert fin.parse_month("") is None

    def test_past_months_are_refused(self):
        """The immutability rule: a from-month before the current month
        never validates."""
        assert fin.parse_month("2026-08", min_month="2026-09") is None
        assert fin.parse_month("2026-09", min_month="2026-09") == "2026-09"

    def test_numbers_with_commas_and_bounds(self):
        assert fin.parse_num("124,523.47", 0, 1e6) == 124523.47
        assert fin.parse_num("-5", 0, 1e6) is None
        assert fin.parse_num("abc", 0, 1e6) is None
        assert fin.parse_num("2000000", 0, fin.MAX_OM) is None

    def test_fx_bounds_reject_a_typo(self):
        assert fin.parse_num("179.8", fin.FX_MIN, fin.FX_MAX) is None
        assert fin.parse_num("17.98", fin.FX_MIN, fin.FX_MAX) == 17.98

    def test_sql_quoting(self):
        assert fin.sq("O'Hara") == "'O''Hara'"


class TestMonthsSeq:
    def test_strictly_after_through_inclusive(self):
        assert fin.months_seq("2031-11", "2032-02") == \
            ["2031-12", "2032-01", "2032-02"]

    def test_nothing_to_add(self):
        assert fin.months_seq("2031-11", "2031-11") == []

    def test_garbage_is_empty(self):
        assert fin.months_seq("bad", "2032-01") == []


class TestSqlBuilders:
    def test_payment_edits_only_touch_the_future(self):
        for sql in (fin.sql_set_payment_mxn("GTO1-L1", "2026-10", 120000),
                    fin.sql_set_payment_ccy("LOAX1-L1", "2026-10", 12000),
                    fin.sql_set_fx("LOAX1-L1", "2026-10", 18.5)):
            assert "ref_month >= DATE '2026-10-01'" in sql

    def test_fx_recomputes_mxn_from_the_authoritative_ccy(self):
        sql = fin.sql_set_fx("LOAX1-L1", "2026-10", 18.5)
        assert "payment_mxn = round(payment_ccy * 18.5000, 2)" in sql
        assert "payment_ccy IS NOT NULL" in sql

    def test_usd_payment_edit_keeps_ccy_authoritative(self):
        sql = fin.sql_set_payment_ccy("LGTO1-L1", "2026-10", 11818.06)
        assert "payment_ccy = 11818.06" in sql
        assert "round(11818.06 * xr, 2)" in sql

    def test_truncate_also_refreshes_the_derived_span(self):
        sqls = fin.sql_truncate("SLP2-L1", "2027-01")
        assert any("DELETE FROM loan_schedule" in s for s in sqls)
        assert any("total_installments = s.n" in s for s in sqls)

    def test_extend_numbers_continue_and_usd_carries_fx(self):
        sql = fin.sql_extend("LOAX1-L1", 83, ["2030-07", "2030-08"],
                             0, payment_ccy=15000, xr=18.0)
        flat = sql.replace(" ", "")
        assert "DATE'2030-07-01',83" in flat
        assert "DATE'2030-08-01',84" in flat
        assert "270000.00,15000.00,18.0000" in flat
        assert "ON CONFLICT DO NOTHING" in sql

    def test_extend_mxn_has_no_invented_fx(self):
        sql = fin.sql_extend("GTO1-L1", 85, ["2031-10"], 124523.47)
        assert "NULL,NULL" in sql

    def test_fee_and_tariff_only_touch_future_months(self):
        assert "make_date(year, month, 1) >= DATE '2026-10-01'" in \
            fin.sql_set_fee("LOAX1", "2026-10", 27000)
        assert "make_date(year, month, 1) >= DATE '2026-10-01'" in \
            fin.sql_set_tariff("GTO1", "2026-10", 2.11)

    def test_audit_detail_is_bounded(self):
        sql = fin.sql_audit("tomasz", "GTO1", "GTO1-L1", "fx", "x" * 900)
        assert "x" * 400 in sql and "x" * 401 not in sql


class TestSetupAppWiring:
    ROUTES = ("finance_om", "finance_principal", "finance_payments",
              "finance_fx", "finance_extend", "finance_truncate",
              "finance_fee", "finance_tariff")

    def _body(self, name):
        m = re.search(r"def %s\(\):\n(.*?)(?=\n@app|\ndef )" % name,
                      SETUP_SRC, re.S)
        assert m, f"route {name} missing"
        return m.group(1)

    def test_every_finance_route_passes_the_admin_csrf_guard(self):
        for name in self.ROUTES:
            body = self._body(name)
            assert "_fin_guard()" in body, name
            assert "stale_page()" in body, name

    def test_every_bulk_edit_validates_the_from_month_against_now(self):
        for name in ("finance_payments", "finance_fx", "finance_truncate",
                     "finance_fee", "finance_tariff"):
            assert "min_month=_fin_month_now()" in self._body(name), name

    def test_the_page_itself_is_admin_only(self):
        m = re.search(r"def finance_page\(.*?\n(.*?)\n    psql",
                      SETUP_SRC, re.S)
        assert m and "if not is_global:" in m.group(1)

    def test_every_write_is_audited_and_regenerates_the_report(self):
        m = re.search(r"def _fin_write\(.*?\n(.*?)\ndef ", SETUP_SRC, re.S)
        assert m
        assert "sql_audit" in m.group(1)
        assert "_fin_regen()" in m.group(1)

    def test_finance_drawer_is_admin_gated(self):
        # v200: the Finance drawer (and the whole catalog) is global-admin
        # only; company admins get the old single page over their users.
        body = SETUP_SRC.split("def finance_page(")[1].split("\ndef ")[0]
        assert "if not is_global:" in body and "Admins only." in body
        body = SETUP_SRC.split("def render(")[1].split("\ndef ")[0]
        assert "if is_global:" in body and "drawer == 'finance'" in body


class TestPgLoans:
    def _patch(self, monkeypatch, rows):
        import argia.store.pgq as pgq
        monkeypatch.setattr(pgq, "psql_rows", lambda sql: rows)

    def test_loans_load_and_shape(self, monkeypatch):
        self._patch(monkeypatch, [
            ["GTO1-L1", "GTO1", "TAIGENE", "BanBajio", "MXN",
             "10459971.00", "84", "2024-10", "2031-09"],
            ["BAD"],
        ])
        from argia.finance.pg_loans import load_loans_pg
        loans = load_loans_pg()
        assert set(loans) == {"GTO1-L1"}
        l = loans["GTO1-L1"]
        assert l.currency == "MXN" and l.total_installments == 84
        assert l.first_month == "2024-10"

    def test_schedule_usd_and_mxn_rows(self, monkeypatch):
        self._patch(monkeypatch, [
            ["LOAX1-L1", "LOAX1", "2026-10", "38", "82",
             "297391.96", "16540.15", "17.98", "0"],
            ["GTO1-L1", "GTO1", "2026-10", "25", "84",
             "124523.47", "", "", "0"],
        ])
        from argia.finance.pg_loans import load_loan_schedule_pg
        rows = load_loan_schedule_pg()
        assert len(rows) == 2
        usd = next(r for r in rows if r.loan_id == "LOAX1-L1")
        mxn = next(r for r in rows if r.loan_id == "GTO1-L1")
        assert usd.is_usd and usd.xr == pytest.approx(17.98)
        assert not mxn.is_usd and mxn.payment_ccy is None

    def test_webreport_prefers_pg_when_the_mirror_is_enabled(self):
        src = (V2 / "argia" / "finance" / "webreport.py").read_text(
            encoding="utf-8")
        i = src.index("pg_mirror.enabled()")
        assert "load_loans_pg" in src[i:i + 400]
        assert "load_loans(sheets)" in src[i:]     # off-server fallback
