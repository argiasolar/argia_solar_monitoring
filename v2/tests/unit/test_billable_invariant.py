"""The billable invariant: billable_kwh >= energy_kwh wherever both are
stamped (deemed only ADDS). Covers the resync SQL and the import writers
that must respect a CLOSED month."""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))

from argia.recon import backfill as B                      # noqa: E402


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


class TestImportWritersRespectTheClose:
    """The 2026-09-01 afternoon incident: argia-sync re-imported a
    pre-repair CSV 45 minutes after the August close and silently
    reverted billable_kwh on closed rows. The sync writer went with
    v207.1 (sheets retired); the remaining writer — the protected upsert
    in kpi_mirror used by kpi_write — (a) protects billable like energy
    on vendor-provenance rows and (b) freezes EVERY column of any row
    whose month has a closed reconciliation."""

    def test_the_kpi_writer_protects_billable_and_freezes_closed_months(self):
        src = (V2 / "argia" / "store" / "kpi_mirror.py").read_text(
            encoding="utf-8")
        assert '"billable_kwh"' in src.split("PROTECTED = ")[1][:120]
        assert "rm.closed_at IS NOT NULL" in src
