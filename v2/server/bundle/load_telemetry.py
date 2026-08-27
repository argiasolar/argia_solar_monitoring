#!/usr/bin/env python3
"""Load the archived telemetry CSVs (143 cols) into the telemetry table (common cols).

Run on pio06:  python3 load_telemetry.py /opt/argia/archive/Telemetry_Archive
Skips the unified 'argia' files (they duplicate the per-plant ones).
Idempotent: ON CONFLICT DO NOTHING on (plant_key, inverter_sn, ts_utc).
"""
import csv
import io
import os
import re
import subprocess
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else '/opt/argia/archive/Telemetry_Archive'
DB = 'argia_mont'
KEEP = ['timestamp_utc', 'inverter_sn', 'status', 'power_w', 'etoday_kwh',
        'temperature_c', 'irradiance_wm2', 'module_temp_c', 'ambient_temp_c',
        'cloud_cover_pct']

files = []
for dp, _, fns in os.walk(ROOT):
    for fn in fns:
        m = re.match(r'telemetry_([a-z0-9]+)_(\d{4}-\d{2}-\d{2})\.csv$', fn)
        if m and m.group(1) != 'argia':
            files.append((m.group(1).upper(), os.path.join(dp, fn)))
files.sort(key=lambda x: x[1])
print(f'{len(files)} per-plant files to load')

def run_psql(sql_stdin):
    r = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB,
                        '-v', 'ON_ERROR_STOP=1', '-q'],
                       input=sql_stdin, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])
    return r.stdout

# staging once
run_psql('''
CREATE TABLE IF NOT EXISTS _tele_stage (
  ts_utc timestamptz, plant_key text, inverter_sn text, status int,
  power_w numeric, etoday_kwh numeric, temperature_c numeric,
  irradiance_wm2 numeric, module_temp_c numeric, ambient_temp_c numeric,
  cloud_cover_pct numeric);
''')

total = 0
bad = 0
for k, (plant, path) in enumerate(files, 1):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    with open(path, encoding='utf-8', errors='replace') as fh:
        rd = csv.DictReader(fh)
        for r in rd:
            ts = (r.get('timestamp_utc') or '').replace('\r',' ').replace('\n',' ').strip()
            sn = (r.get('inverter_sn') or '').replace('\r',' ').replace('\n',' ').strip()[:64]
            if not ts or not sn:
                bad += 1
                continue
            def num(col):
                v = (r.get(col) or '').replace('\r',' ').replace('\n',' ').strip()
                try:
                    float(v)
                except (TypeError, ValueError):
                    return ''
                return v
            w.writerow([ts, plant, sn, num('status'), num('power_w'), num('etoday_kwh'),
                        num('temperature_c'), num('irradiance_wm2'), num('module_temp_c'),
                        num('ambient_temp_c'), num('cloud_cover_pct')])
    data = buf.getvalue()
    if not data:
        continue
    sql = ("TRUNCATE _tele_stage;\n"
           "COPY _tele_stage FROM STDIN WITH (FORMAT csv, NULL '');\n"
           + data + "\\.\n"
           "INSERT INTO telemetry SELECT DISTINCT ON (plant_key, inverter_sn, ts_utc) * FROM _tele_stage "
           "WHERE ts_utc IS NOT NULL ON CONFLICT (plant_key, inverter_sn, ts_utc) DO NOTHING;")
    run_psql(sql)
    total += data.count('\n')
    if k % 25 == 0:
        print(f'  {k}/{len(files)} files, ~{total} rows staged')

run_psql('DROP TABLE IF EXISTS _tele_stage;')
print(f'staged rows: {total}, skipped bad rows: {bad}')
out = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-d', DB, '-t', '-A', '-c',
                      "SELECT count(*) || ' rows, ' || min(ts_utc)::date || ' .. ' || max(ts_utc)::date || ', plants=' || count(DISTINCT plant_key) FROM telemetry;"],
                     capture_output=True, text=True)
print('telemetry table:', out.stdout.strip())
print('=== TELEMETRY DONE ===')
