"""v195 — Sheets retirement phase 3d: Dashboard_Plant / Dashboard_Inverter
in PostgreSQL.

Locks: the switch defaults to sheet; the tables carry the tabs' exact
columns; a rewrite is one transaction (DELETE + INSERT); PG records read
back like the sheet's; every reader goes through the door; the writer
honours sheet|both|pg; parity rules.
"""
import pathlib

import pytest

from argia.report import dashboard as D
from argia.report import dashboard_pg as DP

V2 = pathlib.Path(__file__).resolve().parents[2]


def _plant_matrix():
    row = {c: "" for c in D.PLANT_COLUMNS}
    row.update({"date_mx": "2026-09-04", "bucket_ts": "2026-09-04 13:00:00",
                "hour_label": "13:00", "plant_key": "GTO1", "customer": "TAIGENE",
                "kwp_dc": 818.4, "tariff_mxn_per_kwh": 1.975, "data_start": "2024-10-24",
                "total_kwh": 412.3, "theoretical_kwh": 455.0, "irradiance_kwh_m2": 0.71,
                "irradiance_wm2": 710, "cloud_cover_pct": 12.5, "module_temp_c": 48.2,
                "ambient_temp_c": 29.1, "inverters_total": 4, "inverters_reporting": 4,
                "inverters_faulted": 0, "production_pct": 90.6})
    return [list(D.PLANT_COLUMNS), [row[c] for c in D.PLANT_COLUMNS]]


class TestMode:
    def test_default_pg(self):
        assert DP.mode({}) == "pg" and DP.writes_pg({}) and not DP.writes_sheet({})   # v205
        assert DP.reads_pg({})
        assert DP.mode({"ARGIA_DASHBOARD_SOURCE": "sheet"}) == "sheet"

    def test_both_and_pg(self):
        e = {"ARGIA_DASHBOARD_SOURCE": "both"}
        assert DP.writes_sheet(e) and DP.writes_pg(e) and DP.reads_pg(e)
        e = {"ARGIA_DASHBOARD_SOURCE": "pg"}
        assert not DP.writes_sheet(e) and DP.writes_pg(e) and DP.reads_pg(e)


class TestSchema:
    def test_tables_carry_the_tabs_columns(self):
        for c in D.PLANT_COLUMNS:
            assert f"    {c} " in DP.ENSURE_SQL.split("dashboard_inverter")[0]
        for c in D.INVERTER_COLUMNS:
            assert f"    {c} " in DP.ENSURE_SQL.split("dashboard_inverter")[1]
        assert DP.ENSURE_SQL.count("CREATE TABLE IF NOT EXISTS") == 2

    def test_select_serves_dates_as_text_in_column_order(self):
        assert DP.PLANT_SELECT.startswith("SELECT date_mx::text AS date_mx, "
                                          "to_char(bucket_ts, 'YYYY-MM-DD HH24:MI:SS') AS bucket_ts, hour_label")
        assert "fault_events FROM dashboard_inverter" in DP.INVERTER_SELECT


class TestRewrite:
    def test_one_transaction_delete_then_insert(self):
        sql = DP.build_rewrite_sql(DP.PLANT_TABLE, D.PLANT_COLUMNS, _plant_matrix())
        assert sql.startswith("BEGIN;\nDELETE FROM dashboard_plant;\nINSERT INTO dashboard_plant (date_mx, bucket_ts")
        assert sql.rstrip().endswith("COMMIT;")
        assert "('2026-09-04', '2026-09-04 13:00:00', '13:00', 'GTO1', 'TAIGENE', 818.4, 1.975, '2024-10-24', 412.3" in sql
        assert ", 4, 4, 0, 90.6)" in sql                       # integers stay integers

    def test_empty_window_still_clears(self):
        sql = DP.build_rewrite_sql(DP.PLANT_TABLE, D.PLANT_COLUMNS, [list(D.PLANT_COLUMNS)])
        assert "DELETE FROM dashboard_plant;" in sql and "INSERT" not in sql

    def test_header_mismatch_raises(self):
        with pytest.raises(ValueError):
            DP.build_rewrite_sql(DP.PLANT_TABLE, D.PLANT_COLUMNS, [["a", "b"]])

    def test_blanks_become_null_and_quotes_escape(self):
        m = _plant_matrix(); m[1][D.PLANT_COLUMNS.index("cloud_cover_pct")] = ""
        m[1][D.PLANT_COLUMNS.index("customer")] = "O'Brien"
        sql = DP.build_rewrite_sql(DP.PLANT_TABLE, D.PLANT_COLUMNS, m)
        assert "'O''Brien'" in sql and ", NULL, 48.2," in sql


