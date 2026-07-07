"""Tests: report date resolution (--when) and Report_Outbox queueing.

Contract:
* --date always wins; --when today/yesterday resolves in MX local time
* a successful upload appends exactly one outbox row with an EMPTY
  notified_at (the Apps Script notifier's claim column)
* dry runs never touch the outbox
* an outbox failure must never fail the report (mail is best-effort;
  the Drive upload is the deliverable)
"""

import datetime as dt
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

from argia.core.sheets import SheetsClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
RD = importlib.import_module("report_daily")


class TestResolveReportDate:
    NOW = dt.datetime(2026, 7, 6, 7, 5)   # 07:05 MX

    def test_explicit_date_wins_over_when(self):
        assert RD.resolve_report_date("2026-07-01", "yesterday", self.NOW) \
            == "2026-07-01"

    def test_when_today(self):
        assert RD.resolve_report_date(None, "today", self.NOW) == "2026-07-06"

    def test_when_yesterday(self):
        assert RD.resolve_report_date(None, "yesterday", self.NOW) \
            == "2026-07-05"

    def test_yesterday_across_month_boundary(self):
        now = dt.datetime(2026, 8, 1, 7, 5)
        assert RD.resolve_report_date(None, "yesterday", now) == "2026-07-31"


class TestOutbox:
    def test_append_outbox_row_shape(self):
        sheets = MagicMock(spec=SheetsClient)
        RD.append_outbox(sheets, date_iso="2026-07-05",
                         kind="morning_yesterday",
                         pdf_file_id="PDF123", html_file_id="HTML456",
                         now_utc_iso="2026-07-06T13:07:00Z")
        sheets.ensure_tab.assert_called_once_with("Report_Outbox")
        sheets.ensure_header.assert_called_once_with(
            "Report_Outbox", RD.OUTBOX_HEADER)
        (tab, rows), _ = sheets.append_rows.call_args
        assert tab == "Report_Outbox"
        # channel column added 2026-07-07 (four recipient lists);
        # daily reports default to 'reporting'
        assert rows == [["2026-07-05", "morning_yesterday", "PDF123",
                         "HTML456", "2026-07-06T13:07:00Z", "",
                         "reporting"]]

    def test_notified_at_starts_empty(self):
        """The empty notified_at cell is the notifier's claim column — if
        it were pre-filled the Apps Script would never send the mail.
        (No longer the LAST column since channel was appended; the
        notifier finds it by header name, not position.)"""
        assert "notified_at" in RD.OUTBOX_HEADER
        assert RD.OUTBOX_HEADER[-1] == "channel"
        sheets = MagicMock(spec=SheetsClient)
        RD.append_outbox(sheets, date_iso="d", kind="k",
                         pdf_file_id=None, html_file_id=None,
                         now_utc_iso="t")
        (_, rows), _ = sheets.append_rows.call_args
        # notified_at is index 5 (header-addressed by the notifier);
        # channel now occupies the last slot
        assert rows[0][5] == ""
        assert rows[0][-1] == "reporting"
        assert rows[0][2] == "" and rows[0][3] == ""   # None -> blank cells
