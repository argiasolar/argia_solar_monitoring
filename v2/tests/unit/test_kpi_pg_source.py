"""v190 — Sheets retirement phase 2a: KPI_Daily readers on PostgreSQL.

Locks: the mirror now carries all 24 KPI_Daily columns with the same
protected/frozen semantics; the PG grid has the sheet's shape so the six
readers parse it unchanged; the switch defaults to sheet; the parity
comparator is exact; every reader goes through the one door.
"""
import pathlib

import pytest

from argia.kpi import pg_kpi_source as K
from argia.store import kpi_mirror as M

V2 = pathlib.Path(__file__).resolve().parents[2]
CSV = ("date_iso,plant_key,energy_kwh,irradiance_kwh_m2,irradiance_source,pr,"
       "pr_confidence,capacity_factor,capacity_factor_confidence,"
       "inverters_reporting,inverters_with_reboot,notes,written_at_utc,pr_stc,"
       "specific_yield,availability,soiling_loss_pct,data_class,"
       "cloud_coverage_pct,expected_kwh,production_pct,status_note,design_kwh,"
       "billable_kwh\n"
       "2026-09-03,GTO2,1716.47,7.721,shinemaster_history,0.3959,HIGH,0.1788,HIGH,"
       "4,0,,2026-09-04T12:00:22+00:00,0.4196,3.0564,0.75,0.5342,full,61.8431,"
       "3252.09,0.5278,\"Well below plan (53%) — inverter availability 75% — see Alerts\","
       "2688.3,1716.47\n")


class TestMirrorIsComplete:
    def test_every_sheet_column_has_a_pg_home(self):
        for h in K.HEADER:
            if h in ("date_iso", "plant_key"):
                continue
            assert h in M.COLMAP, f"{h} not mirrored"

    def test_ensure_sql_adds_the_new_columns_idempotently(self):
        assert "ADD COLUMN IF NOT EXISTS specific_yield" in M.ENSURE_SQL
        assert "ADD COLUMN IF NOT EXISTS design_kwh" in M.ENSURE_SQL
        assert M.ENSURE_SQL.count("ADD COLUMN IF NOT EXISTS") == 11

    def test_new_columns_keep_the_protected_and_frozen_semantics(self):
        sql = M.build_upsert_sql([{"prod_date": "2026-09-03", "plant_key": "GTO2",
                                   "specific_yield": 3.05, "design_kwh": 2688.3}])
        assert "specific_yield = CASE WHEN EXISTS" in sql          # frozen month
        assert "COALESCE(EXCLUDED.design_kwh, daily_production.design_kwh)" in sql
        assert "energy_kwh = CASE WHEN EXISTS" in sql               # unchanged

    def test_normalize_types_the_new_columns(self):
        rows = M.normalize_rows([{"date_iso": "2026-09-03", "plant_key": "gto2",
                                  "inverters_with_reboot": "0", "soiling_loss_pct": "0.53",
                                  "irradiance_source": "shinemaster_history"}],
                                lambda v: str(v))
        r = rows[0]
        assert r["inverters_with_reboot"] == 0
        assert r["soiling_loss_pct"] == 0.53
        assert r["irradiance_source"] == "shinemaster_history"


