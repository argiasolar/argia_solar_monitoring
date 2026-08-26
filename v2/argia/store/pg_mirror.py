"""Mirror the narrow cross-vendor telemetry rows into PostgreSQL (pio06).

Phase A of the Pi -> server migration (2026-08-26): the collector keeps
writing Google Sheets exactly as before, and — only where
``ARGIA_PG_MIRROR=1`` (the server) — ALSO upserts the same rows into the
``telemetry`` table of the ``argia_mont`` database. Failure here must
never break the sheet path; callers wrap us in try/except and we raise
nothing fatal ourselves.

Execution model: ``runuser -u postgres -- psql`` (peer auth, root-only
server context) — no new Python dependencies, same pattern as the other
server-side loaders.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import List, Optional, Sequence

LOG = logging.getLogger("argia.store.pg_mirror")

# indexes into ARGIA_COMMON_COLS (argia/telemetry/schema.py)
I_TS, I_VENDOR, I_PLANT, I_SN, I_LABEL, I_STATUS = 0, 2, 3, 4, 5, 6
I_POWER, I_ETODAY, I_TEMP, I_FAULT, I_IRR, I_IRR5M = 7, 8, 9, 10, 11, 12
I_CLOUD, I_AMBIENT, I_MODULE = 13, 14, 15

COLS = ('ts_utc', 'plant_key', 'inverter_sn', 'vendor', 'inverter_label',
        'status', 'power_w', 'etoday_kwh', 'temperature_c', 'fault_code',
        'irradiance_wm2', 'irradiance_kwh_m2_5m', 'cloud_cover_pct',
        'ambient_temp_c', 'module_temp_c')
_NUM = {'power_w', 'etoday_kwh', 'temperature_c', 'irradiance_wm2',
        'irradiance_kwh_m2_5m', 'cloud_cover_pct', 'ambient_temp_c',
        'module_temp_c'}


def enabled() -> bool:
    return os.environ.get('ARGIA_PG_MIRROR', '') == '1'


def _sql_lit(v, numeric: bool) -> str:
    s = '' if v is None else str(v).strip()
    if s == '' or s.lower() in ('none', 'nan'):
        return 'NULL'
    if numeric:
        try:
            float(s)
        except ValueError:
            return 'NULL'
        return s
    return "'" + s.replace("'", "''") + "'"


def _status_lit(v) -> str:
    s = '' if v is None else str(v).strip()
    try:
        return str(int(float(s)))
    except ValueError:
        return 'NULL'


def build_upsert_sql(common_rows: Sequence[Sequence]) -> Optional[str]:
    """One idempotent INSERT..ON CONFLICT statement for the narrow rows,
    or None when there is nothing valid to write."""
    tuples: List[str] = []
    for r in common_rows:
        if len(r) < 16:
            continue
        ts = '' if r[I_TS] is None else str(r[I_TS]).strip()
        plant = '' if r[I_PLANT] is None else str(r[I_PLANT]).strip()
        sn = '' if r[I_SN] is None else str(r[I_SN]).strip()
        if not ts or not plant or not sn:
            continue
        vals = [
            _sql_lit(ts, False), _sql_lit(plant, False), _sql_lit(sn, False),
            _sql_lit(r[I_VENDOR], False), _sql_lit(r[I_LABEL], False),
            _status_lit(r[I_STATUS]), _sql_lit(r[I_POWER], True),
            _sql_lit(r[I_ETODAY], True), _sql_lit(r[I_TEMP], True),
            _sql_lit(r[I_FAULT], False), _sql_lit(r[I_IRR], True),
            _sql_lit(r[I_IRR5M], True), _sql_lit(r[I_CLOUD], True),
            _sql_lit(r[I_AMBIENT], True), _sql_lit(r[I_MODULE], True),
        ]
        tuples.append('(' + ','.join(vals) + ')')
    if not tuples:
        return None
    upd = ', '.join(f'{c}=EXCLUDED.{c}' for c in COLS
                    if c not in ('ts_utc', 'plant_key', 'inverter_sn'))
    return (f'INSERT INTO telemetry ({",".join(COLS)}) VALUES\n'
            + ',\n'.join(tuples)
            + f'\nON CONFLICT (plant_key, inverter_sn, ts_utc) DO UPDATE SET {upd};')


def mirror_common_rows(common_rows: Sequence[Sequence], dry_run: bool = False,
                       log: Optional[logging.Logger] = None) -> int:
    """Upsert the rows into PostgreSQL. Returns rows attempted (0 = no-op)."""
    lg = log or LOG
    if not enabled():
        return 0
    sql = build_upsert_sql(common_rows)
    if sql is None:
        return 0
    n = sql.count('\n(')
    if dry_run:
        lg.info('[PG] DRY RUN: would upsert %d telemetry rows', n)
        return n
    db = os.environ.get('ARGIA_PG_DB', 'argia_mont')
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', db,
                        '-v', 'ON_ERROR_STOP=1', '-q'],
                       input=sql, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        lg.warning('[PG] mirror upsert failed (sheets unaffected): %s',
                   r.stderr[-300:])
        return 0
    lg.info('[PG] mirrored %d telemetry rows to %s', n, db)
    return n
