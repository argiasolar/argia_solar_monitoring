"""billable_kwh migration planner tests.

The planner is pure: given the current KPI_Daily header + rows it decides
whether to add the column and which rows to back-fill. The rules that
matter: never overwrite an existing billable value (idempotent), never
back-fill a no-data (blank energy) day to 0, and source the fill from
energy_kwh by NAME (not a fixed position).
"""

import pytest

from scripts.migrate_add_billable_kwh_col import plan_billable_backfill


HEADER = ["date_iso", "plant_key", "energy_kwh", "notes"]


class TestPlanBillableBackfill:
    def test_adds_header_when_missing(self):
        needs, col, fills = plan_billable_backfill(HEADER, [])
        assert needs is True
        assert col == len(HEADER) + 1     # appended after 'notes'

    def test_no_header_add_when_present(self):
        header = HEADER + ["billable_kwh"]
        needs, col, fills = plan_billable_backfill(header, [])
        assert needs is False
        assert col == len(header)         # 1-based index of billable_kwh

    def test_backfills_energy_into_blank_billable(self):
        header = HEADER + ["billable_kwh"]
        rows = [
            ["2026-07-01", "SLP1", "100.5", "", ""],   # blank billable
            ["2026-07-02", "SLP1", "200.0", "", ""],
        ]
        needs, col, fills = plan_billable_backfill(header, rows)
        assert fills == [(2, 100.5), (3, 200.0)]

    def test_existing_billable_not_overwritten(self):
        header = HEADER + ["billable_kwh"]
        rows = [
            ["2026-07-01", "SLP1", "100.5", "", "150.0"],   # already set
            ["2026-07-02", "SLP1", "200.0", "", ""],        # blank
        ]
        _, _, fills = plan_billable_backfill(header, rows)
        assert fills == [(3, 200.0)]

    def test_blank_energy_day_left_blank(self):
        header = HEADER + ["billable_kwh"]
        rows = [
            ["2026-07-01", "SLP1", "", "", ""],       # no-data day
            ["2026-07-02", "SLP1", "200.0", "", ""],
        ]
        _, _, fills = plan_billable_backfill(header, rows)
        assert fills == [(3, 200.0)]                  # only the real day

    def test_sources_energy_by_name_not_position(self):
        # energy_kwh in a different position than usual
        header = ["plant_key", "date_iso", "pr", "energy_kwh", "billable_kwh"]
        rows = [["SLP1", "2026-07-01", "0.8", "321.0", ""]]
        _, _, fills = plan_billable_backfill(header, rows)
        assert fills == [(2, 321.0)]

    def test_raises_without_energy_column(self):
        with pytest.raises(ValueError):
            plan_billable_backfill(["date_iso", "plant_key"], [])

    def test_ignores_non_data_rows(self):
        header = HEADER + ["billable_kwh"]
        rows = [
            ["", "", "", "", ""],                     # blank row
            ["2026-07-02", "SLP1", "200.0", "", ""],
        ]
        _, _, fills = plan_billable_backfill(header, rows)
        assert fills == [(3, 200.0)]
