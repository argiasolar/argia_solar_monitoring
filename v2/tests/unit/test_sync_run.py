"""v188 — the run log moves from the SyncRuns sheet tab to PostgreSQL.

Locks: the SQL is built correctly and safely from the SyncRuns-shaped row
both writers already produce; the sheet tab is OFF by default and only
returns behind ARGIA_SHEET_JOBLOG; PG logging is best-effort and never
raises; nothing in the live tree reads the SyncRuns tab.
"""
import logging
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from argia.core import job_log
from argia.store import sync_run

V2 = pathlib.Path(__file__).resolve().parents[2]
ROW = ["1788480000-abc123-pio06", "2026-09-04T01:00:00+00:00",
       "2026-09-04T01:00:41+00:00", "telemetry_5m", "OK", 9, 23, ""]


class TestBuildInsertSql:
    def test_full_row(self):
        sql = sync_run.build_insert_sql(ROW, host="pio06")
        assert sql.startswith("INSERT INTO sync_run (")
        assert "'telemetry_5m'" in sql and "'OK'" in sql
        assert "'2026-09-04T01:00:00+00:00'::timestamptz" in sql
        assert ",9,23," in sql
        assert "'pio06'" in sql

    def test_quotes_are_escaped(self):
        row = list(ROW); row[7] = "it's broken: O'Reilly"
        sql = sync_run.build_insert_sql(row, host="h")
        assert "it''s broken: O''Reilly" in sql
        assert sql.count("INSERT") == 1

    def test_short_row_from_job_log_is_accepted(self):
        # job_log.instrument writes 8 columns with counts 0,0
        sql = sync_run.build_insert_sql(
            ["rid", "2026-09-04T00:00:00+00:00", "2026-09-04T00:00:01+00:00",
             "kpi_eod", "FAILED", 0, 0, "exit code 1"], host="h")
        assert "'FAILED'" in sql and "'exit code 1'" in sql

    def test_blank_timestamps_become_null(self):
        row = list(ROW); row[1] = ""; row[2] = None
        sql = sync_run.build_insert_sql(row, host="h")
        assert "NULL,NULL" in sql

    def test_unusable_rows_are_refused_not_written(self):
        assert sync_run.build_insert_sql(None) is None
        assert sync_run.build_insert_sql(["only", "two"]) is None
        assert sync_run.build_insert_sql(["", "", "", "x", "OK"]) is None

    def test_error_text_is_capped(self):
        row = list(ROW); row[7] = "e" * 5000
        assert len(sync_run.build_insert_sql(row, host="h")) < 2600

    def test_ensure_sql_is_idempotent_by_construction(self):
        assert "CREATE TABLE IF NOT EXISTS sync_run" in sync_run.ENSURE_SQL
        assert "CREATE INDEX IF NOT EXISTS" in sync_run.ENSURE_SQL


class TestRecordIsBestEffort:
    def test_off_the_server_it_is_a_quiet_noop(self, monkeypatch):
        monkeypatch.delenv("ARGIA_PG_MIRROR", raising=False)
        with patch("argia.store.pgq.psql_exec") as ex:
            assert sync_run.record(ROW) is False
            ex.assert_not_called()

    def test_on_the_server_it_ensures_then_inserts(self, monkeypatch):
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        with patch("argia.store.pgq.psql_exec") as ex:
            assert sync_run.record(ROW) is True
            sql = ex.call_args[0][0]
            assert "CREATE TABLE IF NOT EXISTS sync_run" in sql
            assert "INSERT INTO sync_run" in sql

    def test_a_db_failure_never_raises(self, monkeypatch):
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        log = MagicMock(spec=logging.Logger)
        with patch("argia.store.pgq.psql_exec",
                   side_effect=RuntimeError("psql failed")):
            assert sync_run.record(ROW, log) is False
        assert log.warning.called


class TestSheetTabIsOffByDefault:
    def test_switch_defaults_off(self):
        assert job_log.sheet_joblog_enabled({}) is False
        assert job_log.sheet_joblog_enabled({"ARGIA_SHEET_JOBLOG": "1"}) is True
        assert job_log.sheet_joblog_enabled({"ARGIA_SHEET_JOBLOG": "off"}) is False

    def test_default_writes_pg_and_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.delenv("ARGIA_SHEET_JOBLOG", raising=False)
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        monkeypatch.setenv("GOOGLE_SHEET_ID_V2", "s1")
        with patch("argia.store.pgq.psql_exec") as ex, \
             patch("argia.core.sheets.SheetsClient") as sc:
            job_log._append_row(ROW)
            ex.assert_called_once()
            sc.assert_not_called()

    def test_switch_on_writes_both(self, monkeypatch):
        monkeypatch.setenv("ARGIA_SHEET_JOBLOG", "1")
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        monkeypatch.setenv("GOOGLE_SHEET_ID_V2", "s1")
        fake = MagicMock()
        with patch("argia.store.pgq.psql_exec") as ex, \
             patch("argia.core.sheets.SheetsClient", return_value=fake):
            job_log._append_row(ROW)
            ex.assert_called_once()
            fake.append_rows.assert_called_once()

    def test_no_sink_at_all_warns_instead_of_failing(self, monkeypatch, caplog):
        monkeypatch.delenv("ARGIA_SHEET_JOBLOG", raising=False)
        monkeypatch.delenv("ARGIA_PG_MIRROR", raising=False)
        with caplog.at_level(logging.WARNING, logger="argia.core.job_log"):
            job_log._append_row(ROW)
        assert "no run-log sink" in caplog.text

    def test_telemetry_writer_uses_the_same_switch(self):
        src = (V2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
        assert "sync_run.record(row, log)" in src
        assert "if not sheet_joblog_enabled():" in src


class TestNothingReadsSyncRuns:
    def test_no_live_reader_of_the_tab(self):
        """The tab may be switched off because nothing reads it back —
        keep it that way."""
        import re
        for p in list((V2 / "scripts").glob("*.py")) + \
                 list((V2 / "argia").rglob("*.py")):
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"read_(?:table|range)\(\s*([^,)]+)", src):
                arg = m.group(1).strip()
                assert arg not in ('"SyncRuns"', "'SyncRuns'", "SYNC_TAB",
                                   "TAB_SYNC"), f"{p.name} reads SyncRuns"
