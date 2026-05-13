"""Tests for argia.telemetry.sheets_writer.

We mock SheetsClient entirely — its real methods have their own tests.
Here we verify the writer module's logic: width checks, dry-run, header
sanity check, calling the right SheetsClient methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.telemetry.schema import ARGIA_SCHEMA, PLANT_SCHEMA
from argia.telemetry.sheets_writer import (
    SchemaMismatchError,
    ensure_telemetry_tab,
    write_telemetry_rows,
)


def _valid_plant_row() -> list:
    return ["x"] * PLANT_SCHEMA.column_count


def _valid_argia_row() -> list:
    return ["x"] * ARGIA_SCHEMA.column_count


def _mock_sheets_with_header(header: list) -> MagicMock:
    """A MagicMock SheetsClient whose read_range returns the given header."""
    sheets = MagicMock()
    sheets.read_range.return_value = [header] if header else []
    return sheets


# ============================================================
# ensure_telemetry_tab — happy path
# ============================================================


class TestEnsureTelemetryTabFresh:
    def test_creates_tab_when_empty(self):
        sheets = _mock_sheets_with_header([])
        ensure_telemetry_tab(sheets, "Telemetry_GTO1", PLANT_SCHEMA)
        sheets.ensure_tab.assert_called_once_with("Telemetry_GTO1")
        sheets.ensure_header.assert_called_once_with(
            "Telemetry_GTO1", PLANT_SCHEMA.header,
        )


class TestEnsureTelemetryTabMatching:
    def test_no_header_write_when_matches(self):
        # Existing header is exactly the plant schema
        sheets = _mock_sheets_with_header(list(PLANT_SCHEMA.columns))
        ensure_telemetry_tab(sheets, "Telemetry_GTO1", PLANT_SCHEMA)
        sheets.ensure_tab.assert_called_once_with("Telemetry_GTO1")
        # Header is already correct → no need to write it
        sheets.ensure_header.assert_not_called()

    def test_argia_schema_match(self):
        sheets = _mock_sheets_with_header(list(ARGIA_SCHEMA.columns))
        ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)
        sheets.ensure_header.assert_not_called()

    def test_trailing_empty_cells_ignored(self):
        # Sheets sometimes returns trailing empty cells in ZZ1 queries
        header_with_trailing = list(ARGIA_SCHEMA.columns) + ["", "", ""]
        sheets = _mock_sheets_with_header(header_with_trailing)
        ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)
        sheets.ensure_header.assert_not_called()


# ============================================================
# ensure_telemetry_tab — schema mismatch
# ============================================================


class TestEnsureTelemetryTabMismatch:
    def test_wrong_width_raises(self):
        # Old Stage 3 wide ARGIA schema had 143 cols; new has 15
        old_wide_header = ["timestamp_utc", "timestamp_mx", "plant_key"] + ["x"] * 140
        sheets = _mock_sheets_with_header(old_wide_header)
        with pytest.raises(SchemaMismatchError, match="doesn't match"):
            ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)

    def test_wrong_column_names_raises(self):
        # Same width but different column names
        bad_header = list(ARGIA_SCHEMA.columns[:-1]) + ["something_else"]
        sheets = _mock_sheets_with_header(bad_header)
        with pytest.raises(SchemaMismatchError):
            ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)

    def test_mismatch_error_contains_fix_instructions(self):
        sheets = _mock_sheets_with_header(["bogus", "header"])
        with pytest.raises(SchemaMismatchError) as exc_info:
            ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)
        # The error message should tell the user how to fix it
        assert "delete the tab" in str(exc_info.value).lower()

    def test_mismatch_does_not_write_header(self):
        # Critically: we must NOT overwrite the existing wrong header
        sheets = _mock_sheets_with_header(["bogus", "header"])
        with pytest.raises(SchemaMismatchError):
            ensure_telemetry_tab(sheets, "Telemetry_Argia", ARGIA_SCHEMA)
        sheets.ensure_header.assert_not_called()


# ============================================================
# write_telemetry_rows — width validation
# ============================================================


class TestWriteRowsValidation:
    def test_empty_rows_skip_upsert(self):
        sheets = MagicMock()
        stats = write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [])
        assert stats == {"inserted": 0, "updated": 0, "unchanged": 0}
        sheets.upsert_rows.assert_not_called()

    def test_short_row_raises(self):
        sheets = MagicMock()
        bad = ["x"] * (PLANT_SCHEMA.column_count - 1)
        with pytest.raises(ValueError, match="expects"):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [bad])
        sheets.upsert_rows.assert_not_called()

    def test_long_row_raises(self):
        sheets = MagicMock()
        bad = ["x"] * (PLANT_SCHEMA.column_count + 1)
        with pytest.raises(ValueError):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [bad])

    def test_one_bad_row_in_batch_aborts_all(self):
        sheets = MagicMock()
        rows = [_valid_plant_row(), ["bad"] * 3, _valid_plant_row()]
        with pytest.raises(ValueError):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, rows)
        sheets.upsert_rows.assert_not_called()

    def test_argia_row_width_checked_separately(self):
        sheets = MagicMock()
        # A 142-col plant row is NOT a valid 15-col argia row
        bad = _valid_plant_row()
        with pytest.raises(ValueError, match="argia"):
            write_telemetry_rows(sheets, "Telemetry_Argia", ARGIA_SCHEMA, [bad])


# ============================================================
# write_telemetry_rows — dry-run vs live
# ============================================================


class TestWriteRowsDryRun:
    def test_dry_run_skips_upsert(self):
        sheets = MagicMock()
        write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA, [_valid_plant_row()], dry_run=True,
        )
        sheets.upsert_rows.assert_not_called()

    def test_dry_run_returns_row_count(self):
        sheets = MagicMock()
        result = write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA,
            [_valid_plant_row(), _valid_plant_row()], dry_run=True,
        )
        assert result.get("dry_run") == 2


class TestWriteRowsLive:
    def test_live_calls_upsert(self):
        sheets = MagicMock()
        sheets.upsert_rows.return_value = {
            "inserted": 1, "updated": 0, "unchanged": 0,
        }
        rows = [_valid_argia_row()]
        result = write_telemetry_rows(
            sheets, "Telemetry_Argia", ARGIA_SCHEMA, rows,
        )
        sheets.upsert_rows.assert_called_once_with(
            tab="Telemetry_Argia",
            rows=rows,
            natural_key_columns=list(ARGIA_SCHEMA.natural_key_columns),
        )
        assert result == {"inserted": 1, "updated": 0, "unchanged": 0}

    def test_live_passes_through_stats(self):
        sheets = MagicMock()
        sheets.upsert_rows.return_value = {
            "inserted": 2, "updated": 1, "unchanged": 3,
        }
        result = write_telemetry_rows(
            sheets, "Telemetry_Argia", ARGIA_SCHEMA, [_valid_argia_row()],
        )
        assert result == {"inserted": 2, "updated": 1, "unchanged": 3}
