"""v184: replaying Telemetry_Argia from Postgres after a sheet outage.

The 2026-09-03 incident: the workbook hit its 10M-cell cap and every
append failed 11:00..18:00 MX. Postgres kept every row. These tests pin
the round trip PG -> sheet row so a replay lands on the SAME natural key
the collector would have written (timestamp_utc, plant_key, inverter_sn)
and therefore fills holes instead of duplicating a day.
"""

import datetime as dt

import pytest

from argia.telemetry.schema import ARGIA_SCHEMA
from scripts.telemetry_sheet_backfill import (
    PG_COLS, SELECT_SQL, parse_pg_ts, rows_from_csv, sheet_row,
)

# a real row as psql prints it (note the embedded comma in fault_code)
CSV = (
    "ts_utc,vendor,plant_key,inverter_sn,inverter_label,status,power_w,"
    "etoday_kwh,temperature_c,fault_code,irradiance_wm2,"
    "irradiance_kwh_m2_5m,cloud_cover_pct,ambient_temp_c,module_temp_c\n"
    "2026-09-03 19:00:10+00,GROWATT,NL1,JGMAE65009,Inverter 1,1,102611.00,"
    "493.500,81.20,0,974.00,0.0812,66.80,37.40,56.70\n"
    "2026-09-03 19:05:27+00,HUAWEI,MEX1,ES2470051825,Inverter 1,1,0,943.74,"
    "47,\"IS=40960,RS=1\",0,0,45.6,17.2,14.8\n"
)


class TestParsePgTs:
    def test_short_utc_offset(self):
        d = parse_pg_ts("2026-09-03 19:00:10+00")
        assert d == dt.datetime(2026, 9, 3, 19, 0, 10, tzinfo=dt.timezone.utc)

    def test_full_iso_offset(self):
        assert parse_pg_ts("2026-09-03T19:00:10+00:00") == \
            parse_pg_ts("2026-09-03 19:00:10+00")

    def test_naive_is_treated_as_utc(self):
        assert parse_pg_ts("2026-09-03 19:00:10").tzinfo is not None

    def test_non_utc_offset_is_converted(self):
        # -06:00 local 13:00 is 19:00 UTC — same instant as the row above
        assert parse_pg_ts("2026-09-03 13:00:10-06:00") == \
            parse_pg_ts("2026-09-03 19:00:10+00")


class TestSheetRow:
    @pytest.fixture
    def row(self):
        return rows_from_csv(CSV)[0]

    def test_width_matches_the_tab_schema(self, row):
        assert len(row) == ARGIA_SCHEMA.column_count == 16

    def test_timestamp_utc_matches_the_collectors_spelling(self, row):
        # the live collector writes isoformat() with a +00:00 offset;
        # the natural key is a string compare, so this must be exact
        assert row[0] == "2026-09-03T19:00:10+00:00"

    def test_timestamp_mx_is_local_wall_clock(self, row):
        assert row[1] == "2026-09-03 13:00:10"

    def test_payload_columns_land_in_schema_order(self, row):
        cols = list(ARGIA_SCHEMA.columns)
        assert row[cols.index("vendor")] == "GROWATT"
        assert row[cols.index("plant_key")] == "NL1"
        assert row[cols.index("inverter_sn")] == "JGMAE65009"
        assert row[cols.index("power_w")] == "102611.00"
        assert row[cols.index("etoday_kwh")] == "493.500"
        assert row[cols.index("irradiance_wm2")] == "974.00"
        assert row[cols.index("module_temp_c")] == "56.70"
        assert row[cols.index("ambient_temp_c")] == "37.40"

    def test_fault_code_with_an_embedded_comma_survives(self):
        second = rows_from_csv(CSV)[1]
        cols = list(ARGIA_SCHEMA.columns)
        assert second[cols.index("fault_code")] == "IS=40960,RS=1"
        assert len(second) == 16

    def test_missing_key_fields_are_dropped_not_written_blank(self):
        bad = CSV.split("\n")[0] + "\n,GROWATT,,,,,,,,,,,,,\n"
        assert rows_from_csv(bad) == []

    def test_replay_is_deterministic(self):
        assert rows_from_csv(CSV) == rows_from_csv(CSV)


class TestQuery:
    def test_selects_every_schema_column_it_can(self):
        # timestamp_mx is derived, not stored — everything else comes from PG
        derived = {"timestamp_mx"}
        stored = {c for c in ARGIA_SCHEMA.columns} - derived
        assert stored - set(PG_COLS) - {"timestamp_utc"} == set()
        assert PG_COLS[0] == "ts_utc"

    def test_filters_on_the_mx_calendar_day(self):
        assert "AT TIME ZONE 'America/Mexico_City')::date" in SELECT_SQL

    def test_orders_deterministically(self):
        assert "ORDER BY ts_utc, plant_key, inverter_sn" in SELECT_SQL
