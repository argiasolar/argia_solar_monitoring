

class TestStampColumnBatched:
    """v86: stamps flush through ONE batch_write_cells call, never a
    per-cell write loop (quota crash regression, 2026-07-10)."""

    def test_all_stamps_in_one_batch(self):
        from unittest.mock import MagicMock

        from argia.archive.kpi_daily import stamp_column
        from argia.core.sheets import SheetsClient
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            ["date_iso", "plant_key", "soiling_loss_pct"],
            ["2026-07-10", "SLP1", ""],
            ["2026-07-10", "GTO1", ""],
        ]
        sheets.batch_write_cells.return_value = 2
        n = stamp_column(sheets, "soiling_loss_pct",
                         {("2026-07-10", "SLP1"): 0.08,
                          ("2026-07-10", "GTO1"): 0.27})
        assert n == 2
        sheets.batch_write_cells.assert_called_once()
        cells = sheets.batch_write_cells.call_args.args[1]
        assert sorted(cells) == [(2, 3, 0.08), (3, 3, 0.27)]
        sheets.write_cell.assert_not_called()

    def test_dry_run_writes_nothing(self):
        from unittest.mock import MagicMock

        from argia.archive.kpi_daily import stamp_column
        from argia.core.sheets import SheetsClient
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            ["date_iso", "plant_key", "soiling_loss_pct"],
            ["2026-07-10", "SLP1", ""],
        ]
        n = stamp_column(sheets, "soiling_loss_pct",
                         {("2026-07-10", "SLP1"): 0.08}, dry_run=True)
        assert n == 1
        sheets.batch_write_cells.assert_not_called()
        sheets.write_cell.assert_not_called()
