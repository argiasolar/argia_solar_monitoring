"""Unit tests — argia.recon.counters (parsers + snapshot SQL)."""

from argia.recon.counters import (
    CounterSnapshot,
    build_snapshot_upsert_sql,
    growatt_counters,
    huawei_station_counters,
    solaredge_energy_kwh,
    solaredge_lifetime_kwh,
)


# ----------------------------------------------------------------- Growatt
def _growatt_envelope(obj):
    # Capture-script wrapper shape: {_meta, response} (unwrap_fixture).
    return {"_meta": {}, "response": {"result": 1, "obj": obj}}


def test_growatt_counters_parses_totals():
    env = _growatt_envelope(
        {"plantId": "123", "eToday": "1875.8", "eTotal": "4839201.4"})
    assert growatt_counters(env) == (1875.8, None, 4839201.4)


def test_growatt_counters_bad_envelope():
    # result:0 ("no data") and structurally-broken input both degrade.
    assert growatt_counters({"_meta": {}, "response": {"result": 0}}) \
        == (None, None, None)
    assert growatt_counters({"garbage": True}) == (None, None, None)


# ------------------------------------------------------------------ Huawei
def test_huawei_station_counters():
    result = {"success": True, "data": [
        {"stationCode": "NE=1234", "dataItemMap": {
            "day_cap": 4125.73, "month_power": 98542.31,
            "total_power": 4839201.44}},
        {"stationCode": "NE=9999", "dataItemMap": {"day_cap": None}},
    ]}
    got = huawei_station_counters(result)
    assert got["NE=1234"] == (4125.73, 98542.31, 4839201.44)
    assert got["NE=9999"] == (None, None, None)


def test_huawei_failed_response_is_empty():
    assert huawei_station_counters({"success": False}) == {}
    assert huawei_station_counters(None) == {}
    assert huawei_station_counters({"success": True, "data": "junk"}) == {}


# --------------------------------------------------------------- SolarEdge
def test_solaredge_energy_day_wh():
    resp = {"energy": {"unit": "Wh", "values": [
        {"date": "2026-08-25 00:00:00", "value": 93827500.0}]}}
    assert solaredge_energy_kwh(resp) == 93827.5


def test_solaredge_energy_month_sums_values():
    resp = {"energy": {"unit": "kWh", "values": [
        {"date": "2026-07-01", "value": 1000.0},
        {"date": "2026-08-01", "value": 2000.5}]}}
    assert solaredge_energy_kwh(resp) == 3000.5


def test_solaredge_energy_empty_or_null():
    assert solaredge_energy_kwh({"energy": {"unit": "Wh", "values": []}}) is None
    assert solaredge_energy_kwh(
        {"energy": {"unit": "Wh",
                    "values": [{"date": "2026-08-25", "value": None}]}}) is None
    assert solaredge_energy_kwh(None) is None


def test_solaredge_unknown_unit_refuses_to_guess():
    resp = {"energy": {"unit": "GWh", "values": [{"date": "x", "value": 1}]}}
    assert solaredge_energy_kwh(resp) is None


def test_solaredge_lifetime():
    resp = {"overview": {"lifeTimeData": {"energy": 4839201440.0}}}
    assert solaredge_lifetime_kwh(resp) == 4839201.44
    assert solaredge_lifetime_kwh({}) is None


# ------------------------------------------------------------ snapshot SQL
def test_snapshot_sql_empty_batch_is_none():
    assert build_snapshot_upsert_sql([]) is None
    assert build_snapshot_upsert_sql(
        [CounterSnapshot("", "GROWATT", "2026-08-25", 1, 2, 3)]) is None


def test_snapshot_sql_shape_and_escaping():
    snaps = [
        CounterSnapshot("gto1", "GROWATT", "2026-08-25", 1875.8, None,
                        4839201.4, "o'brien"),
        CounterSnapshot("MEX1", "SOLAREDGE", "2026-08-25", 93827.5,
                        2100000.125, 4839201.44),
    ]
    sql = build_snapshot_upsert_sql(snaps)
    assert sql.startswith("INSERT INTO vendor_counter_snapshot")
    assert "('GTO1','GROWATT','2026-08-25',1875.800,NULL,4839201.400," in sql
    assert "'o''brien'" in sql                    # quote-safe
    assert "2100000.125" in sql
    assert "ON CONFLICT (plant_key, snap_date) DO UPDATE" in sql
    assert sql.rstrip().endswith("captured_at=now();")
