"""v198 — Sheets retirement phase 5a: Plants / Inverters config in
PostgreSQL behind ARGIA_CONFIG_SOURCE.

Locks: the switch defaults to sheet; the pinned headers are the live
tabs'; the DDL adds exactly the missing columns (text unless date/flag);
PG records parse through load_portfolio to the same PlantConfig /
InverterConfig as the sheet (flags 'TRUE'/'FALSE', dates ISO, mixed text
kept); the fill SQL never touches an existing column; every live reader
goes through the door; parity is authority-aware and date-aware.
"""
import pathlib

import pytest

from argia.core import config_pg as C
from argia.core.config import load_portfolio

V2 = pathlib.Path(__file__).resolve().parents[2]


def sheet_plant_row(**over):
    r = {c: "" for c in C.PLANTS_HEADER}
    r.update({"plant_key": "GTO1", "customer": "TAIGENE PPA roof (Leon, GTO)", "brand": "GROWATT",
              "site_id": 9309575, "kwp_dc": 818.33, "kwp_ac": 680, "lat": 21.10420124,
              "lon": -101.7553161, "expected_factor": 0.74, "pr_target": 0.9,
              "installation_date": 45589, "secret_api_name": "GROWATT_API",
              "secret_user_name": "GROWATT_USER", "secret_pass_name": "GROWATT_PASS",
              "weather_plant_id": 9309575, "datalogger_sn": "DYD0E8501G", "datalogger_addr": 1,
              "active": True, "module_count": 1387, "module_wp": "590+650", "string_count": 77,
              "tilt_deg": 15, "azimuth_deg": 180, "tariff_mxn_per_kwh": 1.975,
              "pr_stc_model": 0.87, "gamma_pmax": -0.0035, "monitoring_class": "B",
              "p90_annual_kwh": 966456, "contracted_kwh": 606, "date_interconnection": 45352,
              "billing_scheme": "net_metering", "pr_baseline": 0.9, "om_cost_monthly_mxn": 3000,
              "portfolio": "PPA", "show_dashboard": True, "show_daily_report": True,
              "show_financial": True})
    r.update(over)
    return r


PG_PLANT_CSV = (",".join(C.PLANTS_HEADER) + "\n"
                "GTO1,\"TAIGENE PPA roof (Leon, GTO)\",GROWATT,9309575,818.330,680.000,21.104201,"
                "-101.755316,0.74,0.9,2024-10-24,GROWATT_API,GROWATT_USER,GROWATT_PASS,9309575,"
                "DYD0E8501G,1,TRUE,1387,590+650,77,15,180,,1.9750,,,0.87,-0.0035,B,966456,606.000,"
                "2024-03-01,net_metering,,0.8800,3000.00,PPA,TRUE,TRUE,TRUE,\n")
PG_INV_CSV = (",".join(C.INVERTERS_HEADER) + "\n"
              "GTO1,JFM7DXN00T,Inverter 1,124.000,TRUE,8,9,131.5,P1,2024-10-24,,TRUE\n")


class FakeSheets:
    def __init__(self, plants=None, invs=None):
        self.plants, self.invs, self.calls = plants or [], invs or [], []

    def read_table(self, tab, a1="A1:Z"):
        self.calls.append((tab, a1))
        return self.plants if tab == "Plants" else self.invs


