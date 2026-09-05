"""v193 — Sheets retirement phase 2b: kpi_eod writes daily_production.

Locks: ARGIA_KPI_WRITE defaults to sheet; pg/both route upsert_kpi_rows
and stamp_column through kpi_mirror's protected upsert (only the given
columns change, NULL keeps, vendor rows and closed months protected);
a stamp never creates a skeleton row; a blank text stamp clears; an
unmapped column raises; the sheet branch is untouched in sheet mode.
"""
import pytest

from argia.archive import kpi_daily as KD
from argia.kpi.reconcile import date_key
from argia.store import kpi_write as W


class FakeSheets:
    def __init__(self):
        self.calls = []

    def read_range(self, tab, a1):
        self.calls.append(("read_range", tab, a1))
        return [KD.KPI_DAILY_HEADER]

    def write_row(self, *a):
        self.calls.append(("write_row",) + a)

    def append_rows(self, *a, **k):
        self.calls.append(("append_rows",) + a)

    def batch_write_cells(self, tab, cells):
        self.calls.append(("batch_write_cells", tab, cells))
        return len(cells)


def _row(date_iso="2026-09-03", pk="GTO1", energy=3252.4):
    r = [""] * len(KD.KPI_DAILY_HEADER)
    r[KD.KPI_DAILY_HEADER.index("date_iso")] = date_iso
    r[KD.KPI_DAILY_HEADER.index("plant_key")] = pk
    r[KD.KPI_DAILY_HEADER.index("energy_kwh")] = energy
    r[KD.KPI_DAILY_HEADER.index("pr")] = 0.81
    return r


class TestMode:
    def test_default_pg(self):
        assert W.mode({}) == "pg" and W.writes_pg({}) and not W.writes_sheet({})      # v205

    def test_both_and_pg(self):
        assert W.mode({"ARGIA_KPI_WRITE": "BOTH"}) == "both"
        assert W.writes_pg({"ARGIA_KPI_WRITE": "pg"}) and not W.writes_sheet({"ARGIA_KPI_WRITE": "pg"})
        assert W.mode({"ARGIA_KPI_WRITE": "nonsense"}) == "pg"
        assert W.mode({"ARGIA_KPI_WRITE": "sheet"}) == "sheet"


class TestPure:
    def test_row_lists_become_mirror_rows(self):
        rows = W.rows_from_lists(KD.KPI_DAILY_HEADER, [_row()], date_key)
        assert rows[0]["prod_date"] == "2026-09-03" and rows[0]["plant_key"] == "GTO1"
        assert rows[0]["energy_kwh"] == 3252.4 and rows[0]["pr"] == 0.81
        assert rows[0]["billable_kwh"] is None            # blank -> NULL -> keep

    def test_stamp_rows_carry_only_that_column(self):
        rows = W.rows_from_stamps("cloud_coverage_pct", {("2026-09-03", "gto1"): 61.8}, date_key)
        assert rows == [{"prod_date": "2026-09-03", "plant_key": "GTO1", "cloud_cover_pct": 61.8}]

    def test_stamp_serial_dates_normalised(self):
        rows = W.rows_from_stamps("design_kwh", {(46268, "NL1"): 2764.3}, date_key)
        assert rows[0]["prod_date"] == "2026-09-03"

    def test_blank_text_stamp_clears_but_blank_number_keeps(self):
        rows = W.rows_from_stamps("status_note", {("2026-09-03", "GTO1"): ""}, date_key)
        assert rows[0]["status_note"] == ""
        rows = W.rows_from_stamps("production_pct", {("2026-09-03", "GTO1"): ""}, date_key)
        assert rows[0]["production_pct"] is None

    def test_unmapped_column_raises(self):
        with pytest.raises(KeyError):
            W.rows_from_stamps("no_such_column", {("2026-09-03", "GTO1"): 1}, date_key)

    def test_upsert_sql_is_the_mirrors_with_returning(self):
        sql = W.upsert_sql(W.rows_from_stamps("billable_kwh", {("2026-09-03", "GTO1"): 3252.4}, date_key))
        assert "ON CONFLICT (plant_key, prod_date) DO UPDATE SET" in sql
        assert "vendor daily counter" in sql and "closed_at IS NOT NULL" in sql
        assert sql.rstrip().endswith("RETURNING plant_key, prod_date::text, (xmax = 0) AS inserted;")
        assert "COALESCE(EXCLUDED.energy_kwh, daily_production.energy_kwh)" in sql   # NULL keeps

    def test_only_existing_rows_are_stamped(self, caplog):
        rows = W.rows_from_stamps("design_kwh", {("2026-09-03", "GTO1"): 1, ("2026-09-03", "TAM1"): 2}, date_key)
        kept = W.only_existing(rows, [["GTO1", "2026-09-03"]])
        assert [r["plant_key"] for r in kept] == ["GTO1"]
        assert "no daily_production row for (2026-09-03, TAM1)" in caplog.text
        assert W.existing_keys_sql(rows) == ("SELECT plant_key, prod_date::text FROM daily_production"
                                            " WHERE prod_date IN (DATE '2026-09-03');")


