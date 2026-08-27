#!/usr/bin/env python3
"""Pull the KPI window from the repo's `data` branch and upsert into PostgreSQL.

Runs on pio06 (systemd argia-sync.timer, daily). Flow:
  GitHub Actions (v2-kpi-export) writes kpi_window.csv to branch `data`
  -> this script: git fetch origin data; parse CSV; upsert daily_production
  -> then the caller regenerates the reports (argia-sync.service does both).

Upsert semantics mirror the sheet's accretion rule: a BLANK never overwrites
stored data (COALESCE on update), rows keyed (plant_key, prod_date), source='v2'.
"""
import csv
import io
import subprocess
import sys

REPO = '/root/argia_v2'
DB = 'argia_mont'
GIT_ENV = {'GIT_SSH_COMMAND': 'ssh -i /root/.ssh/argia_deploy -o StrictHostKeyChecking=no'}

# CSV header name -> daily_production column
# (real KPI_Daily header confirmed live 2026-08-25: also carries availability,
#  expected_kwh, cloud_coverage_pct and its own status_note column)
COLMAP = {
    'date_iso': 'prod_date',
    'plant_key': 'plant_key',
    'energy_kwh': 'energy_kwh',
    'irradiance_kwh_m2': 'irradiance_kwh_m2',
    'pr': 'pr',
    'pr_stc': 'pr_stc',
    'billable_kwh': 'billable_kwh',
    'expected_kwh': 'expected_kwh',
    'availability': 'availability',
    'cloud_coverage_pct': 'cloud_cover_pct',
    'data_class': 'data_class',
    'inverters_reporting': 'inverters_reporting',
    'status_note': 'status_note',
}
NUMERIC = {'energy_kwh', 'irradiance_kwh_m2', 'pr', 'pr_stc', 'billable_kwh',
           'expected_kwh', 'availability', 'cloud_coverage_pct'}
INTEGER = {'inverters_reporting'}


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f'{cmd}: {r.stderr[-600:]}')
    return r.stdout


def psql(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB,
                        '-v', 'ON_ERROR_STOP=1', '-q', '-t', '-A'],
                       input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('psql: ' + r.stderr[-600:])
    return r.stdout.strip()


def q(v, num=False):
    s = ('' if v is None else str(v)).strip()
    if s == '':
        return 'NULL'
    if num:
        try:
            float(s)
        except ValueError:
            return 'NULL'
        return s
    return "'" + s.replace("'", "''") + "'"


def main():
    import os
    env = dict(os.environ, **GIT_ENV)
    sh(['git', '-C', REPO, 'fetch', 'origin', 'data'], env=env)
    text = sh(['git', '-C', REPO, 'show', 'origin/data:kpi_window.csv'], env=env)
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        print('kpi_window.csv is empty — nothing to do')
        return 1

    values = []
    for r in rows:
        d = (r.get('date_iso') or '').strip()
        k = (r.get('plant_key') or '').strip().upper()
        if not d or not k:
            continue
        vals = {'prod_date': f"'{d}'", 'plant_key': f"'{k}'", 'source': "'v2'"}
        for src, dst in COLMAP.items():
            if src in ('date_iso', 'plant_key'):
                continue
            raw = r.get(src)
            if src in INTEGER:
                s = ('' if raw is None else str(raw)).strip()
                vals[dst] = str(int(float(s))) if s else 'NULL'
            else:
                vals[dst] = q(raw, num=src in NUMERIC)
        values.append(vals)

    cols = ['plant_key', 'prod_date', 'energy_kwh', 'irradiance_kwh_m2', 'pr',
            'pr_stc', 'billable_kwh', 'expected_kwh', 'availability',
            'cloud_cover_pct', 'data_class', 'inverters_reporting',
            'status_note', 'source']
    upd = ', '.join(
        f'{c}=COALESCE(EXCLUDED.{c}, daily_production.{c})'
        for c in cols if c not in ('plant_key', 'prod_date', 'source'))
    tuples = ',\n'.join('(' + ','.join(v[c] for c in cols) + ')' for v in values)
    before = psql('SELECT count(*), max(prod_date) FROM daily_production;')
    psql(f'INSERT INTO daily_production ({",".join(cols)}) VALUES\n{tuples}\n'
         f'ON CONFLICT (plant_key, prod_date) DO UPDATE SET {upd};')
    after = psql('SELECT count(*), max(prod_date) FROM daily_production;')
    print(f'upserted {len(values)} rows; table before [{before}] after [{after}]')
    dates = sorted({v["prod_date"] for v in values})
    print(f'window {dates[0]} .. {dates[-1]}')
    print('SYNC OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