class TestReadBack:
    def test_csv_records_parse_like_the_sheet(self):
        hdr = ",".join(D.PLANT_COLUMNS)
        csv = (hdr + "\n2026-09-04,2026-09-04 13:00:00,13:00,GTO1,TAIGENE,818.4,1.975,"
               "2024-10-24,412.3,455.0,0.71,710,12.5,48.2,29.1,4,4,0,90.6\n")
        recs = DP.csv_to_records(csv, D.PLANT_COLUMNS)
        r = recs[0]
        assert r["date_mx"] == "2026-09-04" and r["bucket_ts"] == "2026-09-04 13:00:00"
        assert r["total_kwh"] == 412.3 and r["inverters_total"] == 4 and r["hour_label"] == "13:00"
        # what the readers do with it
        from argia.core.normalize import safe_float
        from argia.core.cells import coerce_date
        assert coerce_date(r["date_mx"]).isoformat() == "2026-09-04"
        assert safe_float(r["total_kwh"]) == 412.3


class TestDoors:
    def test_readers_go_through_the_door(self):
        for rel in ("scripts/dashboard_html_publish.py", "argia/report/daily.py", "scripts/kpi_eod.py"):
            src = (V2 / rel).read_text(encoding="utf-8")
            assert 'read_table("Dashboard_Plant"' not in src, rel
            assert 'read_table("Dashboard_Inverter"' not in src, rel
            assert "plant_records(" in src, rel

    def test_sheet_mode_door_reads_the_tab(self, monkeypatch):
        monkeypatch.setenv("ARGIA_DASHBOARD_SOURCE", "sheet")
        calls = []
        class FS:
            def read_table(self, tab, a1):
                calls.append((tab, a1)); return [{"plant_key": "GTO1"}]
        assert DP.plant_records(FS()) == [{"plant_key": "GTO1"}]
        assert DP.inverter_records(FS()) == [{"plant_key": "GTO1"}]
        assert calls == [("Dashboard_Plant", "A1:ZZ"), ("Dashboard_Inverter", "A1:ZZ")]

    def test_pg_mode_door_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_DASHBOARD_SOURCE", "pg")
        monkeypatch.setattr(DP, "_fetch_csv", lambda sql: ",".join(D.INVERTER_COLUMNS) + "\n")
        class FS:
            def read_table(self, *a):
                raise AssertionError("sheet touched")
        assert DP.inverter_records(FS()) == []

    def test_writer_modes_in_dashboard_update(self):
        src = (V2 / "scripts" / "dashboard_update.py").read_text(encoding="utf-8")
        assert "if DP.writes_pg():" in src and "if DP.writes_sheet():" in src
        assert "DP.rewrite(DP.INVERTER_TABLE, D.INVERTER_COLUMNS, inv_matrix)" in src


class TestParity:
    @pytest.fixture
    def cmp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dashboard_parity", V2 / "scripts" / "dashboard_parity.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_serial_dates_and_float_noise_are_equal(self, cmp):
        a = [{"date_mx": 46269, "plant_key": "GTO1", "bucket_ts": "2026-09-04 13:00:00", "total_kwh": "412.3"}]
        b = [{"date_mx": "2026-09-04", "plant_key": "GTO1", "bucket_ts": "2026-09-04 13:00:00", "total_kwh": 412.30000001}]
        rep = cmp.compare(a, b, ["date_mx", "plant_key", "bucket_ts", "total_kwh"],
                          ["date_mx", "plant_key", "bucket_ts"], {"total_kwh"})
        assert rep["ok"]

    def test_missing_and_different_rows_fail(self, cmp):
        a = [{"date_mx": "2026-09-04", "plant_key": "GTO1", "bucket_ts": "13", "total_kwh": 1}]
        b = [{"date_mx": "2026-09-04", "plant_key": "GTO1", "bucket_ts": "13", "total_kwh": 2}]
        rep = cmp.compare(a, b, ["date_mx", "plant_key", "bucket_ts", "total_kwh"],
                          ["date_mx", "plant_key", "bucket_ts"], {"total_kwh"})
        assert rep["diffs"] and not rep["ok"]
        rep = cmp.compare(a, [], ["date_mx", "plant_key", "bucket_ts", "total_kwh"],
                          ["date_mx", "plant_key", "bucket_ts"], {"total_kwh"})
        assert rep["only_sheet"] and not rep["ok"]
        assert "VERDICT" in cmp.render("x", rep)