class TestDoors:
    def test_sheet_mode_never_touches_pg(self, monkeypatch):
        monkeypatch.setenv("ARGIA_KPI_WRITE", "sheet")
        monkeypatch.setattr(W, "_run", lambda sql: (_ for _ in ()).throw(AssertionError("PG touched")))
        fs = FakeSheets()
        KD.upsert_kpi_rows(fs, [_row()], dry_run=True)
        KD.stamp_column(fs, "design_kwh", {("2026-09-03", "GTO1"): 1}, dry_run=True)
        assert ("read_range", "KPI_Daily", "A2:N") in fs.calls
        assert ("read_range", "KPI_Daily", "A1:ZZ") in fs.calls

    def test_pg_mode_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_KPI_WRITE", "pg")
        ran = []
        def fake_run(sql):
            ran.append(sql)
            if sql.startswith("SELECT plant_key"):
                return [["GTO1", "2026-09-03"]]
            return [["GTO1", "2026-09-03", "f"]]
        monkeypatch.setattr(W, "_run", fake_run)
        fs = FakeSheets()
        stats = KD.upsert_kpi_rows(fs, [_row()], dry_run=False)
        assert stats == {"inserted": 0, "updated": 1, "unchanged": 0, "failed": 0}
        n = KD.stamp_column(fs, "design_kwh", {("2026-09-03", "GTO1"): 3727.3}, dry_run=False)
        assert n == 1
        assert fs.calls == []
        assert any("INSERT INTO daily_production" in s for s in ran)

    def test_both_mode_writes_pg_then_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_KPI_WRITE", "both")
        ran = []
        monkeypatch.setattr(W, "_run", lambda sql: ran.append(sql) or [["GTO1", "2026-09-03", "t"]])
        fs = FakeSheets()
        KD.upsert_kpi_rows(fs, [_row()], dry_run=False)
        assert ran and ("append_rows", "KPI_Daily", [_row()]) in fs.calls

    def test_dry_run_in_pg_mode_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("ARGIA_KPI_WRITE", "pg")
        ran = []
        monkeypatch.setattr(W, "_run", lambda sql: ran.append(sql) or [["GTO1", "2026-09-03"]])
        KD.upsert_kpi_rows(FakeSheets(), [_row()], dry_run=True)
        KD.stamp_column(FakeSheets(), "design_kwh", {("2026-09-03", "GTO1"): 1}, dry_run=True)
        assert all(s.startswith("SELECT") for s in ran)     # only the existence check


class TestKpiEod:
    def test_sheet_only_steps_are_gated(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "if kpi_write.writes_sheet():\n        try:\n            created = create_kpi_daily_tab_if_missing(sheets)" in src
        assert "and not kpi_write.writes_sheet():" in src


class TestNoSheetCopyBehindTheWriter:
    """v193.1: once daily_production is written on the server, no scheduled
    sheet -> PG copy may run behind it (a later copy is a stale copy)."""

    def test_no_kpi_export_action_and_no_sync_unit_exist(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[3]
        assert not (root / ".github" / "workflows" / "v2-kpi-export.yml").exists()
        assert not (root / "v2" / "server" / "bundle" / "argia-sync.timer").exists()
        assert not (root / "v2" / "server" / "bundle" / "sync_kpi.py").exists()
