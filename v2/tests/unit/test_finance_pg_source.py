"""v191 — Sheets retirement phase 3a: finance readers on PostgreSQL.

Locks: the switch defaults to sheet; PG grids/records have the sheets'
shape so the unchanged parsers produce identical typed objects; every
reader goes through its door; the sheet branch still issues the exact
old call; maintenance events are not double-counted in pg mode; the
parity comparator's rules (only-in-sheet fails, only-in-pg allowed
where declared, storage-scale tolerance).
"""
import pathlib

import pytest

from argia.finance import pg_source as P
from argia.finance.contract import ContractMonth, load_contract_monthly, parse_contract_grid
from argia.finance.loans import (Loan, ScheduleRow, load_loan_schedule, load_loans,
                                 loans_from_records, schedule_from_records)
from argia.kpi.design import load_design_monthly, parse_design_grid
from argia.maintenance import events as EV

V2 = pathlib.Path(__file__).resolve().parents[2]

CONTRACT_CSV = ("plant_key,year,month,design_kwh,contract_kwh,tariff_mxn,fixed_income_ccy,ccy\n"
                "GTO1,2026,1,101234.500,95000.000,2.1500,,\n"
                "NL2,2026,1,,,,1250.00,USD\n"
                "QRO1,2026,1,,,,,\n")
LOANS_CSV = ("loan_id,plant_key,project_name,bank,currency,principal_mxn,total_installments,first_month,last_month\n"
             "SLP1-L2,SLP1,SLP refi,BBVA,MXN,150000.00,12,2026-01,2026-12\n")
SCHED_CSV = ("loan_id,plant_key,ref_month,installment_no,total_installments,payment_mxn,payment_ccy,xr,due_after_mxn\n"
             "SLP1-L2,SLP1,2026-02,2,12,12500.00,,,125000.00\n"
             "NL2-L1,NL2,2026-02,5,60,94668.89,5100.00,18.5625,3200000.00\n")


class FakeSheets:
    def __init__(self, grids=None):
        self.grids = grids or {}
        self.calls = []

    def read_range(self, tab, a1):
        self.calls.append(("read_range", tab, a1))
        if tab not in self.grids:
            raise RuntimeError("no tab " + tab)
        return self.grids[tab]

    def read_table(self, tab, a1="A1:Z"):
        self.calls.append(("read_table", tab, a1))
        g = self.grids[tab]
        return P.grid_to_records(g)


class TestSwitch:
    def test_default_is_sheet(self):
        assert P.source({}) == "sheet"
        assert P.source({"ARGIA_FINANCE_SOURCE": "nonsense"}) == "sheet"

    def test_pg(self):
        assert P.source({"ARGIA_FINANCE_SOURCE": "PG "}) == "pg"


class TestGridShape:
    def test_contract_grid_is_typed_like_the_sheet(self):
        g = P.csv_to_grid(CONTRACT_CSV, P.CONTRACT_HEADER)
        assert g[0] == P.CONTRACT_HEADER
        assert g[1] == ["GTO1", 2026, 1, 101234.5, 95000, 2.15, "", ""]
        assert g[2] == ["NL2", 2026, 1, "", "", "", 1250, "USD"]
        assert isinstance(g[1][1], int) and isinstance(g[1][2], int)

    def test_blank_rows_are_dropped(self):
        g = P.csv_to_grid(CONTRACT_CSV + ",,,,,,,\n", P.CONTRACT_HEADER)
        assert len(g) == 4

    def test_records_pad_short_rows(self):
        recs = P.grid_to_records([["a", "b"], [1]])
        assert recs == [{"a": 1, "b": ""}]


