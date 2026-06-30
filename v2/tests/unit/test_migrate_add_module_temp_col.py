"""Tests for scripts/migrate_add_module_temp_col.py.

The decision logic (``plan_header_migration``) is pure and gets the bulk of the
coverage. ``run_migration`` is checked against a spec'd SheetsClient mock to
prove dry-run writes nothing and apply writes exactly the right header.
"""

from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    ARGIA_TAB_NAME,
    PLANT_SCHEMA,
)


# The migration lives in scripts/ (not an installed package), so load it by path.
_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "migrate_add_module_temp_col.py"
)
_spec = importlib.util.spec_from_file_location("migrate_add_module_temp_col", _MOD_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


# ===================================================================
# Pure: plan_header_migration
# ===================================================================


class TestPlanHeaderMigration:
    def test_append_when_header_is_old_schema(self):
        old = list(ARGIA_SCHEMA.columns)[:-1]  # v2 = v3 minus module_temp_c
        action, new_header = mig.plan_header_migration(old, ARGIA_SCHEMA)
        assert action == mig.ACTION_APPEND
        assert new_header == list(ARGIA_SCHEMA.columns)
        assert new_header[-1] == "module_temp_c"

    def test_skip_when_header_already_v3(self):
        current = list(ARGIA_SCHEMA.columns)
        action, new_header = mig.plan_header_migration(current, ARGIA_SCHEMA)
        assert action == mig.ACTION_SKIP
        assert new_header is None

    def test_absent_when_empty(self):
        assert mig.plan_header_migration([], ARGIA_SCHEMA) == (mig.ACTION_ABSENT, None)

    def test_mismatch_when_unexpected(self):
        action, new_header = mig.plan_header_migration(
            ["totally", "different", "header"], ARGIA_SCHEMA
        )
        assert action == mig.ACTION_MISMATCH
        assert new_header is None

    def test_trailing_empty_cells_ignored(self):
        # Sheets often pads with empty trailing cells when querying ZZ1.
        current = list(ARGIA_SCHEMA.columns) + ["", "", ""]
        action, _ = mig.plan_header_migration(current, ARGIA_SCHEMA)
        assert action == mig.ACTION_SKIP

    def test_plant_schema_append(self):
        old = list(PLANT_SCHEMA.columns)[:-1]
        action, new_header = mig.plan_header_migration(old, PLANT_SCHEMA)
        assert action == mig.ACTION_APPEND
        assert new_header == list(PLANT_SCHEMA.columns)
        assert len(new_header) == 143

    def test_idempotent_after_append(self):
        # Applying the result a second time must be a no-op (skip).
        old = list(PLANT_SCHEMA.columns)[:-1]
        _, new_header = mig.plan_header_migration(old, PLANT_SCHEMA)
        action2, _ = mig.plan_header_migration(new_header, PLANT_SCHEMA)
        assert action2 == mig.ACTION_SKIP


# ===================================================================
# run_migration: side-effect discipline
# ===================================================================


class TestRunMigration:
    def _sheets_with_header(self, header):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [list(header)]
        return sheets

    def test_dry_run_writes_nothing(self):
        old = list(ARGIA_SCHEMA.columns)[:-1]
        sheets = self._sheets_with_header(old)
        summary = mig.run_migration(
            sheets, [(ARGIA_TAB_NAME, ARGIA_SCHEMA)], apply=False
        )
        assert summary[mig.ACTION_APPEND] == 1
        sheets.write_header_row.assert_not_called()

    def test_apply_writes_full_v3_header(self):
        old = list(ARGIA_SCHEMA.columns)[:-1]
        sheets = self._sheets_with_header(old)
        summary = mig.run_migration(
            sheets, [(ARGIA_TAB_NAME, ARGIA_SCHEMA)], apply=True
        )
        assert summary[mig.ACTION_APPEND] == 1
        sheets.write_header_row.assert_called_once_with(
            ARGIA_TAB_NAME, list(ARGIA_SCHEMA.columns)
        )

    def test_apply_skips_already_migrated(self):
        current = list(ARGIA_SCHEMA.columns)
        sheets = self._sheets_with_header(current)
        summary = mig.run_migration(
            sheets, [(ARGIA_TAB_NAME, ARGIA_SCHEMA)], apply=True
        )
        assert summary[mig.ACTION_SKIP] == 1
        sheets.write_header_row.assert_not_called()

    def test_mismatch_never_writes(self):
        sheets = self._sheets_with_header(["junk", "header"])
        summary = mig.run_migration(
            sheets, [(ARGIA_TAB_NAME, ARGIA_SCHEMA)], apply=True
        )
        assert summary[mig.ACTION_MISMATCH] == 1
        sheets.write_header_row.assert_not_called()

    def test_unreadable_tab_counts_as_error_and_continues(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.side_effect = RuntimeError("tab not found")
        summary = mig.run_migration(
            sheets, [(ARGIA_TAB_NAME, ARGIA_SCHEMA)], apply=True
        )
        assert summary["error"] == 1
        sheets.write_header_row.assert_not_called()
