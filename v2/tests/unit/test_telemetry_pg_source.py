"""v189 — Phase 1 of the Sheets retirement: telemetry readers on PostgreSQL.

What is locked here:
- the PG grid has the sheet's exact shape and cell typing, so kpi.reader,
  alerts_snapshot and dashboard_update parse it through UNCHANGED code;
- the switch defaults to 'sheet' in this release and never guesses;
- the parity comparator (the gate) is exact and reports precisely;
- telemetry_5m: sheet write behind ARGIA_SHEET_TELEMETRY, and a PG
  failure is FATAL once the sheet is off (PG is the record then);
- the archive's PG mode is an export that never deletes.
"""
import datetime as dt
import pathlib

import pytest

from argia.kpi import reader
from argia.telemetry import pg_source as P
from argia.telemetry.schema import ARGIA_SCHEMA

V2 = pathlib.Path(__file__).resolve().parents[2]
CSV = (
    "ts_utc,vendor,plant_key,inverter_sn,inverter_label,status,power_w,"
    "etoday_kwh,temperature_c,fault_code,irradiance_wm2,"
    "irradiance_kwh_m2_5m,cloud_cover_pct,ambient_temp_c,module_temp_c\n"
    "2026-09-03 19:00:10+00,GROWATT,NL1,JGMAE65009,Inverter 1,1,102611.00,"
    "493.500,81.20,0,974.00,0.0812,66.80,37.40,56.70\n"
    "2026-09-03 19:05:27+00,HUAWEI,MEX1,ES2470051825,Inverter 1,1,0,943.74,"
    "47,\"IS=40960,RS=1\",0,0,45.6,17.2,14.8\n"
    "2026-09-03 19:05:27+00,HUAWEI,MEX1,ES2470051826,Inverter 2,,,,,,,,,,\n"
)


class TestGridShape:
    def test_header_is_the_argia_schema(self):
        g = P.csv_to_grid(CSV)
        assert g[0] == list(ARGIA_SCHEMA.columns)
        assert len(g[0]) == 16

    def test_cells_are_typed_like_read_range_unformatted(self):
        r = P.csv_to_grid(CSV)[1]
        assert r[0] == "2026-09-03T19:00:10+00:00"      # ISO text, as the sheet
        assert r[1] == "2026-09-03 13:00:10"            # MX wall clock
        assert r[6] == 1                                # status -> int
        assert r[7] == 102611.0 and isinstance(r[7], float)
        assert r[10] == 0                               # fault_code '0' -> number

    def test_text_fault_code_stays_text(self):
        r = P.csv_to_grid(CSV)[2]
        assert r[10] == "IS=40960,RS=1"

    def test_null_numerics_become_blank_like_the_sheet(self):
        r = P.csv_to_grid(CSV)[3]
        assert r[6] == "" and r[7] == "" and r[15] == ""

    def test_records_keyed_by_header(self):
        recs = P.grid_to_records(P.csv_to_grid(CSV))
        assert recs[0]["plant_key"] == "NL1"
        assert recs[0]["timestamp_mx"] == "2026-09-03 13:00:10"

    def test_rows_without_a_natural_key_are_dropped(self):
        bad = CSV.split("\n")[0] + "\n,GROWATT,NL1,,,,,,,,,,,,\n"
        assert len(P.csv_to_grid(bad)) == 1


class TestExistingParsersAcceptTheGrid:
    def test_kpi_reader_parses_pg_grid_unchanged(self):
        rows = reader.parse_rows(P.csv_to_grid(CSV))
        assert len(rows) == 3
        nl1 = [r for r in rows if r.plant_key == "NL1"][0]
        assert nl1.power_w == 102611.0
        assert nl1.timestamp_utc == dt.datetime(2026, 9, 3, 19, 0, 10,
                                                tzinfo=dt.timezone.utc)
        assert nl1.fault_code == "0"
        assert nl1.module_temp_c == 56.7

    def test_dashboard_coerce_ts_accepts_the_mx_string(self):
        from argia.core.cells import coerce_ts
        recs = P.grid_to_records(P.csv_to_grid(CSV))
        assert coerce_ts(recs[0]["timestamp_mx"]) == \
            dt.datetime(2026, 9, 3, 13, 0, 10)