class TestGridShape:
    def test_header_matches_the_live_tab_order(self):
        assert K.HEADER[:14] == [
            "date_iso", "plant_key", "energy_kwh", "irradiance_kwh_m2",
            "irradiance_source", "pr", "pr_confidence", "capacity_factor",
            "capacity_factor_confidence", "inverters_reporting",
            "inverters_with_reboot", "notes", "written_at_utc", "pr_stc"]
        assert len(K.HEADER) == 24 and K.HEADER[-1] == "billable_kwh"

    def test_cells_typed_and_dates_iso(self):
        g = K.csv_to_grid(CSV)
        r = dict(zip(g[0], g[1]))
        assert r["date_iso"] == "2026-09-03"
        assert r["energy_kwh"] == 1716.47 and isinstance(r["energy_kwh"], float)
        assert r["inverters_reporting"] == 4
        assert r["data_class"] == "full"
        assert r["notes"] == ""
        assert "see Alerts" in r["status_note"]

    def test_existing_parser_accepts_the_grid(self):
        from argia.archive.kpi_daily import row_to_kpi
        rec = K.grid_to_records(K.csv_to_grid(CSV))[0]
        k = row_to_kpi(rec)
        assert k is not None and k.plant_key == "GTO2" and k.energy_kwh == 1716.47
        assert k.date_iso == "2026-09-03"

    def test_select_aliases_every_header(self):
        sql = K.select_sql("2026-01-01")
        for h in K.HEADER:
            assert f" AS {h}" in sql
        assert "prod_date >= DATE '2026-01-01'" in sql
        assert "cloud_cover_pct AS cloud_coverage_pct" in sql     # the one rename


class TestSwitch:
    def test_defaults_to_sheet(self):
        assert K.source({}) == "sheet"
        assert K.source({"ARGIA_KPI_SOURCE": "pg"}) == "pg"

    def test_one_door_for_every_reader(self):
        """No live reader may call the sheet for KPI_Daily directly any more
        — that is how the two sources would drift apart again."""
        readers = ["scripts/alerts_daily.py", "argia/finance/income.py",
                   "argia/finance/annex.py", "argia/report/daily.py",
                   "scripts/dashboard_update.py"]
        for rel in readers:
            src = (V2 / rel).read_text(encoding="utf-8")
            assert 'read_range(KPI_DAILY_TAB, "A1:ZZ")' not in src, rel
            assert 'read_table("KPI_Daily"' not in src, rel
            assert "pg_kpi_source" in src, rel
        # load_kpi_daily (archive) too
        src = (V2 / "argia/archive/kpi_daily.py").read_text(encoding="utf-8")
        assert 'kpi_records(sheets, "A1:O")' in src

    def test_the_mirror_itself_still_reads_the_sheet(self):
        # kpi_pg_mirror is the sheet->PG bridge until phase 2b; it must not
        # read PG and write PG
        src = (V2 / "scripts/kpi_pg_mirror.py").read_text(encoding="utf-8")
        assert 'read_table("KPI_Daily"' in src
        assert "ENSURE_SQL" in src


class TestParity:
    def _g(self, rows):
        return [list(K.HEADER)] + rows

    def _row(self, d="2026-09-03", pk="GTO2", energy=1716.47, note="x"):
        r = [""] * len(K.HEADER)
        r[0], r[1], r[2] = d, pk, energy
        r[K.HEADER.index("status_note")] = note
        return r

    def test_identical(self):
        from scripts.kpi_parity import compare
        assert compare(self._g([self._row()]), self._g([self._row()]), "2026-01-01")["ok"]

    def test_serial_date_on_the_sheet_side_matches_iso_on_pg(self):
        from scripts.kpi_parity import compare
        s = self._row(d=46268)            # 2026-09-03 as a sheet serial
        assert compare(self._g([s]), self._g([self._row()]), "2026-01-01")["ok"]

    def test_numeric_rounding_ok_real_change_not(self):
        from scripts.kpi_parity import compare
        assert compare(self._g([self._row(energy=1716.47)]),
                       self._g([self._row(energy=1716.472)]), "2026-01-01")["ok"]
        rep = compare(self._g([self._row(energy=1716.47)]),
                      self._g([self._row(energy=1717.0)]), "2026-01-01")
        assert not rep["ok"] and rep["diffs"][0][1] == "energy_kwh"

    def test_written_at_is_ignored_but_status_note_is_not(self):
        from scripts.kpi_parity import compare
        a, b = self._row(note="A"), self._row(note="B")
        assert not compare(self._g([a]), self._g([b]), "2026-01-01")["ok"]
        a2, b2 = self._row(), self._row()
        a2[K.HEADER.index("written_at_utc")] = "t1"
        b2[K.HEADER.index("written_at_utc")] = "t2"
        assert compare(self._g([a2]), self._g([b2]), "2026-01-01")["ok"]

    def test_missing_on_one_side_is_reported(self):
        from scripts.kpi_parity import compare
        rep = compare(self._g([self._row(), self._row(pk="NL1")]),
                      self._g([self._row()]), "2026-01-01")
        assert rep["only_sheet"] == [("2026-09-03", "NL1")]


