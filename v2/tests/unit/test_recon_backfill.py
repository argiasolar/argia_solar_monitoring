"""Unit tests — argia.recon.backfill + historical series parsers."""

from argia.recon.backfill import (
    CLASS_MISSING,
    CLASS_NO_VENDOR,
    CLASS_OK,
    CLASS_OVER,
    CLASS_UNDER,
    build_fix_sql,
    classify_day,
)
from argia.recon.counters import huawei_daily_series, solaredge_daily_series


# ---------------------------------------------------------------- classify
def test_classify_ok_within_1pct():
    cls, d = classify_day(1000.0, 1005.0)
    assert cls == CLASS_OK


def test_classify_missing_and_no_vendor():
    assert classify_day(None, 500.0)[0] == CLASS_MISSING
    assert classify_day(500.0, None)[0] == CLASS_NO_VENDOR


def test_classify_under_and_over():
    cls, d = classify_day(800.0, 1000.0)
    assert cls == CLASS_UNDER and abs(d + 20.0) < 0.01
    assert classify_day(1200.0, 1000.0)[0] == CLASS_OVER


def test_classify_zero_vendor():
    assert classify_day(0.0, 0.0)[0] == CLASS_OK
    assert classify_day(5.0, 0.0)[0] == CLASS_OVER


# ----------------------------------------------------------------- fix SQL
def test_fix_sql_fill_missing():
    sql = build_fix_sql("gto1", "2026-08-25", 2809.9, None)
    assert "('GTO1', DATE '2026-08-25', 2809.900, 'v2'" in sql
    assert "kpi was missing" in sql
    assert "OR daily_production.energy_kwh < 2809.900" in sql


def test_fix_sql_never_lowers():
    # The SQL guard: update fires only when stored < vendor value.
    sql = build_fix_sql("MEX1", "2026-08-20", 1000.0, 900.0)
    assert "WHERE daily_production.energy_kwh IS NULL" in sql
    assert "kpi had 900.0" in sql


# ------------------------------------------------------ historical parsers
def test_solaredge_daily_series():
    resp = {"energy": {"unit": "Wh", "values": [
        {"date": "2026-08-24 00:00:00", "value": 947650.0},
        {"date": "2026-08-25 00:00:00", "value": None},
    ]}}
    got = solaredge_daily_series(resp)
    assert got["2026-08-24"] == 947.65
    assert got["2026-08-25"] is None


def test_huawei_daily_series():
    # 2026-08-25 12:00 MX (UTC-6) = 2026-08-25 18:00 UTC
    import datetime as dt
    ms = dt.datetime(2026, 8, 25, 18, 0,
                     tzinfo=dt.timezone.utc).timestamp() * 1000
    resp = {"success": True, "data": [
        {"stationCode": "NE=1234", "collectTime": ms,
         "dataItemMap": {"inverter_power": 4125.73}},
    ]}
    got = huawei_daily_series(resp)
    assert got[("NE=1234", "2026-08-25")] == 4125.73


def test_huawei_daily_series_failed():
    assert huawei_daily_series({"success": False}) == {}