class TestSameParserSameResult:
    def test_contract_pg_grid_parses_to_the_same_objects_as_the_sheet(self):
        pg = parse_contract_grid(P.csv_to_grid(CONTRACT_CSV, P.CONTRACT_HEADER))
        sheet = parse_contract_grid([P.CONTRACT_HEADER,
                                     ["GTO1", 2026, 1, 101234.5, 95000, 2.15, "", ""],
                                     ["NL2", 2026, 1, "", "", "", 1250, "USD"],
                                     ["QRO1", 2026, 1, "", "", "", "", ""]])
        assert pg == sheet
        assert pg[("GTO1", 2026, 1)] == ContractMonth("GTO1", 2026, 1, 101234.5, 95000.0,
                                                      2.15, None, "")
        assert pg[("NL2", 2026, 1)].is_laas

    def test_loans_pg_records_match_a_sheet_with_date_cells(self):
        pg = loans_from_records(P.grid_to_records(P.csv_to_grid(LOANS_CSV, P.LOANS_HEADER)))
        # the live sheet auto-parsed 'YYYY-MM' into serial date cells
        sheet = loans_from_records([{"loan_id": "SLP1-L2", "plant_key": "SLP1",
                                     "project_name": "SLP refi", "bank": "BBVA",
                                     "currency": "MXN", "principal_mxn": "150,000.00",
                                     "total_installments": 12,
                                     "first_month": 46023, "last_month": 46357}])
        assert pg == sheet
        assert pg["SLP1-L2"] == Loan("SLP1-L2", "SLP1", "SLP refi", "BBVA", "MXN",
                                     150000.0, 12, "2026-01", "2026-12")

    def test_schedule_pg_records_carry_the_joined_columns(self):
        rows = schedule_from_records(P.grid_to_records(P.csv_to_grid(SCHED_CSV, P.SCHEDULE_HEADER)))
        assert [r.loan_id for r in rows] == ["NL2-L1", "SLP1-L2"]   # sorted
        slp = rows[1]
        assert slp == ScheduleRow("SLP1-L2", "SLP1", "2026-02", 2, 12, 12500.0,
                                  None, None, 125000.0)
        nl2 = rows[0]
        assert nl2.is_usd and nl2.total_installments == 60 and nl2.plant_key == "NL2"

    def test_design_from_contract_monthly(self):
        g = P.csv_to_grid("plant_key,year,month,design_kwh\nGTO1,2026,1,101234.500\n",
                          P.DESIGN_HEADER)
        assert parse_design_grid(g, "pg") == {("GTO1", 2026, 1): 101234.5}


class TestDoors:
    def test_sheet_mode_issues_the_exact_old_calls(self, monkeypatch):
        monkeypatch.delenv("ARGIA_FINANCE_SOURCE", raising=False)
        fs = FakeSheets({"Contract_Monthly": [P.CONTRACT_HEADER],
                         "Loans": [P.LOANS_HEADER], "Loan_Schedule": [P.SCHEDULE_HEADER]})
        load_contract_monthly(fs); load_design_monthly(fs); load_loans(fs); load_loan_schedule(fs)
        assert ("read_range", "Contract_Monthly", "A1:H") in fs.calls
        assert ("read_range", "Contract_Monthly", "A1:D") in fs.calls     # design, 1st candidate
        assert ("read_table", "Loans", "A1:Z") in fs.calls
        assert ("read_table", "Loan_Schedule", "A1:Z") in fs.calls

    def test_design_sheet_mode_falls_back_through_the_candidates(self, monkeypatch):
        monkeypatch.delenv("ARGIA_FINANCE_SOURCE", raising=False)
        fs = FakeSheets({"Design_Monthly": [P.DESIGN_HEADER, ["GTO1", 2026, 1, 5]]})
        assert load_design_monthly(fs) == {("GTO1", 2026, 1): 5.0}
        assert ("read_range", "Contract_Monthly", "A1:D") in fs.calls

    def test_pg_mode_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_FINANCE_SOURCE", "pg")
        served = {}
        def fake_fetch(sql):
            served[sql] = True
            if "FROM contract_monthly" in sql and "design_kwh IS NOT NULL" not in sql:
                return CONTRACT_CSV
            if "design_kwh IS NOT NULL" in sql:
                return "plant_key,year,month,design_kwh\nGTO1,2026,1,101234.5\n"
            if "FROM loan ORDER" in sql:
                return LOANS_CSV
            return SCHED_CSV
        monkeypatch.setattr(P, "_fetch_csv", fake_fetch)
        fs = FakeSheets()
        assert len(load_contract_monthly(fs)) == 3
        assert load_design_monthly(fs) == {("GTO1", 2026, 1): 101234.5}
        assert list(load_loans(fs)) == ["SLP1-L2"]
        assert len(load_loan_schedule(fs)) == 2
        assert fs.calls == []
        assert len(served) == 4

    def test_pg_mode_read_failure_is_degraded_like_a_missing_tab(self, monkeypatch):
        monkeypatch.setenv("ARGIA_FINANCE_SOURCE", "pg")
        monkeypatch.setattr(P, "_fetch_csv", lambda sql: (_ for _ in ()).throw(RuntimeError("psql failed")))
        assert load_contract_monthly(FakeSheets()) == {}
        assert load_loans(FakeSheets()) == {}
        assert load_loan_schedule(FakeSheets()) == []


