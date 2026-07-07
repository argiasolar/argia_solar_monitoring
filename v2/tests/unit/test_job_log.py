"""Tests: SyncRuns job logging (argia.core.job_log).

User request 2026-07-07: the sheet showed telemetry timestamps only —
no way to know when the dashboard/KPI/alerts last ran. Every scheduled
job now appends a SyncRuns row, including a FAILED row on crash.
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest

from argia.core import job_log
from argia.core.job_log import apply_flag_write_if, instrument


def run_instrumented(main, argv, env=True):
    rows = []
    fake_sheets = MagicMock()
    fake_sheets.append_rows.side_effect = \
        lambda tab, r: rows.append((tab, r[0]))
    with patch("argia.core.sheets.SheetsClient", return_value=fake_sheets):
        with patch.dict("os.environ",
                        {"GOOGLE_SHEET_ID_V2": "sheet1" if env else ""}):
            wrapped = instrument("testjob")(main)
            try:
                rc = wrapped(argv)
            except Exception as e:
                return rows, e
            return rows, rc


class TestInstrument:
    def test_ok_row_schema(self):
        rows, rc = run_instrumented(lambda argv: 0, [])
        assert rc == 0 and len(rows) == 1
        tab, row = rows[0]
        assert tab == "SyncRuns" and len(row) == 8
        run_id, start, end, script, status, p, r, err = row
        assert script == "testjob" and status == "OK" and err == ""
        assert start <= end                      # ISO strings compare
        assert run_id.count("-") >= 2            # epoch-hex-host

    def test_dry_run_writes_nothing(self):
        rows, rc = run_instrumented(lambda argv: 0, ["--dry-run"])
        assert rc == 0 and rows == []

    def test_exception_logs_failed_and_reraises(self):
        def boom(argv):
            raise RuntimeError("vendor exploded")
        rows, exc = run_instrumented(boom, [])
        assert isinstance(exc, RuntimeError)      # passed through
        assert rows[0][1][4] == "FAILED"
        assert "vendor exploded" in rows[0][1][7]

    def test_nonzero_exit_code_logs_failed(self):
        rows, rc = run_instrumented(lambda argv: 3, [])
        assert rc == 3
        assert rows[0][1][4] == "FAILED"
        assert "exit code 3" in rows[0][1][7]

    def test_logging_failure_never_breaks_the_job(self):
        with patch("argia.core.job_log._append_row",
                   side_effect=RuntimeError("sheets down")):
            wrapped = instrument("t")(lambda argv: 0)
            assert wrapped([]) == 0               # job survives

    def test_missing_sheet_env_skips_quietly(self):
        rows, rc = run_instrumented(lambda argv: 0, [], env=False)
        assert rc == 0 and rows == []

    def test_apply_flag_gating(self):
        assert apply_flag_write_if(["--apply"]) is True
        assert apply_flag_write_if([]) is False


class TestAllJobsAreWired:
    @pytest.mark.parametrize("module,job,needs_apply", [
        ("scripts.dashboard_update", "dashboard_update", True),
        ("scripts.dashboard_html_publish", "dashboard_publish", True),
        ("scripts.alerts_snapshot", "alerts_snapshot", False),
        ("scripts.alerts_daily", "alerts_daily", False),
        ("scripts.kpi_eod", "kpi_eod", False),
        ("scripts.report_daily", "report_daily", False),
    ])
    def test_main_is_instrumented(self, module, job, needs_apply):
        m = importlib.import_module(module)
        assert getattr(m.main, "_job_log_script", None) == job, \
            f"{module}.main is not instrumented"
        gate = m.main._job_log_write_if
        assert gate(["--apply"]) is True
        assert gate([] if needs_apply else ["--dry-run"]) is False