class TestShape:
    def test_switch_default(self):
        assert C.source({}) == "pg" and C.source({"ARGIA_CONFIG_SOURCE": "sheet"}) == "sheet"     # v205: pg by default

    def test_ddl_adds_only_the_missing_columns(self):
        assert C.ENSURE_SQL.count("ALTER TABLE plant ADD COLUMN IF NOT EXISTS") == len(C.PLANTS_HEADER) - len(C.PLANT_EXISTING)
        assert C.ENSURE_SQL.count("ALTER TABLE inverter ADD COLUMN IF NOT EXISTS") == 4
        assert "ADD COLUMN IF NOT EXISTS installation_date date;" in C.ENSURE_SQL
        assert "ADD COLUMN IF NOT EXISTS show_dashboard boolean;" in C.ENSURE_SQL
        assert "ADD COLUMN IF NOT EXISTS module_wp text;" in C.ENSURE_SQL
        assert "ADD COLUMN IF NOT EXISTS kwp_dc " not in C.ENSURE_SQL       # existing, untouched

    def test_select_serves_flags_and_dates_like_the_sheet(self):
        assert "CASE WHEN active THEN 'TRUE' WHEN active IS NULL THEN '' ELSE 'FALSE' END AS active" in C.PLANTS_SELECT
        assert "installation_date::text AS installation_date" in C.PLANTS_SELECT
        assert C.PLANTS_SELECT.endswith("FROM plant ORDER BY plant_key;")
        assert "in_service_today" in C.INVERTERS_SELECT


class TestSameParser:
    def test_pg_plant_parses_like_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "pg")
        monkeypatch.setattr(C, "_fetch_csv", lambda sql: PG_PLANT_CSV if "FROM plant" in sql else PG_INV_CSV)
        pg = load_portfolio(FakeSheets())
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "sheet")
        sh = load_portfolio(FakeSheets([sheet_plant_row(pr_baseline=0.88)],
                                       [{"plant_key": "GTO1", "inverter_sn": "JFM7DXN00T",
                                         "inverter_label": "Inverter 1", "rated_kw": 124, "active": True,
                                         "mppt_count": 8, "strings_total": 9, "rated_kw_dc": 131.5,
                                         "phase": "P1", "date_producing": 45589, "in_service_today": True}]))
        a, b = sh.plants["GTO1"], pg.plants["GTO1"]
        for f in ("plant_key", "customer", "brand", "site_id", "kwp_dc", "kwp_ac", "expected_factor",
                  "pr_target", "secret_api_name", "datalogger_addr", "active", "module_count",
                  "module_wp", "string_count", "tilt_deg", "azimuth_deg", "tariff_mxn_per_kwh",
                  "gamma_pmax", "pr_baseline", "om_cost_monthly_mxn", "portfolio", "show_dashboard",
                  "show_daily_report", "show_financial", "client_channel"):
            assert getattr(a, f) == getattr(b, f), f
        assert abs(a.lat - b.lat) < 1e-5 and abs(a.lon - b.lon) < 1e-5
        assert a.installation_date == "45589" and b.installation_date == "2024-10-24"   # date-aware parity
        ia, ib = sh.inverters_by_plant["GTO1"][0], pg.inverters_by_plant["GTO1"][0]
        assert ia == ib

    def test_blank_flag_stays_blank(self):
        recs = C.csv_to_records(",".join(C.PLANTS_HEADER) + "\nQRO1" + "," * (len(C.PLANTS_HEADER) - 1) + "\n",
                                C.PLANTS_HEADER)
        assert recs[0]["show_dashboard"] == "" and recs[0]["kwp_dc"] == ""


class TestFill:
    def test_fill_touches_new_columns_only(self):
        sql = C.build_fill_sql("plant", C.PLANTS_HEADER, C.PLANT_EXISTING, ["plant_key"],
                               [sheet_plant_row()])
        assert sql.startswith("INSERT INTO plant (plant_key, customer")
        assert "ON CONFLICT (plant_key) DO UPDATE SET" in sql
        assert "expected_factor = COALESCE(plant.expected_factor, EXCLUDED.expected_factor)" in sql
        assert "installation_date = COALESCE(plant.installation_date, EXCLUDED.installation_date)" in sql
        for c in C.PLANT_EXISTING:
            assert f"{c} = COALESCE" not in sql, c
        assert "DATE '2024-10-24'" in sql and "DATE '2024-03-01'" in sql      # serials -> dates
        assert "'590+650'" in sql and ", TRUE, " in sql

    def test_fill_literals(self):
        assert C._lit("active", "FALSE") == "FALSE" and C._lit("active", "") == "NULL"
        assert C._lit("installation_date", "") == "NULL"
        assert C._lit("kwp_dc", "1,234.5") == "1234.5" and C._lit("kwp_dc", "n/a") == "NULL"
        assert C._lit("notes", "it's") == "'it''s'"
        assert C.build_fill_sql("plant", C.PLANTS_HEADER, C.PLANT_EXISTING, ["plant_key"], []) == ""


