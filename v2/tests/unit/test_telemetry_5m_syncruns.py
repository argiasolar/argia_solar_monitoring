"""Unit tests for telemetry_5m.py's SyncRuns observability addition.

Scope: the `_finalize_and_log_run` helper and the `SYNC_RUNS_HEADER`
constant. These tests do NOT exercise the actual vendor pipelines —
those have their own tests.

The tests verify:
  - Status transitions (OK / PARTIAL / FAILED)
  - Sheet write format (tab name, header, 8-column row layout)
  - Header regression against the sheet's actual column layout
  - Dry-run behaviour (no append, but result still finalized)
  - Error tolerance (a SyncRuns write failure never propagates)
  - Error message formatting in the row payload

Run:
  cd v2/
  PYTHONPATH=. python -m pytest tests/unit/test_telemetry_5m_syncruns.py -v
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

# scripts/ isn't a Python package in this repo, so make telemetry_5m
# importable for tests via path injection. Stays scoped to this file.
_REPO_V2 = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_V2 / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import telemetry_5m  # noqa: E402  (must follow sys.path manipulation)

from argia.core.time_utils import now_utc  # noqa: E402
from argia.orchestrator import TAB_SYNC, RunResult  # noqa: E402


# ============================================================
# Fakes / fixtures
# ============================================================


class FakeSheets:
    """In-memory SheetsClient stand-in.

    Mirrors the surface area `_finalize_and_log_run` uses:
        - ensure_tab(tab)
        - ensure_header(tab, header)
        - append_rows(tab, rows)
    """

    def __init__(self) -> None:
        self.tabs: Dict[str, List[List]] = {}
        self.ensure_tab_calls: List[str] = []
        self.ensure_header_calls: List[tuple] = []
        self.append_calls: List[tuple] = []
        # Optional fault injection (None = no fault)
        self.fail_ensure_tab: Exception | None = None
        self.fail_ensure_header: Exception | None = None
        self.fail_append: Exception | None = None

    def ensure_tab(self, tab: str) -> None:
        self.ensure_tab_calls.append(tab)
        if self.fail_ensure_tab is not None:
            raise self.fail_ensure_tab
        self.tabs.setdefault(tab, [])

    def ensure_header(self, tab: str, header: List[str]) -> None:
        self.ensure_header_calls.append((tab, list(header)))
        if self.fail_ensure_header is not None:
            raise self.fail_ensure_header

    def append_rows(self, tab: str, rows: List[List]) -> int:
        self.append_calls.append((tab, [list(r) for r in rows]))
        if self.fail_append is not None:
            raise self.fail_append
        self.tabs.setdefault(tab, []).extend(rows)
        return len(rows)


@pytest.fixture
def silent_log():
    log = logging.getLogger("test_telemetry_5m_syncruns")
    log.setLevel(logging.CRITICAL)
    return log


@pytest.fixture
def fresh_result():
    return RunResult(
        run_id="test-run-1",
        started_at_utc=now_utc(),
        script="telemetry_5m",
    )


@pytest.fixture
def sheets():
    return FakeSheets()


# ============================================================
# Status transitions
# ============================================================


class TestFinalizeStatus:
    """RunResult.finalize() is the authority on status. We're verifying
    the helper passes the right counters in so the status is right."""

    def test_status_ok_when_no_errors(self, fresh_result, sheets, silent_log):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        assert fresh_result.status == "OK"
        assert fresh_result.plants_processed == 10
        assert fresh_result.plants_skipped == 0
        assert fresh_result.rows_written == 30
        assert fresh_result.errors == []
        assert fresh_result.finished_at_utc is not None

    def test_status_partial_when_processed_and_errors(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=8, total_skipped=2, total_errors=3,
            rows_collected=24, dry_run=False, log=silent_log,
        )
        assert fresh_result.status == "PARTIAL"
        assert len(fresh_result.errors) == 1
        assert "3 non-fatal error" in fresh_result.errors[0]

    def test_status_failed_when_no_plants_processed_and_errors(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=0, total_skipped=10, total_errors=1,
            rows_collected=0, dry_run=False, log=silent_log,
        )
        assert fresh_result.status == "FAILED"

    def test_status_ok_when_nothing_to_process(
        self, fresh_result, sheets, silent_log,
    ):
        """Edge case: filter matched no plants. Not an error condition."""
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=0, total_skipped=0, total_errors=0,
            rows_collected=0, dry_run=False, log=silent_log,
        )
        assert fresh_result.status == "OK"
        assert fresh_result.rows_written == 0


# ============================================================
# Sheet write format
# ============================================================


class TestSheetWrite:
    def test_appends_correctly_shaped_row(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        assert sheets.ensure_tab_calls == [TAB_SYNC]
        assert sheets.ensure_header_calls == [
            (TAB_SYNC, telemetry_5m.SYNC_RUNS_HEADER)
        ]
        assert len(sheets.append_calls) == 1
        tab, rows = sheets.append_calls[0]
        assert tab == TAB_SYNC
        assert len(rows) == 1
        row = rows[0]
        # 8 columns: run_id, started, finished, script, status,
        # plants_processed, rows_written, errors_json
        assert len(row) == 8
        assert row[0] == "test-run-1"
        assert row[3] == "telemetry_5m"
        assert row[4] == "OK"
        assert row[5] == 10
        assert row[6] == 30
        assert row[7] == ""  # no errors -> empty string

    def test_partial_run_row_has_error_summary(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=5, total_skipped=0, total_errors=7,
            rows_collected=15, dry_run=False, log=silent_log,
        )
        _, rows = sheets.append_calls[0]
        row = rows[0]
        assert row[4] == "PARTIAL"
        assert row[5] == 5
        assert row[6] == 15
        assert "7 non-fatal error" in row[7]


# ============================================================
# Header regression
# ============================================================


class TestHeaderRegression:
    """The SyncRuns header here MUST match the actual sheet's columns,
    otherwise rows land in the wrong cells. Pin this explicitly so
    a future edit can't drift silently."""

    def test_header_matches_sheet_columns(self):
        assert telemetry_5m.SYNC_RUNS_HEADER == [
            "run_id",
            "started_at_utc",
            "finished_at_utc",
            "script",
            "status",
            "plants_processed",
            "rows_written",
            "errors_json",
        ]


