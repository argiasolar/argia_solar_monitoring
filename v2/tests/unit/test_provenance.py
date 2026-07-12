"""Provenance layer tests.

The completeness tests are the enforcement of the house rule: every
column on a finance tab must state its source, and reports citing a tab
must find provenance for it. Adding a column without documenting where
its data comes from fails the suite.
"""

from unittest.mock import MagicMock, patch

import pytest

from argia.core.sheets import SheetsClient
from argia.finance.contract import CONTRACT_HEADER
from argia.finance.loans import LOANS_HEADER, SCHEDULE_HEADER
from argia.finance.provenance import (
    COLUMN_NOTES, NOTES_MARKER, NOTES_SECTION, report_sources,
)
from argia.maintenance.events import MAINTENANCE_EVENTS_HEADER


class TestCompleteness:
    def test_every_loans_column_documented(self):
        assert set(LOANS_HEADER) <= set(COLUMN_NOTES["Loans"])

    def test_every_schedule_column_documented(self):
        assert set(SCHEDULE_HEADER) <= set(COLUMN_NOTES["Loan_Schedule"])

    def test_every_contract_column_documented(self):
        assert set(CONTRACT_HEADER) <= set(COLUMN_NOTES["Contract_Monthly"])

    def test_every_maintenance_column_documented(self):
        # drift guard: adding a Maintenance_Events column without a
        # provenance note fails here (the setup script enforces the same).
        assert set(MAINTENANCE_EVENTS_HEADER) <= set(
            COLUMN_NOTES["Maintenance_Events"])

    def test_om_manual_input_is_labelled_as_such(self):
        note = COLUMN_NOTES["Plants"]["om_cost_monthly_mxn"]
        assert "MANUAL" in note.upper()
        # v91: also flagged as superseded by events
        assert "SUPERSEDED" in note.upper()

    def test_notes_section_carries_marker_and_key_facts(self):
        assert NOTES_MARKER in NOTES_SECTION
        joined = " ".join(NOTES_SECTION)
        # the facts an auditor must find in-sheet
        for fragment in ("LoanPayments", "ContractData", "17.98",
                         "principal+interest", "energia compensada",
                         "phase-2",
                         # v91 drift guard: the deemed formula + O&M source
                         "deemed_day", "daylight_fraction",
                         "Maintenance_Events"):
            assert fragment in joined, fragment

    def test_report_sources_returns_and_raises(self):
        src = report_sources("Loans", "Contract_Monthly")
        assert "tariff_mxn" in src["Contract_Monthly"]
        with pytest.raises(KeyError):
            report_sources("NoSuchTab")


class TestSetHeaderNotes:
    def _client(self):
        with patch("argia.core.sheets.build") as mock_build, \
             patch("argia.core.sheets.Credentials"):
            mock_build.return_value = MagicMock()
            c = SheetsClient(sheet_id="test",
                             credentials_json='{"type":"service_account"}')
            c._svc = MagicMock()
        return c

    def test_sets_notes_on_matching_headers(self):
        c = self._client()
        c.read_range = MagicMock(return_value=[["loan_id", "bank"]])
        c._tab_gid = MagicMock(return_value=7)
        n = c.set_header_notes("Loans", {"loan_id": "a", "bank": "b"})
        assert n == 2
        body = (c._svc.spreadsheets.return_value.batchUpdate
                .call_args[1]["body"])
        reqs = body["requests"]
        assert len(reqs) == 2
        assert all(r["updateCells"]["fields"] == "note" for r in reqs)
        assert reqs[0]["updateCells"]["range"]["sheetId"] == 7

    def test_header_match_is_case_insensitive(self):
        c = self._client()
        c.read_range = MagicMock(return_value=[["Loan_ID"]])
        c._tab_gid = MagicMock(return_value=1)
        assert c.set_header_notes("Loans", {"loan_id": "x"}) == 1

    def test_unknown_columns_skipped_not_fatal(self):
        c = self._client()
        c.read_range = MagicMock(return_value=[["loan_id"]])
        c._tab_gid = MagicMock(return_value=1)
        n = c.set_header_notes("Loans", {"loan_id": "a", "ghost": "b"})
        assert n == 1

    def test_no_matches_makes_no_api_call(self):
        c = self._client()
        c.read_range = MagicMock(return_value=[["other"]])
        c._tab_gid = MagicMock(return_value=1)
        assert c.set_header_notes("Loans", {"ghost": "b"}) == 0
        c._svc.spreadsheets.return_value.batchUpdate.assert_not_called()
