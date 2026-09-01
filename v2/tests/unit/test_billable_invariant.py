"""The billable invariant — the 2026-09-01 August-close finding.

The nightly self-heal raised ``energy_kwh`` from vendor counters but
left ``billable_kwh`` stamped from the undercounted value, and the
invoice annex bills ``billable``.  For August that quietly shrank the
six PPA invoices by ~17.8 MWh (~39,600 MXN) — precisely the failure the
billing doctrine ("a collection gap can never shrink an invoice")
exists to prevent.

Two layers under test here:

  * build_billable_resync_sql — the nightly invariant restorer, run in
    recon_snapshot right after the PR resync it mirrors;
  * kpi_sheet_month_sync.plan_cells — mirroring a CLOSED month's
    energy+billable into the KPI sheet, fail-closed per plant.

Both are exercised against a real sqlite/in-memory model of the rows,
not by string-matching the SQL alone: never-lowers is a property of the
WHERE clause, so the WHERE clause is what gets executed.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))

from argia.recon import backfill as B                      # noqa: E402
from kpi_sheet_month_sync import plan_cells                # noqa: E402


def run_resync(rows):
    """Execute the real SQL against sqlite; return the resulting rows.

    sqlite accepts the statement's postgres-compatible subset once the
    trim() call is adapted; assert the adaptation touched nothing else.
    """
    sql = B.build_billable_resync_sql()
    lite = sql.replace("trim(", "ltrim(")      # sqlite has no 1-arg trim…
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily_production (plant_key TEXT,"
              " prod_date TEXT, energy_kwh REAL, billable_kwh REAL,"
              " status_note TEXT DEFAULT '')")
    c.executemany("INSERT INTO daily_production VALUES (?,?,?,?,?)", rows)
    c.execute(lite)
    out = c.execute("SELECT plant_key, prod_date, energy_kwh,"
                    " billable_kwh, status_note"
                    " FROM daily_production ORDER BY 1,2").fetchall()
    c.close()
    return out


class TestBillableResyncSql:
    def test_stale_billable_is_lifted_to_energy(self):
        out = run_resync([("GTO1", "2026-08-20", 4833.0, 4160.4, "")])
        assert out[0][3] == 4833.0

    def test_a_deemed_day_is_never_touched(self):
        """billable > energy is a stamped deemed day — sacred."""
        out = run_resync([("SLP2", "2026-08-03", 1460.2, 1650.1, "x")])
        assert out[0][3] == 1650.1
        assert out[0][4] == "x"

    def test_equal_rows_are_left_alone(self):
        out = run_resync([("MEX1", "2026-08-01", 100.0, 100.0, "n")])
        assert out[0][3] == 100.0 and out[0][4] == "n"

    def test_null_billable_is_not_invented(self):
        """billable NULL means the deemed engine has not stamped the day
        — inventing a value here would bill unstamped data."""
        out = run_resync([("NL1", "2026-08-05", 3000.0, None, "")])
        assert out[0][3] is None

    def test_null_energy_changes_nothing(self):
        out = run_resync([("NL1", "2026-08-06", None, 2000.0, "")])
        assert out[0][3] == 2000.0

    def test_it_is_idempotent(self):
        rows = [("GTO1", "2026-08-20", 4833.0, 4160.4, "")]
        once = run_resync(rows)
        again = run_resync([tuple(r) for r in once])
        assert once == again

    def test_provenance_is_stamped_on_touched_rows_only(self):
        out = run_resync([("GTO1", "2026-08-20", 4833.0, 4160.4, "old"),
                          ("GTO1", "2026-08-21", 100.0, 100.0, "keep")])
        assert "billable lifted to corrected energy" in out[0][4]
        assert out[1][4] == "keep"

    def test_never_lowers_is_in_the_where_not_in_hope(self):
        sql = B.build_billable_resync_sql()
        assert re.search(r"WHERE.*billable_kwh\s*<\s*energy_kwh", sql,
                         re.S)

    def test_the_nightly_job_calls_it_after_the_pr_resync(self):
        src = (V2 / "scripts" / "recon_snapshot.py").read_text(
            encoding="utf-8")
        i = src.index("build_pr_resync_sql()")
        assert "build_billable_resync_sql()" in src[i:]
        assert "if not args.dry_run" in src[:i]


HEADER = ["plant_key", "date_iso", "energy_kwh", "billable_kwh",
          "status_note"]


def sheet_row(pk, d, e, b, note=""):
    return [pk, d, e, b, note]


class TestSheetMonthSync:
    PG = {("GTO1", "2026-08-20"): (4833.0, 4833.0),
          ("SLP2", "2026-08-03"): (1460.2, 1460.2)}

    def test_a_closed_plant_gets_both_columns_written(self):
        cells, skipped, changed, _ = plan_cells(
            HEADER, [sheet_row("GTO1", "2026-08-20", 4160.4, 4160.4)],
            {("GTO1", "2026-08-20"): (4833.0, 4833.0)},
            {"GTO1"}, "closenote")
        assert changed == 1 and not skipped
        vals = {(r, c): v for r, c, v in cells}
        assert vals[(2, 3)] == 4833.0          # energy col, 1-indexed
        assert vals[(2, 4)] == 4833.0          # billable col

    def test_an_open_plant_is_never_written_fail_closed(self):
        cells, skipped, changed, _ = plan_cells(
            HEADER, [sheet_row("GTO1", "2026-08-20", 4160.4, 4160.4)],
            {("GTO1", "2026-08-20"): (4833.0, 4833.0)},
            set(), "note")
        assert cells == [] and changed == 0
        assert skipped == {"GTO1"}

    def test_the_close_may_lower_the_sheet(self):
        """SLP2 03/08: the vendor-authoritative export lowered energy;
        the sheet's stale billable must follow the CLOSED value down."""
        cells, _, changed, _ = plan_cells(
            HEADER, [sheet_row("SLP2", "2026-08-03", 1460.2, 1650.1)],
            {("SLP2", "2026-08-03"): (1460.2, 1460.2)},
            {"SLP2"}, "note")
        vals = {(r, c): v for r, c, v in cells}
        assert vals[(2, 4)] == 1460.2

    def test_matching_rows_are_untouched(self):
        cells, _, changed, unchanged = plan_cells(
            HEADER, [sheet_row("GTO1", "2026-08-20", 4833.0, 4833.0)],
            {("GTO1", "2026-08-20"): (4833.0, 4833.0)},
            {"GTO1"}, "note")
        assert cells == [] and unchanged == 1

    def test_a_tiny_float_wobble_is_not_a_change(self):
        cells, _, changed, _ = plan_cells(
            HEADER, [sheet_row("GTO1", "2026-08-20", 4833.001, 4833.0)],
            {("GTO1", "2026-08-20"): (4833.0, 4833.0)},
            {"GTO1"}, "note")
        assert cells == []

    def test_the_note_is_appended_once_not_stacked(self):
        row = sheet_row("GTO1", "2026-08-20", 4160.4, 4160.4,
                        "prior | closenote")
        cells, _, changed, _ = plan_cells(
            HEADER, [row], {("GTO1", "2026-08-20"): (4833.0, 4833.0)},
            {"GTO1"}, "closenote")
        notes = [v for r, c, v in cells if c == 5]
        assert notes == []                     # already carries the note

    def test_a_missing_billable_in_pg_writes_energy_only(self):
        cells, _, changed, _ = plan_cells(
            HEADER, [sheet_row("NL1", "2026-08-05", 2900.0, "")],
            {("NL1", "2026-08-05"): (3000.0, None)},
            {"NL1"}, "note")
        cols = {c for r, c, v in cells if c in (3, 4)}
        assert cols == {3}

    def test_a_header_without_billable_refuses_loudly(self):
        with pytest.raises(ValueError):
            plan_cells(["plant_key", "date_iso", "energy_kwh"],
                       [], {}, set(), "note")


