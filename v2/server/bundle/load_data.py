#!/usr/bin/env python3
"""Load the Argia history bundle into PostgreSQL. Idempotent (ON CONFLICT upsert).

Run on pio06:  python3 load_data.py /opt/argia/bundle
Reads DB password for user 'argia' from /root/.credentials (argia_db=...).
"""
import csv
import subprocess
import sys
import os

BUNDLE = sys.argv[1] if len(sys.argv) > 1 else '/opt/argia/bundle'
DB = 'argia_mont'


def psql(sql, tuples=False):
    cmd = ['runuser', '-u', 'postgres', '--', 'psql', '-d', DB, '-v', 'ON_ERROR_STOP=1', '-q']
    if tuples:
        cmd += ['-t', '-A']
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        print('PSQL ERROR:', r.stderr[-2000:])
        sys.exit(1)
    return r.stdout


def null(v):
    return None if v == '' else v


def load(table, csvname, cols, conflict_cols, update=True):
    path = os.path.join(BUNDLE, csvname)
    rows = list(csv.DictReader(open(path, encoding='utf-8')))
    if not rows:
        print(f'{table}: EMPTY CSV, skipped')
        return
    # build one INSERT ... VALUES ... ON CONFLICT statement in batches
    def q(v):
        if v is None or v == '':
            return 'NULL'
        return "'" + str(v).replace("'", "''") + "'"
    collist = ','.join(cols)
    conflict = ','.join(conflict_cols)
    setlist = ','.join(f'{c}=EXCLUDED.{c}' for c in cols if c not in conflict_cols)
    action = f'DO UPDATE SET {setlist}' if (update and setlist) else 'DO NOTHING'
    B = 500
    total = 0
    for i in range(0, len(rows), B):
        chunk = rows[i:i + B]
        values = ',\n'.join('(' + ','.join(q(r.get(c, '')) for c in cols) + ')' for r in chunk)
        psql(f'INSERT INTO {table} ({collist}) VALUES\n{values}\nON CONFLICT ({conflict}) {action};')
        total += len(chunk)
    n = psql(f'SELECT count(*) FROM {table};', tuples=True).strip()
    print(f'{table}: loaded {total} csv rows -> table now {n}')


load('plant', 'plants.csv',
     ['plant_key', 'customer', 'brand', 'site_id', 'kwp_dc', 'kwp_ac', 'lat', 'lon',
      'portfolio', 'tariff_mxn_per_kwh', 'pr_baseline', 'contracted_kwh',
      'om_cost_monthly_mxn', 'active'],
     ['plant_key'])

load('inverter', 'inverters.csv',
     ['plant_key', 'inverter_sn', 'inverter_label', 'rated_kw', 'phase',
      'date_producing', 'date_decommissioned', 'active'],
     ['plant_key', 'inverter_sn'])

load('daily_production', 'daily_v1.csv',
     ['plant_key', 'prod_date', 'energy_kwh', 'irradiance_kwh_m2', 'cloud_cover_pct', 'source'],
     ['plant_key', 'prod_date'])

load('daily_production', 'daily_v2.csv',
     ['plant_key', 'prod_date', 'energy_kwh', 'irradiance_kwh_m2', 'pr', 'pr_stc',
      'expected_kwh', 'billable_kwh', 'cloud_cover_pct', 'availability',
      'inverters_reporting', 'data_class', 'status_note', 'source'],
     ['plant_key', 'prod_date'])

load('contract_monthly', 'contract_monthly.csv',
     ['plant_key', 'year', 'month', 'design_kwh', 'contract_kwh', 'tariff_mxn',
      'fixed_income_ccy', 'ccy'],
     ['plant_key', 'year', 'month'])

load('loan', 'loans.csv',
     ['loan_id', 'plant_key', 'project_name', 'bank', 'currency', 'principal_mxn',
      'total_installments', 'first_month', 'last_month'],
     ['loan_id'])

load('loan_schedule', 'loan_schedule.csv',
     ['loan_id', 'ref_month', 'installment_no', 'payment_mxn', 'payment_ccy', 'xr',
      'due_after_mxn'],
     ['loan_id', 'ref_month'])

print('=== VERIFICATION ===')
print(psql("""
SELECT source, count(*), min(prod_date), max(prod_date), round(sum(energy_kwh)) AS kwh
FROM daily_production GROUP BY source ORDER BY source;
"""))
print(psql("""
SELECT plant_key, round(sum(energy_kwh)) AS lifetime_kwh
FROM daily_production GROUP BY plant_key ORDER BY lifetime_kwh DESC;
"""))
# expected lifetime totals (v1<=06-30 + v2>=07-01 through 08-19), from the reconciliation:
print("expected: GTO1 1859263, NL1 1585052, MEX1 1219117, SLP2 955546, SLP1 575677, MEX2 480212 (+/- rounding)")
print('=== LOAD DONE ===')
