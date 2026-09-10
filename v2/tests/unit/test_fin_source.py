"""v250 — source pointers: every row says which file, sheet and row it came
from, and the link goes as deep as the file allows (Tomasz 2026-09-10: click
a 741-day-overdue invoice and land on the document that says so)."""
from __future__ import annotations

import pathlib
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

from argia.fin import portfolio as PF, pmo_sheet as PS, source as SRC   # noqa: E402
from argia.fin import books as B, schema as S                           # noqa: E402

SHEET = SRC.SHEET_MIME
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestLinks:
    def test_a_google_sheet_links_to_the_cell_an_xlsx_only_to_the_file(self):
        assert SRC.url("ID1", SHEET, "Book", "8842", 57) == "https://docs.google.com/spreadsheets/d/ID1/edit#gid=8842&range=A57"
        assert SRC.exact(SHEET, "8842", 57) and not SRC.exact(SHEET, "", 57) and not SRC.exact(SHEET, "8842", 0)
        assert SRC.url("ID1", SHEET, "Book", "8842") == "https://docs.google.com/spreadsheets/d/ID1/edit#gid=8842"      # the tab, for "add a line here"
        assert SRC.url("ID1", SHEET, "Book") == "https://docs.google.com/spreadsheets/d/ID1/edit"
        assert SRC.url("ID2", XLSX, "Tracker.xlsx", "", 89) == "https://drive.google.com/file/d/ID2/view"               # Drive has no cell anchor for a binary
        assert not SRC.exact(XLSX, "1", 89)

    def test_without_a_drive_id_the_link_is_a_drive_search_and_nothing_at_all_is_no_link(self):
        assert SRC.url("", XLSX, "Argia Mexico Payables and receivables 2026_V2.xlsx").startswith("https://drive.google.com/drive/search?q=Argia%20Mexico")
        assert SRC.url("", XLSX, "") == "" and SRC.url(" ", "", "") == ""

    def test_the_label_says_file_sheet_row_and_the_column_that_produced_the_number(self):
        en, es = SRC.where("Tracker.xlsx", "Payables and Receivables.", 89, "Payment Due Day", "2024-08-30")
        assert en == 'Tracker.xlsx · sheet “Payables and Receivables.” · row 89 · column “Payment Due Day” = 2024-08-30'
        assert es.startswith("Tracker.xlsx · hoja «Payables and Receivables.» · fila 89 · columna «Payment Due Day»")
        assert SRC.where("F.xlsx")[0] == "F.xlsx" and SRC.where("F.xlsx", "S")[0] == "F.xlsx · sheet “S”"

    def test_the_books_are_read_only_the_editable_files_name_their_owner(self):
        assert not SRC.editable("polizas") and not SRC.editable("auxiliares") and not SRC.editable("acctbook")
        assert SRC.editable("tracker") and SRC.editable("overview") and SRC.editable("pmo_sheet")
        assert SRC.owner("tracker")[0] == "the office manager" and SRC.owner("polizas")[0] == "the accountants"
        assert SRC.owner("")[0] and SRC.owner("nonsense")[1]

    def test_pointer_assembles_a_row_and_survives_a_missing_source(self):
        row = {"src_name": "ARGIA_PROJECT_QUIJOTE", "src_kind": "pmo_sheet", "src_drive_id": "ID9", "src_mime": SHEET,
               "src_sheet": "Costs", "src_row": "31", "src_gid": "77"}
        p = SRC.pointer(row, "Amount_Before_VAT", "12,000.00")
        assert p["url"].endswith("#gid=77&range=A31") and p["exact"] and p["editable"] and p["kind"] == "pmo_sheet"
        assert "row 31" in p["en"] and "Amount_Before_VAT" in p["en"] and "12,000.00" in p["en"]
        assert SRC.pointer(None) is None and SRC.pointer({}) is None and SRC.pointer({"src_name": "", "src_drive_id": None}) is None
        assert SRC.pointer({"src_name": "F.xlsx", "src_row": "not a number"})["url"].startswith("https://drive.google.com/drive/search")


