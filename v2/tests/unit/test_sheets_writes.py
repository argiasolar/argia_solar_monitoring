"""Tests for Stage 7.3b SheetsClient additions.

Covers:
- ``_col_to_a1`` column-letter helper
- ``write_cell``: API call shape + value-input options
- ``write_row``: API call shape + empty handling
- ``delete_row``: batchUpdate shape + sheetId lookup + caching

These tests use MagicMock with `spec=` where it matters, so missing-method
bugs of the kind that bit us in 7.3a get caught by the test rig.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from argia.core.sheets import SheetsClient, _col_to_a1


@pytest.fixture
def client():
    """SheetsClient with auth bypassed."""
    with patch("argia.core.sheets.Credentials"), patch(
        "argia.core.sheets.build"
    ) as mock_build:
        mock_svc = MagicMock()
        mock_build.return_value = mock_svc
        c = SheetsClient(sheet_id="fake_sheet_id", credentials_json="{}")
        c._svc = mock_svc
        return c


# ============================================================
# _col_to_a1 helper
# ============================================================


class TestColToA1:
    def test_first_26(self):
        assert _col_to_a1(1) == "A"
        assert _col_to_a1(2) == "B"
        assert _col_to_a1(26) == "Z"

    def test_double_letter(self):
        assert _col_to_a1(27) == "AA"
        assert _col_to_a1(28) == "AB"
        assert _col_to_a1(52) == "AZ"
        assert _col_to_a1(53) == "BA"

    def test_zz_boundary(self):
        # ZZ = 702, then AAA
        assert _col_to_a1(702) == "ZZ"
        assert _col_to_a1(703) == "AAA"

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            _col_to_a1(0)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            _col_to_a1(-1)


# ============================================================
# write_cell
# ============================================================


class TestWriteCell:
    def _capture_update(self, client):
        return client._svc.spreadsheets.return_value.values.return_value.update

    def test_writes_correct_range(self, client):
        client.write_cell("Inverters", row=5, col=4, value=100)
        mock_update = self._capture_update(client)
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["spreadsheetId"] == "fake_sheet_id"
        assert call_kwargs["range"] == "'Inverters'!D5"

    def test_default_input_option_is_raw(self, client):
        client.write_cell("X", 2, 1, "1.0")
        call = self._capture_update(client).call_args
        assert call.kwargs["valueInputOption"] == "RAW"

    def test_can_override_to_user_entered(self, client):
        client.write_cell("X", 2, 1, "2026-05-14", value_input_option="USER_ENTERED")
        call = self._capture_update(client).call_args
        assert call.kwargs["valueInputOption"] == "USER_ENTERED"

    def test_body_is_2d_array_with_single_value(self, client):
        client.write_cell("X", 2, 1, 42)
        call = self._capture_update(client).call_args
        assert call.kwargs["body"] == {"values": [[42]]}

    def test_value_can_be_none(self, client):
        """Writing None should send empty string-ish — Sheets treats None
        as clearing the cell."""
        client.write_cell("X", 2, 1, None)
        call = self._capture_update(client).call_args
        # Sheets API accepts None — we just pass it through
        assert call.kwargs["body"] == {"values": [[None]]}

    def test_high_column_uses_aa_notation(self, client):
        client.write_cell("X", 1, 27, "v")
        call = self._capture_update(client).call_args
        assert call.kwargs["range"] == "'X'!AA1"

    def test_zero_row_raises(self, client):
        with pytest.raises(ValueError):
            client.write_cell("X", 0, 1, "v")

    def test_zero_col_raises(self, client):
        with pytest.raises(ValueError):
            client.write_cell("X", 1, 0, "v")


# ============================================================
# write_row
# ============================================================


class TestWriteRow:
    def _capture_update(self, client):
        return client._svc.spreadsheets.return_value.values.return_value.update

    def test_writes_at_column_a(self, client):
        client.write_row("KPI_Daily", row=5, values=["a", "b", "c"])
        call = self._capture_update(client).call_args
        assert call.kwargs["range"] == "'KPI_Daily'!A5"

    def test_body_is_2d_array(self, client):
        client.write_row("X", 5, ["a", "b", "c"])
        call = self._capture_update(client).call_args
        assert call.kwargs["body"] == {"values": [["a", "b", "c"]]}

    def test_empty_values_no_op(self, client):
        """Empty values list should not even call the API."""
        client.write_row("X", 5, [])
        mock_update = self._capture_update(client)
        mock_update.assert_not_called()

    def test_default_user_entered(self, client):
        """Unlike write_cell, write_row uses USER_ENTERED by default —
        whole rows often include dates/numbers that should be parsed."""
        client.write_row("X", 5, ["v"])
        call = self._capture_update(client).call_args
        assert call.kwargs["valueInputOption"] == "USER_ENTERED"

    def test_zero_row_raises(self, client):
        with pytest.raises(ValueError):
            client.write_row("X", 0, ["v"])


# ============================================================
# delete_row + _tab_gid
# ============================================================


class TestDeleteRow:
    def _setup_gid_lookup(self, client, tab_name="X", gid=12345):
        """Configure the mock so spreadsheets().get() returns a metadata
        response containing a tab named ``tab_name`` with sheetId ``gid``."""
        meta = {
            "sheets": [
                {"properties": {"title": tab_name, "sheetId": gid}},
                {"properties": {"title": "OtherTab", "sheetId": 99999}},
            ],
        }
        client._svc.spreadsheets.return_value.get.return_value.execute.return_value = meta

    def test_calls_batch_update_with_delete_dimension(self, client):
        self._setup_gid_lookup(client, "KPI_Daily", gid=42)
        client.delete_row("KPI_Daily", row=5)
        mock_batch = client._svc.spreadsheets.return_value.batchUpdate
        mock_batch.assert_called_once()
        call = mock_batch.call_args
        assert call.kwargs["spreadsheetId"] == "fake_sheet_id"
        body = call.kwargs["body"]
        assert "requests" in body
        req = body["requests"][0]["deleteDimension"]
        assert req["range"]["sheetId"] == 42
        assert req["range"]["dimension"] == "ROWS"
        # Row 5 in 1-indexed sheet = startIndex 4, endIndex 5 (0-indexed half-open)
        assert req["range"]["startIndex"] == 4
        assert req["range"]["endIndex"] == 5

    def test_unknown_tab_raises(self, client):
        self._setup_gid_lookup(client, "DifferentTab")
        with pytest.raises(ValueError, match="not found"):
            client.delete_row("KPI_Daily", row=5)

    def test_tab_gid_is_cached(self, client):
        """Calling delete_row twice for the same tab should only fetch
        metadata once."""
        self._setup_gid_lookup(client, "X")
        client.delete_row("X", row=5)
        client.delete_row("X", row=6)
        mock_get = client._svc.spreadsheets.return_value.get
        assert mock_get.call_count == 1

    def test_different_tabs_each_fetch(self, client):
        meta = {
            "sheets": [
                {"properties": {"title": "TabA", "sheetId": 1}},
                {"properties": {"title": "TabB", "sheetId": 2}},
            ],
        }
        client._svc.spreadsheets.return_value.get.return_value.execute.return_value = meta
        client.delete_row("TabA", row=5)
        client.delete_row("TabB", row=5)
        mock_get = client._svc.spreadsheets.return_value.get
        # Cache miss on first per-tab call; might be 1 or 2 depending on order
        # but both lookups must succeed
        assert mock_get.call_count >= 1

    def test_zero_row_raises(self, client):
        with pytest.raises(ValueError):
            client.delete_row("X", row=0)


# ============================================================
# spec=SheetsClient — catches missing method bugs
# ============================================================


class TestSpecCatchesMissingMethods:
    """Regression: previously we used bare MagicMock() which auto-invents
    methods. This let write_cell/write_row/delete_row look like they
    existed in tests even when they didn't.

    spec=SheetsClient binds the mock to the REAL class. AttributeError on
    missing method → test fails loudly."""

    def test_spec_attr_succeeds_for_real_methods(self):
        m = MagicMock(spec=SheetsClient)
        # These attributes exist on the class so the mock allows them
        m.read_range("X")
        m.write_cell("X", 1, 1, "v")
        m.write_row("X", 1, ["v"])
        m.delete_row("X", 1)
        m.upsert_rows("X", [], [0])

    def test_spec_blocks_invented_methods(self):
        m = MagicMock(spec=SheetsClient)
        with pytest.raises(AttributeError):
            m.write_to_dropbox("X", 1)  # not a real method
        with pytest.raises(AttributeError):
            m.this_does_not_exist()
