"""Tests for scripts/migrate_add_gamma_pmax_col.py."""

from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import MagicMock

from argia.core.sheets import SheetsClient

_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "migrate_add_gamma_pmax_col.py"
)
_spec = importlib.util.spec_from_file_location("migrate_add_gamma_pmax_col", _MOD_PATH)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


HEADER_NO_GAMMA = ["plant_key", "customer", "kwp_dc"]
HEADER_WITH_GAMMA = ["plant_key", "customer", "kwp_dc", "gamma_pmax"]


class TestPlanGammaFill:
    def test_new_column_fills_all_plant_rows(self):
        rows = [["SLP1", "Acme", "100"], ["GTO1", "Beta", "80"]]
        needs_header, col_index, fills = mig.plan_gamma_fill(HEADER_NO_GAMMA, rows)
        assert needs_header is True
        assert col_index == 4  # appended after 3 cols
        assert fills == [(2, mig.DEFAULT_GAMMA_PMAX), (3, mig.DEFAULT_GAMMA_PMAX)]

    def test_existing_column_fills_only_blanks(self):
        rows = [
            ["SLP1", "Acme", "100", "-0.0038"],  # already set
            ["GTO1", "Beta", "80", ""],          # blank -> fill
        ]
        needs_header, col_index, fills = mig.plan_gamma_fill(HEADER_WITH_GAMMA, rows)
        assert needs_header is False
        assert col_index == 4
        assert fills == [(3, mig.DEFAULT_GAMMA_PMAX)]

    def test_all_filled_no_fills(self):
        rows = [["SLP1", "Acme", "100", "-0.0038"]]
        _, _, fills = mig.plan_gamma_fill(HEADER_WITH_GAMMA, rows)
        assert fills == []

    def test_skips_blank_plant_rows(self):
        rows = [["", "", ""], ["SLP1", "Acme", "100"]]
        _, _, fills = mig.plan_gamma_fill(HEADER_NO_GAMMA, rows)
        assert fills == [(3, mig.DEFAULT_GAMMA_PMAX)]

    def test_custom_default(self):
        rows = [["SLP1", "Acme", "100"]]
        _, _, fills = mig.plan_gamma_fill(HEADER_NO_GAMMA, rows, default=-0.0040)
        assert fills == [(2, -0.0040)]


class TestRunMigration:
    def _sheets(self, header, rows):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [header] + rows
        return sheets

    def test_dry_run_writes_nothing(self):
        sheets = self._sheets(HEADER_NO_GAMMA, [["SLP1", "A", "100"]])
        summary = mig.run_migration(sheets, apply=False)
        assert summary["header_added"] == 1
        assert summary["cells_to_fill"] == 1
        assert summary["cells_filled"] == 0
        sheets.write_values.assert_not_called()

    def test_apply_writes_header_and_cells(self):
        sheets = self._sheets(HEADER_NO_GAMMA, [["SLP1", "A", "100"], ["GTO1", "B", "80"]])
        summary = mig.run_migration(sheets, apply=True)
        assert summary["cells_filled"] == 2
        # 1 header write + 2 cell writes
        assert sheets.write_values.call_count == 3
        sheets.write_values.assert_any_call("Plants", "D1", [["gamma_pmax"]])
        sheets.write_values.assert_any_call("Plants", "D2", [[-0.0035]])
        sheets.write_values.assert_any_call("Plants", "D3", [[-0.0035]])

    def test_apply_idempotent_when_all_filled(self):
        sheets = self._sheets(HEADER_WITH_GAMMA, [["SLP1", "A", "100", "-0.0035"]])
        summary = mig.run_migration(sheets, apply=True)
        assert summary["header_added"] == 0
        assert summary["cells_filled"] == 0
        sheets.write_values.assert_not_called()
