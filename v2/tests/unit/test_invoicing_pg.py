"""v192 — Sheets retirement phase 3b: Invoicing_Overview -> the
PostgreSQL invoicing register.

Locks: the switch defaults to sheet; the PG grid has the sheet's shape
so annex.parse_invoicing_overview is unchanged; the backfill inserts
missing rows and only fills NULLs on existing ones; invoice_publish's
register row now carries the produced/penalty/expected split; the
parity comparator's rules.
"""
import pathlib

from argia.finance import invoicing_pg as I
from argia.finance import annex as A

V2 = pathlib.Path(__file__).resolve().parents[2]

SHEET = [I.HEADER,
         [2026, "August", 8, "GTO1", 98765.4, 1234.5, 195000.12, 101234.0],
         [2026, "August", 8, "MEX1", 55000, "", 110000.0, ""],
         [2026, "September", 9, "GTO1", "", "", "", 101000.0],   # not invoiced yet
         ["", "", "", "", "", "", "", ""]]
PG_CSV = ("Year,Month,Month_No,Plant_Key,Total_kWh,Penalty_kWh,Total_Income,Expected_kWh\n"
          "2026,August,8,GTO1,98765.400,1234.500,195000.12,101234.000\n"
          "2026,August,8,MEX1,55000.000,0.000,110000.00,\n"
          "2026,September,9,GTO1,,,,101000.000\n")


class TestSwitch:
    def test_default_pg(self):
        assert I.source({}) == "pg"                                            # v205
        assert I.source({"ARGIA_INVOICING_SOURCE": "sheet"}) == "sheet"


class TestGrid:
    def test_pg_grid_parses_exactly_like_the_sheet(self):
        pg = A.parse_invoicing_overview(I.csv_to_grid(PG_CSV), 2026)
        sheet = A.parse_invoicing_overview(SHEET, 2026)
        assert pg["GTO1"]["2026-08"] == sheet["GTO1"]["2026-08"] == {
            "kwh": 98765.4, "penalty": 1234.5, "income": 195000.12, "expected": 101234.0}
        assert pg["MEX1"]["2026-08"] == {"kwh": 55000.0, "penalty": 0.0,
                                         "income": 110000.0, "expected": None}

    def test_month_columns_stay_text(self):
        g = I.csv_to_grid(PG_CSV)
        assert g[1][1] == "August" and g[1][3] == "GTO1" and g[1][0] == 2026

    def test_select_serves_only_invoiced_rows_in_the_sheet_order(self):
        assert "WHERE produced_kwh IS NOT NULL OR expected_kwh IS NOT NULL" in I.SELECT_SQL
        for h in I.HEADER:
            assert f'AS "{h}"' in I.SELECT_SQL


class TestBackfill:
    def test_sheet_rows_keep_expected_only_months_and_skip_blank(self):
        rows = I.sheet_rows(SHEET)
        assert [(r["plant_key"], r["ref_month"]) for r in rows] == [
            ("GTO1", "2026-08-01"), ("MEX1", "2026-08-01"), ("GTO1", "2026-09-01")]
        assert rows[1]["penalty"] is None and rows[1]["expected"] is None
        assert rows[2]["produced"] is None and rows[2]["expected"] == 101000.0

    def test_the_annex_uses_the_expectation_of_an_uninvoiced_month(self):
        # why expected-only rows must travel: rollup_month reads h["expected"]
        src = (V2 / "argia" / "finance" / "annex.py").read_text(encoding="utf-8")
        assert 'if h.get("expected") is not None:' in src
        assert "OR expected_kwh IS NOT NULL" in I.SELECT_SQL

    def test_backfill_inserts_or_fills_nulls_only(self):
        sql = I.build_backfill_sql(I.sheet_rows(SHEET))
        assert sql.count("INSERT INTO invoicing") == 3
        assert "'GTO1', DATE '2026-09-01', NULL, NULL, NULL, NULL, 101000.0, 'EXPECTED_ONLY'" in sql
        assert "'GTO1', DATE '2026-08-01', 99999.9, 195000.12, 98765.4, 1234.5, 101234.0" in sql
        assert "'MEX1', DATE '2026-08-01', 55000.0, 110000.0, 55000.0, 0.0, NULL" in sql
        assert "'SHEET_IMPORT', 'Invoicing_Overview'" in sql
        for c in ("billable_kwh", "amount_mxn", "produced_kwh", "penalty_kwh",
                  "expected_kwh", "source"):
            assert f"{c} = COALESCE(invoicing.{c}, EXCLUDED.{c})" in sql
        assert "check_status = " not in sql          # an existing verdict is kept

    def test_ensure_is_idempotent_and_adds_the_split(self):
        assert I.ENSURE_SQL.count("ADD COLUMN IF NOT EXISTS") == 4
        assert "CREATE TABLE IF NOT EXISTS invoicing" in I.ENSURE_SQL


