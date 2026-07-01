"""Tests for argia.archive.kpi_daily."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.archive.kpi_daily import (
    HOT_WINDOW_DAYS,
    KPI_DAILY_HEADER,
    KPI_DAILY_TAB,
    KpiDailyRow,
    create_kpi_daily_tab_if_missing,
    find_prunable_rows,
    load_kpi_daily,
    perf_to_row,
    prune_old_rows,
    row_to_kpi,
    rows_for_plant_history,
    rows_for_window,
    upsert_kpi_rows,
)
from argia.core.time_utils import UTC
from argia.kpi.energy import EnergyDay
from argia.kpi.irradiance import IrradianceSource
from argia.kpi.performance import Confidence, PlantPerformanceDay


def _perf(plant_key="P1", date_iso="2026-05-14", pr=0.80,
          energy=2400.0, cf=0.25, conf=Confidence.HIGH):
    return PlantPerformanceDay(
        plant_key=plant_key, date_iso=date_iso,
        kwp_dc=500.0, kwp_ac=400.0,
        energy_kwh=energy,
        energy_per_inverter={"A": EnergyDay(
            energy_kwh=energy, energy_kwh_max=energy,
            energy_kwh_last=energy, rows_seen=60, rows_online=60,
            detected_reboot=False, discrepancy_pct=0.0,
        )},
        irradiance_kwh_m2=6.0,
        irradiance_source=IrradianceSource.SHINEMASTER,
        pr=pr, capacity_factor=cf,
        pr_confidence=conf, capacity_factor_confidence=conf,
        inverters_with_data=1, inverters_with_reboot=0,
        notes="",
    )


# ============================================================
# Header
# ============================================================


class TestHeader:
    def test_header_has_14_cols(self):
        assert len(KPI_DAILY_HEADER) == 14

    def test_pinned_column_order(self):
        # Catch reordering bugs that would break existing sheets
        assert KPI_DAILY_HEADER[0] == "date_iso"
        assert KPI_DAILY_HEADER[1] == "plant_key"
        assert KPI_DAILY_HEADER[-1] == "pr_stc"
        assert KPI_DAILY_HEADER[-2] == "written_at_utc"


# ============================================================
# Serialization
# ============================================================


class TestPerfToRow:
    def test_row_has_14_cells(self):
        row = perf_to_row(_perf())
        assert len(row) == 14

    def test_values_in_header_order(self):
        row = perf_to_row(_perf())
        assert row[KPI_DAILY_HEADER.index("plant_key")] == "P1"
        assert row[KPI_DAILY_HEADER.index("date_iso")] == "2026-05-14"
        assert row[KPI_DAILY_HEADER.index("pr")] == 0.80
        assert row[KPI_DAILY_HEADER.index("pr_confidence")] == "HIGH"
        assert row[KPI_DAILY_HEADER.index("irradiance_source")] == "shinemaster"

    def test_none_becomes_empty_string(self):
        row = perf_to_row(_perf(pr=None, energy=None))
        assert row[KPI_DAILY_HEADER.index("pr")] == ""
        assert row[KPI_DAILY_HEADER.index("energy_kwh")] == ""

    def test_pr_stc_serialized(self):
        perf = _perf()
        object.__setattr__(perf, "pr_stc", 0.90)
        row = perf_to_row(perf)
        assert row[KPI_DAILY_HEADER.index("pr_stc")] == 0.90

    def test_pr_stc_none_becomes_empty(self):
        row = perf_to_row(_perf())  # pr_stc defaults None
        assert row[KPI_DAILY_HEADER.index("pr_stc")] == ""

    def test_written_at_uses_passed_time(self):
        when = dt.datetime(2026, 5, 14, 7, 30, tzinfo=UTC)
        row = perf_to_row(_perf(), now_utc=when)
        assert "2026-05-14T07:30" in row[KPI_DAILY_HEADER.index("written_at_utc")]


class TestRowToKpi:
    def _ledger_row(self, **overrides):
        row = {
            "date_iso": "2026-05-14", "plant_key": "P1",
            "energy_kwh": "2400.0", "irradiance_kwh_m2": "6.0",
            "irradiance_source": "shinemaster",
            "pr": "0.80", "pr_confidence": "HIGH",
            "capacity_factor": "0.25", "capacity_factor_confidence": "HIGH",
            "inverters_reporting": "1", "inverters_with_reboot": "0",
            "notes": "", "written_at_utc": "2026-05-14T07:30:00+00:00",
        }
        row.update(overrides)
        return row

    def test_roundtrip(self):
        kpi = row_to_kpi(self._ledger_row())
        assert kpi is not None
        assert kpi.plant_key == "P1"
        assert kpi.pr == 0.80
        assert kpi.energy_kwh == 2400.0

    def test_invalid_date_returns_none(self):
        assert row_to_kpi(self._ledger_row(date_iso="not-a-date")) is None

    def test_missing_plant_key_returns_none(self):
        assert row_to_kpi(self._ledger_row(plant_key="")) is None

    def test_garbage_pr_becomes_none(self):
        kpi = row_to_kpi(self._ledger_row(pr="garbage"))
        assert kpi.pr is None


# ============================================================
# Window queries
# ============================================================


def _row(date_iso, plant_key="P1", pr=0.80):
    return KpiDailyRow(
        date_iso=date_iso, plant_key=plant_key,
        energy_kwh=2400.0, irradiance_kwh_m2=6.0,
        irradiance_source="shinemaster",
        pr=pr, pr_confidence="HIGH",
        capacity_factor=0.25, capacity_factor_confidence="HIGH",
        inverters_reporting=1, inverters_with_reboot=0,
        notes="", written_at_utc="",
    )


class TestRowsForWindow:
    def test_inclusive_end_date(self):
        rows = [_row(f"2026-05-{i:02d}") for i in range(1, 16)]
        result = rows_for_window(rows, "2026-05-14", window_days=7)
        dates = [r.date_iso for r in result]
        assert dates == [f"2026-05-{i:02d}" for i in range(8, 15)]
        assert "2026-05-14" in dates
        assert "2026-05-15" not in dates

    def test_filters_to_plant(self):
        rows = [
            _row("2026-05-14", "P1"), _row("2026-05-14", "P2"),
            _row("2026-05-13", "P1"), _row("2026-05-13", "P2"),
        ]
        result = rows_for_window(rows, "2026-05-14", 7, plant_key="P1")
        assert all(r.plant_key == "P1" for r in result)
        assert len(result) == 2

    def test_sorted_ascending(self):
        rows = [_row("2026-05-14"), _row("2026-05-12"), _row("2026-05-13")]
        result = rows_for_window(rows, "2026-05-14", 7)
        dates = [r.date_iso for r in result]
        assert dates == sorted(dates)

    def test_empty_when_no_match(self):
        rows = [_row("2026-04-01")]
        result = rows_for_window(rows, "2026-05-14", 7)
        assert result == []


class TestRowsForPlantHistory:
    def test_returns_oldest_first(self):
        rows = [_row("2026-05-10"), _row("2026-05-12"), _row("2026-05-11")]
        result = rows_for_plant_history(rows, "P1")
        dates = [r.date_iso for r in result]
        assert dates == sorted(dates)

    def test_filters_to_plant(self):
        rows = [_row("2026-05-10", "P1"), _row("2026-05-10", "P2")]
        result = rows_for_plant_history(rows, "P1")
        assert len(result) == 1
        assert result[0].plant_key == "P1"


# ============================================================
# Upsert
# ============================================================


class TestUpsert:
    def _row_cells(self, date_iso="2026-05-14", plant="P1", pr=0.80):
        return [
            date_iso, plant, 2400.0, 6.0, "shinemaster",
            pr, "HIGH", 0.25, "HIGH", 1, 0, "", "2026-05-14T07:30:00+00:00",
            0.88,
        ]

    def test_empty_input_no_op(self):
        sheets = MagicMock()
        result = upsert_kpi_rows(sheets, [])
        assert result == {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
        sheets.read_range.assert_not_called()

    def test_inserts_when_tab_empty(self):
        sheets = MagicMock()
        sheets.read_range.return_value = []
        result = upsert_kpi_rows(sheets, [self._row_cells()])
        assert result["inserted"] == 1
        assert result["updated"] == 0
        sheets.append_rows.assert_called_once()

    def test_updates_existing_row(self):
        sheets = MagicMock()
        sheets.read_range.return_value = [
            ["2026-05-14", "P1", 2000.0, 5.5, "shinemaster",
             0.75, "HIGH", 0.20, "HIGH", 1, 0, "", "2026-05-14T01:00:00+00:00"],
        ]
        result = upsert_kpi_rows(sheets, [self._row_cells(pr=0.80)])
        assert result["inserted"] == 0
        assert result["updated"] == 1
        sheets.write_row.assert_called_once()

    def test_unchanged_when_same(self):
        cells = self._row_cells()
        sheets = MagicMock()
        # Existing row is identical to the new row
        sheets.read_range.return_value = [list(cells)]
        result = upsert_kpi_rows(sheets, [cells])
        assert result["unchanged"] == 1
        sheets.write_row.assert_not_called()
        sheets.append_rows.assert_not_called()

    def test_mixed_insert_and_update(self):
        sheets = MagicMock()
        # Existing: P1; New: updated P1 + new P2
        sheets.read_range.return_value = [
            ["2026-05-14", "P1", 2000.0, 5.5, "shinemaster",
             0.75, "HIGH", 0.20, "HIGH", 1, 0, "", "2026-05-14T01:00:00+00:00"],
        ]
        new = [
            self._row_cells(plant="P1", pr=0.80),
            self._row_cells(plant="P2", pr=0.78),
        ]
        result = upsert_kpi_rows(sheets, new)
        assert result["updated"] == 1
        assert result["inserted"] == 1

    def test_dry_run_no_writes(self):
        sheets = MagicMock()
        sheets.read_range.return_value = []
        result = upsert_kpi_rows(sheets, [self._row_cells()], dry_run=True)
        assert result["inserted"] == 1
        sheets.append_rows.assert_not_called()
        sheets.write_row.assert_not_called()

    def test_wrong_row_width_raises(self):
        sheets = MagicMock()
        with pytest.raises(ValueError, match="14"):
            upsert_kpi_rows(sheets, [["only", "two"]])


# ============================================================
# Pruning
# ============================================================


class TestPruning:
    def _existing(self, dates_and_plants):
        return [
            [d, p, 2400.0, 6.0, "shinemaster", 0.80, "HIGH", 0.25, "HIGH",
             1, 0, "", "2026-05-14T07:30:00+00:00"]
            for d, p in dates_and_plants
        ]

    def test_find_prunable_rows_finds_old(self):
        sheets = MagicMock()
        sheets.read_range.return_value = self._existing([
            ("2026-04-15", "P1"),  # old
            ("2026-05-13", "P1"),  # in window
        ])
        prunable = find_prunable_rows(sheets, today_iso="2026-05-14",
                                      window_days=14)
        # 2026-04-15 is 29 days back; cutoff is 2026-05-14 - 14 = 2026-04-30
        assert prunable == [2]  # row 2 (A2)

    def test_dry_run_does_not_delete(self):
        sheets = MagicMock()
        sheets.read_range.return_value = self._existing([
            ("2026-04-15", "P1"),
        ])
        result = prune_old_rows(sheets, today_iso="2026-05-14", apply=False)
        assert result["found"] == 1
        assert result["deleted"] == 0
        sheets.delete_row.assert_not_called()

    def test_apply_deletes(self):
        sheets = MagicMock()
        sheets.read_range.return_value = self._existing([
            ("2026-04-15", "P1"), ("2026-04-16", "P2"),
        ])
        result = prune_old_rows(sheets, today_iso="2026-05-14", apply=True)
        assert result["found"] == 2
        assert result["deleted"] == 2
        assert sheets.delete_row.call_count == 2

    def test_deletes_bottom_up(self):
        """Critical: row indices shift when deleting. We delete from
        the bottom up to keep indices stable."""
        sheets = MagicMock()
        sheets.read_range.return_value = self._existing([
            ("2026-04-10", "P1"), ("2026-04-11", "P1"), ("2026-04-12", "P1"),
        ])
        prune_old_rows(sheets, today_iso="2026-05-14", apply=True)
        # delete_row called with rows 4, 3, 2 in that order
        call_args = [c.args[1] for c in sheets.delete_row.call_args_list]
        assert call_args == [4, 3, 2]

    def test_keeps_rows_within_window(self):
        sheets = MagicMock()
        sheets.read_range.return_value = self._existing([
            ("2026-05-13", "P1"),  # 1 day old, keep
            ("2026-05-01", "P1"),  # 13 days, keep (within 14)
            ("2026-04-29", "P1"),  # 15 days, prune
        ])
        prunable = find_prunable_rows(sheets, "2026-05-14", window_days=14)
        # Only the 04-29 row should be flagged
        assert prunable == [4]  # A2, A3, A4 → row 4

    def test_skips_garbage_date_rows(self):
        sheets = MagicMock()
        sheets.read_range.return_value = [
            ["NOT_A_DATE", "P1", 0, 0, "", 0, "", 0, "", 0, 0, "", ""],
        ]
        prunable = find_prunable_rows(sheets, "2026-05-14")
        assert prunable == []  # not pruned (also not parseable as date)


# ============================================================
# load_kpi_daily
# ============================================================


class TestLoadKpiDaily:
    def test_empty_returns_empty(self):
        sheets = MagicMock()
        sheets.read_table.return_value = []
        assert load_kpi_daily(sheets) == []

    def test_sheets_error_returns_empty(self):
        sheets = MagicMock()
        sheets.read_table.side_effect = Exception("missing tab")
        assert load_kpi_daily(sheets) == []

    def test_parses_typical_row(self):
        sheets = MagicMock()
        sheets.read_table.return_value = [{
            "date_iso": "2026-05-14", "plant_key": "P1",
            "energy_kwh": "2400", "irradiance_kwh_m2": "6.0",
            "irradiance_source": "shinemaster",
            "pr": "0.80", "pr_confidence": "HIGH",
            "capacity_factor": "0.25", "capacity_factor_confidence": "HIGH",
            "inverters_reporting": "1", "inverters_with_reboot": "0",
            "notes": "", "written_at_utc": "",
        }]
        rows = load_kpi_daily(sheets)
        assert len(rows) == 1
        assert rows[0].plant_key == "P1"
        assert rows[0].pr == 0.80


# ============================================================
# Bootstrap (prefix-safe header handling)
# ============================================================


class TestCreateKpiDailyTabIfMissing:
    def _sheets(self, header_row):
        sheets = MagicMock()
        sheets.read_range.return_value = [header_row] if header_row is not None else []
        return sheets

    def test_empty_tab_writes_header(self):
        sheets = self._sheets(None)
        assert create_kpi_daily_tab_if_missing(sheets) is True
        sheets.ensure_header.assert_called_once_with(KPI_DAILY_TAB, KPI_DAILY_HEADER)

    def test_richer_existing_header_left_untouched(self):
        # The live sheet carries our columns PLUS extra analytics columns.
        richer = list(KPI_DAILY_HEADER) + [
            "specific_yield", "availability", "soiling_loss_pct",
            "data_class", "cloud_coverage_pct", "expected_kwh",
        ]
        sheets = self._sheets(richer)
        assert create_kpi_daily_tab_if_missing(sheets) is False
        sheets.ensure_header.assert_not_called()
        sheets.write_header_row.assert_not_called()

    def test_older_prefix_header_gets_appended(self):
        older = list(KPI_DAILY_HEADER[:-1])  # missing trailing pr_stc
        sheets = self._sheets(older)
        assert create_kpi_daily_tab_if_missing(sheets) is True
        sheets.write_header_row.assert_called_once_with(KPI_DAILY_TAB, KPI_DAILY_HEADER)

    def test_divergent_header_left_untouched(self):
        sheets = self._sheets(["totally", "different", "layout"])
        assert create_kpi_daily_tab_if_missing(sheets) is False
        sheets.ensure_header.assert_not_called()
        sheets.write_header_row.assert_not_called()