class TestSwitches:
    def test_source_defaults_to_sheet_and_never_guesses(self):
        assert P.source({}) == "sheet"
        assert P.source({"ARGIA_TELEMETRY_SOURCE": "pg"}) == "pg"
        assert P.source({"ARGIA_TELEMETRY_SOURCE": "PG "}) == "pg"
        assert P.source({"ARGIA_TELEMETRY_SOURCE": "postgres"}) == "sheet"

    def test_sheet_write_defaults_on_in_this_release(self):
        assert P.sheet_write_enabled({}) is True
        assert P.sheet_write_enabled({"ARGIA_SHEET_TELEMETRY": "0"}) is False


class TestSql:
    def test_day_filter_is_the_mx_calendar_day(self):
        w = P.where_clause(date_iso="2026-09-03")
        assert "AT TIME ZONE 'America/Mexico_City')::date = DATE '2026-09-03'" in w

    def test_since_filter_is_utc(self):
        t = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-6)))
        assert "ts_utc >= '2026-09-03T18:00:00+00:00'::timestamptz" in \
            P.where_clause(since_utc=t)

    def test_plants_are_quoted_and_uppercased(self):
        assert "plant_key IN ('GTO1','MEX1')" in P.where_clause(plants=["gto1", "MEX1"])

    def test_bad_date_is_refused(self):
        with pytest.raises(ValueError):
            P.where_clause(date_iso="2026-13-01")

    def test_order_is_deterministic(self):
        assert P.select_sql("2026-09-03").endswith(
            "ORDER BY ts_utc, plant_key, inverter_sn;")


class TestParityComparator:
    def _row(self, ts="2026-09-03T19:00:10+00:00", pk="NL1", sn="A", pw=100.0):
        return reader.parse_rows([list(ARGIA_SCHEMA.columns),
            [ts, "", "GROWATT", pk, sn, "Inv", 1, pw, 1, 25, 0, 900, 0.07, 10, 30, 40]])[0]

    def test_identical_is_ok(self):
        from scripts.telemetry_parity import compare
        rep = compare([self._row()], [self._row()])
        assert rep["ok"] and rep["field_diffs"] == []

    def test_missing_row_is_reported_by_side(self):
        from scripts.telemetry_parity import compare
        rep = compare([self._row(), self._row(sn="B")], [self._row()])
        assert not rep["ok"] and len(rep["only_sheet"]) == 1

    def test_field_difference_names_the_field(self):
        from scripts.telemetry_parity import compare
        rep = compare([self._row(pw=100.0)], [self._row(pw=101.0)])
        assert not rep["ok"]
        assert rep["field_diffs"][0][1] == "power_w"

    def test_float_noise_is_not_a_difference(self):
        from scripts.telemetry_parity import compare
        rep = compare([self._row(pw=100.0)], [self._row(pw=100.0000001)])
        assert rep["ok"]

    def test_render_shows_the_verdict(self):
        from scripts.telemetry_parity import compare, render
        out = render(compare([self._row()], [self._row()]), "2026-09-03")
        assert "VERDICT: IDENTICAL" in out


class TestWiring:
    def test_kpi_reader_takes_the_pg_path_only_when_told(self):
        src = (V2 / "argia" / "kpi" / "reader.py").read_text(encoding="utf-8")
        assert 'if src == "pg":' in src
        assert "pg_source.read_grid(date_iso=date_iso)" in src

    def test_alerts_snapshot_and_dashboard_are_wired(self):
        a = (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
        d = (V2 / "scripts" / "dashboard_update.py").read_text(encoding="utf-8")
        assert "pg_source.read_grid(since_utc=since)" in a
        assert "pg_source.read_records(since_utc=since)" in d

    def test_telemetry_5m_sheet_write_is_gated_and_pg_failure_is_fatal(self):
        s = (V2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
        assert "if all_common and sheet_on:" in s
        assert "PostgreSQL telemetry write FAILED and the sheet" in s
        assert "no telemetry sink" in s

    def test_archive_pg_mode_never_deletes(self):
        s = (V2 / "scripts" / "telemetry_archive.py").read_text(encoding="utf-8")
        head, _, pg_part = s.partition("def export_days_pg")
        body = pg_part.split("def _setup_logging")[0]
        assert "delete_row" not in body and "delete_row_range" not in body
        assert "drive.find_file(folder, name) is not None" in body   # idempotent


class TestExportDates:
    def test_window_is_complete_days_only(self):
        from scripts.telemetry_archive import export_dates
        d = export_dates(dt.date(2026, 9, 4), 3, None)
        assert d == ["2026-09-03", "2026-09-02", "2026-09-01"]

    def test_explicit_date_wins(self):
        from scripts.telemetry_archive import export_dates
        assert export_dates(dt.date(2026, 9, 4), 3, "2026-08-20") == ["2026-08-20"]
