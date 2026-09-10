"""v245 — the books: CONTPAQi prints, the accountants' workbook, the
projects overview, the AR/AP tracker, one PMO sheet → readers, row
builders, the page derivations and the pages themselves (fake rows).
Everything runs on the synthetic fixtures in tests/fixtures/fin/books
(scripts/fin_books_fixtures.py) — never on the real books."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
from decimal import Decimal

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "fin" / "books"
sys.path.insert(0, str(V2 / "scripts"))

from argia.fin import acctbook as AB, books as B, contpaq as CP, pmo_sheet as PS, portfolio as PF   # noqa: E402
from argia.fin.money import D                                                                        # noqa: E402


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pol():
    return CP.parse_polizas(load("polizas_rows.json"))


@pytest.fixture(scope="module")
def aux():
    return CP.parse_auxiliares(load("auxiliares_rows.json"))


@pytest.fixture(scope="module")
def book():
    return AB.read_workbook(load("acctbook_sheets.json"))


# ------------------------------------------------------------------ contpaq
class TestPolizas:
    def test_header_and_journals(self, pol):
        assert pol.entity == "ARGIA DEMO" and pol.rfc == "DEM150101AB1"
        assert (pol.period_from, pol.period_to, pol.printed) == (dt.date(2026, 1, 1), dt.date(2026, 2, 28), dt.date(2026, 3, 21))
        assert len(pol.journals) == 8 and pol.lines == 20
        j = pol.journals[0]
        assert j.key == "2026-01-15:Diario:1" and j.concept.startswith("FACTURA 2001") and j.control == "20402008.0"
        assert [l.account for l in j.lines] == ["105-01-001", "401-03-000", "209-01-000"]
        assert j.lines[0].segment == 9001 and j.lines[0].debit == D("1160000") and j.lines[1].credit == D("1000000")

    def test_every_journal_balances_and_keys_are_unique(self, pol):
        assert all(j.balanced for j in pol.journals)
        assert len({j.key for j in pol.journals}) == len(pol.journals)

    def test_dates_in_spanish_and_english(self):
        assert CP.parse_date("13/Ene/2026") == dt.date(2026, 1, 13)
        assert CP.parse_date("05/Aug/2026") == dt.date(2026, 8, 5)
        assert CP.parse_date("hello") is None and CP.parse_date(None) is None
        assert CP.parse_date(dt.datetime(2026, 2, 3, 10)) == dt.date(2026, 2, 3)

    def test_segment_is_the_business_case(self, pol):
        segs = {l.segment for j in pol.journals for l in j.lines}
        assert segs == {701, 9001, 9002}


class TestAuxiliares:
    def test_accounts_openings_and_natures(self, aux):
        assert aux.period_from == dt.date(2026, 1, 1) and aux.period_to == dt.date(2026, 2, 28)
        bank = aux.accounts["102-01-001"]
        assert bank.name == "Banorte MXN 9001" and bank.opening == D("250000") and bank.nature == "debit"
        assert bank.closing_shown == D("250000") + D("580000") - D("348000") - D("1450.50")
        sup = aux.accounts["201-01-001"]
        assert sup.nature == "credit" and sup.closing_shown == D("696000") - D("348000")     # shown positive when in credit
        assert sup.closing == -sup.closing_shown                                             # normalised debit-positive

    def test_running_balance_agrees_in_both_natures(self, aux):
        assert all(a.running_ok for a in aux.accounts.values())

    def test_an_account_without_movements_takes_the_class_nature(self, aux):
        delta = aux.accounts["201-01-002"]        # only an opening balance in the print
        assert not delta.movements and delta.nature == "credit" and delta.closing_shown == D("50000")
        assert CP.class_nature("401-03-000") == "credit" and CP.class_nature("601-49-001") == "debit"

    def test_bank_prefix_and_closing_at(self, aux):
        banks = aux.by_prefix(CP.BANK_PREFIX)
        assert [a.account for a in banks] == ["102-01-001", "102-01-002", "102-01-003"]
        assert aux.accounts["102-01-001"].closing_at(dt.date(2026, 1, 31)) == D("830000")


class TestCrossCheck:
    def test_the_unposted_poliza_is_a_finding_not_a_crash(self, pol, aux):
        f = CP.cross_check(pol, aux)
        assert len(f) == 2 and all(("201-01-002" in x or "102-01-001" in x) for x in f)
        assert B.posted_keys(aux) == {j.key for j in pol.journals} - {"2026-02-25:Egresos:99"}

    def test_balanza_agrees_with_auxiliares(self, aux, book):
        bal = {b.dashed: b for b in CP.parse_balanza(load("acctbook_sheets.json")["Balanza"])}
        assert bal["102-01-001"].level == 4 and bal["102-01-001"].closing == aux.accounts["102-01-001"].closing
        assert bal["201-01-001"].closing == aux.accounts["201-01-001"].closing      # both normalised
        assert CP.BalanceRow("10000000", "Activo", D(0), D(0), D(0), D(0), D(0), D(0)).level == 0
        assert CP.BalanceRow("10201000", "x", D(0), D(0), D(0), D(0), D(0), D(0)).level == 3


# ----------------------------------------------------------------- acctbook
class TestWorkbook:
    def test_cover_mapping_projects(self, book):
        assert (book.period.year, book.period.month, book.period.label) == (2026, 2, "2026-02")
        assert book.mapping["102-01-001"].report_code == "BS_100" and book.mapping["401-03-000"].a_p == "R"
        assert book.projects[9001].is_business_case and not book.projects[701].is_business_case
        assert book.projects[9003].project_type == "LAAS"

    def test_pl_lines_months_and_ytd(self, book):
        rev = book.line("PL_040")
        assert rev.label == "Revenues" and rev.year == 2026 and rev.months[:3] == [D(1000), D(368), D(0)]
        assert rev.ytd_through(2) == D(1368) and rev.ytd == D(1368)
        gm = [l for l in book.pl if l.code == "label:Gross Margin"][0]
        assert gm.is_subtotal and gm.ytd_through(2) == D(768)

    def test_budget_keeps_the_real_year_despite_stale_headers(self, book):
        b = book.line("PL_040", "budget")
        assert b.year == 2026 and b.ytd_through(2) == D(1800)

    def test_bs_and_gm_and_loans(self, book):
        cash = book.line("BS_100", "bs")
        assert cash.months[1] == D("942.55")
        m = book.gm[9001]
        assert (m.revenue_ytd, m.cos_ytd, m.gm, m.planned_value, m.planned_cost) == (D(1000000), D(-600000), D(400000), D(1200000), D(-840000))
        assert m.variance_gm == D(400000) - D(360000)
        assert book.loans[0].bank.strip() == "BancoDemo" and book.loans[0].currency.strip() == "USD"


# ---------------------------------------------------------------- portfolio
class TestOverviewAndTracker:
    def test_overview_rows(self):
        rows = PF.read_overview(load("overview_rows.json"))
        assert [r.code for r in rows] == [9001, 9002, 9003, 8001]
        r = rows[0]
        assert r.project_id == "ARG9001" and r.phase == "3_execution" and r.active and r.status == "ok"
        assert (r.value_mxn, r.planned_cost_mxn, r.progress, r.invoiced_mxn, r.paid_mxn) == (D(1200000), D(840000), D("0.7"), D(1000000), D(580000))
        assert r.contract_start == dt.date(2026, 1, 10) and r.project_manager == "Eduardo Fraga"
        assert not rows[3].active and rows[2].status == "warning" and rows[2].comment == "pending signature"

    def test_tracker_items(self):
        items = PF.read_tracker(load("tracker_rows.json"))
        assert [(i.side, i.invoice) for i in items] == [("ar", "2001"), ("ar", "2003"), ("ar", "2002"), ("ap", "A-77"), ("ap", "Permanent"), ("ap", "Permanent"), ("ap", "B-12")]
        a = items[0]
        assert a.project_code == 9001 and a.currency == "MXN" and a.total == D(580000) and a.days_to_due == -15 and not a.is_paid
        assert a.folio_fiscal == "11111111-2222-3333-4444-555555555555"
        usd = items[1]
        assert usd.currency == "USD" and usd.total == D(12000) and usd.net_usd == D("10344.83") and usd.open_amount == D(12000)
        assert items[2].is_paid and items[2].paid_on == dt.date(2026, 2, 20) and items[2].open_amount == 0


# ---------------------------------------------------------------- pmo sheet
class TestPmoSheet:
    def test_project_summary_from_vertical_pairs_only(self):
        p = PS.read_project(load("pmo_tabs.json"))
        assert p.project_id == "ARG9001" and p.code == 9001 and p.customer == "Cliente Alfa" and p.location == "León"
        # the dropdown lists beside the block ('Ok', '0 - Closing', 'Lighting') are never read as values
        assert (p.status, p.phase, p.contract_type, p.manager, p.supervisor) == ("In Progress", "3 - Execution", "Solar", "Eduardo Fraga", "Emilio Cardiel (ARGIA)")
        assert p.start == dt.date(2026, 1, 10) and p.end == dt.date(2026, 5, 30) and p.value == D(1200000) and p.cost == D(840000)

    def test_tasks_costs_invoices_logs(self):
        p = PS.read_project(load("pmo_tabs.json"))
        assert len(p.tasks) == 9 and [m.task_id for m in p.milestones] == ["M001", "M002", "M003"]
        t2 = [t for t in p.tasks if t.task_id == "T002"][0]
        assert t2.progress == D("0.5") and t2.duration_days == 14 and t2.priority == "High" and t2.phase_no == "2"
        assert p.cost_by_status() == {"Paid": D(600000), "Committed": D(150000), "Planned": D(90000)}
        assert p.costs[0].date == dt.date(2026, 1, 20) and p.costs[2].date == dt.date(2026, 1, 3)   # "15/02/26" is D/M/YY text, "1/3/2026" is the sheet's M/D/YYYY
        assert p.invoices[0].received == D(580000) and p.invoices[0].paid_on == dt.date(2026, 1, 28)
        assert p.logs[0].hours == D(8) and p.logs[0].progress == D("0.5")
        assert p.progress == (D(1) + D(1) + D("0.5") + D(0) + D(0) + D(0)) / 6

    def test_no_summary_means_none(self):
        assert PS.read_project({"x": [["a", "b"]]}) is None


# ------------------------------------------------------------- row builders
class TestBooksRows:
    def test_journal_rows_flag_unposted_and_skip_empty(self, pol, aux):
        jr, lr = B.journal_rows("ARGIA-MX", pol, "sha1", B.posted_keys(aux))
        assert len(jr) == 8 and len(lr) == 20
        assert [r["jkey"] for r in jr if not r["posted"]] == ["2026-02-25:Egresos:99"]
        sql = B.journal_sql("ARGIA-MX", jr, lr, pol.period_from, pol.period_to)
        assert sql.startswith("DELETE FROM gl_journal WHERE entity_id = 'ARGIA-MX' AND jdate BETWEEN '2026-01-01' AND '2026-02-28' AND jkey NOT IN (")
        assert sql.count("ON CONFLICT (entity_id, jkey) DO UPDATE") == 8 and sql.count("ON CONFLICT (entity_id, jkey, line_no)") == 20

    def test_balance_and_account_rows(self, aux, book):
        rows = B.balance_rows("ARGIA-MX", aux, "sha", "2026-02")
        bank = [r for r in rows if r["account"] == "102-01-001"][0]
        assert bank["closing"] == D("480549.50") and bank["nature"] == "debit" and bank["movements"] == 3
        accts = B.gl_account_rows("ARGIA-MX", book.mapping, aux)
        assert {a["account"]: a["nature"] for a in accts}["201-01-001"] == "credit"
        assert "ON CONFLICT (entity_id, period, account) DO UPDATE" in B.balance_sql(rows)

    def test_report_margin_portfolio_rows(self, book):
        rr = B.report_rows("ARGIA-MX", "2026-02", "pl", book.pl, "sha")
        rev = [r for r in rr if r["code"] == "PL_040"][0]
        assert rev["m01"] == D(1000) and rev["m12"] == D(0) and rev["ytd"] == D(1368)
        mr = B.margin_rows("ARGIA-MX", "2026-02", book.gm, "sha")
        assert len(mr) == 3 and mr[0]["code"] == 9001
        pr = B.portfolio_rows("ARGIA-MX", PF.read_overview(load("overview_rows.json")), "sha")
        assert pr[0]["project_id"] == "ARG9001" and "DELETE FROM portfolio_project" in B.portfolio_sql("ARGIA-MX", pr)

    def test_open_items_identical_rows_get_distinct_keys(self):
        items = PF.read_tracker(load("tracker_rows.json"))
        rows = B.open_item_rows("ARGIA-MX", items, "sha")
        keys = [r["item_key"] for r in rows]
        assert len(keys) == len(set(keys)) == 7
        assert B.open_item_sql("ARGIA-MX", rows).startswith("DELETE FROM open_item WHERE entity_id = 'ARGIA-MX';")

    def test_pmo_rows(self):
        p = PS.read_project(load("pmo_tabs.json"))
        proj, tasks, costs, invs = B.pmo_rows("ARGIA-MX", p, "sha", "1abc", dt.datetime(2026, 3, 1, 12))
        assert proj["project_id"] == "ARG9001" and proj["code"] == 9001 and len(tasks) == 9 and len(costs) == 3 and len(invs) == 1
        sql = B.pmo_sql(proj, tasks, costs, invs)
        assert "DELETE FROM pmo_task WHERE project_id = 'ARG9001';" in sql and sql.index("INSERT INTO pmo_project") < sql.index("DELETE FROM pmo_task")

    def test_period_helpers(self):
        assert B.month_prefix_period("0726 Polizas Argia.xlsx") == "2026-07"
        assert B.acctbook_period("Argia_Accounting_Data_07_26_V1.xlsx") == "2026-07"
        assert B.period_of(dt.date(2026, 7, 31)) == "2026-07" and B.period_of(None) == ""
        assert B.ENTITY_MX["rfc"] == "AME1407113A7" and "ON CONFLICT (entity_id)" in B.entity_sql()


# ------------------------------------------------------------ ingest script
class TestIngestScript:
    def test_discovery_prefers_the_newest_period_and_skips_old_folders(self, tmp_path):
        import fin_drive_ingest as I
        root = tmp_path
        for rel in ("ACCOUNTING/Accounting Reporting/2026/6.- June/0626 Polizas Argia.xlsx", "ACCOUNTING/Accounting Reporting/2026/7.- July/0726 Polizas Argia.xlsx",
                    "ACCOUNTING/Accounting Reporting/2026/7.- July/OLD/0726 Polizas Argia.xlsx", "ACCOUNTING/Accounting Reporting/2026/7.- July/Argia_Accounting_Data_07_26_V1.xlsx",
                    "ACCOUNTING/Accounting Reporting/2026/6.- June/Argia_Accounting_Data_06_26_V2.xlsm", "ACCOUNTING/Accounting Reporting/2026/7.- July/V1/Argia_Accounting_Data_07_26_V3.xlsx"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rel.encode())
        src = I.LocalSource(root)
        assert src.latest(I.POLIZAS_RE).name == "0726 Polizas Argia.xlsx" and "OLD" not in src.latest(I.POLIZAS_RE).folder
        assert src.latest(I.BOOK_RE).name == "Argia_Accounting_Data_07_26_V1.xlsx"      # V3 sits in a version folder → ignored
        assert I._period_key("Argia_Accounting_Data_07_26_V2.xlsx") == "2026-07-02" and I._period_key("0626 Auxiliares Argia.xlsx") == "2026-06"
        assert I.PROJECT_FOLDER_RE.match("Project ARG1473 - Quijote Capex 417.3 kWp").group(1) == "1473"
        assert I.PMO_TITLE_RE.search("RYDER NVO LAREDO ARGIA PROJECT") and I.PMO_TITLE_RE.search("ARGIA_PROJECT_QUIJOTE")

    def test_dummy_pld_projects_are_below_the_code_floor(self):
        import fin_drive_ingest as I
        assert I.PMO_MIN_CODE == 1000 and all(c < I.PMO_MIN_CODE for c in range(0, 13))

    def test_sha_of_a_sheet_is_content_not_metadata(self):
        import fin_drive_ingest as I
        a = I.SourceFile("pmo_sheet", "A", tabs={"T": [["1", "2"]]}, drive_id="x", modified=dt.datetime(2026, 1, 1))
        b = I.SourceFile("pmo_sheet", "B", tabs={"T": [["1", "2"]]}, drive_id="y", modified=dt.datetime(2026, 2, 2))
        assert a.sha == b.sha


# ------------------------------------------------------------------- pages
flask = pytest.importorskip("flask")
sys.path.insert(0, str(V2 / "server" / "bundle"))
import fin_app as F      # noqa: E402
import fin_books as FB   # noqa: E402

_I18N = re.compile(r'<span data-en="[^"]*" data-es="[^"]*">([^<]*)</span>')
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def en(h):
    return _I18N.sub(r"\1", h)


def book_rows():
    """What the books queries return for the synthetic world (strings, like psql --csv)."""
    aux = CP.parse_auxiliares(load("auxiliares_rows.json"))
    wb = AB.read_workbook(load("acctbook_sheets.json"))
    bal = {r["account"]: r for r in B.balance_rows("X", aux, "s", "2026-02")}
    items = B.open_item_rows("X", PF.read_tracker(load("tracker_rows.json"), "Payables and Receivables."), "s")
    ov = B.portfolio_rows("X", PF.read_overview(load("overview_rows.json")), "s")
    gm = {r["code"]: r for r in B.margin_rows("X", "2026-02", wb.gm, "s")}
    pmo = PS.read_project(load("pmo_tabs.json"))
    proj, tasks, costs, invs = B.pmo_rows("X", pmo, "s")

    def s(v):
        return "" if v is None else ("t" if v is True else "f" if v is False else str(v))

    SRC_TRACKER = {"src_name": "Tracker.xlsx", "src_kind": "tracker", "src_drive_id": "TRK", "src_mime": XLSX_MIME}
    SRC_OVERVIEW = {"src_name": "Overview.xlsx", "src_kind": "overview", "src_drive_id": "OVW", "src_mime": XLSX_MIME}
    SRC_PMO = {"src_name": "ARGIA PROJECT ARG9001", "src_kind": "pmo_sheet", "src_drive_id": "PMO", "src_mime": SHEET_MIME}

    def portfolio():
        out = []
        for r in ov:
            m = gm.get(r["code"], {})
            row = {k: s(v) for k, v in r.items()}
            row.update({k: s(m.get(k)) for k in ("revenue_ytd", "cos_ytd", "revenue_total", "cos_total", "gm", "planned_value", "planned_cost", "planned_margin")})
            row.update(SRC_OVERVIEW)
            if r["code"] == 9001:
                row.update({"pmo_id": "ARG9001", "pmo_phase": proj["phase"], "pmo_status": proj["status"], "pmo_progress": s(proj["progress"]), "pmo_manager": proj["manager"]})
            else:
                row.update({"pmo_id": "", "pmo_phase": "", "pmo_status": "", "pmo_progress": "", "pmo_manager": ""})
            out.append(row)
        return out

    def rows(name, sql):
        if name == "period":
            return [{"period": "2026-02"}]
        if name == "report_periods":
            return [{"period": "2026-02"}, {"period": "2025-12"}]
        if name == "cash":
            return [{k: s(v) for k, v in r.items()} for a, r in sorted(bal.items()) if a.startswith("102-01-")]
        if name == "book_ar":
            return [{k: s(v) for k, v in r.items()} for a, r in sorted(bal.items()) if a.startswith("105-01-") and r["closing"]]
        if name == "book_ap":
            return [{k: s(v) for k, v in r.items()} for a, r in sorted(bal.items()) if a.startswith("201-01-") and r["closing"]]
        if name in ("open_ar", "open_ap"):
            return [dict({k: s(v) for k, v in r.items()}, **SRC_TRACKER) for r in items if r["side"] == name[5:]]
        if name.startswith("report_"):
            lines = {"pl": wb.pl, "bs": wb.bs, "budget": wb.budget}[name[7:]]
            return [{k: s(v) for k, v in r.items()} for r in B.report_rows("X", "2026-02", name[7:], lines, "s")]
        if name == "portfolio":
            return portfolio()
        if name == "project_gl":
            return [{"account": "401-03-000", "bucket": "Revenues", "bs_pl": "PL", "a_p": "R", "debit": "0", "credit": "1000000.00", "n": "1", "first": "2026-01-15", "last": "2026-01-15"},
                    {"account": "501-11-000", "bucket": "Cost of Sales", "bs_pl": "PL", "a_p": "C", "debit": "600000.00", "credit": "0", "n": "1", "first": "2026-01-20", "last": "2026-01-20"}]
        if name == "project_items":
            return [dict({k: s(v) for k, v in r.items()}, **SRC_TRACKER) for r in items if r["project_code"] == 9001]
        if name == "pmo_tasks":
            return [dict({k: s(v) for k, v in r.items()}, **SRC_PMO) for r in tasks]
        if name == "pmo_costs":
            return [dict({k: s(v) for k, v in r.items()}, src_gid="55", **SRC_PMO) for r in costs]
        if name == "pmo_invoices":
            return [dict({k: s(v) for k, v in r.items()}, **SRC_PMO) for r in invs]
        if name == "bank_lines":
            return [{"jdate": "2026-02-03", "kind": "Egresos", "number": "1", "concept": "PAGO PROVEEDOR GAMMA", "reference": "SPEI 5120", "debit": "0", "credit": "348000.00", "segment": "9001"}]
        if name == "sources":
            return [{"kind": "polizas", "name": "0226 Polizas Argia.xlsx", "period": "2026-02", "modified": "2026-03-21", "imported": "2026-03-22 07:00", "rows": "20", "notes": ""}]
        # v248 — cost centres and suppliers
        if name == "cost_centers":
            return [{"code": "9001", "name": "SOLAR CAPEX ROOF 300 kWp DEMO UNO", "kind": "project", "grp": "", "manual": "f"},
                    {"code": "701", "name": "OPERATION COSTS", "kind": "overhead", "grp": "", "manual": "f"},
                    {"code": "704", "name": "VIT KOVARIK", "kind": "payroll", "grp": "SALARIES", "manual": "f"},
                    {"code": "705", "name": "TOMASZ ZEMELKA", "kind": "payroll", "grp": "SALARIES", "manual": "t"}]
        if name == "cost_seg":
            return [{"code": "9001", "cost_ytd": "600000.00", "cost_month": "600000.00", "rev_ytd": "1000000.00", "n": "2", "last": "2026-02-20"},
                    {"code": "701", "cost_ytd": "250000.00", "cost_month": "40000.00", "rev_ytd": "0", "n": "30", "last": "2026-02-27"},
                    {"code": "704", "cost_ytd": "90000.00", "cost_month": "45000.00", "rev_ytd": "0", "n": "4", "last": "2026-02-28"},
                    {"code": "705", "cost_ytd": "60000.00", "cost_month": "30000.00", "rev_ytd": "0", "n": "4", "last": "2026-02-28"}]
        if name == "open_ap_code":
            return [{"code": "701", "currency": "MXN", "total": "12000.00", "n": "2", "overdue": "5000.00"}, {"code": "701", "currency": "USD", "total": "300.00", "n": "1", "overdue": "0"}]
        if name == "seg_lines":
            if "l.segment = 701" not in sql:
                return []
            return [{"jdate": "2026-02-27", "kind": "Egresos", "number": "7", "concept": "RENTA OFICINA", "account": "601-01-000", "account_name": "Rentas", "bs_pl": "PL", "a_p": "C", "reference": "F-77", "debit": "40000.00", "credit": "0"}]
        if name == "suppliers":
            return [{"account": "201-01-005", "name": "PROVEEDOR GAMMA SA DE CV", "opening": "0", "debits": "348000.00", "credits": "580000.00", "closing": "232000.00", "movements": "3", "last": "2026-02-03"},
                    {"account": "201-01-006", "name": "PROVEEDOR GAMMA USD", "opening": "0", "debits": "0", "credits": "1500.00", "closing": "1500.00", "movements": "1", "last": "2026-02-10"},
                    {"account": "201-01-007", "name": "PROVEEDOR GAMMA COMPL", "opening": "0", "debits": "0", "credits": "27000.00", "closing": "27000.00", "movements": "1", "last": "2026-02-10"}]
        if name == "acct_lines":
            return [{"jdate": "2026-02-03", "kind": "Egresos", "number": "1", "concept": "PAGO PROVEEDOR GAMMA", "reference": "SPEI 5120", "debit": "348000.00", "credit": "0", "segment": "9001"},
                    {"jdate": "2026-01-20", "kind": "Diario", "number": "4", "concept": "FACTURA GAMMA", "reference": "A-1", "debit": "0", "credit": "580000.00", "segment": "9001"}]
        if name == "fx":
            return []
        if name.startswith("src_"):
            k = name[4:]
            return [{"src_name": {"polizas": "0226 Polizas Argia.xlsx", "tracker": "Tracker.xlsx", "acctbook": "Argia_Accounting_Data_02_26.xlsx"}.get(k, k),
                     "src_kind": k, "src_drive_id": k.upper(), "src_mime": XLSX_MIME}]
        return []                      # the demo pages ask for their own tables — empty here
    return rows


@pytest.fixture
def client(monkeypatch):
    F.app.config["ROWS"] = book_rows()
    F.app.config["MODE"] = "books"
    F.app.config["ENTITY"] = "ARGIA-MX"
    F.app.config["TODAY"] = lambda: dt.date(2026, 3, 1)
    F.ALLOWED_USERS.add("tomasz")
    yield F.app.test_client()
    F.app.config.pop("ROWS", None)
    F.app.config.pop("MODE", None)
    F.app.config.pop("ENTITY", None)
    F.app.config.pop("TODAY", None)


H = {"X-Remote-User": "tomasz"}


class TestDerivations:
    def test_cash_view_merges_a_dollar_account_with_its_complement(self):
        v = FB.cash_view([{"account": "102-01-001", "name": "Banorte MXN 9001", "closing": "480549.50", "movements": "3"},
                          {"account": "102-01-002", "name": "Banorte DLLS 9002", "closing": "25000.00", "movements": "1"},
                          {"account": "102-01-003", "name": "Banorte DLLS 9002 Compl", "closing": "435000.00", "movements": "1"},
                          {"account": "102-01-009", "name": "Intercam USD 0043 Compl", "closing": "0", "movements": "7"}])
        assert [x["name"] for x in v] == ["Banorte MXN 9001", "Banorte DLLS 9002"]         # the empty account is hidden
        usd = v[1]
        assert usd["usd"] and usd["usd_amount"] == D(25000) and usd["mxn"] == D(460000) and usd["accounts"] == ["102-01-002", "102-01-003"]

    def test_a_complement_without_the_base_name_pairs_with_the_previous_dollar_account(self):
        # the real chart: 102-01-008 'Banbajio DLLS 402' and 102-01-009 'Banbajio DLLS Compl' (no '402' in the complement)
        v = FB.cash_view([{"account": "102-01-008", "name": "Banbajio DLLS 402", "closing": "16024.08", "movements": "1"},
                          {"account": "102-01-009", "name": "Banbajio DLLS Compl", "closing": "264610.44", "movements": "1"},
                          {"account": "102-01-017", "name": "BanBajio cta 40397689 DLLS", "closing": "17669.78", "movements": "1"},
                          {"account": "102-01-018", "name": "BanBajio cta 40397689 Compl", "closing": "291786.38", "movements": "1"}])
        assert [(x["name"], str(x["usd_amount"]), str(x["mxn"])) for x in v] == [("BanBajio cta 40397689 DLLS", "17669.78", "309456.16"), ("Banbajio DLLS 402", "16024.08", "280634.52")]

    def test_open_summary_ages_from_the_final_due_date(self):
        items = [{"currency": "MXN", "total": "580000", "final_due": "2026-02-14", "paid_on": ""}, {"currency": "USD", "total": "12000", "due": "2026-03-27", "paid_on": ""},
                 {"currency": "USD", "total": "20000", "due": "2026-02-20", "paid_on": "2026-02-20"}]
        s = FB.open_summary(items, dt.date(2026, 3, 1))
        assert s["MXN"] == {"open": D(580000), "overdue": D(580000), "n": 1, "n_over": 1, "max_days": 15}
        assert s["USD"]["open"] == D(12000) and s["USD"]["n_over"] == 0

    def test_subtotal_labels_match_across_sheets(self):
        assert FB.norm_code("label:LAAS and PPA  EBITDA") == FB.norm_code("label:LAAS EBITDA") == "label:laas ebitda"
        assert FB.norm_code("PL_040") == "PL_040"
        idx = FB.report_index([{"code": "label:EBITDA incl. LAAS", "m01": "1"}])
        assert FB.rline(idx, "label:EBITDA incl. LAAS")["m01"] == "1"


class TestBooksPages:
    def test_today_reads_the_books(self, client):
        h = en(client.get("/finance/", headers=H).data.decode())
        assert "books through 2026-02" in h and "Cash in the books" in h
        assert "480,550" in h and "Banorte DLLS 9002" in h and "25,000 <span class=\"unit\">USD" in h and "460,000 MXN" in h   # the USD figure, its peso value beside it
        assert "Receivables (tracker) · MXN" in h and "580,000" in h and "1 open · 1 overdue" in h
        assert "Revenue YTD" in h and "1,368,000" in h and "budget 1,800k" in h
        assert "ARG9001 · SOLAR CAPEX ROOF 300 kWp DEMO UNO" in h and "DEMO CERO" not in h   # v248: code first, one case; done projects stay off Today
        assert "0226 Polizas Argia.xlsx" in h and "How the numbers are calculated" in h

    def test_ledgers_bank_pl_portfolio_project(self, client):
        ar = en(client.get("/finance/ar/", headers=H).data.decode())
        assert "CLIENTE ALFA" in ar and "Balances in the books" in ar and "11111111" in ar
        ap = en(client.get("/finance/ap/", headers=H).data.decode())
        assert "PROVEEDOR GAMMA" in ap and "ARRENDADORA DEMO" in ap
        bank = en(client.get("/finance/bank/", headers=H).data.decode())
        assert "2026-01-14 → 2026-02-28" in bank and "PAGO PROVEEDOR GAMMA" in bank
        pl = en(client.get("/finance/pl/", headers=H).data.decode())
        assert "Management P&amp;L" in pl and "% Gross Margin" in pl and "56.1%" in pl and "Balance sheet" in pl
        pf = en(client.get("/projects/", headers=H).data.decode())
        assert "Active projects" in pf and "on hold" not in pf.split("Portfolio")[0] and "ARG9001 · SOLAR CAPEX ROOF 300 kWp DEMO UNO" in pf and "sheet" in pf
        pj = en(client.get("/projects/ARG9001/", headers=H).data.decode())
        assert "PMO sheet" in pj and "M001" in pj and "Costs · Paid" in pj and "segment 9001" in pj and "Cost of Sales" in pj
        assert client.get("/projects/ARG9999/", headers=H).status_code == 404
        assert client.get("/projects/nope/", headers=H).status_code == 404

    def test_spanish_view_carries_no_english(self, client):
        h = client.get("/finance/", headers=H).data.decode()
        es = re.sub(r'<span data-en="[^"]*" data-es="([^"]*)">[^<]*</span>', r"\1", h)
        body = es.split('<div class="wrap">', 1)[1]
        for phrase in ("Cash in the books", "Receivables (tracker)", "Revenue YTD", "Active projects", "Data sources", "How the numbers"):
            assert phrase not in body, phrase
        assert "Efectivo en libros" in body and "Fuentes de datos" in body

    def test_demo_world_stays_under_its_own_paths(self, client):
        assert client.get("/finance/demo/", headers=H).status_code == 200
        assert client.get("/finance/", headers={"X-Remote-User": "nobody"}).status_code == 403
        assert F.mode() == "books" and F._ent() == "DEMO-MX"


class TestTableTools:
    """v246 (Tomasz): filtering, searching, tooltips, full width, print buttons on every table."""

    def test_data_table_markup(self):
        import portal_chrome as C
        h = C.data_table(["A", "B"], ["<tr><td>1</td><td>x</td></tr>"], columns=[("what A is", "qué es A"), None])
        assert 'class="dtwrap"' in h and '<table class="dt "' in h
        assert 'title="what A is" data-tip-es="qué es A"' in h and 'class="ti"' in h and h.count('class="sarr"') == 2
        assert "Columns explained" in h and "<dt>A</dt>" in h and "<dt>B</dt>" not in h
        assert "function argiaTables()" in C.JS and "argiaTables();" in C.JS
        assert "body.wide .wrap" in C.CSS and "@page{size:A4 landscape" in C.CSS and ".flipin{transform:none!important}" in C.CSS

    def test_every_books_page_is_wide_with_tools_and_a_print_button(self, client):
        for path, tables in (("/finance/", 2), ("/finance/ar/", 2), ("/finance/ap/", 2), ("/finance/bank/", 1), ("/finance/pl/", 2), ("/projects/", 2), ("/projects/ARG9001/", 5)):
            h = client.get(path, headers=H).data.decode()
            assert '<body class="wide">' in h, path
            assert h.count('class="dtwrap"') >= tables, (path, h.count('class="dtwrap"'))
            assert 'class="printbtn noprint"' in h and 'onclick="window.print()"' in h, path
            assert "Columns explained" in h, path
        ap = client.get("/finance/ap/", headers=H).data.decode()
        assert 'title="From the tracker: Delay = past its final due date' in ap and "data-tip-es=\"Del seguimiento" in ap

    def test_demo_pages_get_the_same_tools(self, client):
        h = client.get("/finance/demo/", headers=H).data.decode()
        assert '<body class="wide">' in h and 'class="dtwrap"' in h

    def test_every_column_key_used_exists(self):
        src = (V2 / "server" / "bundle" / "fin_books.py").read_text(encoding="utf-8")
        used = set(re.findall(r"COLS\(([^)]*)\)", src))
        keys = {m for grp in used for m in re.findall(r"'([a-z_]+)'", grp)} - {"ar"}      # 'ar' is the side switch, not a key
        missing = keys - set(FB._COL)
        assert not missing, missing
        for k, (en, es) in FB._COL.items():
            assert len(en) > 8 and len(es) > 8 and en != es, k


class TestWiring:
    def test_chrome_has_the_pl_tab_and_the_units_and_docs_know_the_timer(self):
        import portal_chrome as C
        assert ("pl", "P&L", "Resultados") in C.SECTIONS["finance"][2]
        bundle = V2 / "server" / "bundle"
        assert (bundle / "argia-fin-drive.timer").exists() and (bundle / "argia-fin-drive.service").exists()
        unit = (bundle / "argia-fin-drive.service").read_text(encoding="utf-8")
        assert "fin_drive_ingest.py --drive --apply" in unit and "run_job.sh" in unit
        ops = (V2 / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        assert "argia-fin-drive" in ops and "fin_drive_ingest" in ops
        assert "openpyxl" in (V2 / "requirements.txt").read_text(encoding="utf-8")


# ------------------------------------------------------------- v248
class TestCostCentres:
    def test_catalogue_rules_on_the_accountants_codes(self):
        from argia.fin import costcenter as CC
        assert CC.classify(701, "OPERATION COSTS", "_") == CC.OVERHEAD
        assert CC.classify(706, "PAYROLL", "PL_080") == CC.PAYROLL
        assert CC.classify(704, "VIT KOVARIK", "PL_195") == CC.PAYROLL and CC.classify(1362, "Martín de Jesús Arias Parada", "") == CC.PAYROLL
        assert CC.classify(1350, "SOLAR PPA ROOF 605.5 kWp TAIGENE GUANAJUATO", "GM") == CC.PROJECT
        assert CC.classify(1238, "Prologis Smart Mtering Arcos Office", "") == CC.PROJECT     # 'Office' inside a Prologis name is still a project
        assert CC.classify(1259, "Argia Holding", "") == CC.OVERHEAD and CC.classify(918, "PROLOGIS GENERAL", "_") == CC.OVERHEAD
        assert CC.classify(1100, "Garantias Acuity", "") == CC.WARRANTY and CC.classify(772, "PIRELLI", "_") == CC.OTHER
        assert not CC.is_person("RYDER GUADALAJARA") and CC.is_person("SALVADOR BRAVO") and not CC.is_person("C10 GROUP SRO")

    def test_one_case_and_code_first_labels(self):
        from argia.fin import costcenter as CC
        assert CC.canon("Solar Capex Roof 947.7 KwP Teneria Panamericana BJX") == "SOLAR CAPEX ROOF 947.7 kWp TENERIA PANAMERICANA BJX"
        assert CC.canon("Leon Warehpuse") == "LEON WAREHOUSE" and CC.canon("  c10  Group s.r.o ") == "C10 GROUP S.R.O"
        assert CC.label(1350, "Solar PPA Taigene") == "ARG1350 · SOLAR PPA TAIGENE"
        assert CC.label(701, "Operation costs", CC.OVERHEAD) == "701 · OPERATION COSTS" and CC.label(919, "Prologis - Autotek 1", CC.PROJECT) == "919 · PROLOGIS - AUTOTEK 1"
        assert CC.label(None, "no code") == "NO CODE" and CC.href(1350) == "/projects/ARG1350/" and CC.href(701, CC.OVERHEAD) == "/finance/costs/701/"

    def test_build_keeps_manual_kinds_and_trusts_the_overview(self):
        from argia.fin import costcenter as CC
        projects = [{"code": 1414, "name": "Ryder Guadalajara", "project_type": ""}, {"code": 772, "name": "PIRELLI", "project_type": "_"}, {"code": 713, "name": "not used yet", "project_type": "_"}]
        out = {c.code: c for c in CC.build(projects, {772: CC.CostCenter(772, "PIRELLI", CC.PROJECT, "", manual=True)}, project_codes={1414})}
        assert out[1414].kind == CC.PROJECT and out[772].kind == CC.PROJECT and out[772].manual and 713 not in out
        sql = B.cost_center_sql(B.cost_center_rows("X", {1414: AB.ProjectCode(1414, "Ryder Guadalajara", "", ""), 704: AB.ProjectCode(704, "VIT KOVARIK", "PL_195", "")}, {1414}))
        assert "CASE WHEN cost_center.manual THEN cost_center.kind" in sql and "'SALARIES'" in sql and sql.count("INSERT INTO cost_center") == 2

    def test_party_key_joins_tracker_names_to_sub_accounts(self):
        assert FB.party_key("C10 Group s.r.o") == "C10 GROUP" == FB.party_key("C10 GROUP SRO")
        assert FB.party_key("PROLOGIS PROPERTY MEXICO COMPL") == FB.party_key("Prologis Property Mexico USD") == FB.party_key("PROLOGIS PROPERTY MEXICO")
        assert FB.party_key("JOSE DE JESUS MUÑOZ LOPEZ") == "JOSE JESUS MUNOZ LOPEZ" and FB.party_key("Electro Experts, MXP Proveed") == "ELECTRO EXPERTS"
        groups = FB.supplier_groups([{"account": "201-01-005", "name": "PROVEEDOR GAMMA SA DE CV", "closing": "232000", "credits": "580000", "debits": "348000", "movements": "3", "last": "2026-02-03"},
                                     {"account": "201-01-006", "name": "PROVEEDOR GAMMA USD", "closing": "1500", "credits": "1500", "debits": "0", "movements": "1", "last": "2026-02-10"},
                                     {"account": "201-01-007", "name": "PROVEEDOR GAMMA COMPL", "closing": "27000", "credits": "27000", "debits": "0", "movements": "1", "last": "2026-02-10"}])
        assert len(groups) == 1 and groups[0]["name"] == "PROVEEDOR GAMMA SA DE CV" and groups[0]["accounts"] == ["201-01-005", "201-01-006", "201-01-007"]
        assert groups[0]["closing"] == {"MXN": D(259000), "USD": D(1500)}        # the peso complement counts as MXN, USD at face value
        idx = FB.supplier_index([{"account": "201-01-005", "name": "PROVEEDOR GAMMA SA DE CV", "closing": "1", "credits": "0", "debits": "0", "movements": "1", "last": ""}])
        assert FB.match_account(idx, "Proveedor Gamma, S.A. de C.V.") == "201-01-005" and FB.match_account(idx, "OTRO") == ""

    def test_cost_pages(self, client):
        h = en(client.get("/finance/costs/", headers=H).data.decode())
        assert "Where the money goes" in h and "ARG9001 · SOLAR CAPEX ROOF 300 kWp DEMO UNO" in h and "701 · OPERATION COSTS" in h
        assert "SALARIES &amp; FEES" in h and "(2 people)" in h and "150,000" in h        # 704 + 705 rolled into one line
        assert "VIT KOVARIK" not in h and 'data-sum="2,3,5,6"' in h and '12,000 <span class="unit">MXN</span> · 300 <span class="unit">USD</span>' in h
        assert "1,000,000" in h and "60.0%" in h                                            # project share of the 1,000,000 total
        d = en(client.get("/finance/costs/701/", headers=H).data.decode())
        assert "701 · OPERATION COSTS" in d and "RENTA OFICINA" in d and "Overhead" in d and "40,000.00" in d
        s_ = en(client.get("/finance/costs/salaries/", headers=H).data.decode())
        assert "704 · VIT KOVARIK" in s_ and "705 · TOMASZ ZEMELKA" in s_ and "90,000" in s_
        r = client.get("/finance/costs/9001/", headers=H)
        assert r.status_code == 302 and r.headers["Location"].endswith("/projects/ARG9001/")
        assert client.get("/finance/costs/4242/", headers=H).status_code == 404

    def test_supplier_pages_group_sub_accounts(self, client):
        h = en(client.get("/finance/suppliers/", headers=H).data.decode())
        assert "PROVEEDOR GAMMA SA DE CV" in h and h.count("201-01-006") >= 1 and "259,000 <span class=\"unit\">MXN</span> · 1,500 <span class=\"unit\">USD</span>" in h and "1 suppliers (3 sub-accounts)" in h
        d = en(client.get("/finance/suppliers/201-01-006/", headers=H).data.decode())     # any sub-account opens the supplier
        assert "PROVEEDOR GAMMA SA DE CV" in d and "PAGO PROVEEDOR GAMMA" in d and "ARG9001 · SOLAR CAPEX ROOF 300 kWp DEMO UNO" in d
        assert client.get("/finance/suppliers/201-01-999/", headers=H).status_code == 404 and client.get("/finance/suppliers/x/", headers=H).status_code == 404

    def test_ledger_has_code_first_projects_dates_nowrap_doc_links_and_totals(self, client):
        h = en(client.get("/finance/ap/", headers=H).data.decode())
        assert "ARG9001 · SOLAR CAPEX ROOF 300 kWp DEMO UNO" in h and 'class="nw"' in h and 'data-sum="8,9"' in h
        assert "drive.google.com/drive/search?q=" in h and 'href="/finance/suppliers/201-01-005/"' in h
        assert "Proveedor Gamma" not in h and "PROVEEDOR GAMMA" in h                       # one case everywhere

    def test_pl_offers_the_closed_years(self, client):
        h = en(client.get("/finance/pl/", headers=H).data.decode())
        assert 'href="?period=2025-12">2025</a>' in h and 'href="?period=2026-02" class=on>2026-02</a>' in h
        assert client.get("/finance/pl/?period=2025-12", headers=H).status_code == 200
        assert client.get("/finance/pl/?period=bogus", headers=H).status_code == 200                # unknown → the current one

    def test_history_period_parsers(self):
        assert B.month_prefix_period("122023 Auxiliares Argia Final (3).xlsx") == "2023-12" and B.month_prefix_period("2025 Polizas Argia.xlsx") == "2025-12"
        assert B.month_prefix_period("1224 Polizas Argia1.xlsx") == "2024-12" and B.month_prefix_period("0726 Polizas Argia.xlsx") == "2026-07"
        assert B.acctbook_period("Argia_Accounting_Data_12_2023_cambio saldos.xlsm") == "2023-12" and B.acctbook_period("Argia_Accounting_Data_12_25_V3.0 - After (PPA as Assets).xlsm") == "2025-12"
        import fin_drive_ingest as I
        assert I.SKIP_NAMES.search("Argia_Accounting_Data_12_25_V3 - Before (Accruals Model).xlsm") and not I.SKIP_NAMES.search("Argia_Accounting_Data_12_24.xlsm")
        assert I.POLIZAS_RE.match("122023 Polizas Argia Final (2).xlsx") and I.AUX_RE.match("2025 Auxiliares Argia.xlsx") and I.BOOK_RE.match("Argia_Accounting_Data_12_2023_cambio saldos.xlsm")

    def test_local_source_picks_the_year_and_the_latest_re_export(self, tmp_path):
        import fin_drive_ingest as I
        d = tmp_path / "2024" / "12. December"
        d.mkdir(parents=True)
        (d / "1224 Polizas Argia.xlsx").write_bytes(b"old")
        (d / "1224 Polizas Argia1.xlsx").write_bytes(b"re-export")
        import os
        os.utime(d / "1224 Polizas Argia1.xlsx", (2_000_000_000, 2_000_000_000))
        os.utime(d / "1224 Polizas Argia.xlsx", (1_900_000_000, 1_900_000_000))
        (tmp_path / "2026").mkdir()
        (tmp_path / "2026" / "0726 Polizas Argia.xlsx").write_bytes(b"now")
        (tmp_path / "2025").mkdir()
        (tmp_path / "2025" / "Argia_Accounting_Data_12_25_V3 - Before (Accruals Model).xlsm").write_bytes(b"x")
        src = I.LocalSource(tmp_path)
        assert src.latest(I.POLIZAS_RE).name == "0726 Polizas Argia.xlsx"
        assert src.latest(I.POLIZAS_RE, 2024).name == "1224 Polizas Argia1.xlsx" and src.latest(I.POLIZAS_RE, 2023) is None
        assert src.latest(I.BOOK_RE, 2025) is None                                                  # the 'Before' copy is never read

    def test_display_currency_cookie_converts_and_keeps_the_original(self, client):
        r = client.get("/finance/ap/?ccy=MXN", headers=H)
        assert r.status_code == 302 and "fin_ccy=MXN" in r.headers.get("Set-Cookie", "")
        client.set_cookie("fin_ccy", "MXN")
        h = client.get("/finance/ap/", headers=H).data.decode()
        assert 'class="ccysw' in h and 'class=on>MXN' in h
        # the synthetic books hold a USD bank account with its peso complement → the rate comes from the books
        rate, src = FB.books_rate("2026-02")
        assert rate is not None and src == ("books", "2026-02") and D("18.3") < rate < D("18.5")
        assert 'class="conv"' in h and "as issued" in h
        client.set_cookie("fin_ccy", "")
        assert 'class="conv"' not in client.get("/finance/ap/", headers=H).data.decode()


class TestSourcePointers:
    def test_every_tracker_row_links_to_the_document_that_makes_it_overdue(self, client):
        h = client.get("/finance/ap/", headers=H).data.decode()
        assert h.count('class="src"') >= 1 and "Tracker.xlsx" in h and "drive.google.com/file/d/TRK/view" in h
        assert 'sheet “' in h and "column “Payment Due Day”" in h          # the tooltip says where to look inside the file
        assert en(h).count("Source") >= 1

    def test_the_project_page_says_where_to_change_each_thing(self, client):
        h = en(client.get("/projects/ARG9001/", headers=H).data.decode())
        assert "Where to change this" in h and "data-notools" in h          # a reference table, no search box
        assert "Overview.xlsx" in h and "the portfolio owner" in h and "Edit here" in h
        assert "ARGIA PROJECT ARG9001" in h and "the project manager" in h
        assert "0226 Polizas Argia.xlsx" in h and "the accountants" in h and "Read-only" in h
        assert "docs.google.com/spreadsheets/d/PMO/edit#gid=55" in h        # the Costs tab, ready for a new line

    def test_a_cost_row_links_to_its_own_cell_in_the_sheet(self, client):
        h = client.get("/projects/ARG9001/", headers=H).data.decode()
        assert "docs.google.com/spreadsheets/d/PMO/edit#gid=55&amp;range=A" in h
        assert "⎘" in h and "Amount_Before_VAT" in h

    def test_the_cost_centre_page_names_the_three_files_behind_it(self, client):
        h = en(client.get("/finance/costs/701/", headers=H).data.decode())
        assert "Where to change this" in h and "Tracker.xlsx" in h and "0226 Polizas Argia.xlsx" in h and "Argia_Accounting_Data_02_26.xlsx" in h
        assert h.count("Read-only") >= 2                                     # the books and the accountants' workbook