class TestParityKnowsWherePgWins:
    """The gate's first live run: every difference fell into a class where
    PostgreSQL is the designed authority. The gate must say so, and must
    still fail on anything else."""

    def _g(self, rows):
        return [list(K.HEADER)] + rows

    def _row(self, d="2026-08-02", pk="GTO1", **over):
        r = [""] * len(K.HEADER)
        r[0], r[1] = d, pk
        for c, v in over.items():
            r[K.HEADER.index(c)] = v
        return r

    def test_pr_on_a_vendor_row_is_expected_not_a_failure(self):
        from scripts.kpi_parity import compare
        rep = compare(self._g([self._row(pr=0.3429)]),
                      self._g([self._row(pr=0.4505)]), "2026-01-01",
                      frozenset(), frozenset({("2026-08-02", "GTO1")}))
        assert rep["ok"] and len(rep["expected"]) == 1 and not rep["unexpected"]

    def test_status_note_on_a_frozen_month_is_expected(self):
        from scripts.kpi_parity import compare
        rep = compare(self._g([self._row(status_note="Above plan")]),
                      self._g([self._row(status_note="Above plan | billable raised")]),
                      "2026-01-01", frozenset({("GTO1", "2026-08")}), frozenset())
        assert rep["ok"]

    def test_the_same_diff_on_an_open_non_vendor_row_fails(self):
        from scripts.kpi_parity import compare
        rep = compare(self._g([self._row(pr=0.3429)]),
                      self._g([self._row(pr=0.4505)]), "2026-01-01")
        assert not rep["ok"] and rep["unexpected"][0][1] == "pr"

    def test_a_non_protected_column_differing_always_fails(self):
        from scripts.kpi_parity import compare
        rep = compare(self._g([self._row(specific_yield=4.45)]),
                      self._g([self._row(specific_yield="")]), "2026-01-01",
                      frozenset({("GTO1", "2026-08")}), frozenset({("2026-08-02", "GTO1")}))
        assert not rep["ok"]                      # new column NULL is NOT ok

    def test_more_history_in_pg_is_fine_missing_in_pg_is_not(self):
        from scripts.kpi_parity import compare
        a = self._row(d="2026-08-02")
        old = self._row(d="2024-03-01")
        assert compare(self._g([a]), self._g([a, old]), "2024-01-01")["ok"]
        assert not compare(self._g([a, old]), self._g([a]), "2024-01-01")["ok"]


class TestFillNullsOnly:
    def test_sql_fills_only_nulls_and_ignores_the_freeze_on_purpose(self):
        sql = M.build_fill_nulls_sql([{"prod_date": "2026-07-01", "plant_key": "GTO1",
                                       "specific_yield": 4.4565, "design_kwh": None,
                                       "irradiance_source": "shinemaster_history"}])
        assert "specific_yield = COALESCE(daily_production.specific_yield, 4.4565)" in sql
        assert "irradiance_source = COALESCE(daily_production.irradiance_source, 'shinemaster_history')" in sql
        assert "design_kwh" not in sql.split("WHERE")[0]      # None: nothing to fill
        assert "reconciliation_monthly" not in sql             # no freeze: cannot change a value
        assert "IS NULL" in sql

    def test_never_touches_protected_columns(self):
        sql = M.build_fill_nulls_sql([{"prod_date": "2026-07-01", "plant_key": "GTO1",
                                       "energy_kwh": 1.0, "specific_yield": 2.0}])
        assert "energy_kwh" not in sql

    def test_nothing_to_fill_is_none(self):
        assert M.build_fill_nulls_sql([{"prod_date": "2026-07-01", "plant_key": "GTO1"}]) is None