class TestMaintenanceDoor:
    def test_pg_mode_serves_the_pg_table(self, monkeypatch):
        monkeypatch.setenv("ARGIA_FINANCE_SOURCE", "pg")
        monkeypatch.setattr(EV, "load_maintenance_events_pg", lambda: ["pg-event"])
        fs = FakeSheets()
        assert EV.load_maintenance_events(fs) == ["pg-event"]
        assert fs.calls == []

    def test_sheet_mode_reads_the_tab(self, monkeypatch):
        monkeypatch.delenv("ARGIA_FINANCE_SOURCE", raising=False)
        fs = FakeSheets({"Maintenance_Events": [["plant_key", "start_ts"]]})
        assert EV.load_maintenance_events(fs) == []
        assert ("read_table", "Maintenance_Events", "A1:ZZ") in fs.calls

    def test_kpi_eod_does_not_double_count_in_pg_mode(self):
        src = (V2 / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "load_maintenance_events(sheets) + load_maintenance_events_pg()" not in src
        assert 'if _fin_source() != "pg":' in src


class TestSQL:
    def test_schedule_joins_loan_for_the_sheet_only_columns(self):
        assert "JOIN loan l ON l.loan_id = s.loan_id" in P.SCHEDULE_SQL
        assert "l.plant_key" in P.SCHEDULE_SQL and "l.total_installments" in P.SCHEDULE_SQL
        assert "to_char(s.ref_month, 'YYYY-MM')" in P.SCHEDULE_SQL

    def test_loans_months_are_yyyy_mm(self):
        assert "to_char(first_month, 'YYYY-MM') AS first_month" in P.LOANS_SQL

    def test_design_comes_from_contract_monthly(self):
        assert "FROM contract_monthly" in P.DESIGN_SQL
        assert "design_kwh IS NOT NULL" in P.DESIGN_SQL


class TestParityRules:
    @pytest.fixture
    def cmp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "finance_parity", V2 / "scripts" / "finance_parity.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_only_in_sheet_fails(self, cmp):
        rep = cmp.compare_maps({"a": 1, "b": 2}, {"a": 1})
        assert rep["only_sheet"] == ["b"] and not rep["ok"]

    def test_only_in_pg_allowed_only_where_declared(self, cmp):
        rep = cmp.compare_maps({"a": 1}, {"a": 1, "z": 9})
        assert not rep["ok"]
        rep = cmp.compare_maps({"a": 1}, {"a": 1, "z": 9}, allow_only_pg=True)
        assert rep["ok"] and rep["only_pg"] == ["z"]

    def test_dataclass_fields_compared_with_tolerance(self, cmp):
        a = ContractMonth("GTO1", 2026, 1, 101234.5, 95000.0, 2.15, None, "")
        b = ContractMonth("GTO1", 2026, 1, 101234.504, 95000.0, 2.15, None, "")
        c = ContractMonth("GTO1", 2026, 1, 101234.6, 95000.0, 2.15, None, "")
        assert cmp.compare_maps({"k": a}, {"k": b})["ok"]
        rep = cmp.compare_maps({"k": a}, {"k": c})
        assert rep["diffs"] == [("k", "design_kwh", 101234.5, 101234.6)]

    def test_none_vs_number_is_a_diff(self, cmp):
        a = ContractMonth("Q", 2026, 1, None, None, None, None, "")
        b = ContractMonth("Q", 2026, 1, 5.0, None, None, None, "")
        assert not cmp.compare_maps({"k": a}, {"k": b})["ok"]

    def test_render_states_the_verdict(self, cmp):
        rep = cmp.compare_maps({"a": 1}, {"a": 1})
        assert "VERDICT: CLEAN" in cmp.render("Loans", rep)


class TestParityAuthority:
    """v191.1: the live gate found SLP1-L2 (a test loan deleted through
    /setup/finance on 2026-09-01, finance_audit #2) still in the sheet and
    SLP1-L3 (the real credit) only in PG. Audited loan_ids are PG-authoritative."""

    @pytest.fixture
    def cmp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "finance_parity", V2 / "scripts" / "finance_parity.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_audited_loan_differences_are_expected_not_failures(self, cmp):
        exp = cmp.loan_expected(frozenset({"SLP1-L3"}))
        sheet = {"SLP1-L1": 1, "SLP1-L2": 2}
        pg = {"SLP1-L1": 1, "SLP1-L3": 3}
        rep = cmp.compare_maps(sheet, pg, expected=exp)
        assert not rep["ok"]                      # SLP1-L2 is NOT audited
        assert rep["only_sheet"] == ["SLP1-L2"] and rep["only_pg"] == []
        assert ("only_pg", "SLP1-L3") in rep["expected"]
        rep = cmp.compare_maps(sheet, pg, expected=cmp.loan_expected({"SLP1-L2", "SLP1-L3"}))
        assert rep["ok"] and len(rep["expected"]) == 2

    def test_schedule_keys_use_the_loan_id_part(self, cmp):
        exp = cmp.loan_expected(frozenset({"SLP1-L3"}))
        assert exp(("SLP1-L3", "2026-06")) and not exp(("NL2-L1", "2026-06"))

    def test_field_diffs_on_audited_loans_are_listed_not_failed(self, cmp):
        a = Loan("X", "P", "n", "b", "MXN", 100.0, 12, "2026-01", "2026-12")
        b = Loan("X", "P", "n", "b", "MXN", 50.0, 12, "2026-01", "2026-12")
        rep = cmp.compare_maps({"X": a}, {"X": b}, expected=cmp.loan_expected({"X"}))
        assert rep["ok"] and rep["diffs"] == []
        assert rep["expected"][0][0] == "diff"
        assert "expected (PG authoritative, audited): 1" in cmp.render("Loans", rep)

    def test_unaudited_loan_diff_still_fails(self, cmp):
        a = Loan("X", "P", "n", "b", "MXN", 100.0, 12, "2026-01", "2026-12")
        b = Loan("X", "P", "n", "b", "MXN", 50.0, 12, "2026-01", "2026-12")
        assert not cmp.compare_maps({"X": a}, {"X": b}, expected=cmp.loan_expected(set()))["ok"]

    def test_run_compares_design_from_contract_monthly_not_the_legacy_tab(self, cmp):
        src = (V2 / "scripts" / "finance_parity.py").read_text(encoding="utf-8")
        assert 'sheets.read_range("Contract_Monthly", "A1:D")' in src
        assert "not read by any job" in src
