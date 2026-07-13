"""Telemetry archive job tests (v95).

The load-bearing safety property: a row is deleted from the live tab ONLY
after its day's CSV is confirmed on Drive. These tests prove the delete
never fires when the archive fails, that dry-run touches nothing, and
that the stamp interlock and the happy path behave.
"""

import datetime as dt
from unittest.mock import MagicMock

from argia.core.sheets import SheetsClient
from scripts.telemetry_archive import process_tab

KEEP = dt.date(2026, 7, 3)          # keeps >= Jul 3


def _rows():
    return [
        ["timestamp_utc", "kw"],
        ["2026-06-30T18:00:00+00:00", 10],   # old, MX 2026-06-30
        ["2026-07-01T18:00:00+00:00", 12],   # old, MX 2026-07-01
        ["2026-07-12T18:00:00+00:00", 15],   # recent → keep
    ]


def _sheets(rows=None):
    s = MagicMock(spec=SheetsClient)
    s.read_range.return_value = rows if rows is not None else _rows()
    return s


def _drive(upload_ok=True, verify_ok=True):
    d = MagicMock()
    if upload_ok:
        d.upload_file.return_value = "fileid"
    else:
        d.upload_file.side_effect = RuntimeError("drive down")
    d.find_file.return_value = "fileid" if verify_ok else None
    return d


def _folders(drive):
    f = MagicMock()
    f.month_folder.return_value = "monthfolderid"
    return f


STAMPED = {"2026-06-30", "2026-07-01"}


class TestDryRun:
    def test_touches_nothing(self):
        s, d = _sheets(), _drive()
        n_arch, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2", STAMPED, KEEP,
            apply=False)
        assert n_arch == 2 and n_del == 0
        d.upload_file.assert_not_called()
        s.delete_row_range.assert_not_called()


class TestApplyHappyPath:
    def test_archives_then_deletes(self):
        s, d = _sheets(), _drive()
        n_arch, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2", STAMPED, KEEP,
            apply=True)
        assert n_arch == 2 and n_del == 2
        # one CSV per old day
        assert d.upload_file.call_count == 2
        names = [c.args[1] for c in d.upload_file.call_args_list]
        assert "telemetry_mex2_2026-06-30.csv" in names
        assert "telemetry_mex2_2026-07-01.csv" in names
        # deletes the contiguous top block: rows 2..3 inclusive
        s.delete_row_range.assert_called_once_with("Telemetry_MEX2", 2, 3)


class TestArchiveBeforeDelete:
    def test_upload_failure_aborts_delete(self):
        s, d = _sheets(), _drive(upload_ok=False)
        n_arch, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2", STAMPED, KEEP,
            apply=True)
        assert n_del == 0
        s.delete_row_range.assert_not_called()   # nothing deleted

    def test_verify_failure_aborts_delete(self):
        # upload "succeeds" but the file isn't found afterwards
        s, d = _sheets(), _drive(verify_ok=False)
        _, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2", STAMPED, KEEP,
            apply=True)
        assert n_del == 0
        s.delete_row_range.assert_not_called()


class TestInterlock:
    def test_unstamped_old_day_is_not_pruned(self):
        # Jul-1 old but not stamped → only Jun-30 prunes, delete rows 2..2
        s, d = _sheets(), _drive()
        n_arch, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2",
            {"2026-06-30"}, KEEP, apply=True)
        assert n_arch == 1 and n_del == 1
        s.delete_row_range.assert_called_once_with("Telemetry_MEX2", 2, 2)


class TestGuards:
    def test_missing_timestamp_column_skips(self):
        s = _sheets([["date", "kw"], ["2026-06-30", 5]])
        d = _drive()
        n_arch, n_del = process_tab(
            s, d, _folders(d), "Telemetry_MEX2", "MEX2", STAMPED, KEEP,
            apply=True)
        assert (n_arch, n_del) == (0, 0)
        s.delete_row_range.assert_not_called()

    def test_missing_tab_skips(self):
        s = MagicMock(spec=SheetsClient)
        s.read_range.side_effect = RuntimeError("no tab")
        d = _drive()
        assert process_tab(s, d, _folders(d), "Telemetry_X", "X",
                           STAMPED, KEEP, apply=True) == (0, 0)
