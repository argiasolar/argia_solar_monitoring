"""Tests for argia.telemetry.sheets_writer.

We mock SheetsClient entirely — those methods have their own unit tests in
test_sheets.py. Here we verify the writer module's own logic: width checks,
dry-run behavior, calling the right SheetsClient methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.telemetry.schema import PLANT_SCHEMA
from argia.telemetry.sheets_writer import (
    ensure_telemetry_tab,
    write_telemetry_rows,
)


def _valid_row() -> list:
    """Return a row with exactly PLANT_SCHEMA.column_count cells."""
    return ["x"] * PLANT_SCHEMA.column_count


# ============================================================
# ensure_telemetry_tab
# ============================================================


class TestEnsureTelemetryTab:
    def test_calls_ensure_tab_then_ensure_header(self):
        sheets = MagicMock()
        ensure_telemetry_tab(sheets, "Telemetry_GTO1", PLANT_SCHEMA)
        sheets.ensure_tab.assert_called_once_with("Telemetry_GTO1")
        sheets.ensure_header.assert_called_once_with(
            "Telemetry_GTO1", PLANT_SCHEMA.header,
        )

    def test_passes_schema_header_list(self):
        sheets = MagicMock()
        ensure_telemetry_tab(sheets, "Telemetry_GTO1", PLANT_SCHEMA)
        _, header_arg = sheets.ensure_header.call_args.args
        assert isinstance(header_arg, list)
        assert len(header_arg) == PLANT_SCHEMA.column_count


# ============================================================
# write_telemetry_rows — width validation
# ============================================================


class TestWriteRowsValidation:
    def test_empty_rows_skip_upsert_and_return_zeros(self):
        sheets = MagicMock()
        stats = write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [])
        assert stats == {"inserted": 0, "updated": 0, "unchanged": 0}
        sheets.upsert_rows.assert_not_called()

    def test_short_row_raises_value_error(self):
        sheets = MagicMock()
        bad = ["x"] * (PLANT_SCHEMA.column_count - 1)
        with pytest.raises(ValueError, match="expects"):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [bad])
        sheets.upsert_rows.assert_not_called()

    def test_long_row_raises_value_error(self):
        sheets = MagicMock()
        bad = ["x"] * (PLANT_SCHEMA.column_count + 1)
        with pytest.raises(ValueError):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, [bad])

    def test_one_bad_row_in_batch_aborts_all(self):
        # Defensive: if even one row is misaligned, we should refuse to write
        # any. Half-written batches are worse than no write.
        sheets = MagicMock()
        rows = [_valid_row(), ["bad"] * 3, _valid_row()]
        with pytest.raises(ValueError):
            write_telemetry_rows(sheets, "Telemetry_GTO1", PLANT_SCHEMA, rows)
        sheets.upsert_rows.assert_not_called()


# ============================================================
# write_telemetry_rows — dry-run vs live
# ============================================================


class TestWriteRowsDryRun:
    def test_dry_run_does_not_call_upsert(self):
        sheets = MagicMock()
        write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA, [_valid_row()], dry_run=True,
        )
        sheets.upsert_rows.assert_not_called()

    def test_dry_run_returns_row_count(self):
        sheets = MagicMock()
        result = write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA,
            [_valid_row(), _valid_row()], dry_run=True,
        )
        assert result.get("dry_run") == 2


class TestWriteRowsLive:
    def test_live_calls_upsert_with_right_args(self):
        sheets = MagicMock()
        sheets.upsert_rows.return_value = {
            "inserted": 1, "updated": 0, "unchanged": 0,
        }

        rows = [_valid_row()]
        result = write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA, rows,
        )

        sheets.upsert_rows.assert_called_once_with(
            tab="Telemetry_GTO1",
            rows=rows,
            natural_key_columns=list(PLANT_SCHEMA.natural_key_columns),
        )
        assert result == {"inserted": 1, "updated": 0, "unchanged": 0}

    def test_live_passes_through_upsert_stats(self):
        sheets = MagicMock()
        sheets.upsert_rows.return_value = {
            "inserted": 2, "updated": 1, "unchanged": 3,
        }
        result = write_telemetry_rows(
            sheets, "Telemetry_GTO1", PLANT_SCHEMA, [_valid_row()],
        )
        assert result == {"inserted": 2, "updated": 1, "unchanged": 3}
