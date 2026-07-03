"""Tests for the monthly archive (plan #8): pure logic + safety invariants."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

from argia.archive.monthly import (
    MonthBlock,
    chunk_rows,
    locate_month_block,
    month_title,
    previous_month,
    projected_cells,
    verify_copy,
)
from argia.core.drive import DriveClient

SERIAL_EPOCH = dt.date(1899, 12, 30)


def _serial(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return (dt.date(y, m, d) - SERIAL_EPOCH).days


class TestMonthHelpers:
    def test_title(self):
        assert month_title("2026-07") == "Argia_Mont_Archive_2026_07"

    def test_previous_month(self):
        assert previous_month(dt.date(2026, 7, 3)) == "2026-06"
        assert previous_month(dt.date(2026, 1, 15)) == "2025-12"

    def test_chunking(self):
        rows = [[i] for i in range(1201)]
        chunks = chunk_rows(rows, chunk=500)
        assert [len(c) for c in chunks] == [500, 500, 201]
        assert chunk_rows([], 500) == []


class TestLocateMonthBlock:
    HEADER = ["timestamp_mx", "plant_key", "v"]

    def _data(self, days):
        return [self.HEADER] + [[f"{d} 10:00:00", "SLP1", 1] for d in days]

    def test_contiguous_month_located(self):
        data = self._data(["2026-06-29", "2026-06-30",
                           "2026-07-01", "2026-07-02",
                           "2026-08-01"])
        b = locate_month_block("T", data, "2026-07",
                               lambda r: str(r[0])[:10])
        assert b.count == 2
        assert (b.start_row, b.end_row) == (4, 5)   # sheet rows
        assert b.contiguous is True
        assert b.total_data_rows == 5

    def test_non_contiguous_flagged(self):
        data = self._data(["2026-07-01", "2026-06-30", "2026-07-02"])
        b = locate_month_block("T", data, "2026-07",
                               lambda r: str(r[0])[:10])
        assert b.count == 2 and b.contiguous is False

    def test_empty_month(self):
        data = self._data(["2026-06-30"])
        b = locate_month_block("T", data, "2026-07",
                               lambda r: str(r[0])[:10])
        assert b.count == 0 and b.contiguous is False

    def test_undatable_rows_never_match(self):
        data = [self.HEADER, ["", "SLP1", 1], [None, "SLP1", 1]]
        b = locate_month_block("T", data, "2026-07", lambda r: "")
        assert b.count == 0

    def test_serial_dates_via_date_key(self):
        # KPI_Daily stores date_iso as a serial — same normalizer as upsert.
        from argia.kpi.reconcile import date_key
        data = [["date_iso", "plant_key"],
                [_serial("2026-06-30"), "SLP1"],
                [_serial("2026-07-01"), "SLP1"]]
        b = locate_month_block("KPI_Daily", data, "2026-07",
                               lambda r: date_key(r[0]))
        assert b.count == 1 and b.start_row == 3


class TestVerifyAndBudget:
    def _block(self, n, cols=3):
        return MonthBlock(tab="T", header=["a"] * cols, rows=[[1]] * n,
                          start_row=2, end_row=1 + n, contiguous=True,
                          total_data_rows=n)

    def test_verify_pass_and_fail(self):
        ok, _ = verify_copy(self._block(10), 10)
        assert ok
        bad, msg = verify_copy(self._block(10), 9)
        assert not bad and "VERIFY FAILED" in msg

    def test_projected_cells(self):
        blocks = [self._block(9, cols=4), self._block(1, cols=2)]
        # (9+1)*4 + (1+1)*2 = 44
        assert projected_cells(blocks) == 44


class TestDriveClientRequestShapes:
    """DriveClient with an injected fake service — request shapes only."""

    def _svc(self):
        svc = MagicMock()
        return svc

    def test_find_returns_none_when_absent(self):
        svc = self._svc()
        svc.files().list().execute.return_value = {"files": []}
        d = DriveClient(service=svc)
        assert d.find_spreadsheet("FOLDER", "X") is None

    def test_find_returns_id_and_query_shape(self):
        svc = self._svc()
        svc.files().list().execute.return_value = {
            "files": [{"id": "ID1", "name": "X"}]}
        d = DriveClient(service=svc)
        assert d.find_spreadsheet("FOLDER", "X") == "ID1"
        q = svc.files().list.call_args.kwargs["q"]
        assert "name = 'X'" in q and "'FOLDER' in parents" in q
        assert "trashed = false" in q

    def test_create_places_file_in_folder(self):
        svc = self._svc()
        svc.files().create().execute.return_value = {"id": "NEW"}
        d = DriveClient(service=svc)
        assert d.create_spreadsheet("FOLDER", "Argia_Mont_Archive_2026_07") == "NEW"
        body = svc.files().create.call_args.kwargs["body"]
        assert body["parents"] == ["FOLDER"]
        assert body["mimeType"].endswith("spreadsheet")

    def test_trash_marks_trashed(self):
        svc = self._svc()
        d = DriveClient(service=svc)
        d.trash_file("ID1")
        kw = svc.files().update.call_args.kwargs
        assert kw["fileId"] == "ID1" and kw["body"] == {"trashed": True}


class TestDeleteRowRange:
    def test_request_shape(self):
        from argia.core.sheets import SheetsClient
        c = SheetsClient.__new__(SheetsClient)      # skip auth
        c.sheet_id = "SID"
        c._svc = MagicMock()
        c._tab_gid = lambda tab: 77
        c.delete_row_range("Telemetry_Argia", 100, 250)
        body = c._svc.spreadsheets().batchUpdate.call_args.kwargs["body"]
        rng = body["requests"][0]["deleteDimension"]["range"]
        assert rng == {"sheetId": 77, "dimension": "ROWS",
                       "startIndex": 99, "endIndex": 250}

    def test_bad_range_raises(self):
        import pytest
        from argia.core.sheets import SheetsClient
        c = SheetsClient.__new__(SheetsClient)
        c.sheet_id = "SID"; c._svc = MagicMock(); c._tab_gid = lambda t: 1
        with pytest.raises(ValueError):
            c.delete_row_range("T", 10, 5)
