"""Tests for argia.core.sheets.

We test the upsert logic by mocking the underlying Sheets API methods.
The point is to verify the inserted/updated/unchanged accounting works
correctly — that's the critical bit that prevents duplicate rows.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from argia.core.sheets import SheetsClient


@pytest.fixture
def client():
    """A SheetsClient with auth bypassed."""
    with patch("argia.core.sheets.Credentials"), patch(
        "argia.core.sheets.build"
    ) as mock_build:
        mock_svc = MagicMock()
        mock_build.return_value = mock_svc
        c = SheetsClient(sheet_id="fake_sheet_id", credentials_json="{}")
        c._svc = mock_svc
        return c


class TestConstructor:
    def test_requires_sheet_id(self):
        with pytest.raises(ValueError):
            SheetsClient(sheet_id="", credentials_json="{}")

    def test_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CREDENTIALS", raising=False)
        with pytest.raises(RuntimeError, match="GOOGLE_CREDENTIALS"):
            SheetsClient(sheet_id="x")


class TestUpsertRows:
    """The big one — make sure idempotency actually works."""

    def _setup_existing(self, client, existing_rows):
        """Helper: configure the mock to return existing_rows on read."""
        mock_get = client._svc.spreadsheets.return_value.values.return_value.get
        mock_get.return_value.execute.return_value = {"values": existing_rows}

    def _capture_calls(self, client):
        """Capture append + batchUpdate calls on the mock service.
        v81: updates go through ONE values.batchUpdate instead of one
        values.update per row — the per-row loop blew the Sheets
        60-writes/min quota once v80 overlap windows made multi-row
        updates routine (live 429s, 2026-07-10)."""
        values = client._svc.spreadsheets.return_value.values.return_value
        return values.append, values.batchUpdate

    def test_all_new_rows_inserted(self, client):
        # Existing sheet has only header
        self._setup_existing(client, [["date", "plant_key", "kwh"]])
        append, update = self._capture_calls(client)

        new_rows = [
            ["4/15/2026", "MEX1", 1500.0],
            ["4/15/2026", "MEX2", 1200.0],
        ]
        result = client.upsert_rows(
            "DailyProduction", new_rows, natural_key_columns=[0, 1]
        )

        assert result == {"inserted": 2, "updated": 0, "unchanged": 0}
        append.assert_called_once()
        update.assert_not_called()

    def test_unchanged_rows_not_rewritten(self, client):
        # Existing data already contains the same rows we're upserting
        self._setup_existing(
            client,
            [
                ["date", "plant_key", "kwh"],
                ["4/15/2026", "MEX1", 1500.0],
            ],
        )
        append, update = self._capture_calls(client)

        result = client.upsert_rows(
            "DailyProduction",
            [["4/15/2026", "MEX1", 1500.0]],
            natural_key_columns=[0, 1],
        )

        assert result == {"inserted": 0, "updated": 0, "unchanged": 1}
        append.assert_not_called()
        update.assert_not_called()

    def test_changed_rows_updated(self, client):
        # Existing kWh value differs from the new one — should update
        self._setup_existing(
            client,
            [
                ["date", "plant_key", "kwh"],
                ["4/15/2026", "MEX1", 1000.0],  # old value
            ],
        )
        append, update = self._capture_calls(client)

        result = client.upsert_rows(
            "DailyProduction",
            [["4/15/2026", "MEX1", 1500.0]],  # new value
            natural_key_columns=[0, 1],
        )

        assert result == {"inserted": 0, "updated": 1, "unchanged": 0}
        append.assert_not_called()
        # one batched write, carrying the one changed row
        body = update.call_args.kwargs["body"]
        assert len(body["data"]) == 1
        assert body["data"][0]["values"] == [["4/15/2026", "MEX1",
                                              1500.0]]
        update.assert_called_once()

    def test_mixed_insert_update_unchanged(self, client):
        self._setup_existing(
            client,
            [
                ["date", "plant_key", "kwh"],
                ["4/15/2026", "MEX1", 1500.0],  # will stay unchanged
                ["4/15/2026", "MEX2", 999.0],  # will be updated
                # MEX3 doesn't exist yet — will be inserted
            ],
        )

        result = client.upsert_rows(
            "DailyProduction",
            [
                ["4/15/2026", "MEX1", 1500.0],  # unchanged
                ["4/15/2026", "MEX2", 1200.0],  # change
                ["4/15/2026", "MEX3", 800.0],  # new
            ],
            natural_key_columns=[0, 1],
        )

        assert result == {"inserted": 1, "updated": 1, "unchanged": 1}

    def test_running_twice_is_idempotent(self, client):
        # First run: all new
        self._setup_existing(client, [["date", "plant_key", "kwh"]])
        rows = [["4/15/2026", "MEX1", 1500.0]]
        first = client.upsert_rows("DailyProduction", rows, natural_key_columns=[0, 1])
        assert first["inserted"] == 1

        # Second run with same data: simulate that the row is now in the sheet
        self._setup_existing(
            client,
            [["date", "plant_key", "kwh"], ["4/15/2026", "MEX1", 1500.0]],
        )
        second = client.upsert_rows("DailyProduction", rows, natural_key_columns=[0, 1])
        assert second == {"inserted": 0, "updated": 0, "unchanged": 1}

    def test_empty_input_is_noop(self, client):
        result = client.upsert_rows(
            "DailyProduction", [], natural_key_columns=[0, 1]
        )
        assert result == {"inserted": 0, "updated": 0, "unchanged": 0}

    def test_composite_key_two_columns(self, client):
        # Two rows with same plant but different dates — both inserted
        self._setup_existing(client, [["date", "plant_key", "kwh"]])
        rows = [
            ["4/14/2026", "MEX1", 1400.0],
            ["4/15/2026", "MEX1", 1500.0],
        ]
        result = client.upsert_rows(
            "DailyProduction", rows, natural_key_columns=[0, 1]
        )
        assert result["inserted"] == 2


class TestFormattedReadbackEquivalence:
    """v81: Sheets returns values FORMATTED (6.0 -> "6"), so the old
    raw-string comparison re-updated identical rows on every poll —
    each a quota-costing write. Numeric equivalence must survive the
    round-trip."""

    def test_cell_equivalence_table(self):
        from argia.core.sheets import _cells_equivalent
        assert _cells_equivalent("6", 6.0)
        assert _cells_equivalent("435.14", 435.14)
        assert _cells_equivalent("", None)
        assert _cells_equivalent("0", 0.0)
        assert not _cells_equivalent("6", 7.0)
        assert not _cells_equivalent("ONLINE", "OFFLINE")
        assert _cells_equivalent(" MPPT ", "MPPT")

    def test_roundtrip_row_counts_as_unchanged(self):
        from argia.core.sheets import _rows_equivalent
        written = ["2026-07-10 13:40:00", "QRO1", 6.0, 435.14, None]
        readback = ["2026-07-10 13:40:00", "QRO1", "6", "435.14", ""]
        assert _rows_equivalent(readback, written)


class TestBatchWriteCells:
    """v86: stamp_column's per-cell write loop hit the 60-writes/min
    quota at fleet size 10 (live crash, July rerun 2026-07-10). N
    scattered cells must cost ONE batchUpdate request."""

    def test_single_request_carries_all_cells(self, client):
        values = client._svc.spreadsheets.return_value.values.return_value
        n = client.batch_write_cells(
            "KPI_Daily", [(60, 17, 0.0877), (61, 17, 0.0266),
                          (62, 17, 0.275)])
        assert n == 3
        values.batchUpdate.assert_called_once()
        body = values.batchUpdate.call_args.kwargs["body"]
        assert len(body["data"]) == 3
        assert body["data"][0]["values"] == [[0.0877]]
        assert "Q60" in body["data"][0]["range"]   # col 17 == Q

    def test_empty_is_free(self, client):
        values = client._svc.spreadsheets.return_value.values.return_value
        assert client.batch_write_cells("KPI_Daily", []) == 0
        values.batchUpdate.assert_not_called()


def test_blank_never_overwrites_data():
    """v89: the SolarEdge overlap window re-parses older rows WITHOUT
    the weather snapshot; each poll erased its predecessor's weather
    (live 2026-07-11 — QRO1/GTO2 theoretical died on the client
    pages). A blank incoming cell is equivalent to any stored value;
    a stored blank still accepts new data."""
    from argia.core.sheets import _cells_equivalent, _rows_equivalent
    assert _cells_equivalent("527.0", "")        # blank won't overwrite
    assert _cells_equivalent("527.0", None)
    assert not _cells_equivalent("", "527.0")    # data still lands
    assert not _cells_equivalent("527.0", "600") # real change updates
    # the exact incident: re-parsed SE row, weather columns empty
    stored = ["2026-07-11 09:20", "QRO1", 55.2, "527", "88.1"]
    reparse = ["2026-07-11 09:20", "QRO1", 55.2, "", None]
    assert _rows_equivalent(stored, reparse)