class TestReadersCarryCoordinates:
    def test_tracker_rows_know_their_sheet_and_row(self):
        rows = [["RECEIVABLES"], ["Status", "Invoice", "Company", "Project", "PO", "Invoice Issued", "Payment Due Day", "Real Status", "Days to Due (Real)", "Total Amount MXN"],
                ["Delay", "A-1", "CLIENT UNO", "1350 Taigene", "PO1", "2024-08-15", "2024-08-30", "Delay", "-741", "1000"]]
        items = PF.read_tracker(rows, "Payables and Receivables.")
        assert len(items) == 1 and items[0].sheet == "Payables and Receivables." and items[0].row == 3      # 1-based, header rows counted
        r = B.open_item_rows("X", items, "sha")[0]
        assert r["src_sheet"] == "Payables and Receivables." and r["src_row"] == 3 and r["src_gid"] == ""

    def test_overview_rows_know_their_row(self):
        rows = [["x"], ["", "", "", "", "", "Id", "Project Name", "Phase", "Invoiced MXN"],
                ["", "", "", "", "", 1350, "1350 Taigene Roof", "3_execution", 100]]
        got = PF.read_overview(rows)
        assert len(got) == 1 and got[0].sheet == "Data" and got[0].row == 3
        assert B.portfolio_rows("X", got, "sha")[0]["src_row"] == 3

    def test_pmo_tasks_costs_and_invoices_know_their_tab_and_row_and_pick_up_the_tab_id(self):
        tabs = {"Summary": [["Project_ID", "ARG1473"], ["Project_Name", "Quijote"]],
                "Tasks": [["Task_ID", "WBS", "Task_Name"], ["T1", "1", "Kickoff"]],
                "Costs": [["hello"], ["Cost_ID", "Vendor", "Amount_Before_VAT"], ["C1", "ACME", 500]],
                "Invoices": [["Invoice_ID", "Invoice_Amount"], ["I1", 900]]}
        p = PS.read_project(tabs)
        assert p.tasks[0].sheet == "Tasks" and p.tasks[0].row == 2
        assert p.costs[0].sheet == "Costs" and p.costs[0].row == 3            # the Costs header is on row 2
        assert p.invoices[0].sheet == "Invoices" and p.invoices[0].row == 2
        _proj, tasks, costs, invs = B.pmo_rows("X", p, "sha", "ID9", None, {"Tasks": "11", "Costs": "22", "Invoices": "33"})
        assert (tasks[0]["src_sheet"], tasks[0]["src_gid"]) == ("Tasks", "11")
        assert (costs[0]["src_sheet"], costs[0]["src_row"], costs[0]["src_gid"]) == ("Costs", 3, "22")
        assert invs[0]["src_gid"] == "33"
        assert B.pmo_rows("X", p, "sha")[2][0]["src_gid"] == ""               # no tab ids known (a local mirror) — still fine


class TestSchemaMigration:
    def test_every_declared_column_is_also_in_the_create_table_so_fresh_and_migrated_agree(self):
        for table, col, decl in S.ADD_COLUMNS:
            assert f"CREATE TABLE IF NOT EXISTS {table} " in S.ENSURE_SQL, table
            body = S.ENSURE_SQL.split(f"CREATE TABLE IF NOT EXISTS {table} ", 1)[1].split(");", 1)[0]
            assert col in body, f"{table}.{col} declared in ADD_COLUMNS but not in the table"
            assert decl.split()[0] in body
        assert S.MIGRATE_SQL.count("ADD COLUMN IF NOT EXISTS") == len(S.ADD_COLUMNS)
        assert "ALTER TABLE open_item ADD COLUMN IF NOT EXISTS src_row integer NOT NULL DEFAULT 0;" in S.MIGRATE_SQL

    def test_touching_a_skipped_file_refreshes_only_the_pointer(self):
        sql = B.source_touch_sql("abc", "DRIVE1", SRC.SHEET_MIME)
        assert sql.startswith("UPDATE fin_source_file SET") and "'DRIVE1'" in sql and "WHERE sha256 = 'abc'" in sql
        assert "rows" not in sql and "imported_at" not in sql          # nothing about the content changes
        assert "coalesce" in B.source_touch_sql("abc", "", "")         # an empty id never wipes a known one
