"""v203 — telemetry_detail: the wide vendor row is kept in PostgreSQL.

Locks: column set derives from PLANT_SCHEMA (a schema change shows up
here), literals are typed (ints for codes, arrays for channel families,
trailing empties trimmed), the upsert replaces every column, the mirror
is fail-soft and wired into every vendor's plant-row path, and the
switch defaults on with the PG mirror.
"""
import pathlib

from argia.store import pg_detail as D
from argia.telemetry.schema import PLANT_SCHEMA

V2 = pathlib.Path(__file__).resolve().parents[2]


def _row(**over):
    r = [""] * PLANT_SCHEMA.column_count
    idx = {c: i for i, c in enumerate(PLANT_SCHEMA.columns)}
    base = {"timestamp_utc": "2026-09-04T20:20:29+00:00", "timestamp_mx": "2026-09-04 14:20:29",
            "inverter_sn": "JFM7DXN03J", "inverter_label": "Inverter 1", "status": 1,
            "power_w": 99672.7, "etoday_kwh": 574.1, "pac_w": 99672.7, "iac_a": 124.0,
            "vacr_v": 267.4, "vacs_v": 268.7, "vact_v": 268.5, "fac_hz": 59.99,
            "temperature_c": 60.3, "fault_type": 0, "derating_mode": 0, "real_op_percent": 0,
            "pv_iso": 649, "p_bus_voltage_v": 354.1, "n_bus_voltage_v": 356.7,
            "str_unmatch": 13, "vpv1_v": 637.6, "vpv2_v": 664.6, "vpv3_v": 662.4, "vpv4_v": 655.4,
            "ppv1_w": 25000, "ppv2_w": 25100}
    base.update(over)
    for k, v in base.items():
        r[idx[k]] = v
    return r


class TestShape:
    def test_columns_follow_the_schema(self):
        assert D.COLUMNS[:3] == ["ts_utc", "plant_key", "inverter_sn"]
        for n, _ in D.SCALARS:
            assert n in PLANT_SCHEMA.columns, n
        assert D._ARRAY_IDX["vpv_v"] and len(D._ARRAY_IDX["vpv_v"]) == 16
        assert len(D._ARRAY_IDX["ppv_mppt_w"]) == 9 and len(D._ARRAY_IDX["epv_today_kwh"]) == 15
        assert "PRIMARY KEY (plant_key, inverter_sn, ts_utc)" in D.ENSURE_SQL
        assert "derating_mode integer" in D.ENSURE_SQL and "vpv_v numeric[]" in D.ENSURE_SQL

    def test_switch(self, monkeypatch):
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        assert D.enabled({}) and not D.enabled({"ARGIA_PG_DETAIL": "0"})
        monkeypatch.setenv("ARGIA_PG_MIRROR", "0")
        assert not D.enabled({})


class TestLiterals:
    def test_row_values_are_typed(self):
        vals = dict(zip(D.COLUMNS, D.row_values("SLP2", _row())))
        assert vals["plant_key"] == "'SLP2'" and vals["inverter_sn"] == "'JFM7DXN03J'"
        assert vals["vacr_v"] == "267.4" and vals["fac_hz"] == "59.99"
        assert vals["fault_type"] == "0" and vals["str_unmatch"] == "13"      # ints, not 13.0
        assert vals["pv_iso"] == "649.0"
        assert vals["vpv_v"] == "ARRAY[637.6,664.6,662.4,655.4]::numeric[]"   # trailing empties trimmed
        assert vals["ppv_mppt_w"] == "ARRAY[25000.0,25100.0]::numeric[]"
        assert vals["istring_a"] == "NULL" and vals["warn_code"] == "NULL"

    def test_garbage_and_short_rows(self):
        assert D.row_values("X", ["2026-09-04T20:20:29+00:00", "", "SN"]) is None    # short
        assert D.row_values("X", _row(inverter_sn="")) is None
        vals = dict(zip(D.COLUMNS, D.row_values("X", _row(fac_hz="n/a", vpv2_v="bad"))))
        assert vals["fac_hz"] == "NULL" and vals["vpv_v"] == "ARRAY[637.6,NULL,662.4,655.4]::numeric[]"

    def test_upsert_replaces_every_column(self):
        sql = D.build_upsert_sql("SLP2", [_row(), _row(inverter_sn="JFM7DXN039")])
        assert sql.startswith("INSERT INTO telemetry_detail (ts_utc, plant_key, inverter_sn, pac_w")
        assert sql.count("\n(") == 2
        assert "ON CONFLICT (plant_key, inverter_sn, ts_utc) DO UPDATE SET pac_w = EXCLUDED.pac_w" in sql
        assert "epv_today_kwh = EXCLUDED.epv_today_kwh;" in sql
        assert D.build_upsert_sql("SLP2", [["", ""]]) is None


class TestWiring:
    def test_every_vendor_path_mirrors_before_the_sheet_gate(self):
        src = (V2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
        assert "from argia.store import pg_detail" in src
        start = src.index("def _mirror_plant_tab")
        body = src[start:start + 1500]
        assert body.index("pg_detail.mirror_plant_rows(plant_key, plant_rows") < body.index("plant_tabs_enabled() if enabled")
        assert src.count("_mirror_plant_tab(sheets, plant.plant_key, plant_rows") == 4     # growatt, huawei, solaredge, sma

    def test_mirror_is_fail_soft(self, monkeypatch):
        monkeypatch.setenv("ARGIA_PG_MIRROR", "1")
        import argia.store.pgq as pgq
        monkeypatch.setattr(pgq, "psql_exec", lambda *a, **k: (_ for _ in ()).throw(OSError("no psql")))
        assert D.mirror_plant_rows("SLP2", [_row()]) == 0
        assert D.mirror_plant_rows("SLP2", [_row()], dry_run=True) == 1
