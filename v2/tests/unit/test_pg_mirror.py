"""Unit tests for argia/store/pg_mirror.py (pure SQL building + gating)."""
import os

from argia.store.pg_mirror import build_upsert_sql, enabled, mirror_common_rows

ROW = ['2026-08-26T18:00:00+00:00', '2026-08-26 12:00:00', 'GROWATT', 'GTO1',
       'JGM123', 'Inverter 1', 1, 45000.0, 123.4, 41.2, '', 850.0, 0.070,
       35.0, 29.1, 44.0]


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv('ARGIA_PG_MIRROR', raising=False)
    assert not enabled()
    assert mirror_common_rows([ROW]) == 0     # silent no-op on the Pi


def test_build_sql_basics():
    sql = build_upsert_sql([ROW])
    assert sql.startswith('INSERT INTO telemetry (ts_utc,plant_key,inverter_sn')
    assert "'GTO1'" in sql and "'JGM123'" in sql and "'GROWATT'" in sql
    assert 'ON CONFLICT (plant_key, inverter_sn, ts_utc) DO UPDATE SET' in sql
    assert 'ts_utc=EXCLUDED' not in sql       # key columns never updated


def test_build_sql_skips_broken_rows():
    missing_key = list(ROW); missing_key[3] = ''
    short = ROW[:10]
    assert build_upsert_sql([missing_key, short]) is None
    sql = build_upsert_sql([missing_key, ROW])
    assert sql.count('\n(') == 1              # only the valid row survives


def test_build_sql_nulls_and_quoting():
    r = list(ROW)
    r[9] = None                                # temperature -> NULL
    r[10] = "F'AULT"                           # fault code with quote
    r[7] = 'not-a-number'                      # power garbage -> NULL
    sql = build_upsert_sql([r])
    assert "'F''AULT'" in sql
    assert sql.count('NULL') >= 2


def test_dry_run_counts(monkeypatch):
    monkeypatch.setenv('ARGIA_PG_MIRROR', '1')
    assert mirror_common_rows([ROW, ROW], dry_run=True) == 2