class TestDoors:
    def test_sheet_mode_issues_the_old_calls(self, monkeypatch):
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "sheet")
        fs = FakeSheets()
        load_portfolio(fs)
        assert fs.calls == [("Plants", "A1:AZ"), ("Inverters", "A1:Z")]

    def test_pg_mode_never_touches_the_sheet(self, monkeypatch):
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "pg")
        monkeypatch.setattr(C, "_fetch_csv", lambda sql: PG_PLANT_CSV if "FROM plant" in sql else PG_INV_CSV)
        fs = FakeSheets()
        p = load_portfolio(fs)
        assert fs.calls == [] and list(p.plants) == ["GTO1"]

    def test_pg_read_failure_raises(self, monkeypatch):
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "pg")
        monkeypatch.setattr(C, "_fetch_csv", lambda sql: (_ for _ in ()).throw(RuntimeError("psql failed")))
        with pytest.raises(RuntimeError):
            load_portfolio(FakeSheets())

    def test_live_readers_go_through_the_door(self):
        for rel in ("argia/core/config.py", "scripts/dashboard_update.py",
                    "scripts/dashboard_html_publish.py"):
            src = (V2 / rel).read_text(encoding="utf-8")
            assert 'read_table("Plants"' not in src, rel
            assert 'read_table("Inverters"' not in src, rel
            assert "plants_records(" in src, rel


class TestParity:
    @pytest.fixture
    def cmp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "config_backfill_pg", V2 / "scripts" / "config_backfill_pg.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def _plants(self, monkeypatch, **pg_over):
        monkeypatch.setenv("ARGIA_CONFIG_SOURCE", "sheet")
        sh = load_portfolio(FakeSheets([sheet_plant_row()])).plants
        pg = load_portfolio(FakeSheets([sheet_plant_row(installation_date="2024-10-24", **pg_over)])).plants
        return sh, pg

    def test_dates_and_audited_fields(self, cmp, monkeypatch):
        sh, pg = self._plants(monkeypatch, pr_baseline=0.88, lat=21.104201)
        rep = cmp.compare_plants(sh, pg, frozenset({("GTO1", "pr_baseline")}))
        assert rep["ok"] and rep["diffs"] == []
        assert rep["expected"] == [("GTO1", "pr_baseline", 0.9, 0.88)]
        rep = cmp.compare_plants(sh, pg, frozenset())
        assert not rep["ok"] and rep["diffs"] == [("GTO1", "pr_baseline", 0.9, 0.88)]

    def test_unaudited_change_fails(self, cmp, monkeypatch):
        sh, pg = self._plants(monkeypatch, kwp_ac=700)
        rep = cmp.compare_plants(sh, pg, frozenset({("GTO1", "pr_baseline")}))
        assert rep["diffs"] == [("GTO1", "kwp_ac", 680.0, 700.0)] and not rep["ok"]

    def test_missing_plant_fails(self, cmp, monkeypatch):
        sh, pg = self._plants(monkeypatch)
        assert not cmp.compare_plants(sh, {}, frozenset())["ok"]
        assert "VERDICT: CLEAN" in cmp.render("x", cmp.compare_plants(sh, pg, frozenset()))

    def test_site_id_fix_is_narrow(self, cmp):
        assert "WHERE site_id ~ '\\.0$'" in cmp.SITE_ID_FIX_SQL