# ============================================================
# Dry-run behaviour
# ============================================================


class TestDryRun:
    def test_dry_run_does_not_append(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=True, log=silent_log,
        )
        assert sheets.append_calls == []

    def test_dry_run_still_finalizes_result(
        self, fresh_result, sheets, silent_log,
    ):
        """Even in dry-run, the in-memory RunResult should be finalized so
        the caller can log it to stdout / use it for the return code."""
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=True, log=silent_log,
        )
        assert fresh_result.status == "OK"
        assert fresh_result.finished_at_utc is not None
        assert fresh_result.rows_written == 30


# ============================================================
# Error tolerance
# ============================================================


class TestErrorTolerance:
    """The whole purpose of SyncRuns is observability. A bug in the
    SyncRuns write itself must not turn a successful telemetry run red."""

    def test_swallows_ensure_tab_failure(
        self, fresh_result, sheets, silent_log,
    ):
        sheets.fail_ensure_tab = RuntimeError("network down")
        # Must not raise
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        # Append must NOT be called if we couldn't ensure the tab
        assert sheets.append_calls == []
        # Result is still finalized — caller's logic doesn't break
        assert fresh_result.status == "OK"

    def test_swallows_ensure_header_failure(
        self, fresh_result, sheets, silent_log,
    ):
        sheets.fail_ensure_header = RuntimeError("permission denied")
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        assert sheets.append_calls == []
        assert fresh_result.status == "OK"

    def test_swallows_append_failure(
        self, fresh_result, sheets, silent_log,
    ):
        sheets.fail_append = RuntimeError("rate limit 429")
        # Must not raise
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        # We tried to append once; the failure was swallowed
        assert len(sheets.append_calls) == 1
        assert fresh_result.status == "OK"


# ============================================================
# Error message content
# ============================================================


class TestErrorMessage:
    def test_error_summary_includes_count(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=5, total_skipped=0, total_errors=12,
            rows_collected=15, dry_run=False, log=silent_log,
        )
        assert len(fresh_result.errors) == 1
        assert "12 non-fatal error" in fresh_result.errors[0]
        row = fresh_result.to_sheet_row()
        assert "12 non-fatal error" in row[7]

    def test_no_error_message_when_zero_errors(
        self, fresh_result, sheets, silent_log,
    ):
        telemetry_5m._finalize_and_log_run(
            result=fresh_result, sheets=sheets,
            total_processed=10, total_skipped=0, total_errors=0,
            rows_collected=30, dry_run=False, log=silent_log,
        )
        assert fresh_result.errors == []


# ============================================================
# Module-level invariants
# ============================================================


class TestModuleSurface:
    """Pin the public surface so the imports in main() don't drift."""

    def test_helper_callable(self):
        assert callable(telemetry_5m._finalize_and_log_run)

    def test_header_is_list_of_8_strings(self):
        assert isinstance(telemetry_5m.SYNC_RUNS_HEADER, list)
        assert len(telemetry_5m.SYNC_RUNS_HEADER) == 8
        assert all(isinstance(c, str) for c in telemetry_5m.SYNC_RUNS_HEADER)
