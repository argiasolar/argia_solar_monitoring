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

# v206
import pathlib
from argia.recon import backfill as B
from argia.recon import engine as E

V2 = pathlib.Path(__file__).resolve().parents[2]


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
    assert "row created by the nightly reconciliation" in sql
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


# ------------------------------------------------------------- pr resync
def test_pr_resync_sql_scoped_and_guarded():
    from argia.recon.backfill import build_pr_resync_sql
    sql = build_pr_resync_sql()
    # strictly scoped to vendor-authoritative rows (substring: covers
    # both the backfill note and the sheet-echoed note flavor)
    assert "status_note LIKE '%vendor daily counter%'" in sql
    # implausible PR (broken irradiance) must go NULL, never a lie
    assert "<= 1.05" in sql and "ELSE NULL END" in sql
    # never divides by a dark day or a zero capacity
    assert "irradiance_kwh_m2 > 0.5" in sql
    assert "kwp_dc > 0" in sql
    # idempotent: only rows whose pr actually changes
    assert "IS DISTINCT FROM" in sql


def test_pr_resync_sql_custom_threshold():
    from argia.recon.backfill import build_pr_resync_sql
    assert "<= 1.2" in build_pr_resync_sql(plausible_max=1.2)


# ---------------------------------------------------------- recon dates
import sys as _sys
from pathlib import Path as _Path

_SCRIPTS = _Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS))


def test_recon_dates_skips_today_midday():
    import datetime as dt
    import recon_snapshot as RS
    today = dt.date(2026, 8, 26)
    ds = RS.recon_dates(today, 3, hour_mx=12, today=today)
    assert ds == ["2026-08-25", "2026-08-24"]


def test_recon_dates_includes_today_at_night():
    import datetime as dt
    import recon_snapshot as RS
    today = dt.date(2026, 8, 26)
    ds = RS.recon_dates(today, 3, hour_mx=23, today=today)
    assert ds == ["2026-08-26", "2026-08-25", "2026-08-24"]


def test_recon_dates_historical_base_unaffected():
    import datetime as dt
    import recon_snapshot as RS
    base = dt.date(2026, 8, 20)
    ds = RS.recon_dates(base, 2, hour_mx=9, today=dt.date(2026, 8, 26))
    assert ds == ["2026-08-20", "2026-08-19"]



class TestInverterCounterRule:
    def test_fix_sql_names_the_basis_and_never_lowers(self):
        sql = B.build_fix_sql("slp2", "2026-09-04", 1513.4, 1368.6, basis="inverter_counters",
                              detail="inverter_counters; vendor plant daily 1368.6")
        assert "energy from inverter counters (inverter_counters; vendor plant daily 1368.6; kpi had 1368.6)" in sql
        assert "WHERE daily_production.energy_kwh IS NULL OR daily_production.energy_kwh < 1513.400" in sql
        legacy = B.build_fix_sql("SLP2", "2026-09-04", 1368.6, None)
        assert "energy from vendor daily counter (row created by the nightly reconciliation)" in legacy

    def test_pr_resync_covers_both_notes(self):
        sql = B.build_pr_resync_sql()
        assert "'%vendor daily counter%'" in sql and "'%inverter counters%'" in sql

    def test_fix_script_plan(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "inverter_counter_fix", V2 / "scripts" / "inverter_counter_fix.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        rows = [("SLP2", "2026-09-04", 1513.4, 1368.6, 1368.6, False),   # the case
                ("GTO1", "2026-09-04", 4864.8, 4860.0, 4864.8, False),   # fine
                ("MEX1", "2026-08-20", 3000.0, 3200.0, 3000.0, True),    # vendor higher, CLOSED month
                ("NL1", "2026-09-03", None, 2800.0, None, False)]        # missing row, vendor only
        out = m.plan(rows)
        assert [(f["plant_key"], f["basis"], f["reference"], f["closed"]) for f in out] == [
            ("SLP2", E.BASIS_INV, 1513.4, False), ("MEX1", E.BASIS_VENDOR_DAILY, 3200.0, True),
            ("NL1", E.BASIS_VENDOR_DAILY, 2800.0, False)]

    def test_fix_script_skips_the_running_day(self):
        src = (V2 / "scripts" / "inverter_counter_fix.py").read_text(encoding="utf-8")
        assert "if k[1] < today_mx" in src
        assert src.index("for f in open_fixes:") > src.index("resyncs always run on --apply")

    def test_recon_wiring(self):
        snap = (V2 / "scripts" / "recon_snapshot.py").read_text(encoding="utf-8")
        assert "B.classify_day(kpi, r.reference_kwh)" in snap
        assert "basis=r.reference_basis" in snap and "RECON_DAILY_ENSURE_SQL" in snap
        assert "reference_kwh=EXCLUDED.reference_kwh" in snap
        close = (V2 / "scripts" / "recon_close.py").read_text(encoding="utf-8")
        assert "greatest(interval_kwh, vendor_daily_kwh)" in close
        assert 's.get("ref_sum"), s.get("ref_days", 0))' in close