class TestImportWritersRespectTheClose:
    """The 2026-09-01 afternoon incident: argia-sync re-imported a
    pre-repair CSV 45 minutes after the August close and silently
    reverted billable_kwh on closed rows. Both import writers now (a)
    protect billable like energy on vendor-provenance rows and (b)
    freeze EVERY column of any row whose month has a closed
    reconciliation."""

    SYNC = (V2 / "server" / "bundle" / "sync_kpi.py").read_text(
        encoding="utf-8")

    def test_sync_protects_billable(self):
        import re
        m = re.search(r"protected = \{([^}]*)\}", self.SYNC)
        assert m and "billable_kwh" in m.group(1)

    def test_sync_freezes_closed_months_on_every_column(self):
        assert "rm.closed_at IS NOT NULL" in self.SYNC
        i = self.SYNC.index("for c in cols:")
        loop = self.SYNC[i:i + 700]
        # the frozen CASE wraps every appended column expression
        assert "CASE WHEN {frozen}" in loop
        assert "parts.append(f'{c}=CASE WHEN {frozen}" in loop

    def test_the_mirror_twin_is_covered_too(self):
        src = (V2 / "argia" / "store" / "kpi_mirror.py").read_text(
            encoding="utf-8")
        assert '"billable_kwh"' in src.split("PROTECTED = ")[1][:120]
        assert "rm.closed_at IS NOT NULL" in src
