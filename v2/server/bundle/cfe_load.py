#!/usr/bin/env python3
"""Load CFE tariff rows into PostgreSQL (argia_mont.cfe_tariff).

Usage:  python3 cfe_load.py <csv> <source-tag>
        source-tag: master_db_10 (seed) | cfe_scrape (official monthly fetch)

Upsert rule: a cfe_scrape row is authoritative — a seed load never overwrites
it; a scrape load overwrites anything. PK (tariff_code, region, month, charge_type).
"""
import csv
import subprocess
import sys

CSV, SOURCE = sys.argv[1], sys.argv[2]
assert SOURCE in ('master_db_10', 'cfe_scrape'), SOURCE


def psql(sql):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', 'argia_mont',
                        '-v', 'ON_ERROR_STOP=1', '-q', '-t', '-A'],
                       input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        print('PSQL ERROR:', r.stderr[-800:])
        sys.exit(1)
    return r.stdout


psql('''
CREATE TABLE IF NOT EXISTS cfe_tariff (
    tariff_code text NOT NULL,
    region      text NOT NULL,
    month       date NOT NULL,
    charge_type text NOT NULL,
    unit        text,
    value_mxn   numeric(14,6),
    source      text NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tariff_code, region, month, charge_type)
);
CREATE INDEX IF NOT EXISTS idx_cfe_month ON cfe_tariff (month);
''')

rows = list(csv.DictReader(open(CSV, encoding='utf-8')))
guard = '' if SOURCE == 'cfe_scrape' else "WHERE cfe_tariff.source <> 'cfe_scrape'"
B = 800
total = 0
for i in range(0, len(rows), B):
    chunk = rows[i:i + B]
    vals = ',\n'.join(
        "('{}','{}','{}','{}','{}',{},'{}')".format(
            r['tariff_code'].replace("'", ''), r['region'].replace("'", ''),
            r['month'], r['charge_type'].replace("'", ''),
            (r['unit'] or '').replace("'", ''), float(r['value_mxn']), SOURCE)
        for r in chunk)
    psql(f'INSERT INTO cfe_tariff (tariff_code,region,month,charge_type,unit,value_mxn,source) '
         f'VALUES\n{vals}\nON CONFLICT (tariff_code,region,month,charge_type) DO UPDATE SET '
         f'unit=EXCLUDED.unit, value_mxn=EXCLUDED.value_mxn, source=EXCLUDED.source, '
         f'loaded_at=now() {guard};')
    total += len(chunk)

print(f'loaded {total} rows as {SOURCE}')
print(psql("SELECT source, count(*), min(month), max(month), count(DISTINCT tariff_code), "
           "count(DISTINCT region) FROM cfe_tariff GROUP BY source;"))
print('=== CFE LOAD DONE ===')