class TestParity:
    def test_clean(self):
        h = A.parse_invoicing_overview(SHEET, 2026)
        assert I.compare_history(h, h)["ok"]

    def test_only_in_sheet_fails_and_field_diffs_listed(self):
        s = A.parse_invoicing_overview(SHEET, 2026)
        p = A.parse_invoicing_overview(I.csv_to_grid(PG_CSV.replace("195000.12", "195000.99")), 2026)
        rep = I.compare_history(s, p)
        assert rep["diffs"] == [(("GTO1", "2026-08"), "income", 195000.12, 195000.99)]
        del p["MEX1"]
        rep = I.compare_history(s, p)
        assert rep["only_sheet"] == [("MEX1", "2026-08")] and not rep["ok"]

    def test_none_vs_value_is_a_diff(self):
        s = {"GTO1": {"2026-08": {"kwh": 1.0, "penalty": 0.0, "income": None, "expected": 5.0}}}
        p = {"GTO1": {"2026-08": {"kwh": 1.0, "penalty": 0.0, "income": None, "expected": None}}}
        assert I.compare_history(s, p)["diffs"] == [(("GTO1", "2026-08"), "expected", 5.0, None)]


class TestDoor:
    def test_pg_mode_reads_the_register_not_the_workbook(self, monkeypatch):
        monkeypatch.setenv("ARGIA_INVOICING_SOURCE", "pg")
        monkeypatch.delenv("ARGIA_SOLAR_SHEET_ID", raising=False)
        monkeypatch.setattr(I, "_fetch_csv", lambda sql: PG_CSV)
        h = A.load_invoicing_overview(2026)
        assert set(h) == {"GTO1", "MEX1"} and h["GTO1"]["2026-09"]["kwh"] is None

    def test_pg_read_failure_degrades_to_atoms(self, monkeypatch):
        monkeypatch.setenv("ARGIA_INVOICING_SOURCE", "pg")
        monkeypatch.setattr(I, "_fetch_csv",
                            lambda sql: (_ for _ in ()).throw(RuntimeError("psql failed")))
        assert A.load_invoicing_overview(2026) == {}

    def test_sheet_mode_without_id_is_unchanged(self, monkeypatch):
        monkeypatch.setenv("ARGIA_INVOICING_SOURCE", "sheet")
        monkeypatch.delenv("ARGIA_SOLAR_SHEET_ID", raising=False)
        assert A.load_invoicing_overview(2026) == {}


class TestRegisterRow:
    def test_upsert_carries_the_split(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "invoice_publish", V2 / "scripts" / "invoice_publish.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sql = mod.register_upsert_sql("GTO1", "2026-08",
                                      {"kwh": 98765.4, "penalty": 1234.5, "income": 195000.12,
                                       "expected": 101234.0},
                                      99999.9, 195000.12, 1.975, 99990.0, 9.9, 0.0099, "OK")
        assert "produced_kwh, penalty_kwh, expected_kwh, source" in sql
        assert "'OK', 98765.4, 1234.5, 101234.0," in sql
        assert "'invoice_publish'" in sql
        assert "expected_kwh = COALESCE(EXCLUDED.expected_kwh, invoicing.expected_kwh)" in sql
        assert "billable_kwh = EXCLUDED.billable_kwh" in sql
        src = (V2 / "scripts" / "invoice_publish.py").read_text(encoding="utf-8")
        assert "psql_exec(ENSURE_SQL)" in src
        assert "WHERE billable_kwh IS NOT NULL" in src     # index skips EXPECTED_ONLY rows
