"""KPI sheet -> PG mirror (P1-9) — protected-upsert semantics."""

from argia.kpi.reconcile import date_key
from argia.store.kpi_mirror import (
    PROTECTED,
    VENDOR_NOTE_PREFIX,
    build_upsert_sql,
    normalize_rows,
)


def _rec(**kw):
    base = {"date_iso": 46247.0, "plant_key": "gto1", "energy_kwh": "4215.0",
            "irradiance_kwh_m2": "7.31", "pr": "0.7423", "pr_stc": "",
            "billable_kwh": "", "expected_kwh": "4437.2",
            "availability": "0.985", "cloud_coverage_pct": "32",
            "data_class": "full", "inverters_reporting": "6",
            "status_note": "OK"}
    base.update(kw)
    return base


def test_normalize_serial_date_and_types():
    rows = normalize_rows([_rec()], date_key)
    assert len(rows) == 1
    r = rows[0]
    assert r["plant_key"] == "GTO1"
    assert r["prod_date"] == "2026-08-13"          # serial 46247
    assert r["energy_kwh"] == 4215.0
    assert r["pr_stc"] is None                     # blank -> None
    assert r["inverters_reporting"] == 6
    assert r["cloud_cover_pct"] == 32.0            # renamed column


def test_normalize_drops_bad_and_old_rows():
    recs = [_rec(plant_key=""), _rec(date_iso=""),
            _rec(date_iso="2026-07-01"), _rec(date_iso="2026-08-20")]
    rows = normalize_rows(recs, date_key, min_date="2026-08-01")
    assert [r["prod_date"] for r in rows] == ["2026-08-20"]


def test_upsert_sql_protects_vendor_rows():
    sql = build_upsert_sql(normalize_rows([_rec()], date_key))
    assert sql.startswith("INSERT INTO daily_production")
    assert "ON CONFLICT (plant_key, prod_date) DO UPDATE SET" in sql
    # every protected column keeps the stored value on vendor rows
    for c in PROTECTED:
        assert (f"{c} = CASE WHEN daily_production.status_note LIKE "
                f"'{VENDOR_NOTE_PREFIX}%' THEN daily_production.{c}") in sql
    # unprotected columns still COALESCE normally
    assert ("expected_kwh = COALESCE(EXCLUDED.expected_kwh,"
            " daily_production.expected_kwh)") in sql


def test_upsert_sql_blank_never_overwrites():
    sql = build_upsert_sql(normalize_rows([_rec(expected_kwh="")], date_key))
    # the blank arrives as NULL in VALUES; COALESCE keeps the stored value
    assert "COALESCE(EXCLUDED.expected_kwh, daily_production.expected_kwh)" in sql


def test_upsert_sql_quotes_and_empty():
    assert build_upsert_sql([]) is None
    sql = build_upsert_sql(normalize_rows(
        [_rec(status_note="it's fine")], date_key))
    assert "it''s fine" in sql
